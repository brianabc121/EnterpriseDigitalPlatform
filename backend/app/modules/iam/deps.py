from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import get_app_settings, get_database
from app.core.errors import Forbidden, Unauthorized
from app.core.security import AccessClaims, TokenError, decode_access_token
from app.db.session import Database
from app.modules.iam.principal import Principal
from app.modules.iam.service import load_principal
from app.observability.context import note_tenant

bearer_scheme = HTTPBearer(auto_error=False, description="员工 Access Token")


async def get_access_claims(
    settings: Annotated[Settings, Depends(get_app_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer_scheme)],
) -> AccessClaims:
    if credentials is None:
        raise Unauthorized("未登录")
    try:
        claims = decode_access_token(
            credentials.credentials, secret=settings.jwt_secret.get_secret_value()
        )
    except TokenError as exc:
        raise Unauthorized("登录已失效，请重新登录") from exc
    note_tenant(claims.tenant_id)
    return claims


async def get_tenant_db(
    db: Annotated[Database, Depends(get_database)],
    claims: Annotated[AccessClaims, Depends(get_access_claims)],
) -> AsyncIterator[AsyncSession]:
    async with db.tenant_session(claims.tenant_id) as session:
        yield session


# scope="function"：会话在路由函数返回后、响应发出前关闭，未提交的改动随之回滚。
TenantDb = Annotated[AsyncSession, Depends(get_tenant_db, scope="function")]


async def get_current_principal(
    claims: Annotated[AccessClaims, Depends(get_access_claims)], session: TenantDb
) -> Principal:
    principal = await load_principal(session, claims)
    if principal is None:
        raise Unauthorized("登录已失效，请重新登录")
    return principal


CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def require_permission(*permissions: str) -> Callable[..., Awaitable[Principal]]:
    async def dependency(principal: CurrentPrincipal) -> Principal:
        if not all(principal.has(p) for p in permissions):
            raise Forbidden("没有执行该操作的权限")
        return principal

    return dependency
