"""基于 Redis 的租约：保证某项工作同一时刻只有一个持有者（例如一个事件分区、调度进程）。

持有者需要在租约到期前续约；进程崩溃后租约自然过期，由其他实例接手。
"""

from redis.asyncio import Redis

# 键不存在时获取；持有者是自己时续约；否则失败。
_HOLD = """
local current = redis.call('GET', KEYS[1])
if not current then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
  return 1
end
if current == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], ARGV[2])
  return 1
end
return 0
"""

_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class Lease:
    def __init__(self, redis: Redis, key: str, owner: str, *, ttl_ms: int = 15_000) -> None:
        self.key = key
        self.owner = owner
        self.ttl_ms = ttl_ms
        self._hold = redis.register_script(_HOLD)
        self._release = redis.register_script(_RELEASE)

    async def hold(self) -> bool:
        """获取或续约；返回当前是否持有。"""
        return bool(await self._hold(keys=[self.key], args=[self.owner, self.ttl_ms]))

    async def release(self) -> None:
        await self._release(keys=[self.key], args=[self.owner])
