"""企业 token 计费（设计文档 §37）：按月汇总企业 AI 用掉的 tokens 和费用，近 7 天的逐次明细
和导出。

数据来自大模型网关的记账（llm_calls）：每次调用记场景、模型、输入和输出 tokens、按价格算好的
费用（分）、触发的员工和关联的会话。月份、每天按企业时区（工作时间设置里的时区）划分。
"""

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import Date, Select, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.context import AppContext
from app.core.csvfile import BOM, line
from app.core.errors import Unprocessable
from app.modules.ai.llm_router import SCENES
from app.modules.ai.models import AiSettings, LlmCall
from app.modules.iam.models import Staff
from app.modules.iam.principal import Principal
from app.modules.profit.report import tenant_zone
from app.modules.tokens.schemas import (
    TokenAmounts,
    TokenCall,
    TokenCallPage,
    TokenDay,
    TokenGroup,
    TokenOption,
    TokenStaff,
    TokenSummary,
)

DETAIL_DAYS = 7
EXPORT_BATCH = 500
SYSTEM = "system"
# 企业自带接口密钥的调用记的供应商名（见 ai/llm_router.py 的 byo_endpoint）。
OWN_KEY_PROVIDER = "tenant"
# 网关记账用的场景名称：AI 接待等可以选择模型的场景（SCENES），加上向量化、检索等不选模型的场景。
SCENE_LABELS: dict[str, str] = {
    **SCENES,
    "embed": "知识向量化",
    "search": "知识检索",
    "rerank": "检索重排序",
    "product_search": "商品检索",
    "product_embed": "商品向量化",
    "todo_merge": "待办去重",
}
STATUS_LABELS = {"ok": "成功", "busy": "并发已满", "error": "失败"}


def scene_label(scene: str) -> str:
    return SCENE_LABELS.get(scene, scene)


def _now() -> datetime:
    return datetime.now(UTC)


def _measures() -> tuple[ColumnElement[Any], ...]:
    return (
        func.count(),
        func.count().filter(LlmCall.status != "ok"),
        func.coalesce(func.sum(LlmCall.prompt_tokens), 0),
        func.coalesce(func.sum(LlmCall.completion_tokens), 0),
        func.coalesce(func.sum(LlmCall.cost), 0.0),
    )


def _amounts(calls: Any, failed: Any, prompt: Any, completion: Any, cost: Any) -> dict[str, Any]:
    prompt, completion = int(prompt or 0), int(completion or 0)
    return {
        "calls": int(calls or 0),
        "failed": int(failed or 0),
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "tokens": prompt + completion,
        "cost": round(float(cost or 0), 4),
    }


# ---- 月份 ----


@dataclass(frozen=True)
class Month:
    """一个月在企业时区里的范围；本月只算到现在。"""

    first: date
    last: date  # 统计到的最后一天（本月是今天）
    start: datetime
    end: datetime
    previous_start: datetime
    previous_end: datetime

    @property
    def key(self) -> str:
        return f"{self.first:%Y-%m}"


def _first_of_next(day: date) -> date:
    return date(day.year + (day.month == 12), day.month % 12 + 1, 1)


def _first_of_previous(day: date) -> date:
    return date(day.year - (day.month == 1), (day.month - 2) % 12 + 1, 1)


def month_range(month: str | None, tz: ZoneInfo, now: datetime) -> Month:
    today = now.astimezone(tz).date()
    if month:
        try:
            first = date(int(month[:4]), int(month[5:7]), 1)
        except ValueError as exc:
            raise Unprocessable("月份格式是 YYYY-MM") from exc
    else:
        first = today.replace(day=1)
    current = first == today.replace(day=1)
    if first > today:
        raise Unprocessable("不能查看以后的月份")
    start = datetime.combine(first, time(), tz)
    following = _first_of_next(first)
    previous_start = datetime.combine(_first_of_previous(first), time(), tz)
    if current:
        # 和上个月的同一时段比较；上个月短的时候不超过上个月月底。
        end = now
        previous_end = min(previous_start + (now - start), start)
        last = today
    else:
        end = datetime.combine(following, time(), tz)
        previous_end = start
        last = following - timedelta(days=1)
    return Month(first, last, start, end, previous_start, previous_end)


# ---- 汇总 ----


