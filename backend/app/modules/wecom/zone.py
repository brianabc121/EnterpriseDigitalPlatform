"""数据与智能专区（可选增强，设计文档 §10.6）。

企业购买会话存档并授权专区后，服务商把分析程序部署在专区里：群聊原文只在专区内处理，平台调用
专区程序（chatdata/sync_call_program）取回分析结果——群聊摘要、情绪、标签和问答候选，拿不到原文。

- 调度进程每小时为开启了专区的企业取回上次以来的结果；问答候选进入知识审核台（来源记为 zone）。
- 请求和返回的内容由平台与自己的专区程序约定（request_data / response_data 都是 JSON 字符串）：
  请求 {"action": "analyze_group_chats", "since": 秒, "until": 秒}；
  返回 {"results": [{"chat_id", "kind": summary|sentiment|tags|qa_candidates, "payload": {...},
  "window_start": 秒, "window_end": 秒}]}。
- 专区的可用模型、资源和费用需要 POC 确认（设计 §10.6），接口以官方为准。
"""

import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.integrations.wecom import WeComError
from app.modules.ai import service as ai_service
from app.modules.kb.extraction import Pair, record_pair
from app.modules.kb.models import CandidateSource
from app.modules.wecom.models import (
    CorpStatus,
    WecomCorp,
    WecomGroupChat,
    WecomZoneResult,
    ZoneResultKind,
)
from app.modules.wecom.schemas import WecomSettings
from app.modules.wecom.service import active_corp

logger = logging.getLogger(__name__)

_FIRST_WINDOW = timedelta(days=1)
_KINDS = {k.value for k in ZoneResultKind}
ZONE_MODEL = "wecom-zone"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ts(value: Any) -> datetime | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


def zone_configured(settings: WecomSettings) -> bool:
    return bool(settings.zone_enabled and settings.zone_program_id and settings.zone_ability_id)


async def pull_zone_results(ctx: AppContext) -> int:
    """调度进程：为开启了专区的企业取回分析结果，返回新保存的结果数。"""
    if ctx.wecom is None:
        return 0
    async with ctx.db.platform_sessionmaker() as session:
        rows = (
            await session.execute(
                select(WecomCorp.tenant_id, WecomCorp.settings).where(
                    WecomCorp.status == CorpStatus.ACTIVE
                )
            )
        ).all()
    total = 0
    for tenant_id, settings in rows:
        if not zone_configured(WecomSettings.of(settings)):
            continue
        try:
            total += await pull_tenant(ctx, tenant_id)
        except WeComError as exc:
            logger.warning("zone results for tenant %s failed: %s", tenant_id, exc)
            await _record(ctx, tenant_id, error=str(exc)[:300])
    return total


async def _record(
    ctx: AppContext,
    tenant_id: UUID,
    *,
    error: str | None = None,
    count: int | None = None,
    until: datetime | None = None,
) -> None:
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await active_corp(session)
        if corp is None:
            return
        previous = dict((corp.sync_state or {}).get("zone") or {})
        entry: dict[str, Any] = {**previous, "at": utcnow().isoformat(), "error": error}
        if count is not None:
            entry["count"] = count
        if until is not None:
            entry["until"] = until.isoformat()
        corp.sync_state = {**(corp.sync_state or {}), "zone": entry}
        await session.commit()


async def pull_tenant(ctx: AppContext, tenant_id: UUID, *, now: datetime | None = None) -> int:
    """取回一个企业上次以来的分析结果。"""
    if ctx.wecom is None:
        return 0
    now = now or utcnow()
    async with ctx.db.tenant_session(tenant_id) as session:
        corp = await active_corp(session)
        if corp is None:
            return 0
        settings = WecomSettings.of(corp.settings)
        if not zone_configured(settings):
            return 0
        state = (corp.sync_state or {}).get("zone") or {}
        corp_id = corp.corp_id
    try:
        since = datetime.fromisoformat(str(state.get("until")))
    except ValueError:
        since = now - _FIRST_WINDOW
    request = {
        "action": "analyze_group_chats",
        "since": int(since.timestamp()),
        "until": int(now.timestamp()),
    }
    data = await ctx.wecom.corp_call(
        corp_id,
        "POST",
        "/cgi-bin/chatdata/sync_call_program",
        json={
            "program_id": settings.zone_program_id,
            "ability_id": settings.zone_ability_id,
            "notify_id": uuid.uuid4().hex,
            "request_data": json.dumps(request),
        },
    )
    try:
        response = json.loads(str(data.get("response_data") or "{}"))
    except json.JSONDecodeError:
        await _record(ctx, tenant_id, error="专区程序返回的内容不是 JSON")
        return 0
    results = [
        r
        for r in (response.get("results") or [])
        if isinstance(r, dict) and r.get("chat_id") and r.get("kind") in _KINDS
    ]
    saved = await save_results(ctx, tenant_id, results, now=now)
    await _record(ctx, tenant_id, count=saved, until=now)
    return saved


