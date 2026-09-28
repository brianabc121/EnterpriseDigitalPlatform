from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import get_app_settings, get_database
from app.core.errors import Unauthorized
from app.core.security import PlatformClaims, TokenError, decode_platform_token
from app.db.session import Database
from app.modules.tenancy.models import PlatformUser, PlatformUserStatus

platform_bearer = HTTPBearer(
    auto_error=False, scheme_name="PlatformBearer", description="平台运营 Access Token"
)


async def get_platform_claims(
    settings: Annotated[Settings, Depends(get_app_settings)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(platform_bearer)],
) -> PlatformClaims:
    if credentials is None:
        raise Unauthorized("未登录")
    try:
        return decode_platform_token(
            credentials.credentials, secret=settings.platform_jwt_secret.get_secret_value()
        )
    except TokenError as exc:
        raise Unauthorized("登录已失效，请重新登录") from exc


async def get_platform_db(
    db: Annotated[Database, Depends(get_database)],
) -> AsyncIterator[AsyncSession]:
    async with db.platform_sessionmaker() as session:
        yield session


PlatformDb = Annotated[AsyncSession, Depends(get_platform_db, scope="function")]


async def get_current_platform_user(
    claims: Annotated[PlatformClaims, Depends(get_platform_claims)], session: PlatformDb
) -> PlatformUser:
    user = await session.get(PlatformUser, claims.user_id)
    if user is None or user.status != PlatformUserStatus.ACTIVE:
        raise Unauthorized("登录已失效，请重新登录")
    return user


CurrentPlatformUser = Annotated[PlatformUser, Depends(get_current_platform_user)]
