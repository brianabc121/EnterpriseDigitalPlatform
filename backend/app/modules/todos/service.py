"""待办的登记、去重合并与数据范围（设计文档 §24.3、§24.4、§24.8）。

- AI 生成的（AI 接待、会话后解析、专区）先进入待确认页，由确认人确认后才进入待办列表；
  员工新建的、系统规则生成的和企业系统创建的直接进入待办列表。
- 同一客户、同一类型、还没完成（包括待确认的）或最近完成的待办，需求相似时不新建，而是在原待办
  上记一次客户催促；催促达到 2 次，优先级提高一级。
- 提醒不在事务里发送：待办上记下待发送的提醒（notify_reason），由调度进程或接口在提交后发送。
"""

import hashlib
import json
import logging
import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import ColumnElement, and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import NotFound
from app.core.ids import new_id
from app.core.permissions import Permission
from app.db.counters import next_number
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway
from app.modules.conversation.models import ChatSession
from app.modules.customer.models import Customer
from app.modules.customer.service import visible_to as customer_visible_to
from app.modules.iam.principal import Principal
from app.modules.kb.text import terms
from app.modules.opportunities import service as opportunities
from app.modules.routing.assign import PolicyResolver
from app.modules.routing.models import SkillGroupMember
from app.modules.routing.scope import led_groups, team_members
from app.modules.security.keys import TenantKeyring
from app.modules.todos import assign, events, sla
from app.modules.todos import fields as todo_fields
from app.modules.todos import settings as todo_settings
from app.modules.todos.models import (
    AI_SOURCES,
    UNFINISHED,
    ActorType,
    NotifyReason,
    Todo,
    TodoSource,
    TodoStatus,
    TodoType,
    raise_priority,
)
from app.modules.todos.presets import LEAVE_MESSAGE, type_by_code

logger = logging.getLogger(__name__)

NOT_FOUND = "待办不存在"
NUMBER_SCOPE = "todo"
NUMBER_PREFIX = "TD"
TITLE_LIMIT = 100
DETAIL_LIMIT = 4000
# 需求相似（合并为一条）的阈值：词项的包含度（较短的一段有多少词项出现在另一段里），
# 或者向量余弦相似度。
SIMILAR_TERMS = 0.7
SIMILAR_VECTOR = 0.9
# 催促达到这个次数时优先级提高一级。
NUDGES_TO_RAISE = 2
# 同一条待办的催促提醒最多发这么多次，避免打扰。
NUDGE_NOTICES = 5


def utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class Draft:
    """要登记的待办。fields 是按类型清理过的明文（见 fields.clean）。"""

    type: TodoType
    title: str
    source: TodoSource
    created_by_type: ActorType
    detail: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    created_by: uuid.UUID | None = None
    customer_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    # 关联的商机（设计文档 §40.7）；不填时挂到客户进行中的商机上（系统生成的订单待办除外）。
    opportunity_id: uuid.UUID | None = None
    channel_account_id: uuid.UUID | None = None
    # 规则里 channel_group 的技能组（排队超时的留言用会话所在的技能组）。
    group_hint: uuid.UUID | None = None
    evidence_message_ids: list[uuid.UUID] = field(default_factory=list)
    expected_at: datetime | None = None
    due_at: datetime | None = None
    priority: str | None = None
    confidence: float | None = None
    dedupe_key: str | None = None
    # 员工新建时指定的处理人或待认领的技能组（不按规则分派）。
    explicit: bool = False
    assignee_id: uuid.UUID | None = None
    skill_group_id: uuid.UUID | None = None


