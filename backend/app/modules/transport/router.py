"""传输加密的握手（设计文档 §25.15）。这两个接口本身不加密。"""

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from redis.exceptions import RedisError

from app.core.config import Settings
from app.core.deps import client_ip, get_app_settings, get_rate_limiter
from app.core.errors import ERROR_RESPONSES, Conflict, ServiceUnavailable, Unprocessable
from app.core.ratelimit import Limit, RateLimiter
from app.modules.transport import crypto
from app.modules.transport.schemas import HandshakeIn, HandshakeOut, TransportKey
from app.modules.transport.sessions import SessionStore

router = APIRouter(prefix="/api/v1/transport", tags=["transport"], responses=ERROR_RESPONSES)

SettingsDep = Annotated[Settings, Depends(get_app_settings)]
Limiter = Annotated[RateLimiter, Depends(get_rate_limiter)]


def get_session_store(request: Request) -> SessionStore:
    store: SessionStore = request.app.state.transport_store
    return store


Store = Annotated[SessionStore, Depends(get_session_store)]


@router.get("/key", response_model=TransportKey)
async def transport_key(settings: SettingsDep) -> TransportKey:
    """握手签名用的服务器公钥。生产环境的前端在构建时写入公钥，不从这里获取。"""
    return TransportKey(
        key_id=crypto.key_id(settings),
        public_key=crypto.public_key_b64(settings),
        mode=settings.transport_mode,
    )


@router.post("/handshake", response_model=HandshakeOut)
async def handshake(
    payload: HandshakeIn, request: Request, settings: SettingsDep, limiter: Limiter, store: Store
) -> HandshakeOut:
    """浏览器的一次性公钥 → 会话号、服务器的一次性公钥和签名；会话密钥存在 Redis 里。
    关闭了传输加密时返回 409（浏览器改为不加密）。"""
    if settings.transport_mode == "off":
        raise Conflict("传输加密没有开启")
    rule = Limit("transport-handshake-ip", settings.transport_handshakes_per_minute, 60)
    await limiter.check(rule, client_ip(request) or "unknown")
    now = int(time.time())
    try:
        result, keys = crypto.handshake(settings, crypto.unb64url(payload.client_key), now=now)
    except crypto.SealError as exc:
        raise Unprocessable("公钥无效") from exc
    try:
        await store.save(result.session, keys, settings.transport_session_ttl_seconds)
    except RedisError as exc:
        raise ServiceUnavailable("加密服务暂时不可用，请稍后重试") from exc
    return HandshakeOut(
        session=result.session,
        server_key=crypto.b64url(result.server_key),
        expires_at=result.expires_at,
        server_time=now,
        signature=crypto.b64url(result.signature),
        key_id=crypto.key_id(settings),
    )
