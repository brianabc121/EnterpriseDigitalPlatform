"""按租户限流（设计文档 §7.1、§9.3 租户公平）：单个租户的突发流量不影响其他租户。

- 每个租户每分钟的员工接口请求（api）、访客接口请求（visitor）、OpenIM 回调（webhook）和大模型调用
  （llm）各有上限。默认取平台配置（EDP_TENANT_*_PER_MINUTE），平台可以按租户调整
  （tenants.settings.rate_limits），各进程缓存 30 秒；0 表示不限。
- 计数用 Redis 固定窗口，key 是 t:{租户}:rl:{类型}；Redis 不可用时放行。
- 员工、访客接口超过上限返回 429；OpenIM 回调超过上限时暂不入库，由调度进程按 seq 对账补上
  （最多晚一分钟）；大模型调用超过上限时本轮转人工（与并发已满相同）。
- 访客另有每人每分钟的上限（EDP_VISITOR_PER_MINUTE）。
"""

import time
import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy import select

from app.context import AppContext
from app.core.errors import TooManyRequests
from app.core.ratelimit import Limit, RateLimiter
from app.modules.tenancy.models import Tenant
from app.observability import metrics

SETTINGS_KEY = "rate_limits"
_WINDOW_SECONDS = 60
_CACHE_SECONDS = 30.0


class Kind(StrEnum):
    API = "api"
    VISITOR = "visitor"
    WEBHOOK = "webhook"
    LLM = "llm"


KINDS = tuple(Kind)
_MESSAGES = {
    Kind.API: "本企业的请求过于频繁，请稍后再试",
    Kind.VISITOR: "请求过于频繁，请稍后再试",
}

_overrides: dict[uuid.UUID, tuple[float, dict[str, int]]] = {}


def defaults(ctx: AppContext) -> dict[Kind, int]:
    s = ctx.settings
    return {
        Kind.API: s.tenant_api_per_minute,
        Kind.VISITOR: s.tenant_visitor_per_minute,
        Kind.WEBHOOK: s.tenant_webhook_per_minute,
        Kind.LLM: s.tenant_llm_per_minute,
    }


def parse_overrides(settings: dict[str, Any] | None) -> dict[str, int]:
    raw = (settings or {}).get(SETTINGS_KEY)
    if not isinstance(raw, dict):
        return {}
    return {k: int(v) for k, v in raw.items() if k in KINDS and isinstance(v, int) and v >= 0}


async def overrides(ctx: AppContext, tenant_id: uuid.UUID) -> dict[str, int]:
    cached = _overrides.get(tenant_id)
    if cached is not None and time.monotonic() - cached[0] < _CACHE_SECONDS:
        return cached[1]
    async with ctx.db.app_sessionmaker() as session:
        settings = await session.scalar(select(Tenant.settings).where(Tenant.id == tenant_id))
    values = parse_overrides(settings)
    _overrides[tenant_id] = (time.monotonic(), values)
    return values


def forget(tenant_id: uuid.UUID) -> None:
    """平台修改了租户的限额（其他进程在缓存到期后生效）。"""
    _overrides.pop(tenant_id, None)


async def limits(ctx: AppContext, tenant_id: uuid.UUID) -> dict[Kind, int]:
    custom = await overrides(ctx, tenant_id)
    return {kind: custom.get(kind, value) for kind, value in defaults(ctx).items()}


def _key(tenant_id: uuid.UUID, kind: Kind) -> str:
    return f"t:{tenant_id}:rl:{kind}"


def _limiter(ctx: AppContext) -> RateLimiter:
    return ctx.limiter or RateLimiter(ctx.redis)


async def allow(ctx: AppContext, tenant_id: uuid.UUID, kind: Kind) -> bool:
    """计数一次，返回是否在上限以内。"""
    limit = (await limits(ctx, tenant_id))[kind]
    if limit <= 0:
        return True
    usage = await _limiter(ctx).hit_key(_key(tenant_id, kind), _WINDOW_SECONDS)
    if usage.count > limit:
        metrics.RATE_LIMITED.labels(f"tenant-{kind}", metrics.tenant_label(tenant_id)).inc()
        return False
    return True


async def check(ctx: AppContext, tenant_id: uuid.UUID, kind: Kind) -> None:
    """计数一次；超过上限时拒绝（429）。"""
    limit = (await limits(ctx, tenant_id))[kind]
    if limit <= 0:
        return
    usage = await _limiter(ctx).hit_key(_key(tenant_id, kind), _WINDOW_SECONDS)
    if usage.count > limit:
        metrics.RATE_LIMITED.labels(f"tenant-{kind}", metrics.tenant_label(tenant_id)).inc()
        raise TooManyRequests(_MESSAGES.get(kind, "请求过于频繁"), retry_after=usage.retry_after)


async def usage(ctx: AppContext, tenant_id: uuid.UUID) -> dict[Kind, int]:
    """当前一分钟窗口内的计数（运营后台显示）。"""
    limiter = _limiter(ctx)
    return {kind: await limiter.count_key(_key(tenant_id, kind)) for kind in KINDS}


def visitor_limit(ctx: AppContext) -> Limit:
    return Limit("visitor", ctx.settings.visitor_per_minute, _WINDOW_SECONDS)


async def check_visitor(ctx: AppContext, tenant_id: uuid.UUID, visitor: str) -> None:
    """访客接口：先按访客本人，再按租户计数。"""
    rule = visitor_limit(ctx)
    if rule.limit > 0:
        hit = await _limiter(ctx).hit(rule, f"{tenant_id}:{visitor}")
        if hit.count > rule.limit:
            metrics.RATE_LIMITED.labels("visitor", metrics.tenant_label(tenant_id)).inc()
            raise TooManyRequests(_MESSAGES[Kind.VISITOR], retry_after=hit.retry_after)
    await check(ctx, tenant_id, Kind.VISITOR)