async def own_key(session: AsyncSession) -> bool:
    """现在是否用企业自己的大模型接口密钥（AI 设置里的 byo_llm）。"""
    byo = await session.scalar(select(AiSettings.byo_llm))
    return bool(byo and byo.get("enabled", True) and byo.get("base_url"))


def _in(start: datetime, end: datetime) -> list[ColumnElement[bool]]:
    return [LlmCall.created_at >= start, LlmCall.created_at < end]


async def _staff_names(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = await session.execute(select(Staff.id, Staff.display_name).where(Staff.id.in_(ids)))
    return {row.id: row.display_name for row in rows}


def _sorted(groups: list[TokenGroup]) -> list[TokenGroup]:
    return sorted(groups, key=lambda g: (-g.tokens, -g.calls, g.label))


async def summary(
    session: AsyncSession, principal: Principal, month: str | None, now: datetime | None = None
) -> TokenSummary:
    now = now or _now()
    tz = await tenant_zone(session)
    period = month_range(month, tz, now)
    current = _in(period.start, period.end)

    totals = (await session.execute(select(*_measures()).where(*current))).one()
    previous = (
        await session.execute(
            select(*_measures()).where(*_in(period.previous_start, period.previous_end))
        )
    ).one()

    local_day = cast(func.timezone(tz.key, LlmCall.created_at), Date)
    by_day = {
        row[0]: row[1:]
        for row in await session.execute(
            select(local_day, *_measures()).where(*current).group_by(local_day)
        )
    }
    days: list[TokenDay] = []
    day = period.first
    while day <= period.last:
        days.append(TokenDay(day=day, **_amounts(*by_day.get(day, (0, 0, 0, 0, 0)))))
        day += timedelta(days=1)

    scenes = await session.execute(
        select(LlmCall.scene, *_measures()).where(*current).group_by(LlmCall.scene)
    )
    by_scene = [
        TokenGroup(key=row[0], label=scene_label(row[0]), **_amounts(*row[1:])) for row in scenes
    ]

    models = await session.execute(
        select(LlmCall.provider, LlmCall.model, *_measures())
        .where(*current)
        .group_by(LlmCall.provider, LlmCall.model)
    )
    by_model = [
        TokenGroup(
            key=f"{row[0]}/{row[1]}",
            label=f"{row[1]}（{'自带密钥' if row[0] == OWN_KEY_PROVIDER else row[0]}）",
            **_amounts(*row[2:]),
        )
        for row in models
    ]

    staff_rows = (
        await session.execute(
            select(LlmCall.staff_id, *_measures()).where(*current).group_by(LlmCall.staff_id)
        )
    ).all()
    names = await _staff_names(session, {row[0] for row in staff_rows if row[0] is not None})
    by_staff = [
        TokenGroup(
            key=str(row[0]) if row[0] else SYSTEM,
            label=(names.get(row[0], "已删除的员工") if row[0] else "系统（AI 接待、定时任务）"),
            **_amounts(*row[1:]),
        )
        for row in staff_rows
    ]

    return TokenSummary(
        month=period.key,
        today=now.astimezone(tz).date(),
        timezone=tz.key,
        own_key=await own_key(session),
        totals=TokenAmounts(**_amounts(*totals)),
        previous=TokenAmounts(**_amounts(*previous)),
        days=days,
        by_scene=_sorted(by_scene),
        by_model=_sorted(by_model),
        by_staff=_sorted(by_staff),
    )


# ---- 近 7 天明细 ----


@dataclass(frozen=True)
class CallFilters:
    scene: str | None = None
    status: str | None = None
    staff_id: str | None = None


def _conditions(filters: CallFilters, since: datetime) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = [LlmCall.created_at >= since]
    if filters.scene:
        conditions.append(LlmCall.scene == filters.scene)
    if filters.status == "ok":
        conditions.append(LlmCall.status == "ok")
    elif filters.status == "failed":
        conditions.append(LlmCall.status != "ok")
    if filters.staff_id == SYSTEM:
        conditions.append(LlmCall.staff_id.is_(None))
    elif filters.staff_id:
        try:
            conditions.append(LlmCall.staff_id == uuid.UUID(filters.staff_id))
        except ValueError as exc:
            raise Unprocessable("员工 ID 不正确") from exc
    return conditions


def _rows(conditions: list[ColumnElement[bool]]) -> Select[LlmCall, str]:
    return (
        select(LlmCall, Staff.display_name)
        .outerjoin(Staff, Staff.id == LlmCall.staff_id)
        .where(*conditions)
        .order_by(LlmCall.created_at.desc(), LlmCall.id.desc())
    )


def call_out(call: LlmCall, staff_name: str | None) -> TokenCall:
    return TokenCall(
        id=call.id,
        created_at=call.created_at,
        scene=call.scene,
        scene_label=scene_label(call.scene),
        provider=call.provider,
        model=call.model,
        prompt_tokens=call.prompt_tokens,
        completion_tokens=call.completion_tokens,
        tokens=call.prompt_tokens + call.completion_tokens,
        cost=round(call.cost, 4),
        latency_ms=call.latency_ms,
        status=call.status,
        error=call.error,
        staff_id=call.staff_id,
        staff_name=staff_name,
        session_id=call.session_id,
        own_key=call.provider == OWN_KEY_PROVIDER,
    )


async def calls(
    session: AsyncSession,
    filters: CallFilters,
    *,
    limit: int = 50,
    offset: int = 0,
    now: datetime | None = None,
) -> TokenCallPage:
    since = (now or _now()) - timedelta(days=DETAIL_DAYS)
    conditions = _conditions(filters, since)
    total, tokens, cost = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens), 0),
                func.coalesce(func.sum(LlmCall.cost), 0.0),
            ).where(*conditions)
        )
    ).one()
    rows = await session.execute(_rows(conditions).limit(limit).offset(offset))
    items = [call_out(call, name) for call, name in rows]

    recent = LlmCall.created_at >= since
    scene_keys = await session.scalars(select(LlmCall.scene).where(recent).distinct())
    scenes = sorted(
        (TokenOption(key=key, label=scene_label(key)) for key in scene_keys),
        key=lambda option: option.label,
    )
    staff_ids = set(await session.scalars(select(LlmCall.staff_id).where(recent).distinct()))
    names = await _staff_names(session, {i for i in staff_ids if i is not None})
    staff = sorted(
        (TokenStaff(id=str(i), name=name) for i, name in names.items()), key=lambda s: s.name
    )
    if None in staff_ids:
        staff.append(TokenStaff(id=SYSTEM, name="系统"))
    return TokenCallPage(
        items=items,
        total=int(total),
        tokens=int(tokens),
        cost=round(float(cost), 4),
        since=since,
        scenes=scenes,
        staff=staff,
    )


