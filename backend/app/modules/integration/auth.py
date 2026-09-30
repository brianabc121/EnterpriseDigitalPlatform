"""接口密钥（设计文档 §25.8）：企业系统调用开放接口时在请求头里带上

    Authorization: Bearer edp_<前缀>_<密钥>

平台只保存密钥的 SHA-256；前缀是明文的一部分，用来查找密钥和在界面上辨认。密钥按权限范围授权，
撤销后立即失效；租户停用、注销时一律拒绝。每个密钥、每个租户分别限流。
"""

import hashlib
import hmac
import secrets
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context import AppContext
from app.core.deps import get_context, get_rate_limiter
from app.core.errors import Forbidden, Unauthorized
from app.core.ratelimit import Limit, RateLimiter
from app.modules.integration.models import ApiKey
from app.modules.tenancy import ratelimits
from app.modules.tenancy.models import Tenant, TenantStatus
from app.observability.context import note_tenant

PREFIX = "edp_"
PER_KEY = Limit("open-api-key", 600, 60)
# last_used_at 最多每分钟写一次，减少写入。
TOUCH_EVERY = timedelta(minutes=1)
INVALID = "接口密钥无效或已撤销"

api_key_scheme = HTTPBearer(
    auto_error=False, scheme_name="ApiKey", description="企业系统的接口密钥（edp_ 开头）"
)


def new_key() -> tuple[str, str]:
    """生成一个新密钥：返回（明文，前缀）。明文只在创建时显示一次。"""
    prefix = secrets.token_hex(5)
    return f"{PREFIX}{prefix}_{secrets.token_urlsafe(24)}", prefix


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def prefix_of(key: str) -> str | None:
    if not key.startswith(PREFIX):
        return None
    prefix, sep, rest = key[len(PREFIX) :].partition("_")
    return prefix if sep and rest and len(prefix) == 10 else None


def display(prefix: str) -> str:
    return f"{PREFIX}{prefix}_••••"


@dataclass(frozen=True)
class ApiCaller:
    """调用开放接口的企业系统（一个接口密钥）。"""

    tenant_id: uuid.UUID
    key_id: uuid.UUID
    name: str
    scopes: frozenset[str]

    def has(self, scope: str) -> bool:
        return scope in self.scopes


async def get_api_caller(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(api_key_scheme)],
    limiter: Annotated[RateLimiter, Depends(get_rate_limiter)],
) -> ApiCaller:
    if credentials is None:
        raise Unauthorized("缺少接口密钥")
    key = credentials.credentials.strip()
    prefix = prefix_of(key)
    if prefix is None:
        raise Unauthorized(INVALID)
    ctx: AppContext = get_context(request)
    now = datetime.now(UTC)
    # 按前缀跨租户查找密钥（平台连接），校验哈希、租户状态后才进入租户的数据范围。
    async with ctx.db.platform_sessionmaker() as session:
        row = await session.scalar(select(ApiKey).where(ApiKey.prefix == prefix))
        if (
            row is None
            or row.revoked_at is not None
            or not hmac.compare_digest(row.key_hash, hash_key(key))
        ):
            raise Unauthorized(INVALID)
        tenant = await session.get(Tenant, row.tenant_id)
        if tenant is None or tenant.status != TenantStatus.ACTIVE:
            raise Forbidden("企业账号已停用")
        caller = ApiCaller(
            tenant_id=row.tenant_id, key_id=row.id, name=row.name, scopes=frozenset(row.scopes)
        )
        if row.last_used_at is None or now - row.last_used_at >= TOUCH_EVERY:
            row.last_used_at = now
            await session.commit()
    note_tenant(caller.tenant_id)
    await limiter.check(PER_KEY, str(caller.key_id))
    await ratelimits.check(ctx, caller.tenant_id, ratelimits.Kind.API)
    return caller


Caller = Annotated[ApiCaller, Depends(get_api_caller)]


def require_scope(scope: str) -> Callable[..., Awaitable[ApiCaller]]:
    async def dependency(caller: Caller) -> ApiCaller:
        if not caller.has(scope):
            raise Forbidden(f"接口密钥没有 {scope} 权限")
        return caller

    return dependency


async def get_api_db(request: Request, caller: Caller) -> AsyncIterator[AsyncSession]:
    async with get_context(request).db.tenant_session(caller.tenant_id) as session:
        yield session


# 与员工接口一样：会话在路由函数返回后、响应发出前关闭，未提交的改动随之回滚。
ApiDb = Annotated[AsyncSession, Depends(get_api_db, scope="function")]
