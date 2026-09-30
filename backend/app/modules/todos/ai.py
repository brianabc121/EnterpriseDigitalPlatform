"""AI 接待的待办工具（设计文档 §11.1、§24.4）。

- create_todo：登记需要员工线下处理的事。只能用租户启用并开启了 AI 登记的类型；服务端按类型校验
  字段（必填、格式）、解析客户期望的时间（只接受未来 30 天内）、限制频率（每个会话每小时最多几条），
  同一次调用重试时不重复创建；与这位客户未完成的同类待办是同一件事时合并为一次催促。
  登记后进入待确认页，由人工确认；AI 只按类型的话术答复客户，不承诺处理时间和结果。
- lookup_todos：查询当前客户未完成的和最近 30 天完成的待办（类型、状态、预计完成时间、客户可见的
  进度说明），同时记一次催促。匿名访客只能查到在当前访客身份下登记的。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.modules.billing.entitlements import has_feature
from app.modules.channels.models import ChannelAccount, ChannelType
from app.modules.conversation.models import ChatSession, Room
from app.modules.customer.models import CustomerIdentity
from app.modules.todos import fields as todo_fields
from app.modules.todos import notify, presets, service, sla
from app.modules.todos import settings as todo_settings
from app.modules.todos.models import (
    UNFINISHED,
    ActorType,
    Todo,
    TodoSource,
    TodoStatus,
    TodoType,
    raise_priority,
)

DEFAULT_PROMISE = "已为您记录，客服确认后会尽快为您处理。"
LOOKUP_DAYS = 30
LOOKUP_LIMIT = 10
# 客户看到的状态。
CUSTOMER_STATUS: dict[str, str] = {
    TodoStatus.PENDING: "已登记，等待客服确认",
    TodoStatus.OPEN: "已受理，等待处理",
    TodoStatus.IN_PROGRESS: "处理中",
    TodoStatus.WAITING: "等待您补充信息",
    TodoStatus.DONE: "已完成",
    TodoStatus.CANCELLED: "已关闭",
}


@dataclass(frozen=True)
class AiType:
    id: uuid.UUID
    code: str
    name: str
    hint: str
    examples: tuple[str, ...]
    fields: tuple[dict[str, Any], ...]
    handoff: bool


async def ai_types(ctx: AppContext, tenant_id: uuid.UUID) -> list[AiType]:
    """AI 可以登记的类型：启用、开启了 AI 登记、不是系统类型。租户没有待办功能时为空。"""
    async with ctx.db.tenant_session(tenant_id) as session:
        if not await has_feature(session, tenant_id, "todos"):
            return []
        await presets.ensure_presets(session, tenant_id)
        await session.commit()
        rows = (
            await session.scalars(
                select(TodoType)
                .where(TodoType.enabled, TodoType.ai_enabled, ~TodoType.system)
                .order_by(TodoType.sort, TodoType.created_at)
            )
        ).all()
    return [
        AiType(
            id=row.id,
            code=row.code,
            name=row.name,
            hint=row.ai_hint,
            examples=tuple(row.examples or []),
            fields=tuple(row.fields or []),
            handoff=row.handoff,
        )
        for row in rows
    ]


def _describe(t: AiType) -> str:
    required = [f"{f['key']}（{f['label']}）" for f in t.fields if f.get("required")]
    optional = [f"{f['key']}（{f['label']}）" for f in t.fields if not f.get("required")]
    parts = [f"- {t.code}（{t.name}）：{t.hint}"]
    if t.examples:
        parts.append(f"客户常说：{' / '.join(t.examples[:3])}")
    if required:
        parts.append(f"必填字段：{'、'.join(required)}")
    if optional:
        parts.append(f"可填字段：{'、'.join(optional)}")
    return "；".join(parts)


def create_spec(types: list[AiType]) -> tuple[str, dict[str, Any]]:
    """create_todo 的说明和参数（类型按租户的配置动态生成）。"""
    description = "\n".join(
        [
            "登记客户提出的、需要员工线下处理的事（登记后由客服确认再处理）。"
            "必填字段不全时先向客户追问，补全后再调用；不要编造客户没有说的信息。可以登记的类型：",
            *(_describe(t) for t in types),
        ]
    )
    parameters = {
        "type": "object",
        "properties": {
            "type": {
                "type": "string",
                "enum": [t.code for t in types],
                "description": "事项类型的编码",
            },
            "title": {"type": "string", "description": "一句话标题，如「开具增值税专用发票」"},
            "detail": {"type": "string", "description": "客户的需求描述，保持客户原意，不加推测"},
            "fields": {
                "type": "object",
                "description": "类型的字段，键是字段编码，值是客户提供的内容",
                "additionalProperties": {"type": "string"},
            },
            "expected_time": {
                "type": "string",
                "description": "客户期望的时间，ISO 8601 格式（如 2026-10-01T10:00:00+08:00）；"
                "客户没有提到时不填",
            },
            "urgency": {"type": "string", "enum": ["normal", "high"]},
        },
        "required": ["type", "title", "detail"],
    }
    return description, parameters


LOOKUP_DESCRIPTION = (
    "查询当前客户之前登记的事项（如回电、开票、售后）的处理进度。客户询问进度时使用。"
)
LOOKUP_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {"type": {"type": "string", "description": "只查这个类型（编码），可以不填"}},
}


@dataclass(frozen=True)
class Registered:
    output: str
    todo_id: uuid.UUID | None = None
    handoff: AiType | None = None
    collecting: bool = False  # 正在追问必填信息（这一轮不计入 AI 接待轮次）


def _promise(type_: TodoType) -> str:
    return type_.promise_text.strip() or DEFAULT_PROMISE


async def register(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    types: list[AiType],
    args: dict[str, Any],
    *,
    session_id: uuid.UUID | None,
    customer_id: uuid.UUID | None,
    evidence_ids: list[uuid.UUID],
    dry_run: bool,
    now: datetime | None = None,
) -> Registered:
    now = now or datetime.now(UTC)
    code = str(args.get("type") or "").strip()
    wanted = next((t for t in types if t.code == code), None)
    if wanted is None:
        return Registered(f"类型只能是：{'、'.join(t.code for t in types)}。")
    title = str(args.get("title") or "").strip()[: service.TITLE_LIMIT]
    detail = str(args.get("detail") or "").strip()[:1500]
    if not title and not detail:
        return Registered("请提供事项的标题和客户的需求描述。")
    raw = args.get("fields")
    async with ctx.db.tenant_session(tenant_id) as db:
        type_ = await db.get(TodoType, wanted.id)
        if type_ is None or not type_.enabled or not type_.ai_enabled or type_.system:
            return Registered("这个类型现在不能登记。")
        cleaned = todo_fields.clean(type_, raw if isinstance(raw, dict) else {})
        if cleaned.errors or cleaned.missing:
            return Registered(
                f"还不能登记：{'；'.join(cleaned.problems())}。请先向客户询问或确认这些信息，"
                "补全后再登记。",
                collecting=True,
            )
        spec = await sla.business_hours(db)
        expected_at = todo_fields.parse_time(
            str(args.get("expected_time") or "") or None, sla.tz_of(spec), now
        )
        priority = raise_priority(type_.priority) if args.get("urgency") == "high" else None
        if dry_run or session_id is None or customer_id is None:
            return Registered(f"（试一试：不会登记）已登记。请这样答复客户：{_promise(type_)}")
        settings = await todo_settings.load(db, tenant_id)
        recent = await db.scalar(
            select(func.count())
            .select_from(Todo)
            .where(
                Todo.session_id == session_id,
                Todo.source == TodoSource.AI_CHAT,
                Todo.created_at >= now - timedelta(hours=1),
            )
        )
        if int(recent or 0) >= settings.ai_hourly_limit:
            return Registered(
                "本次对话登记的事项已经达到上限，不能继续登记。请告诉客户会转交人工客服处理。"
            )
        key = service.dedupe_key(session_id, code, title, detail, sorted(cleaned.values.items()))
        existing = await db.scalar(select(Todo).where(Todo.dedupe_key == key))
        if existing is not None:
            return Registered(
                f"已登记（编号 {existing.no}）。请这样答复客户：{_promise(type_)}", existing.id
            )
        chat = await db.get(ChatSession, session_id)
        draft = service.Draft(
            type=type_,
            title=title or type_.name,
            detail=detail,
            fields=cleaned.values,
            source=TodoSource.AI_CHAT,
            created_by_type=ActorType.AI,
            customer_id=customer_id,
            session_id=session_id,
            channel_account_id=chat.channel_account_id if chat else None,
            evidence_message_ids=evidence_ids,
            expected_at=expected_at,
            priority=priority,
            dedupe_key=key,
        )
        duplicate = await service.find_duplicate(ctx, db, draft, now=now)
        if duplicate is not None:
            service.nudge(
                db,
                duplicate,
                actor_type=ActorType.AI,
                detail=detail or title,
                source=TodoSource.AI_CHAT,
                evidence=evidence_ids,
            )
            await db.commit()
            await notify.dispatch(ctx, tenant_id=tenant_id, ids=[duplicate.id])
            state = CUSTOMER_STATUS.get(duplicate.status, "处理中")
            return Registered(
                f"客户之前已经登记过这件事（编号 {duplicate.no}，{state}），已提醒客服尽快处理，"
                "不需要重复登记。请告诉客户已经在跟进。",
                duplicate.id,
            )
        try:
            todo = await service.create(db, ctx.keys, draft, now=now)
            await db.commit()
        except IntegrityError:
            # 同一次调用并发重试：已经登记过了。
            await db.rollback()
            existing = await db.scalar(select(Todo).where(Todo.dedupe_key == key))
            if existing is None:
                raise
            return Registered(
                f"已登记（编号 {existing.no}）。请这样答复客户：{_promise(type_)}", existing.id
            )
        promise = _promise(type_)
    await notify.dispatch(ctx, tenant_id=tenant_id, ids=[todo.id])
    return Registered(
        f"已登记（编号 {todo.no}），客服确认后处理。请这样答复客户：{promise}",
        todo.id,
        handoff=wanted if wanted.handoff else None,
    )


async def anonymous_room(session: AsyncSession, session_id: uuid.UUID) -> uuid.UUID | None:
    """匿名的网页访客：返回当前访客身份的 Room（只能查在这里登记的）；实名访客和其他渠道为空。"""
    chat = await session.get(ChatSession, session_id)
    if chat is None:
        return None
    room = await session.get(Room, chat.room_id)
    if room is None:
        return None
    channel = await session.get(ChannelAccount, room.channel_account_id)
    identity = await session.get(CustomerIdentity, room.identity_id)
    if (
        channel is not None
        and channel.type == ChannelType.WEB
        and not (identity and identity.verified)
    ):
        return room.id
    return None


def customer_scope(customer_id: uuid.UUID, room_id: uuid.UUID | None, now: datetime) -> list[Any]:
    """客户能看到的待办：自己的、未完成的或最近 30 天结束的（不含驳回的）；
    匿名访客只含在当前身份下登记的。"""
    conditions: list[Any] = [
        Todo.customer_id == customer_id,
        or_(
            Todo.status.in_(UNFINISHED),
            and_(
                Todo.status.in_((TodoStatus.DONE, TodoStatus.CANCELLED)),
                Todo.closed_at >= now - timedelta(days=LOOKUP_DAYS),
            ),
        ),
    ]
    if room_id is not None:
        # 在这个访客身份的对话里登记的；访客自己提交、还没有会话的留言。
        conditions.append(
            or_(
                Todo.session_id.in_(select(ChatSession.id).where(ChatSession.room_id == room_id)),
                and_(Todo.session_id.is_(None), Todo.source == TodoSource.VISITOR),
            )
        )
    return conditions


async def lookup(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    *,
    session_id: uuid.UUID | None,
    customer_id: uuid.UUID | None,
    type_code: str | None,
    now: datetime | None = None,
) -> str:
    if session_id is None or customer_id is None:
        return "（试一试：没有真实客户）"
    now = now or datetime.now(UTC)
    async with ctx.db.tenant_session(tenant_id) as db:
        room_id = await anonymous_room(db, session_id)
        query = (
            select(Todo, TodoType)
            .join(TodoType, TodoType.id == Todo.type_id)
            .where(*customer_scope(customer_id, room_id, now))
        )
        if type_code:
            query = query.where(TodoType.code == type_code)
        rows = (await db.execute(query.order_by(Todo.created_at.desc()).limit(LOOKUP_LIMIT))).all()
        if not rows:
            return "没有查到客户登记过的事项。"
        spec = await sla.business_hours(db)
        tz = sla.tz_of(spec)
        latest = next((todo for todo, _ in rows if todo.status in UNFINISHED), None)
        if latest is not None:
            service.nudge(
                db, latest, actor_type=ActorType.AI, detail="客户询问进度", source="lookup"
            )
            await db.commit()
        lines = []
        for todo, type_ in rows:
            parts = [f"{type_.name}「{todo.title}」：{CUSTOMER_STATUS.get(todo.status, '处理中')}"]
            if todo.status in (TodoStatus.OPEN, TodoStatus.IN_PROGRESS) and todo.due_at:
                parts.append(f"预计 {todo.due_at.astimezone(tz):%m月%d日 %H:%M} 前完成")
            if todo.status in (TodoStatus.DONE, TodoStatus.CANCELLED) and todo.closed_at:
                parts.append(f"{todo.closed_at.astimezone(tz):%m月%d日}")
            if todo.progress_note:
                parts.append(f"进度说明：{todo.progress_note}")
            lines.append("，".join(parts))
    if latest is not None:
        await notify.dispatch(ctx, tenant_id=tenant_id, ids=[latest.id])
    return "\n".join(lines)