# ---- 导出 ----

HEADER = [
    "时间",
    "场景",
    "供应商",
    "模型",
    "输入 tokens",
    "输出 tokens",
    "合计 tokens",
    "费用（元）",
    "耗时（毫秒）",
    "状态",
    "原因",
    "员工",
    "会话",
]


def _values(item: TokenCall, tz: ZoneInfo) -> list[object]:
    return [
        item.created_at.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S"),
        item.scene_label,
        "自带密钥" if item.own_key else item.provider,
        item.model,
        item.prompt_tokens,
        item.completion_tokens,
        item.tokens,
        f"{item.cost / 100:.4f}",
        item.latency_ms,
        STATUS_LABELS.get(item.status, item.status),
        item.error or "",
        item.staff_name or ("系统" if item.staff_id is None else ""),
        str(item.session_id) if item.session_id else "",
    ]


async def export_rows(
    ctx: AppContext, principal: Principal, filters: CallFilters, now: datetime | None = None
) -> AsyncIterator[bytes]:
    """近 7 天符合筛选条件的调用，逐批生成 CSV（新的在前）。使用自己的数据库会话。"""
    since = (now or _now()) - timedelta(days=DETAIL_DAYS)
    yield (BOM + line(HEADER)).encode()
    async with ctx.db.tenant_session(principal.tenant_id) as session:
        tz = await tenant_zone(session)
        query = _rows(_conditions(filters, since))
        offset = 0
        while True:
            rows = (await session.execute(query.limit(EXPORT_BATCH).offset(offset))).all()
            if not rows:
                return
            yield "".join(line(_values(call_out(call, name), tz)) for call, name in rows).encode()
            if len(rows) < EXPORT_BATCH:
                return
            offset += EXPORT_BATCH


async def export_count(
    session: AsyncSession, filters: CallFilters, now: datetime | None = None
) -> int:
    since = (now or _now()) - timedelta(days=DETAIL_DAYS)
    return int(
        await session.scalar(
            select(func.count()).select_from(LlmCall).where(*_conditions(filters, since))
        )
        or 0
    )
