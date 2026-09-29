"""routing and collaboration: queue priority, intent routes, overflow, AI while queued,
session watchers (monitor and assist), customer transfer requests

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("session_watchers", "customer_transfer_requests")

STATEMENTS = [
    # 路由策略（设计文档 §11.3）：带这些标签的客户排在前面（VIP）；投诉、情绪激动优先；按意图分配到
    # 技能组；排队期间 AI 继续回答客户的其他问题。
    "ALTER TABLE routing_policies ADD COLUMN priority_tags text[] NOT NULL DEFAULT '{VIP}'",
    "ALTER TABLE routing_policies ADD COLUMN urgent_first boolean NOT NULL DEFAULT true",
    "ALTER TABLE routing_policies ADD COLUMN ai_while_queued boolean NOT NULL DEFAULT false",
    "ALTER TABLE routing_policies ADD COLUMN intent_routes jsonb NOT NULL DEFAULT '[]'",
    # 技能组溢出：排队超过这么久仍没有分配，改由备用技能组接待（0 表示不溢出）。
    "ALTER TABLE skill_groups ADD COLUMN overflow_group_id uuid",
    "ALTER TABLE skill_groups ADD COLUMN overflow_after_seconds integer NOT NULL DEFAULT 0",
    """
    ALTER TABLE skill_groups ADD CONSTRAINT fk_skill_groups_overflow
      FOREIGN KEY (tenant_id, overflow_group_id) REFERENCES skill_groups (tenant_id, id)
      ON DELETE SET NULL (overflow_group_id)
    """,
    """
    ALTER TABLE skill_groups ADD CONSTRAINT ck_skill_groups_overflow
      CHECK (overflow_after_seconds BETWEEN 0 AND 86400 AND overflow_group_id IS DISTINCT FROM id)
    """,
    # 会话：识别出的意图、溢出时间。
    "ALTER TABLE sessions ADD COLUMN intent varchar(32)",
    "ALTER TABLE sessions ADD COLUMN overflowed_at timestamptz",
    # 旁听（monitor，只看）与协助（assist，可以发言）：加入服务群的其他员工。
    """
    CREATE TABLE session_watchers (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      session_id uuid NOT NULL,
      staff_id uuid NOT NULL,
      role varchar(16) NOT NULL,
      invited_by uuid,
      joined_at timestamptz NOT NULL DEFAULT now(),
      left_at timestamptz,
      PRIMARY KEY (tenant_id, session_id, staff_id),
      FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id) ON DELETE CASCADE,
      FOREIGN KEY (tenant_id, staff_id) REFERENCES staff (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_session_watchers_role CHECK (role IN ('monitor', 'assist'))
    )
    """,
    "CREATE INDEX ix_session_watchers_staff ON session_watchers (tenant_id, staff_id)"
    " WHERE left_at IS NULL",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON session_watchers TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON session_watchers TO edp_platform",
    # 客户转移申请（设计文档 §14.1）：坐席申请，有分配权限的员工审批。
    """
    CREATE TABLE customer_transfer_requests (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      from_owner_id uuid,
      to_owner_id uuid,
      requested_by uuid,
      reason text,
      status varchar(16) NOT NULL DEFAULT 'pending',
      decided_by uuid,
      decided_at timestamptz,
      decision_note text,
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_customer_transfer_requests_status
        CHECK (status IN ('pending', 'approved', 'rejected', 'cancelled'))
    )
    """,
    "CREATE UNIQUE INDEX uq_customer_transfer_requests_pending"
    " ON customer_transfer_requests (tenant_id, customer_id) WHERE status = 'pending'",
    "CREATE INDEX ix_customer_transfer_requests_status"
    " ON customer_transfer_requests (tenant_id, status, created_at DESC)",
    "GRANT SELECT, INSERT, UPDATE ON customer_transfer_requests TO edp_app",
    # 审批通过的转移申请在归属历史里记为 request。
    "ALTER TABLE customer_owner_history DROP CONSTRAINT ck_customer_owner_history_reason",
    """
    ALTER TABLE customer_owner_history ADD CONSTRAINT ck_customer_owner_history_reason
      CHECK (reason IN ('session_transfer', 'manual', 'handover', 'wecom', 'request'))
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON customer_transfer_requests TO edp_platform",
]


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
    for statement in STATEMENTS:
        op.execute(statement)
    for table in TENANT_TABLES:
        for statement in _rls(table):
            op.execute(statement)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE customer_owner_history DROP CONSTRAINT ck_customer_owner_history_reason"
    )
    op.execute(
        "ALTER TABLE customer_owner_history ADD CONSTRAINT ck_customer_owner_history_reason"
        " CHECK (reason IN ('session_transfer', 'manual', 'handover', 'wecom'))"
    )
    op.execute("DROP TABLE IF EXISTS customer_transfer_requests, session_watchers")
    for column in ("overflowed_at", "intent"):
        op.execute(f"ALTER TABLE sessions DROP COLUMN {column}")
    op.execute("ALTER TABLE skill_groups DROP CONSTRAINT ck_skill_groups_overflow")
    op.execute("ALTER TABLE skill_groups DROP CONSTRAINT fk_skill_groups_overflow")
    for column in ("overflow_after_seconds", "overflow_group_id"):
        op.execute(f"ALTER TABLE skill_groups DROP COLUMN {column}")
    for column in ("intent_routes", "ai_while_queued", "urgent_first", "priority_tags"):
        op.execute(f"ALTER TABLE routing_policies DROP COLUMN {column}")
