"""会话小结（设计文档 §11.4）：人工接待的会话结束后，由大模型写小结和标签；坐席确认（可以修改）
后写入客户档案——标签并入客户标签，小结记到备注的最前面。

调度任务每分钟为最近一天内结束、有坐席接待、还没有小结的会话生成草稿；坐席也可以在结束会话后
立即生成。租户没有 AI 功能或没有配置大模型时不生成。
"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.errors import Conflict, Forbidden, NotFound, ServiceUnavailable
from app.core.permissions import Permission
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway, pii, prompts
from app.modules.ai.models import SessionSummary, SummaryStatus
from app.modules.ai.schemas import SessionSummaryOut
from app.modules.audit.service import record_audit
from app.modules.billing.entitlements import has_feature, require_feature
from app.modules.conversation.models import ChatSession, Message, SenderType, SessionStatus
from app.modules.customer.models import Customer
from app.modules.iam.principal import Principal
from app.modules.sessions.service import visible_session

logger = logging.getLogger(__name__)

WINDOW = timedelta(days=1)
BATCH = 20
MAX_TAGS = 5
MAX_CUSTOMER_TAGS = 50
_ROLE: dict[str, str] = {
    SenderType.CUSTOMER: "客户",
    SenderType.AGENT: "坐席",
    SenderType.BOT: "智能客服",
}


def _parse(content: str) -> tuple[str, list[str]] | None:
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(content[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        return None
    summary = pii.strip_placeholders(data["summary"]).strip()[:500]
    tags = [
        str(t).strip()[:32]
        for t in data.get("tags") or []
        if isinstance(t, str | int) and str(t).strip()
    ]
    return (summary, list(dict.fromkeys(tags))[:MAX_TAGS]) if summary else None


async def transcript(session: AsyncSession, session_id: uuid.UUID) -> list[tuple[str, str]]:
    """会话的文字记录（最近 60 条，客户、坐席和智能客服的），脱敏后用于交给大模型。"""
    rows = (
        await session.scalars(
            select(Message)
            .where(Message.session_id == session_id, Message.sender_type.in_(list(_ROLE)))
            .order_by(Message.sent_at, Message.id)
        )
    ).all()
    mapping: dict[str, str] = {}
    lines: list[tuple[str, str]] = []
    for message in rows[-60:]:
        text = message.text_plain or f"[{message.content_type}]"
        lines.append((_ROLE[message.sender_type], pii.mask(text[:500], mapping)[0]))
    return lines


async def generate(ctx: AppContext, tenant_id: uuid.UUID, session_id: uuid.UUID) -> SessionSummary:
    """生成（或重新生成）会话小结草稿。已经确认的小结不再覆盖。"""
    async with ctx.db.tenant_session(tenant_id) as session:
        chat = await session.get(ChatSession, session_id)
        if chat is None:
            raise NotFound("会话不存在")
        existing = await session.get(SessionSummary, session_id)
        if existing is not None and existing.status == SummaryStatus.CONFIRMED:
            return existing
        lines = await transcript(session, session_id)
        customer_id = chat.customer_id
    if not any(role == "客户" for role, _ in lines):
        raise Conflict("会话里没有客户的消息，不需要小结")
    prompt = await ctx.prompts.get("session_summary")
    try:
        result = await gateway.chat(
            ctx,
            tenant_id,
            prompts.session_summary_messages(transcript=lines, template=prompt.content),
            scene="session_summary",
            json_mode=True,
            max_tokens=500,
            session_id=session_id,
            prompt_version=prompt.version,
        )
    except LLMUnavailable as exc:
        raise ServiceUnavailable("大模型暂时不可用，请稍后再试") from exc
    parsed = _parse(result.content)
    if parsed is None:
        raise ServiceUnavailable("没能生成小结，请稍后再试")
    summary, tags = parsed
    now = datetime.now(UTC)
    values = {
        "summary": summary,
        "tags": tags,
        "status": SummaryStatus.DRAFT.value,
        "generated_at": now,
    }
    async with ctx.db.tenant_session(tenant_id) as session:
        await session.execute(
            insert(SessionSummary)
            .values(tenant_id=tenant_id, session_id=session_id, customer_id=customer_id, **values)
            .on_conflict_do_update(
                index_elements=[SessionSummary.session_id],
                set_=values,
                where=SessionSummary.status != SummaryStatus.CONFIRMED.value,
            )
        )
        await session.commit()
        row = await session.get(SessionSummary, session_id)
        assert row is not None
        return row


async def confirm(
    session: AsyncSession,
    summary: SessionSummary,
    *,
    text: str,
    tags: list[str],
    staff_id: uuid.UUID,
    tz: ZoneInfo,
) -> SessionSummary:
    """坐席确认小结：写入客户档案（标签合并，小结记到备注最前面）。由调用方提交。"""
    if summary.status == SummaryStatus.CONFIRMED:
        raise Conflict("小结已经确认过了")
    customer = await session.get(Customer, summary.customer_id, with_for_update=True)
    if customer is None:
        raise NotFound("客户不存在")
    text = text.strip()
    summary.summary = text
    summary.tags = list(dict.fromkeys(t.strip() for t in tags if t.strip()))[:MAX_TAGS]
    summary.status = SummaryStatus.CONFIRMED
    summary.confirmed_by = staff_id
    summary.confirmed_at = datetime.now(UTC)
    customer.tags = list(dict.fromkeys([*(customer.tags or []), *summary.tags]))[:MAX_CUSTOMER_TAGS]
    day = summary.confirmed_at.astimezone(tz).strftime("%Y-%m-%d")
    entry = f"【{day} 会话小结】{text}"
    customer.notes = f"{entry}\n{customer.notes}" if customer.notes else entry
    return summary


def _pending(now: datetime) -> Any:
    """最近一天结束、有坐席接待、还没有小结的会话。"""
    return (
        select(ChatSession.id)
        .outerjoin(SessionSummary, SessionSummary.session_id == ChatSession.id)
        .where(
            ChatSession.status == SessionStatus.CLOSED,
            ChatSession.closed_at >= now - WINDOW,
            ChatSession.assigned_at.is_not(None),
            SessionSummary.session_id.is_(None),
        )
    )


async def run_pending(ctx: AppContext, *, now: datetime | None = None) -> int:
    """调度任务：为最近结束、有坐席接待、还没有小结的会话生成草稿。返回生成的数量。

    先找出有待处理会话的租户，只处理有 AI 功能、配置了大模型的租户，避免其他租户的会话占满每一批。
    """
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
    generated = 0
    budget = BATCH
    for tenant_id in tenant_ids:
        if budget <= 0:
            break
        async with ctx.db.tenant_session(tenant_id) as session:
            if not await has_feature(session, tenant_id, "ai"):
                continue
            if not await ctx.llms.chat_enabled(tenant_id, "session_summary"):
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
                await generate(ctx, tenant_id, session_id)
                generated += 1
            except Conflict:
                # 没有客户消息：记一条已丢弃的小结，不再重复处理。
                await _skip(ctx, tenant_id, session_id)
            except (ServiceUnavailable, NotFound) as exc:
                logger.warning("session summary for %s failed: %s", session_id, exc)
    return generated


async def _skip(ctx: AppContext, tenant_id: uuid.UUID, session_id: uuid.UUID) -> None:
    async with ctx.db.tenant_session(tenant_id) as session:
        chat = await session.get(ChatSession, session_id)
        if chat is None:
            return
        await session.execute(
            insert(SessionSummary)
            .values(
                tenant_id=tenant_id,
                session_id=session_id,
                customer_id=chat.customer_id,
                summary="",
                status=SummaryStatus.DISCARDED.value,
            )
            .on_conflict_do_nothing(index_elements=[SessionSummary.session_id])
        )
        await session.commit()


# ---- 接口 ----


def summary_out(row: SessionSummary) -> SessionSummaryOut:
    return SessionSummaryOut(
        session_id=row.session_id,
        customer_id=row.customer_id,
        summary=row.summary,
        tags=list(row.tags or []),
        status=row.status,
        generated_at=row.generated_at,
        confirmed_by=row.confirmed_by,
        confirmed_at=row.confirmed_at,
    )


async def _writable(
    session: AsyncSession, principal: Principal, session_id: uuid.UUID
) -> ChatSession:
    """接待这个会话的坐席、客户的归属坐席、能强制转接的主管可以生成和确认小结。"""
    chat, *_ = await visible_session(session, principal, session_id)
    if chat.assignee_id == principal.staff_id or principal.has(Permission.SESSION_TRANSFER_ANY):
        return chat
    owner = await session.scalar(select(Customer.owner_id).where(Customer.id == chat.customer_id))
    if owner is not None and owner == principal.staff_id:
        return chat
    raise Forbidden("只有接待坐席、客户的归属坐席或主管可以处理会话小结")


async def get_for(
    session: AsyncSession, principal: Principal, session_id: uuid.UUID
) -> SessionSummaryOut | None:
    await visible_session(session, principal, session_id)
    row = await session.get(SessionSummary, session_id)
    return summary_out(row) if row is not None else None


async def generate_for(
    ctx: AppContext, session: AsyncSession, principal: Principal, session_id: uuid.UUID
) -> SessionSummaryOut:
    chat = await _writable(session, principal, session_id)
    if chat.status != SessionStatus.CLOSED:
        raise Conflict("会话结束后才能生成小结")
    await require_feature(session, principal.tenant_id, "ai")
    if not await ctx.llms.chat_enabled(principal.tenant_id, "session_summary"):
        raise ServiceUnavailable("还没有配置大模型，不能生成小结")
    row = await generate(ctx, principal.tenant_id, session_id)
    if row.status == SummaryStatus.CONFIRMED:
        raise Conflict("小结已经确认过了")
    return summary_out(row)


async def confirm_for(
    session: AsyncSession,
    principal: Principal,
    session_id: uuid.UUID,
    *,
    text: str,
    tags: list[str],
    tz: ZoneInfo,
    ip: str | None,
) -> SessionSummaryOut:
    await _writable(session, principal, session_id)
    row = await session.get(SessionSummary, session_id, with_for_update=True)
    if row is None or row.status == SummaryStatus.DISCARDED:
        raise NotFound("还没有生成小结")
    await confirm(session, row, text=text, tags=tags, staff_id=principal.staff_id, tz=tz)
    record_audit(
        session,
        action="customer.summary_confirm",
        actor_type="staff",
        actor_id=principal.staff_id,
        tenant_id=principal.tenant_id,
        resource_type="customer",
        resource_id=str(row.customer_id),
        detail={"session_id": str(session_id), "tags": row.tags},
        ip=ip,
    )
    await session.commit()
    return summary_out(row)


async def discard_for(
    session: AsyncSession, principal: Principal, session_id: uuid.UUID
) -> SessionSummaryOut:
    await _writable(session, principal, session_id)
    row = await session.get(SessionSummary, session_id, with_for_update=True)
    if row is None:
        raise NotFound("还没有生成小结")
    if row.status == SummaryStatus.CONFIRMED:
        raise Conflict("小结已经确认过了")
    row.status = SummaryStatus.DISCARDED
    await session.commit()
    return summary_out(row)


async def customer_summaries(
    session: AsyncSession, customer_id: uuid.UUID, limit: int = 20
) -> list[SessionSummary]:
    """客户已确认的会话小结（新的在前）。调用方负责检查客户可见。"""
    rows = await session.scalars(
        select(SessionSummary)
        .where(
            SessionSummary.customer_id == customer_id,
            SessionSummary.status == SummaryStatus.CONFIRMED,
        )
        .order_by(SessionSummary.confirmed_at.desc())
        .limit(limit)
    )
    return list(rows.all())
