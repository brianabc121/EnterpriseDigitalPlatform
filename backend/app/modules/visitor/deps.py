from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.deps import get_app_settings, get_database
from app.core.errors import Unauthorized
from app.core.security import TokenError, VisitorClaims, decode_visitor_token
from app.db.session import Database

VISITOR_TOKEN_HEADER = "X-Visitor-Token"


async def get_visitor_claims(
    settings: Annotated[Settings, Depends(get_app_settings)],
    token: Annotated[
        str | None,
        Header(alias=VISITOR_TOKEN_HEADER, description="访客初始化返回的 visitor_token"),
    ] = None,
) -> VisitorClaims:
    if not token:
        raise Unauthorized("缺少访客令牌")
    try:
        return decode_visitor_token(token, secret=settings.visitor_jwt_secret.get_secret_value())
    except TokenError as exc:
        raise Unauthorized("访客令牌无效或已过期") from exc


@dataclass(frozen=True)
class VisitorContext:
    claims: VisitorClaims
    session: AsyncSession


async def get_visitor(
    db: Annotated[Database, Depends(get_database)],
    claims: Annotated[VisitorClaims, Depends(get_visitor_claims)],
) -> AsyncIterator[VisitorContext]:
    async with db.tenant_session(claims.tenant_id) as session:
        yield VisitorContext(claims, session)


CurrentVisitor = Annotated[VisitorContext, Depends(get_visitor, scope="function")]
