from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import Settings

TENANT_INFO_KEY = "tenant_id"
_SET_TENANT_SQL = text("SELECT set_config('app.tenant_id', :tenant_id, true)")


class Database:
    """两个连接池：租户请求用 edp_app（受 RLS 约束），平台运营用 edp_platform。"""

    def __init__(self, settings: Settings) -> None:
        self.app_engine = create_async_engine(settings.database_url_app, pool_pre_ping=True)
        self.platform_engine = create_async_engine(
            settings.database_url_platform, pool_pre_ping=True
        )
        self.app_sessionmaker = async_sessionmaker(self.app_engine, expire_on_commit=False)
        self.platform_sessionmaker = async_sessionmaker(
            self.platform_engine, expire_on_commit=False
        )

    @asynccontextmanager
    async def tenant_session(self, tenant_id: UUID) -> AsyncIterator[AsyncSession]:
        async with self.app_sessionmaker() as session:
            await bind_tenant(session, tenant_id)
            yield session

    async def dispose(self) -> None:
        await self.app_engine.dispose()
        await self.platform_engine.dispose()


@event.listens_for(Session, "after_begin")
def _apply_tenant_context(session: Session, _tx: SessionTransaction, conn: Connection) -> None:
    # 租户上下文是事务级的（set_config 第三个参数为 true），所以每个新事务开始时都要重新设置，
    # 这样在同一个请求里 commit 之后的查询仍然受 RLS 约束。
    tenant_id = session.info.get(TENANT_INFO_KEY)
    if tenant_id is not None:
        conn.execute(_SET_TENANT_SQL, {"tenant_id": str(tenant_id)})


async def bind_tenant(session: AsyncSession, tenant_id: UUID) -> None:
    """把会话绑定到租户：对当前事务立即生效，之后的每个事务自动生效。"""
    session.info[TENANT_INFO_KEY] = tenant_id
    if session.in_transaction():
        await session.execute(_SET_TENANT_SQL, {"tenant_id": str(tenant_id)})


def current_tenant(session: AsyncSession) -> UUID:
    tenant_id: Any = session.info.get(TENANT_INFO_KEY)
    if not isinstance(tenant_id, UUID):
        raise RuntimeError("session is not bound to a tenant")
    return tenant_id
