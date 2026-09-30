"""按租户限制同时进行的大模型调用（设计文档 §7：单租户并发上限，防止某个租户挤占资源）。

用 Redis 有序集合做分布式信号量：成员是一次调用的租约，分数是租约到期时间，进程中途退出时租约到期
自动释放。没有名额时每 0.2 秒重试，最多等 EDP_LLM_QUEUE_SECONDS 秒，仍拿不到就放弃这次调用
（AI 接待随即转人工）。Redis 不可用时不限制，不影响接待。

上限默认是 EDP_LLM_TENANT_CONCURRENCY，平台可以在运营后台按租户调整（ai_settings.llm_concurrency）。
"""

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.exceptions import RedisError
from sqlalchemy import select

from app.context import AppContext
from app.integrations.llm import LLMUnavailable
from app.modules.ai.models import AiSettings
from app.modules.tenancy import ratelimits

logger = logging.getLogger(__name__)

_ACQUIRE = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[2]) then
  redis.call('ZADD', KEYS[1], ARGV[3], ARGV[4])
  redis.call('PEXPIRE', KEYS[1], ARGV[5])
  return 1
end
return 0
"""
_RETRY_SECONDS = 0.2
_LIMIT_CACHE_SECONDS = 10.0
_limits: dict[uuid.UUID, tuple[float, int]] = {}


class LlmBusy(LLMUnavailable):
    """租户的大模型并发已满，排队超时。"""


def _key(tenant_id: uuid.UUID) -> str:
    return f"llm:slots:{tenant_id}"


async def tenant_limit(ctx: AppContext, tenant_id: uuid.UUID) -> int:
    cached = _limits.get(tenant_id)
    if cached is not None and time.monotonic() - cached[0] < _LIMIT_CACHE_SECONDS:
        return cached[1]
    async with ctx.db.tenant_session(tenant_id) as session:
        override = await session.scalar(
            select(AiSettings.llm_concurrency).where(AiSettings.tenant_id == tenant_id)
        )
    limit = override or ctx.settings.llm_tenant_concurrency
    _limits[tenant_id] = (time.monotonic(), limit)
    return limit


def forget_limit(tenant_id: uuid.UUID) -> None:
    """平台修改租户的并发上限后调用（其他进程在缓存到期后生效）。"""
    _limits.pop(tenant_id, None)


async def in_use(ctx: AppContext, tenant_id: uuid.UUID) -> int:
    """当前占用的名额（运营后台显示）。"""
    try:
        await ctx.redis.zremrangebyscore(_key(tenant_id), "-inf", int(time.time() * 1000))
        return int(await ctx.redis.zcard(_key(tenant_id)))
    except RedisError:
        return 0


@asynccontextmanager
async def slot(ctx: AppContext, tenant_id: uuid.UUID) -> AsyncIterator[None]:
    """占用一个名额执行一次调用。每分钟的调用次数也有上限（按租户限流）。"""
    if not await ratelimits.allow(ctx, tenant_id, ratelimits.Kind.LLM):
        raise LlmBusy("大模型调用过于频繁，稍后再试")
    limit = await tenant_limit(ctx, tenant_id)
    key = _key(tenant_id)
    token = uuid.uuid4().hex
    # 租约比一次调用（含重试）的最长耗时更长。
    lease_ms = int((ctx.settings.llm_timeout_seconds * 3 + 30) * 1000)
    deadline = time.monotonic() + ctx.settings.llm_queue_seconds
    acquired = False
    try:
        while True:
            now_ms = int(time.time() * 1000)
            granted = await ctx.redis.eval(
                _ACQUIRE, 1, key, now_ms, limit, now_ms + lease_ms, token, lease_ms
            )
            if granted:
                acquired = True
                break
            if time.monotonic() >= deadline:
                raise LlmBusy(f"大模型并发已满（上限 {limit}）")
            await asyncio.sleep(_RETRY_SECONDS)
    except RedisError:
        logger.warning("llm concurrency limiter unavailable, calling without a slot")
    try:
        yield
    finally:
        if acquired:
            try:
                await ctx.redis.zrem(key, token)
            except RedisError:
                logger.warning("failed to release llm slot %s", token)
