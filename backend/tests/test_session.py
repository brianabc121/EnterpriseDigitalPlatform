"""ORM 会话与租户上下文：绑定租户后，commit 之后的新事务仍然受 RLS 约束。"""

from fastapi import FastAPI
from sqlalchemy import select

from app.db.session import Database
from app.modules.customer.models import Customer
from app.modules.iam.models import Staff
from tests.support import TwoTenants


async def test_bound_session_stays_scoped_after_commit(
    app: FastAPI, two_tenants: TwoTenants
) -> None:
    db: Database = app.state.db
    async with db.tenant_session(two_tenants.tenant_a) as session:
        assert (await session.scalars(select(Staff.username))).all() == ["alice"]

        session.add(Customer(tenant_id=two_tenants.tenant_a, display_name="C1"))
        await session.commit()

        assert (await session.scalars(select(Staff.username))).all() == ["alice"]
        assert (await session.scalars(select(Customer.display_name))).all() == ["C1"]


async def test_unbound_app_session_sees_nothing(app: FastAPI, two_tenants: TwoTenants) -> None:
    db: Database = app.state.db
    async with db.app_sessionmaker() as session:
        assert (await session.scalars(select(Staff))).all() == []
