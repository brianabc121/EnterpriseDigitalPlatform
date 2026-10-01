"""传输加密的会话（设计文档 §25.15）：会话密钥存在 Redis 里，到期自动删除；随机数用过一次就记下，
10 分钟内再出现的是重放。

Redis 不可用时加密的请求无法处理，返回 503（不退回成明文）。
"""

import base64

from redis.asyncio import Redis

from app.modules.transport.crypto import SessionKeys

NONCE_TTL_SECONDS = 600

# 一次往返：取会话密钥，同时登记随机数（已经登记过的是重放）。
# 返回 {0} 会话不存在；{1, 密钥, 0} 重放；{1, 密钥, 1} 正常。
_USE_SCRIPT = """
local keys = redis.call('GET', KEYS[1])
if not keys then return {0} end
local fresh = redis.call('SET', KEYS[2], '1', 'NX', 'EX', ARGV[1])
if not fresh then return {1, keys, 0} end
return {1, keys, 1}
"""


class SessionStore:
    def __init__(self, redis: Redis, *, prefix: str = "edp:transport") -> None:
        self._redis = redis
        self._prefix = prefix
        self._use = redis.register_script(_USE_SCRIPT)

    def _session_key(self, session: str) -> str:
        return f"{self._prefix}:s:{session}"

    async def save(self, session: str, keys: SessionKeys, ttl_seconds: int) -> None:
        value = base64.b64encode(keys.request + keys.response)
        await self._redis.set(self._session_key(session), value, ex=ttl_seconds)

    async def use(self, session: str, nonce: str) -> tuple[SessionKeys | None, bool]:
        """取会话密钥并登记随机数：(密钥或 None, 是不是第一次出现)。"""
        found = await self._use(
            keys=[self._session_key(session), f"{self._prefix}:n:{session}:{nonce}"],
            args=[NONCE_TTL_SECONDS],
        )
        if not found or int(found[0]) == 0:
            return None, False
        raw = base64.b64decode(found[1])
        return SessionKeys(request=raw[:32], response=raw[32:]), int(found[2]) == 1
