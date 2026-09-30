"""从对话里解析待办（设计文档 §24.3、§24.4）。

- 会话后解析：人工接待的会话结束后，由大模型找出客户还没有解决的诉求和坐席答应客户的事，
  置信度达到阈值（默认 0.6）的进入待确认页；与这位客户已有的同类待办是同一件事的跳过。
  每个会话只解析一次（todo_extractions）。租户没有 AI 或待办功能、关闭了会话后解析、
  没有配置大模型时不解析。
- AI 预填：坐席在工作台选中几条消息、员工在侧边栏粘贴客户的话，由大模型预填待办表单（不保存），
  员工核对后保存，直接进入待办列表。
"""

import json
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import ServiceUnavailable, Unprocessable
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway, pii, prompts
from app.modules.billing.entitlements import has_feature, require_feature
from app.modules.conversation.models import ChatSession, Message, SenderType, SessionStatus
from app.modules.iam.principal import Principal
from app.modules.todos import events, notify, presets, service, sla
from app.modules.todos import fields as todo_fields
from app.modules.todos import settings as todo_settings
from app.modules.todos.models import ActorType, TodoExtraction, TodoSource, TodoType
from app.modules.todos.schemas import ExtractRequest, ExtractResult, TodoSuggestion

logger = logging.getLogger(__name__)

WINDOW = timedelta(days=1)
BATCH = 10
MAX_ITEMS = 5
TRANSCRIPT_LIMIT = 80
_ROLE: dict[str, str] = {
    SenderType.CUSTOMER: "客户",
    SenderType.AGENT: "坐席",
    SenderType.BOT: "智能客服",
}


@dataclass
class Item:
    type: TodoType
    title: str
    detail: str
    fields: dict[str, str]
    missing: list[str]
    expected_at: datetime | None
    confidence: float
    promised_by_agent: bool
    evidence: list[uuid.UUID] = field(default_factory=list)


def _describe(row: TodoType) -> str:
    fields = "、".join(
        f"{f['key']}（{f['label']}{'，必填' if f.get('required') else ''}）"
        for f in row.fields or []
    )
    examples = f"；客户常说：{' / '.join(row.examples[:3])}" if row.examples else ""
    return f"- {row.code}（{row.name}）：{row.ai_hint}{examples}；字段：{fields or '无'}"


async def _types(session: AsyncSession, tenant_id: uuid.UUID) -> list[TodoType]:
    await presets.ensure_presets(session, tenant_id)
    return list(
        (
            await session.scalars(
                select(TodoType)
                .where(TodoType.enabled, TodoType.ai_enabled, ~TodoType.system)
                .order_by(TodoType.sort, TodoType.created_at)
            )
        ).all()
    )


def _json(content: str) -> dict[str, Any] | None:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def parse(
    content: str,
    types: Sequence[TodoType],
    *,
    mapping: dict[str, str],
    message_ids: Sequence[uuid.UUID | None],
    tz: Any,
    now: datetime,
) -> list[Item] | None:
    """解析模型的输出（不合法时为空）：只接受已启用类型；字段按类型清理，占位符还原成原文。"""
    data = _json(content)
    if data is None or not isinstance(data.get("todos"), list):
        return None
    by_code = {t.code: t for t in types}
    items: list[Item] = []
    for raw in data["todos"][:MAX_ITEMS]:
        if not isinstance(raw, dict):
            continue
        type_ = by_code.get(str(raw.get("type") or ""))
        if type_ is None:
            continue
        title = pii.unmask(str(raw.get("title") or ""), mapping).strip()[: service.TITLE_LIMIT]
        detail = pii.unmask(str(raw.get("detail") or ""), mapping).strip()[:1500]
        if not title and not detail:
            continue
        raw_fields = raw.get("fields")
        values = (
            {str(k): pii.unmask(str(v), mapping) for k, v in raw_fields.items() if v}
            if isinstance(raw_fields, dict)
            else {}
        )
        cleaned = todo_fields.clean(type_, values)
        evidence: list[uuid.UUID] = []
        for number in raw.get("evidence") or []:
            if isinstance(number, int) and 1 <= number <= len(message_ids):
                found = message_ids[number - 1]
                if found is not None:
                    evidence.append(found)
        items.append(
            Item(
                type=type_,
                title=title or type_.name,
                detail=detail,
                fields=cleaned.values,
                missing=cleaned.missing,
                expected_at=todo_fields.parse_time(str(raw.get("due_at") or "") or None, tz, now),
                confidence=_confidence(raw.get("confidence")),
                promised_by_agent=bool(raw.get("promised_by_agent")),
                evidence=list(dict.fromkeys(evidence)),
            )
        )
    return items


