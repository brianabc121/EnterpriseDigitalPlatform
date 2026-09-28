import os
import uuid
from dataclasses import dataclass

import asyncpg
from sqlalchemy.engine import URL, make_url

SUPERUSER_URL = os.environ.get(
    "EDP_TEST_PG_SUPERUSER_URL", "postgresql://postgres:postgres@localhost:5432/postgres"
)
REDIS_URL = os.environ.get("EDP_TEST_REDIS_URL", "redis://localhost:6379/15")
ROLE_PASSWORDS = {"edp_app": "edp_app", "edp_platform": "edp_platform"}


def _render(url: URL) -> str:
    return url.render_as_string(hide_password=False)


class DatabaseUrls:
    """同一个测试数据库的几种连接方式：SQLAlchemy URL（+asyncpg）和 asyncpg DSN。"""

    def __init__(self, dbname: str) -> None:
        base = make_url(SUPERUSER_URL).set(database=dbname)
        self.owner = _render(base.set(drivername="postgresql+asyncpg"))
        self.owner_dsn = _render(base.set(drivername="postgresql"))
        self.app = _render(self._role(base, "edp_app").set(drivername="postgresql+asyncpg"))
        self.app_dsn = _render(self._role(base, "edp_app").set(drivername="postgresql"))
        self.platform = _render(
            self._role(base, "edp_platform").set(drivername="postgresql+asyncpg")
        )
        self.platform_dsn = _render(self._role(base, "edp_platform").set(drivername="postgresql"))

    @staticmethod
    def _role(base: URL, role: str) -> URL:
        return base.set(username=role, password=ROLE_PASSWORDS[role])


@dataclass(frozen=True)
class TwoTenants:
    tenant_a: uuid.UUID
    tenant_b: uuid.UUID
    staff_a: uuid.UUID
    staff_b: uuid.UUID


async def seed_two_tenants(platform_dsn: str) -> TwoTenants:
    """直接用平台角色写入两个租户、各一名员工（绕过应用层，只用于数据库层面的测试）。"""
    ids = TwoTenants(uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4())
    conn = await asyncpg.connect(platform_dsn)
    try:
        await conn.execute(
            "INSERT INTO tenants (id, code, name) "
            "VALUES ($1, 'tenant-a', 'A'), ($2, 'tenant-b', 'B')",
            ids.tenant_a,
            ids.tenant_b,
        )
        await conn.execute(
            "INSERT INTO staff (id, tenant_id, username, display_name, password_hash) "
            "VALUES ($1, $2, 'alice', 'Alice', 'x'), ($3, $4, 'bob', 'Bob', 'x')",
            ids.staff_a,
            ids.tenant_a,
            ids.staff_b,
            ids.tenant_b,
        )
    finally:
        await conn.close()
    return ids
