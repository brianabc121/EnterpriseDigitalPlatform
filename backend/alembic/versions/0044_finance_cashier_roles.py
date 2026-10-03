"""Finance and cashier system roles; retire the knowledge_manager system role

在 feat/staff-mind-map-roles 分支上原来编号 0039（见 0042 的说明），可以重复执行。

企业以前按"财务"岗位自己建的角色可能正好用了 finance、cashier 这两个代码（P17、P26 的
做法）：分支上的迁移遇到这种情况会中止；合并时改为把这些自定义角色的代码加上后缀（名称、
权限和员工不变），再给每个企业建系统角色。

Revision ID: 0044
Revises: 0043
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE staff_roles NO FORCE ROW LEVEL SECURITY")
    # 自定义的 finance、cashier 角色改用 finance_1a2b3c 这样的代码（角色代码最长 32 位）。
    op.execute("""
        UPDATE roles SET code = code || '_' || substr(replace(id::text, '-', ''), 1, 6)
        WHERE code IN ('finance', 'cashier') AND NOT is_system
    """)
    for code, name in (("finance", "财务"), ("cashier", "出纳")):
        op.execute(f"""
            INSERT INTO roles (id, tenant_id, code, name, permissions, is_system)
            SELECT gen_random_uuid(), t.id, '{code}', '{name}', '{{}}', true FROM tenants t
            WHERE NOT EXISTS (
              SELECT 1 FROM roles r WHERE r.tenant_id = t.id AND r.code = '{code}'
            )
        """)
    # staff_roles 的外键级联只移除角色关联，不删除员工。
    op.execute("DELETE FROM roles WHERE code = 'knowledge_manager' AND is_system")
    # 先执行当前事务中延迟的外键检查，否则 ALTER TABLE 会因 pending trigger events 失败。
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("ALTER TABLE staff_roles FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # 角色撤销已影响授权，无法从当前数据还原原知识管理员分配；避免伪装可逆。
    raise RuntimeError("0044 changes role assignments; restore a pre-migration backup to revert")
