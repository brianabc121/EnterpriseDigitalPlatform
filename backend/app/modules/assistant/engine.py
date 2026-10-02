"""AI 公司助理的回答引擎（设计文档 §27.3.4）：大模型 + 以员工本人权限执行的工具。

- 限流：每个员工每分钟最多 N 次（租户设置）。
- 上下文：这个员工在这个渠道（某个机器人或控制台）最近 20 条对话。
- 脱敏：员工的消息先脱敏再交给模型，工具输出也脱敏；回复里的占位符还原给员工。
- 模型不支持工具调用时只做知识库问答。
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.integrations.llm import ChatResult, LLMUnavailable
from app.modules.ai import gateway, pii, prompts
from app.modules.assistant import settings as assistant_settings
from app.modules.assistant import tools as staff_tools
from app.modules.assistant.models import AssistantMessage, MessageRole
from app.modules.billing.entitlements import has_feature
from app.modules.iam.models import Role, StaffRole
from app.modules.iam.principal import Principal
from app.modules.iam.service import principal_for
from app.modules.kb.models import Visibility
from app.modules.kb.search import search as kb_search
from app.modules.todos import sla
from app.observability.context import bind_staff

logger = logging.getLogger(__name__)

HISTORY = 20
MAX_TOOL_ROUNDS = 4
REPLY_LIMIT = 2000
RATE_KEY = "t:{tenant}:assistant:{staff}"
NOT_ENABLED = "AI 助理还没有启用，请联系管理员。"
NO_MODEL = "还没有配置大模型，暂时不能回答。"
BUSY = "提问太频繁了，请稍后再试。"
UNAVAILABLE = "模型暂时不可用，请稍后再试。"
NO_FEATURE = "当前套餐不包含 AI 功能。"


@dataclass(frozen=True)
class Reply:
    text: str
    tools: list[str] = field(default_factory=list)


def utcnow() -> datetime:
    return datetime.now(UTC)


async def _history(
    session: AsyncSession, staff_id: uuid.UUID, bot_id: uuid.UUID | None
) -> list[tuple[str, str]]:
    rows = (
        await session.scalars(
            select(AssistantMessage)
            .where(
                AssistantMessage.staff_id == staff_id,
                AssistantMessage.bot_id.is_(None)
                if bot_id is None
                else AssistantMessage.bot_id == bot_id,
            )
            .order_by(AssistantMessage.created_at.desc(), AssistantMessage.id.desc())
            .limit(HISTORY)
        )
    ).all()
    return [(r.role, r.text) for r in reversed(rows)]


async def _roles(session: AsyncSession, staff_id: uuid.UUID) -> str:
    names = (
        await session.scalars(
            select(Role.name)
            .join(StaffRole, StaffRole.role_id == Role.id)
            .where(StaffRole.staff_id == staff_id)
        )
    ).all()
    return "、".join(names) or "员工"


def _record(
    session: AsyncSession,
    tenant_id: uuid.UUID,
    staff_id: uuid.UUID,
    bot_id: uuid.UUID | None,
    question: str,
    reply: Reply,
) -> None:
    session.add(
        AssistantMessage(
            tenant_id=tenant_id,
            bot_id=bot_id,
            staff_id=staff_id,
            role=MessageRole.USER.value,
            text=question,
        )
    )
    session.add(
        AssistantMessage(
            tenant_id=tenant_id,
            bot_id=bot_id,
            staff_id=staff_id,
            role=MessageRole.ASSISTANT.value,
            text=reply.text,
            tools=reply.tools,
        )
    )


async def _knowledge_only(
    ctx: AppContext, session: AsyncSession, principal: Principal, question: str
) -> Reply:
    """模型不支持工具调用：只检索知识库。"""
    if not principal.has("kb:read"):
        return Reply("当前模型不支持查询，我还不能帮你查待办和订单。")
    hits = await kb_search(
        ctx,
        session,
        principal.tenant_id,
        question,
        visibilities=(Visibility.PUBLIC, Visibility.AGENT),
        limit=2,
    )
    if not hits:
        return Reply("知识库里没有找到相关资料。当前模型不支持查询待办和订单。")
    text = "\n\n".join(f"{h.title}\n{h.text}" for h in hits)
    return Reply(f"知识库里找到：\n{text}"[:REPLY_LIMIT], ["search_knowledge"])


async def answer(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    staff_id: uuid.UUID,
    question: str,
    *,
    bot_id: uuid.UUID | None,
    now: datetime | None = None,
) -> Reply:
    """回答一位员工的提问并记录对话。任何情况下都返回一段可以直接发给员工的文字。

    机器人的消息不是员工请求，期间的大模型调用也记到这位员工名下（设计文档 §37）。
    """
    with bind_staff(tenant_id, staff_id):
        return await _answer(ctx, tenant_id, staff_id, question, bot_id=bot_id, now=now)


async def _answer(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    staff_id: uuid.UUID,
    question: str,
    *,
    bot_id: uuid.UUID | None,
    now: datetime | None,
) -> Reply:
    now = now or utcnow()
    question = question.strip()[:2000]
    async with ctx.db.tenant_session(tenant_id) as session:
        settings = await assistant_settings.load(session, tenant_id)
        if not settings.enabled:
            return Reply(NOT_ENABLED)
        if not await has_feature(session, tenant_id, "ai"):
            return Reply(NO_FEATURE)
        principal = await principal_for(session, tenant_id, staff_id)
        if principal is None:
            return Reply("账号已停用。")
        limiter = ctx.limiter
        if limiter is not None:
            usage = await limiter.hit_key(RATE_KEY.format(tenant=tenant_id, staff=staff_id), 60)
            if usage.count > settings.per_minute:
                return Reply(BUSY)
        if not await ctx.llms.chat_enabled(tenant_id, "assistant"):
            return Reply(NO_MODEL)
        use_tools = await ctx.llms.tools_supported(tenant_id, "assistant")
        history = await _history(session, staff_id, bot_id)
        roles = await _roles(session, staff_id)
        tz = sla.tz_of(await sla.business_hours(session))
        if not use_tools:
            reply = await _knowledge_only(ctx, session, principal, question)
            _record(session, tenant_id, staff_id, bot_id, question, reply)
            await session.commit()
            return reply
    mapping: dict[str, str] = {}
    masked_history: list[tuple[str, str]] = []
    for role, text in history:
        masked, mapping = pii.mask(text, mapping)
        masked_history.append((role, masked))
    masked_question, mapping = pii.mask(question, mapping)
    prompt = await ctx.prompts.get("assistant")
    toolbox = staff_tools.StaffToolBox(
        ctx=ctx, principal=principal, mapping=mapping, tz=tz, now=now
    )
    messages: list[dict[str, Any]] = prompts.assistant_messages(
        company=principal.tenant_name,
        bot_name=settings.name,
        persona=settings.persona,
        staff_name=principal.display_name,
        roles=roles,
        now=now.astimezone(tz).strftime("%Y-%m-%d %H:%M %A"),
        history=masked_history,
        question=masked_question,
        template=prompt.content,
    )
    specs = staff_tools.specs(principal)
    try:
        result: ChatResult | None = None
        for round_ in range(MAX_TOOL_ROUNDS + 1):
            result = await gateway.chat(
                ctx,
                tenant_id,
                messages,
                scene="assistant",
                max_tokens=1000,
                tools=specs if round_ < MAX_TOOL_ROUNDS else None,
                prompt_version=prompt.version,
            )
            if not result.tool_calls or result.message is None:
                break
            messages.append(result.message)
            for call in result.tool_calls:
                output = await toolbox.run(call)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
        assert result is not None
        text = pii.unmask(result.content.strip(), mapping)[:REPLY_LIMIT]
    except LLMUnavailable as exc:
        logger.warning("assistant unavailable for tenant %s: %s", tenant_id, exc)
        text = UNAVAILABLE
    if not text:
        text = "抱歉，我没有得到答案，请换个说法再问一次。"
    reply = Reply(text, [call["name"] for call in toolbox.log])
    async with ctx.db.tenant_session(tenant_id) as session:
        _record(session, tenant_id, staff_id, bot_id, question, reply)
        await session.commit()
    return reply
