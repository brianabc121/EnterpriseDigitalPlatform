"""record history (design §25.14): every create, change and delete of orders, material
requisitions, production receipts, to-dos, finished goods and materials keeps a version with the
full content after the operation (append-only); existing order revisions are carried over

Revision ID: 0024
Revises: 0023
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TYPES = ("order", "requisition", "receipt", "todo", "goods", "material")

TABLE = f"""
CREATE TABLE record_versions (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
  record_type varchar(16) NOT NULL,
  record_id uuid NOT NULL,
  seq integer NOT NULL,
  action varchar(24) NOT NULL,
  actions text[] NOT NULL DEFAULT '{{}}',
  actor_type varchar(8) NOT NULL,
  actor_id uuid,
  label varchar(160) NOT NULL DEFAULT '',
  reason varchar(200),
  note text,
  snapshot jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
  CONSTRAINT uq_record_versions_seq UNIQUE (tenant_id, record_type, record_id, seq),
  CONSTRAINT ck_record_versions_type CHECK (record_type IN ({", ".join(f"'{t}'" for t in TYPES)}))
)
"""

# 订单原来的修改记录（order_revisions，只追加）并入修改历史：版本号、操作人、原因和完整内容不变。
COPY_ORDER_REVISIONS = """
INSERT INTO record_versions (id, tenant_id, record_type, record_id, seq, action, actions,
                             actor_type, actor_id, label, reason, note, snapshot, created_at)
SELECT gen_random_uuid(), r.tenant_id, 'order', r.order_id, r.version, a.action, ARRAY[a.action],
       r.actor_type, r.actor_id, o.no, r.reason, r.note, r.snapshot, r.created_at
FROM order_revisions r
JOIN orders o ON o.tenant_id = r.tenant_id AND o.id = r.order_id
CROSS JOIN LATERAL (
  SELECT CASE r.kind WHEN 'created' THEN 'create' WHEN 'edit' THEN 'update'
         ELSE r.kind END AS action
) a
"""


def _rls(table: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY tenant_isolation ON {table}
          USING (tenant_id = app_current_tenant())
          WITH CHECK (tenant_id = app_current_tenant())
        """,
        f"CREATE POLICY platform_access ON {table} TO edp_platform USING (true) WITH CHECK (true)",
    ]


def upgrade() -> None:
    op.execute(TABLE)
    op.execute(
        "CREATE INDEX ix_record_versions_recent ON record_versions"
        " (tenant_id, created_at DESC, id DESC)"
    )
    op.execute(
        "CREATE INDEX ix_record_versions_actor ON record_versions"
        " (tenant_id, actor_id, created_at DESC) WHERE actor_id IS NOT NULL"
    )
    # 只追加：员工端不能修改版本；删除和清除快照里的收货信息只用于客户数据的隐私删除（运营后台
    # 注销租户时整体清除）。
    op.execute("GRANT SELECT, INSERT, DELETE ON record_versions TO edp_app")
    op.execute("GRANT UPDATE (snapshot) ON record_versions TO edp_app")
    op.execute("GRANT SELECT, INSERT, DELETE ON record_versions TO edp_platform")
    # 迁移以表的所有者执行、没有租户上下文：强制行级安全下读不到订单，复制时先取消强制；
    # 新表在复制完数据之后才启用行级安全。
    for table in ("orders", "order_revisions"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(COPY_ORDER_REVISIONS)
    for table in ("orders", "order_revisions"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    for statement in _rls("record_versions"):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE record_versions")
