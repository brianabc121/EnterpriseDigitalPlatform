"""Add finance/cashier system roles and retire knowledge_manager."""

from alembic import op

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE staff_roles NO FORCE ROW LEVEL SECURITY")
    # 不覆盖企业已有的同代码自定义角色；冲突时整笔迁移回滚。
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM roles WHERE code IN ('finance', 'cashier') AND NOT is_system)
          THEN RAISE EXCEPTION 'Custom finance/cashier role exists; rename its code before upgrade';
          END IF;
        END $$
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
    raise RuntimeError("0039 changes role assignments; restore a pre-migration backup to revert")
