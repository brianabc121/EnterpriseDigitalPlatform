"""基于 Redis 的固定窗口限流。

Redis 不可用时放行并记录日志：限流是防护措施，不应该让登录和访客接入随 Redis 一起不可用。
"""

import logging
import math
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.errors import TooManyRequests, Unauthorized

logger = logging.getLogger(__name__)

# 原子地计数，第一次计数时设置窗口过期时间；返回 {计数, 剩余毫秒}。
_HIT_SCRIPT = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('PEXPIRE', KEYS[1], ARGV[1]) end
return {n, redis.call('PTTL', KEYS[1])}
"""


@dataclass(frozen=True)
class Limit:
    name: str
    limit: int
    window_seconds: int


# 员工和平台账号登录：每个 IP 每分钟 30 次；同一账号 15 分钟内失败 10 次后锁定到窗口结束。
LOGIN_PER_IP = Limit("login-ip", 30, 60)
LOGIN_FAILURES = Limit("login-fail", 10, 15 * 60)
# 访客初始化会创建客户、IM 用户和服务群，按 IP 限制。
VISITOR_INIT_PER_IP = Limit("visitor-init-ip", 30, 60)
# 企业自助注册：每个 IP 每小时 5 次。
SIGNUP_PER_IP = Limit("signup-ip", 5, 3600)


@dataclass(frozen=True)
class Usage:
    count: int
    retry_after: int


class RateLimiter:
    def __init__(self, redis: Redis, *, prefix: str = "rl") -> None:
        self._redis = redis
        self._prefix = prefix
        self._hit = redis.register_script(_HIT_SCRIPT)

    def _key(self, rule: Limit, subject: str) -> str:
        return f"{self._prefix}:{rule.name}:{subject}"

    async def hit(self, rule: Limit, subject: str) -> Usage:
        """计数一次（不判断是否超限）。"""
        try:
            count, ttl_ms = await self._hit(
                keys=[self._key(rule, subject)], args=[rule.window_seconds * 1000]
            )
        except RedisError:
            logger.warning("rate limiter unavailable, allowing %s", rule.name, exc_info=True)
            return Usage(0, 0)
        return Usage(int(count), max(1, math.ceil(int(ttl_ms) / 1000)))

    async def check(self, rule: Limit, subject: str) -> None:
        """计数一次；超过上限时拒绝。"""
        usage = await self.hit(rule, subject)
        if usage.count > rule.limit:
            raise TooManyRequests("请求过于频繁，请稍后再试", retry_after=usage.retry_after)

    async def peek(self, rule: Limit, subject: str) -> Usage:
        key = self._key(rule, subject)
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.get(key)
                pipe.pttl(key)
                count, ttl_ms = await pipe.execute()
        except RedisError:
            logger.warning("rate limiter unavailable, allowing %s", rule.name, exc_info=True)
            return Usage(0, 0)
        return Usage(int(count or 0), max(1, math.ceil(max(int(ttl_ms), 0) / 1000)))

    async def reset(self, rule: Limit, subject: str) -> None:
        try:
            await self._redis.delete(self._key(rule, subject))
        except RedisError:
            logger.warning("rate limiter unavailable, cannot reset %s", rule.name, exc_info=True)


@asynccontextmanager
async def login_attempt(
    limiter: RateLimiter, *, ip: str | None, account: str
) -> AsyncIterator[None]:
    """包住一次登录校验：按 IP 限制尝试次数，按账号累计失败次数，成功后清零。"""
    if ip:
        await limiter.check(LOGIN_PER_IP, ip)
    failures = await limiter.peek(LOGIN_FAILURES, account)
    if failures.count >= LOGIN_FAILURES.limit:
        minutes = math.ceil(failures.retry_after / 60)
        raise TooManyRequests(
            f"登录失败次数过多，请 {minutes} 分钟后再试", retry_after=failures.retry_after
        )
    try:
        yield
    except Unauthorized:
        await limiter.hit(LOGIN_FAILURES, account)
        raise
    await limiter.reset(LOGIN_FAILURES, account)
