"""从助理记录的内部群聊里提炼知识候选（设计文档 §27.4）。

调度进程每小时处理开启了提炼的群：取还没提炼的消息（至少 MIN_MESSAGES 条、最后一条已经过了
SETTLE 时间），先脱敏，再把说话人换成"同事1""同事2"，交给"群聊知识提炼"提示词；输出与会话提炼相同的
结构，经 kb.extraction.record_pair / record_gap 进入同一个审核台（来源"群聊"）。不跨租户。
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.context import AppContext
from app.integrations.llm import LLMUnavailable
from app.modules.ai import gateway, pii, prompts
from app.modules.ai import service as ai_service
from app.modules.assistant import settings as assistant_settings
from app.modules.assistant.models import (
    AssistantBot,
    AssistantGroup,
    AssistantGroupMessage,
    BotStatus,
)
from app.modules.billing.entitlements import has_feature
from app.modules.kb import extraction as kb_extraction
from app.modules.kb.models import CandidateSource
from app.modules.tenancy.models import Tenant, TenantStatus

logger = logging.getLogger(__name__)

MIN_MESSAGES = 3
SETTLE = timedelta(minutes=10)
BATCH_MESSAGES = 200
MAX_GROUPS = 50


@dataclass
class GroupReport:
    tenants: int = 0
    groups: int = 0
    messages: int = 0
    candidates: int = 0
    failed: int = 0


@dataclass(frozen=True)
class Outcome:
    messages: int
    candidates: int
    error: str | None = None


def transcript(messages: list[AssistantGroupMessage]) -> list[tuple[str, str]]:
    """（匿名的说话人, 脱敏后的内容）：同一个人连续的消息合并。"""
    mapping: dict[str, str] = {}
    speakers: dict[str, str] = {}
    lines: list[tuple[str, str]] = []
    for m in messages:
        text = m.text.strip()
        if not text or kb_extraction._GREETING.match(text):
            continue
        who = speakers.setdefault(m.sender_external_id, f"同事{len(speakers) + 1}")
        masked, mapping = pii.mask(text, mapping)
        if lines and lines[-1][0] == who:
            lines[-1] = (who, f"{lines[-1][1]}\n{masked}")
        else:
            lines.append((who, masked))
    return lines


def _evidence(
    group: AssistantGroup,
    question: str,
    lines: list[tuple[str, str]],
    indices: list[int],
    now: datetime,
) -> dict[str, Any]:
    picked = (
        [lines[i - 1] for i in sorted(set(indices)) if 1 <= i <= len(lines)]
        if indices
        else lines[:4]
    )
    return {
        "session_id": None,
        "group_id": str(group.id),
        "group_name": group.name,
        "seen_at": now.isoformat(),
        "question": question,
        "lines": [{"role": role, "text": text[:500]} for role, text in picked],
    }


async def extract_group(
    ctx: AppContext,
    tenant_id: uuid.UUID,
    group_id: uuid.UUID,
    *,
    now: datetime | None = None,
    force: bool = False,
    auto_merge: bool = False,
) -> Outcome:
    """提炼一个群里还没提炼的消息。force：不管条数和沉淀时间（控制台"立即提炼"）。"""
    now = now or datetime.now(UTC)
    async with ctx.db.tenant_session(tenant_id) as session:
        group = await session.get(AssistantGroup, group_id)
        if group is None:
            return Outcome(0, 0, "群不存在")
        rows = list(
            (
                await session.scalars(
                    select(AssistantGroupMessage)
                    .where(
                        AssistantGroupMessage.group_id == group.id,
                        AssistantGroupMessage.extracted_at.is_(None),
                    )
                    .order_by(AssistantGroupMessage.sent_at, AssistantGroupMessage.id)
                    .limit(BATCH_MESSAGES)
                )
            ).all()
        )
        if not rows:
            return Outcome(0, 0, None)
        if not force and (len(rows) < MIN_MESSAGES or rows[-1].sent_at > now - SETTLE):
            return Outcome(0, 0, None)
        ids = [r.id for r in rows]
        lines = transcript(rows)
    if not lines:
        async with ctx.db.tenant_session(tenant_id) as session:
            await _mark(session, group_id, ids, now, 0)
            await session.commit()
        return Outcome(len(ids), 0, None)
    prompt = await ctx.prompts.get("group_extract")
    try:
        result = await gateway.chat(
            ctx,
            tenant_id,
            prompts.group_extract_messages(transcript=lines, template=prompt.content),
            scene="group_extract",
            json_mode=True,
            max_tokens=1500,
            prompt_version=prompt.version,
        )
    except LLMUnavailable as exc:
        return Outcome(0, 0, str(exc)[:200])
    parsed = kb_extraction.parse(result.content, len(lines))
    if parsed is None:
        return Outcome(0, 0, "模型输出无法解析")
    pairs, gaps = parsed
    recorded = 0
    async with ctx.db.tenant_session(tenant_id) as session:
        group = await session.get(AssistantGroup, group_id)
        assert group is not None
        for pair in pairs:
            if await kb_extraction.record_pair(
                ctx,
                session,
                tenant_id,
                pair,
                _evidence(group, pair.question, lines, pair.evidence, now),
                now=now,
                model=result.model,
                auto_merge=auto_merge,
                source=CandidateSource.GROUP,
                prompt_version=prompt.version,
            ):
                recorded += 1
        for question in gaps:
            if await kb_extraction.record_gap(
                ctx,
                session,
                tenant_id,
                question,
                _evidence(group, question, lines, [], now),
                now=now,
                model=result.model,
                prompt_version=prompt.version,
            ):
                recorded += 1
        await _mark(session, group_id, ids, now, recorded)
        await session.commit()
    return Outcome(len(ids), recorded, None)


async def _mark(
    session: Any, group_id: uuid.UUID, ids: list[uuid.UUID], now: datetime, recorded: int
) -> None:
    for row in await session.scalars(
        select(AssistantGroupMessage).where(AssistantGroupMessage.id.in_(ids))
    ):
        row.extracted_at = now
    group = await session.get(AssistantGroup, group_id)
    if group is not None:
        group.last_extracted_at = now
        group.extracted_candidates += recorded


async def run_group_extraction(
    ctx: AppContext, *, now: datetime | None = None, tenant_code: str | None = None
) -> GroupReport:
    """调度任务：逐个租户提炼开启了提炼的群。"""
    report = GroupReport()
    if not await ctx.llms.any_enabled():
        return report
    now = now or datetime.now(UTC)
    async with ctx.db.platform_sessionmaker() as session:
        query = select(Tenant.id).where(Tenant.status == TenantStatus.ACTIVE)
        if tenant_code:
            query = query.where(Tenant.code == tenant_code)
        tenant_ids = list((await session.scalars(query.order_by(Tenant.created_at))).all())
    for tenant_id in tenant_ids:
        async with ctx.db.tenant_session(tenant_id) as session:
            settings = await assistant_settings.load(session, tenant_id)
            ai_settings = await ai_service.load(session, tenant_id)
            if (
                not settings.enabled
                or not settings.group_extraction
                or not await has_feature(session, tenant_id, "extraction")
                or not await ctx.llms.chat_enabled(tenant_id, "group_extract")
            ):
                continue
            group_ids = list(
                (
                    await session.scalars(
                        select(AssistantGroup.id)
                        .join(AssistantBot, AssistantBot.id == AssistantGroup.bot_id)
                        .where(
                            AssistantGroup.extract.is_(True),
                            AssistantBot.status == BotStatus.ACTIVE,
                            AssistantGroup.id.in_(
                                select(AssistantGroupMessage.group_id).where(
                                    AssistantGroupMessage.extracted_at.is_(None)
                                )
                            ),
                        )
                        .limit(MAX_GROUPS)
                    )
                ).all()
            )
            auto_merge = ai_settings.auto_merge_similar
        if not group_ids:
            continue
        report.tenants += 1
        for group_id in group_ids:
            try:
                outcome = await extract_group(
                    ctx, tenant_id, group_id, now=now, auto_merge=auto_merge
                )
            except Exception:
                logger.exception("group extraction failed for %s", group_id)
                report.failed += 1
                continue
            if outcome.error:
                report.failed += 1
            elif outcome.messages:
                report.groups += 1
                report.messages += outcome.messages
                report.candidates += outcome.candidates
    return report