async def save_results(
    ctx: AppContext, tenant_id: UUID, results: list[dict[str, Any]], *, now: datetime
) -> int:
    """保存分析结果；问答候选进入知识审核台（与会话提炼一样去重、聚类）。"""
    async with ctx.db.tenant_session(tenant_id) as session:
        ai = await ai_service.load(session, tenant_id)
        names = dict(
            (
                await session.execute(
                    select(WecomGroupChat.chat_id, WecomGroupChat.name).where(
                        WecomGroupChat.chat_id.in_({str(r["chat_id"]) for r in results})
                    )
                )
            ).all()
        )
        for r in results:
            chat_id = str(r["chat_id"])[:64]
            raw = r.get("payload")
            payload: dict[str, Any] = raw if isinstance(raw, dict) else {}
            session.add(
                WecomZoneResult(
                    tenant_id=tenant_id,
                    chat_id=chat_id,
                    kind=str(r["kind"]),
                    payload=payload,
                    window_start=_ts(r.get("window_start")),
                    window_end=_ts(r.get("window_end")),
                )
            )
            if r["kind"] == ZoneResultKind.QA_CANDIDATES and ai.extraction_enabled:
                await _candidates(
                    ctx,
                    session,
                    tenant_id,
                    chat_id,
                    names.get(chat_id),
                    payload,
                    now,
                    auto_merge=ai.auto_merge_similar,
                )
        await session.commit()
    return len(results)


async def _candidates(
    ctx: AppContext,
    session: AsyncSession,
    tenant_id: UUID,
    chat_id: str,
    group_name: str | None,
    payload: dict[str, Any],
    now: datetime,
    *,
    auto_merge: bool,
) -> None:
    for raw in payload.get("qa_pairs") or []:
        if not isinstance(raw, dict):
            continue
        question = str(raw.get("question") or "").strip()[:500]
        answer = str(raw.get("answer") or "").strip()[:2000]
        try:
            confidence = float(raw.get("confidence", 0.7))
        except (TypeError, ValueError):
            confidence = 0.7
        if not question or not answer:
            continue
        pair = Pair(
            question=question,
            answer=answer,
            category=str(raw.get("category") or "")[:64],
            confidence=round(min(1.0, max(0.0, confidence)), 4),
        )
        evidence: dict[str, Any] = {
            "session_id": None,
            "chat_id": chat_id,
            "group_name": group_name,
            "seen_at": now.isoformat(),
            "question": question,
            "lines": [],
        }
        await record_pair(
            ctx,
            session,
            tenant_id,
            pair,
            evidence,
            now=now,
            model=ZONE_MODEL,
            auto_merge=auto_merge,
            source=CandidateSource.ZONE,
        )


async def latest_analysis(
    session: AsyncSession, chat_ids: set[str]
) -> dict[str, dict[str, str | None]]:
    """各客户群最近一次的摘要和情绪（客户 360 视图、侧边栏显示）。"""
    if not chat_ids:
        return {}
    rows = (
        await session.execute(
            select(WecomZoneResult.chat_id, WecomZoneResult.kind, WecomZoneResult.payload)
            .where(
                WecomZoneResult.chat_id.in_(chat_ids),
                WecomZoneResult.kind.in_([ZoneResultKind.SUMMARY, ZoneResultKind.SENTIMENT]),
            )
            .order_by(WecomZoneResult.created_at.desc())
        )
    ).all()
    # 结果按时间倒序：每个群每种结果取最新的一条。
    result: dict[str, dict[str, str | None]] = {}
    for chat_id, kind, payload in rows:
        entry = result.setdefault(chat_id, {"summary": None, "sentiment": None})
        data = payload or {}
        if kind == ZoneResultKind.SUMMARY and entry["summary"] is None:
            entry["summary"] = str(data.get("text") or "") or None
        elif kind == ZoneResultKind.SENTIMENT and entry["sentiment"] is None:
            entry["sentiment"] = str(data.get("label") or "") or None
    return result