async def _transcript(
    session: AsyncSession, messages: Sequence[Message]
) -> tuple[list[tuple[str, str]], list[uuid.UUID | None], dict[str, str]]:
    mapping: dict[str, str] = {}
    lines: list[tuple[str, str]] = []
    ids: list[uuid.UUID | None] = []
    for message in messages[-TRANSCRIPT_LIMIT:]:
        if message.sender_type not in _ROLE:
            continue
        text = message.text_plain or f"[{message.content_type}]"
        lines.append((_ROLE[message.sender_type], pii.mask(text[:500], mapping)[0]))
        ids.append(message.id)
    return lines, ids, mapping


async def _ask(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    types: list[TodoType],
    lines: list[tuple[str, str]],
    *,
    session_id: uuid.UUID | None,
    tz: Any,
    now: datetime,
) -> str:
    prompt = await ctx.prompts.get("todo_extract")
    result = await gateway.chat(
        ctx,
        tenant_id,
        prompts.todo_extract_messages(
            types=[_describe(t) for t in types],
            transcript=lines,
            now=now.astimezone(tz).isoformat(timespec="minutes"),
            template=prompt.content,
        ),
        scene="todo_extract",
        json_mode=True,
        max_tokens=1200,
        session_id=session_id,
        prompt_version=prompt.version,
    )
    return result.content


# ---- 会话后解析 ----


def _pending(now: datetime) -> Any:
    """最近一天结束、有坐席接待、还没有解析的会话。"""
    return (
        select(ChatSession.id)
        .outerjoin(TodoExtraction, TodoExtraction.session_id == ChatSession.id)
        .where(
            ChatSession.status == SessionStatus.CLOSED,
            ChatSession.closed_at >= now - WINDOW,
            ChatSession.assigned_at.is_not(None),
            TodoExtraction.session_id.is_(None),
        )
    )


async def _record(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    status: str,
    *,
    created: int = 0,
    skipped: int = 0,
) -> None:
    async with ctx.db.tenant_session(tenant_id) as session:
        await session.execute(
            insert(TodoExtraction)
            .values(
                tenant_id=tenant_id,
                session_id=session_id,
                status=status,
                created=created,
                skipped=skipped,
            )
            .on_conflict_do_nothing(index_elements=[TodoExtraction.session_id])
        )
        await session.commit()


