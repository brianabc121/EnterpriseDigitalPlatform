"""role-based consoles (design §25.15): the console a custom role belongs to, per-console menu
settings for the tenant, and the keeper system role for existing tenants

Revision ID: 0025
Revises: 0024
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSOLES = ("admin", "supervisor", "agent", "keeper", "worker", "knowledge")

STATEMENTS = [
    # 自定义角色选择的岗位；为空时按权限判断（系统角色的岗位以代码为准）。
    "ALTER TABLE roles ADD COLUMN console varchar(16)",
    f"""
    ALTER TABLE roles ADD CONSTRAINT ck_roles_console
      CHECK (console IS NULL OR console IN ({", ".join(f"'{c}'" for c in CONSOLES)}))
    """,
    # 除管理员以外每个岗位显示的菜单（没有调整的岗位用默认值）。
    "ALTER TABLE tenant_settings ADD COLUMN console jsonb NOT NULL DEFAULT '{}'",
]

# 已有租户补上"仓管"系统角色（新租户在开通时创建；权限以代码中的定义为准）。
KEEPER_ROLE = """
INSERT INTO roles (id, tenant_id, code, name, permissions, is_system)
SELECT gen_random_uuid(), t.id, 'keeper', '仓管', '{}', true FROM tenants t
WHERE NOT EXISTS (SELECT 1 FROM roles r WHERE r.tenant_id = t.id AND r.code = 'keeper')
"""


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    # 迁移以表的所有者执行、没有租户上下文：强制行级安全下所有者写不进角色表，先取消强制。
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute(KEEPER_ROLE)
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # 员工与角色的关联随角色一起删除（外键 ON DELETE CASCADE）。
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE staff_roles NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM roles WHERE code = 'keeper' AND is_system")
    op.execute("ALTER TABLE staff_roles FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_settings DROP COLUMN console")
    op.execute("ALTER TABLE roles DROP CONSTRAINT ck_roles_console")
    op.execute("ALTER TABLE roles DROP COLUMN console")
