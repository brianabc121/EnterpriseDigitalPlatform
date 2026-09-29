"""usage and reports: daily usage per tenant, indexes for date-range reports

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    # 用量按日汇总（设计文档 §7.3）：调度进程定期重算当天和前一天，结果幂等覆盖。
    # day 按平台计费时区（EDP_USAGE_TIMEZONE，默认 Asia/Shanghai）划分。
    """
    CREATE TABLE usage_daily (
      tenant_id uuid NOT NULL REFERENCES tenants (id),
      day date NOT NULL,
      metric varchar(32) NOT NULL,
      value bigint NOT NULL DEFAULT 0,
      updated_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant_id, day, metric)
    )
    """,
    "CREATE INDEX ix_usage_daily_day ON usage_daily (day)",
    "GRANT SELECT ON usage_daily TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON usage_daily TO edp_platform",
    "ALTER TABLE usage_daily ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE usage_daily FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY tenant_isolation ON usage_daily
      USING (tenant_id = app_current_tenant())
      WITH CHECK (tenant_id = app_current_tenant())
    """,
    "CREATE POLICY platform_access ON usage_daily TO edp_platform USING (true) WITH CHECK (true)",
    # 报表与用量都按时间范围统计。
    "CREATE INDEX ix_messages_tenant_id_sent_at ON messages (tenant_id, sent_at)",
    "CREATE INDEX ix_sessions_tenant_id_created_at ON sessions (tenant_id, created_at)",
    "CREATE INDEX ix_customers_tenant_id_created_at ON customers (tenant_id, created_at)",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS usage_daily")
    op.execute("DROP INDEX IF EXISTS ix_messages_tenant_id_sent_at")
    op.execute("DROP INDEX IF EXISTS ix_sessions_tenant_id_created_at")
    op.execute("DROP INDEX IF EXISTS ix_customers_tenant_id_created_at")