async def extract_session(
    ctx: AppContext, tenant_id: uuid.UUID, session_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """解析一个已结束的会话，返回进入待确认页的数量。"""
    now = now or datetime.now(UTC)
    async with ctx.db.tenant_session(tenant_id) as session:
        chat = await session.get(ChatSession, session_id)
        if chat is None:
            return 0
        settings = await todo_settings.load(session, tenant_id)
        types = await _types(session, tenant_id)
        await session.commit()
        spec = await sla.business_hours(session)
        tz = sla.tz_of(spec)
        messages = (
            await session.scalars(
                select(Message)
                .where(Message.session_id == session_id)
                .order_by(Message.sent_at, Message.id)
            )
        ).all()
        lines, ids, mapping = await _transcript(session, messages)
    if not types or not any(role == "客户" for role, _ in lines):
        await _record(ctx, tenant_id, session_id, "empty")
        return 0
    try:
        content = await _ask(ctx, tenant_id, types, lines, session_id=session_id, tz=tz, now=now)
    except LLMUnavailable as exc:
        logger.warning("to-do extraction for session %s failed: %s", session_id, exc)
        return 0  # 下次再试
    items = parse(content, types, mapping=mapping, message_ids=ids, tz=tz, now=now)
    if items is None:
        await _record(ctx, tenant_id, session_id, "failed")
        return 0
    created: list[uuid.UUID] = []
    skipped = 0
    async with ctx.db.tenant_session(tenant_id) as session:
        chat = await session.get(ChatSession, session_id)
        for item in items:
            if item.confidence < settings.extract_min_confidence:
                skipped += 1
                continue
            type_ = await session.get(TodoType, item.type.id)
            if type_ is None or chat is None:
                skipped += 1
                continue
            draft = service.Draft(
                type=type_,
                title=item.title,
                detail=item.detail,
                fields=item.fields,
                source=TodoSource.AI_SUMMARY,
                created_by_type=ActorType.AI,
                customer_id=chat.customer_id,
                session_id=chat.id,
                channel_account_id=chat.channel_account_id,
                evidence_message_ids=item.evidence,
                expected_at=item.expected_at,
                confidence=item.confidence,
                dedupe_key=service.dedupe_key("summary", chat.id, type_.code, item.title),
            )
            if await service.find_duplicate(ctx, session, draft, now=now) is not None:
                skipped += 1
                continue
            todo = await service.create(session, ctx.keys, draft, now=now)
            events.record(
                session,
                todo,
                "extracted",
                actor_type=ActorType.AI,
                payload={"promised_by_agent": item.promised_by_agent, "missing": item.missing},
            )
            created.append(todo.id)
        session.add(
            TodoExtraction(
                tenant_id=tenant_id,
                session_id=session_id,
                status="done" if items else "empty",
                created=len(created),
                skipped=skipped,
            )
        )
        await session.commit()
    if created:
        await notify.dispatch(ctx, tenant_id=tenant_id, ids=created)
    return len(created)


async def run_pending(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：解析最近结束的人工会话。先找出有待解析会话的租户，只处理开启了的租户。"""
    now = now or datetime.now(UTC)
    async with ctx.db.platform_sessionmaker() as session:
        tenant_ids = list(
            (
                await session.scalars(
                    select(ChatSession.tenant_id)
                    .where(ChatSession.id.in_(_pending(now)))
                    .distinct()
                )
            ).all()
        )
    created = 0
    budget = BATCH
    for tenant_id in tenant_ids:
        if budget <= 0:
            break
        async with ctx.db.tenant_session(tenant_id) as session:
            if not await has_feature(session, tenant_id, "ai"):
                continue
            if not await has_feature(session, tenant_id, "todos"):
                continue
            if not (await todo_settings.load(session, tenant_id)).extract_enabled:
                continue
            if not await ctx.llms.chat_enabled(tenant_id, "todo_extract"):
                continue
            session_ids = list(
                (
                    await session.scalars(
                        _pending(now).order_by(ChatSession.closed_at).limit(budget)
                    )
                ).all()
            )
        budget -= len(session_ids)
        for session_id in session_ids:
            try:
                created += await extract_session(ctx, tenant_id, session_id, now=now)
            except Exception:
                logger.exception("to-do extraction for session %s failed", session_id)
                await _record(ctx, tenant_id, session_id, "failed")
    return created


# ---- AI 预填 ----


async def prefill(
    ctx: AppContext, session: AsyncSession, principal: Principal, payload: ExtractRequest
) -> ExtractResult:
    """从选中的消息或粘贴的文字预填待办（不保存）。"""
    await require_feature(session, principal.tenant_id, "ai")
    now = datetime.now(UTC)
    types = await _types(session, principal.tenant_id)
    await session.commit()
    if not types:
        raise Unprocessable("没有可以由 AI 预填的待办类型")
    spec = await sla.business_hours(session)
    tz = sla.tz_of(spec)
    if payload.message_ids:
        if payload.session_id is None:
            raise Unprocessable("选中消息时需要指定会话")
        from app.modules.sessions.service import visible_session

        await visible_session(session, principal, payload.session_id)
        messages = (
            await session.scalars(
                select(Message)
                .where(
                    Message.session_id == payload.session_id,
                    Message.id.in_(payload.message_ids),
                )
                .order_by(Message.sent_at, Message.id)
            )
        ).all()
        lines, ids, mapping = await _transcript(session, messages)
    elif payload.text and payload.text.strip():
        mapping = {}
        lines = [("客户", pii.mask(payload.text.strip()[:4000], mapping)[0])]
        ids = [None]
    else:
        raise Unprocessable("请选择消息或粘贴客户的话")
    if not lines:
        raise Unprocessable("没有找到选中的消息")
    try:
        content = await _ask(
            ctx, principal.tenant_id, types, lines, session_id=payload.session_id, tz=tz, now=now
        )
    except LLMUnavailable as exc:
        raise ServiceUnavailable("大模型暂时不可用，请稍后再试或手工填写") from exc
    items = parse(content, types, mapping=mapping, message_ids=ids, tz=tz, now=now)
    if items is None:
        raise ServiceUnavailable("没能识别出待办，请手工填写")
    return ExtractResult(
        items=[
            TodoSuggestion(
                type_id=item.type.id,
                type_code=item.type.code,
                type_name=item.type.name,
                title=item.title,
                detail=item.detail,
                fields=item.fields,
                missing=item.missing,
                expected_at=item.expected_at,
                confidence=item.confidence,
                promised_by_agent=item.promised_by_agent,
                evidence_message_ids=item.evidence,
            )
            for item in items
        ]
    )