def dedupe_key(*parts: Any) -> str:
    raw = json.dumps([str(p) for p in parts], ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


async def _vip(session: AsyncSession, customer_id: uuid.UUID) -> bool:
    """客户带有路由策略里的优先标签（默认 VIP）时，待办优先级提高一级。"""
    tags = await session.scalar(select(Customer.tags).where(Customer.id == customer_id))
    if not tags:
        return False
    policy = await PolicyResolver(session).default()
    return bool(set(tags) & set(policy.priority_tags or []))


async def create(
    session: AsyncSession,
    keys: TenantKeyring | None,
    draft: Draft,
    *,
    now: datetime | None = None,
) -> Todo:
    """登记一条待办（加入当前事务，由调用方提交）。"""
    now = now or utcnow()
    type_ = draft.type
    tenant_id = type_.tenant_id
    spec = await sla.business_hours(session)
    if draft.explicit:
        target = assign.Target(draft.assignee_id, draft.skill_group_id, "explicit")
    else:
        target = await assign.resolve(
            session,
            type_,
            customer_id=draft.customer_id,
            session_id=draft.session_id,
            channel_account_id=draft.channel_account_id,
            group_hint=draft.group_hint,
        )
    priority = draft.priority or type_.priority
    if draft.customer_id is not None and await _vip(session, draft.customer_id):
        priority = raise_priority(priority)
    pending = draft.source in AI_SOURCES
    opportunity_id = draft.opportunity_id
    if opportunity_id is None and not type_.system:
        opportunity_id = await opportunities.open_opportunity_id(session, draft.customer_id)
    todo = Todo(
        id=new_id(),
        tenant_id=tenant_id,
        no=await next_number(
            session,
            tenant_id,
            scope=NUMBER_SCOPE,
            prefix=NUMBER_PREFIX,
            now=now,
            tz=sla.tz_of(spec),
        ),
        type_id=type_.id,
        title=draft.title.strip()[:TITLE_LIMIT] or type_.name,
        detail=draft.detail.strip()[:DETAIL_LIMIT],
        fields=await todo_fields.seal(keys, tenant_id, type_, draft.fields),
        customer_id=draft.customer_id,
        session_id=draft.session_id,
        order_id=draft.order_id,
        opportunity_id=opportunity_id,
        source=draft.source,
        confidence=draft.confidence,
        evidence_message_ids=list(dict.fromkeys(draft.evidence_message_ids)),
        priority=priority,
        status=TodoStatus.PENDING if pending else TodoStatus.OPEN,
        assignee_id=target.assignee_id,
        skill_group_id=target.skill_group_id,
        assigned_by=(
            draft.created_by
            if draft.explicit and target.assignee_id not in (None, draft.created_by)
            else None
        ),
        expected_at=draft.expected_at,
        dedupe_key=draft.dedupe_key,
        created_by_type=draft.created_by_type,
        created_by=draft.created_by,
        created_at=now,
    )
    if pending:
        # 截止时间从确认时开始计算；待确认时显示按时限建议的截止时间。
        todo.due_at = draft.due_at or sla.suggested_due(type_, spec, now, draft.expected_at)
        settings = await todo_settings.load(session, tenant_id)
        todo.pending_remind_at = sla.pending_remind_at(spec, now, settings.pending_remind_minutes)
        todo.notify_reason = NotifyReason.PENDING
    else:
        todo.confirmed_at = now
        sla.start_clock(todo, type_, spec, now, due_at=draft.due_at)
        todo.notify_reason = NotifyReason.ASSIGNED
    if draft.created_by is not None and todo.assignee_id == draft.created_by:
        todo.notified_at = now  # 员工给自己建的不需要提醒
    session.add(todo)
    await session.flush()
    events.record(
        session,
        todo,
        "created",
        actor_type=draft.created_by_type,
        actor_id=draft.created_by,
        payload={
            "source": draft.source,
            "status": todo.status,
            "assignee_id": str(todo.assignee_id) if todo.assignee_id else None,
            "skill_group_id": str(todo.skill_group_id) if todo.skill_group_id else None,
            "assigned_by_rule": target.by,
        },
    )
    # 商机的时间线（§40.7）。
    await opportunities.todo_changed(
        session,
        todo,
        "created",
        staff_id=draft.created_by if draft.created_by_type == ActorType.STAFF else None,
    )
    return todo


# ---- 去重与合并 ----


def _term_similarity(a: str, b: str) -> float:
    left, right = set(terms(a)), set(terms(b))
    if not left or not right:
        return 0.0
    return len(left & right) / min(len(left), len(right))


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


async def _most_similar(
    ctx: AppContext | None, tenant_id: uuid.UUID, text: str, candidates: list[Todo]
) -> Todo | None:
    """需求描述最相似、达到阈值的待办：先比较词项，词项不够相似时再比较向量（有向量模型时）。"""
    texts = [f"{c.title}\n{c.detail}" for c in candidates]
    scores = [_term_similarity(text, t) for t in texts]
    best = max(range(len(scores)), key=scores.__getitem__)
    if scores[best] >= SIMILAR_TERMS:
        return candidates[best]
    if ctx is None or not await ctx.llms.embed_enabled():
        return None
    try:
        vectors = await gateway.embed(ctx, tenant_id, [text, *texts], scene="todo_merge")
    except LLMUnavailable:
        return None
    if len(vectors) != len(texts) + 1:
        return None
    cosines = [_cosine(vectors[0], v) for v in vectors[1:]]
    best = max(range(len(cosines)), key=cosines.__getitem__)
    return candidates[best] if cosines[best] >= SIMILAR_VECTOR else None


async def find_duplicate(
    ctx: AppContext | None,
    session: AsyncSession,
    draft: Draft,
    *,
    now: datetime | None = None,
) -> Todo | None:
    """同一客户、同一类型、未完成（包括待确认）或最近完成的待办里，与这次需求是同一件事的。

    关键字段（如订单号）都有时按是否相同判断；否则比较需求描述的相似度。
    """
    if draft.customer_id is None:
        return None
    now = now or utcnow()
    settings = await todo_settings.load(session, draft.type.tenant_id)
    recent = now - timedelta(days=settings.reopen_days)
    candidates = (
        await session.scalars(
            select(Todo)
            .where(
                Todo.customer_id == draft.customer_id,
                Todo.type_id == draft.type.id,
                or_(
                    Todo.status.in_(UNFINISHED),
                    and_(Todo.status == TodoStatus.DONE, Todo.closed_at >= recent),
                ),
            )
            .order_by(Todo.created_at.desc())
            .limit(5)
        )
    ).all()
    if not candidates:
        return None
    wanted = todo_fields.identity_values(draft.fields)
    undecided = []
    for candidate in candidates:
        known = todo_fields.identity_values(candidate.fields)
        common = set(known) & set(wanted)
        if common:
            if all(known[k] == wanted[k] for k in common):
                return candidate
            continue
        undecided.append(candidate)
    if not undecided:
        return None
    return await _most_similar(
        ctx, draft.type.tenant_id, f"{draft.title}\n{draft.detail}", undecided
    )


def nudge(
    session: AsyncSession,
    todo: Todo,
    *,
    actor_type: str,
    detail: str,
    source: str,
    evidence: list[uuid.UUID] | None = None,
) -> None:
    """客户催促（或再次提出同一件事）：追加依据，记一次催促并提醒处理人或确认人。"""
    todo.nudge_count += 1
    todo.evidence_message_ids = list(
        dict.fromkeys([*(todo.evidence_message_ids or []), *(evidence or [])])
    )
    payload: dict[str, Any] = {"count": todo.nudge_count, "source": source, "detail": detail[:500]}
    if todo.nudge_count == NUDGES_TO_RAISE:
        raised = raise_priority(todo.priority)
        if raised != todo.priority:
            payload["priority"] = {"from": todo.priority, "to": raised}
            todo.priority = raised
    # 还没发出的新待办提醒已经包含这件事，不改成催促。
    waiting = todo.notified_at is None and todo.notify_reason in (
        NotifyReason.PENDING,
        NotifyReason.ASSIGNED,
    )
    if not waiting and todo.nudge_count <= NUDGE_NOTICES:
        todo.notify_reason = NotifyReason.NUDGED
        todo.notified_at = None
    events.record(session, todo, "nudged", actor_type=actor_type, payload=payload)


# ---- 数据范围 ----


def my_groups(staff_id: uuid.UUID) -> Any:
    return select(SkillGroupMember.skill_group_id).where(SkillGroupMember.staff_id == staff_id)


def visible_to(principal: Principal) -> ColumnElement[bool]:
    """数据范围（与客户一致）：分派给自己的、交给自己确认的、自己分派或新建的、自己能看到其客户的，
    以及所在技能组待认领的；主管另外能看到团队成员的和所带技能组的；能分派待办的人能看到公共
    待认领池；能看到全部客户的人能看到全部待办。"""
    if principal.has(Permission.CUSTOMER_READ_ALL) or principal.has(Permission.SESSION_READ_ALL):
        return true()
    me = principal.staff_id
    conditions: list[ColumnElement[bool]] = [
        Todo.assignee_id == me,
        Todo.created_by == me,
        Todo.assigned_by == me,
        and_(Todo.assignee_id.is_(None), Todo.skill_group_id.in_(my_groups(me))),
        Todo.customer_id.in_(select(Customer.id).where(customer_visible_to(principal))),
    ]
    if principal.has(Permission.SESSION_READ_TEAM):
        conditions += [
            Todo.assignee_id.in_(team_members(me)),
            Todo.skill_group_id.in_(led_groups(me)),
        ]
    if principal.has(Permission.TODO_ASSIGN):
        conditions.append(and_(Todo.assignee_id.is_(None), Todo.skill_group_id.is_(None)))
    return or_(*conditions)


async def get_visible(
    session: AsyncSession, principal: Principal, todo_id: uuid.UUID, *, lock: bool = False
) -> Todo:
    query = select(Todo).where(Todo.id == todo_id, visible_to(principal))
    if lock:
        query = query.with_for_update(of=Todo)
    todo = await session.scalar(query)
    if todo is None:
        raise NotFound(NOT_FOUND)
    return todo


# ---- 留言（设计文档 §8.2、§11.3：非工作时间、排队超时、访客主动留言都转为"留言"类待办） ----

LEAVE_MESSAGE_TITLES = {
    "visitor": "访客留言",
    "off_hours": "非工作时间留言",
    "queue_timeout": "排队超时留言",
}


async def leave_message(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    customer_id: uuid.UUID,
    session_id: uuid.UUID | None,
    origin: str,
    detail: str,
    channel_account_id: uuid.UUID | None = None,
    group_hint: uuid.UUID | None = None,
    contact: str | None = None,
    keys: TenantKeyring | None = None,
    now: datetime | None = None,
) -> Todo:
    """系统规则或访客自己生成的留言：直接进入待办列表，按"留言"类型的规则分派。
    租户把联系方式设成敏感字段时需要 keys 加密。"""
    type_ = await type_by_code(session, tenant_id, LEAVE_MESSAGE)
    visitor = origin == "visitor"
    values = todo_fields.clean(type_, {"contact": contact} if contact else {}, require=False)
    return await create(
        session,
        keys,
        Draft(
            type=type_,
            title=LEAVE_MESSAGE_TITLES[origin],
            detail=detail,
            fields=values.values,
            source=TodoSource.VISITOR if visitor else TodoSource.RULE,
            created_by_type=ActorType.VISITOR if visitor else ActorType.SYSTEM,
            customer_id=customer_id,
            session_id=session_id,
            channel_account_id=channel_account_id,
            group_hint=group_hint,
        ),
        now=now,
    )


async def open_leave_message(
    session: AsyncSession, room_id: uuid.UUID, since: datetime
) -> Todo | None:
    """这个 Room 最近生成、还没有人处理的系统留言（非工作时间的后续消息追加到这里）。"""
    return await session.scalar(
        select(Todo)
        .join(ChatSession, ChatSession.id == Todo.session_id)
        .join(TodoType, TodoType.id == Todo.type_id)
        .where(
            ChatSession.room_id == room_id,
            TodoType.code == LEAVE_MESSAGE,
            Todo.source == TodoSource.RULE,
            Todo.status == TodoStatus.OPEN,
            Todo.first_response_at.is_(None),
            Todo.created_at >= since,
        )
        .order_by(Todo.created_at.desc())
        .limit(1)
        .with_for_update(of=Todo)
    )
