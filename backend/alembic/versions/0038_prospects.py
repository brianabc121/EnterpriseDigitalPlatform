"""Prospective customers (design §35): prospect records and follow-ups

Revision ID: 0038
Revises: 0037
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("customer_prospects", "prospect_followups")

STATEMENTS = [
    # 意向客户设置（prospects/settings.py 的 ProspectSettings）：AI 转入的方式、最低意向、
    # 默认跟进天数。
    "ALTER TABLE tenant_settings ADD COLUMN prospects jsonb NOT NULL DEFAULT '{}'",
    # 意向记录：一个客户同时最多一条待确认（AI 建议）或跟进中的。
    """
    CREATE TABLE customer_prospects (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      status varchar(12) NOT NULL DEFAULT 'active',
      level varchar(8) NOT NULL DEFAULT 'medium',
      interest text,
      concerns text,
      source varchar(8) NOT NULL DEFAULT 'staff',
      session_id uuid,
      follower_id uuid,
      next_follow_at date,
      last_followed_at timestamptz,
      follow_count integer NOT NULL DEFAULT 0,
      order_id uuid,
      lost_reason text,
      created_by uuid,
      closed_by uuid,
      closed_at timestamptz,
      -- 开始跟进（转入、重新跟进）的时间：之后确认的订单算成交，之后的会话算"又来咨询"。
      opened_at timestamptz NOT NULL DEFAULT now(),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      FOREIGN KEY (tenant_id, follower_id) REFERENCES staff (tenant_id, id)
        ON DELETE SET NULL (follower_id),
      FOREIGN KEY (tenant_id, order_id) REFERENCES orders (tenant_id, id)
        ON DELETE SET NULL (order_id),
      FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id)
        ON DELETE SET NULL (session_id),
      CONSTRAINT ck_customer_prospects_status
        CHECK (status IN ('suggested', 'active', 'won', 'lost', 'dismissed')),
      CONSTRAINT ck_customer_prospects_level CHECK (level IN ('high', 'medium', 'low')),
      CONSTRAINT ck_customer_prospects_source CHECK (source IN ('ai', 'staff'))
    )
    """,
    """
    CREATE UNIQUE INDEX uq_customer_prospects_open ON customer_prospects (tenant_id, customer_id)
      WHERE status IN ('suggested', 'active')
    """,
    """
    CREATE INDEX ix_customer_prospects_due ON customer_prospects (tenant_id, next_follow_at)
      WHERE status = 'active'
    """,
    """
    CREATE INDEX ix_customer_prospects_status
      ON customer_prospects (tenant_id, status, updated_at)
    """,
    "CREATE INDEX ix_customer_prospects_customer ON customer_prospects (tenant_id, customer_id)",
    "CREATE INDEX ix_customer_prospects_follower ON customer_prospects (tenant_id, follower_id)",
    "CREATE INDEX ix_customer_prospects_session ON customer_prospects (tenant_id, session_id)",
    # 跟进记录：staff_id 为空的是系统记的（客户又来咨询了）；同一会话只记一次。
    """
    CREATE TABLE prospect_followups (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      prospect_id uuid NOT NULL,
      method varchar(12) NOT NULL DEFAULT 'other',
      content text NOT NULL,
      next_follow_at date,
      staff_id uuid,
      session_id uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      FOREIGN KEY (tenant_id, prospect_id) REFERENCES customer_prospects (tenant_id, id)
        ON DELETE CASCADE,
      FOREIGN KEY (tenant_id, staff_id) REFERENCES staff (tenant_id, id)
        ON DELETE SET NULL (staff_id),
      FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id)
        ON DELETE SET NULL (session_id),
      CONSTRAINT ck_prospect_followups_method
        CHECK (method IN ('phone', 'wechat', 'chat', 'visit', 'other'))
    )
    """,
    """
    CREATE INDEX ix_prospect_followups_prospect
      ON prospect_followups (tenant_id, prospect_id, created_at DESC)
    """,
    """
    CREATE UNIQUE INDEX uq_prospect_followups_session
      ON prospect_followups (tenant_id, prospect_id, session_id) WHERE session_id IS NOT NULL
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    for table in TENANT_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edp_app")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edp_platform")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (tenant_id = app_current_tenant())
              WITH CHECK (tenant_id = app_current_tenant())
            """
        )
        op.execute(
            f"CREATE POLICY platform_access ON {table} TO edp_platform"
            " USING (true) WITH CHECK (true)"
        )
        # 增量更新索引（§33.9）。
        op.execute(f"SELECT edp_track_changes('{table}'::regclass)")


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE tenant_settings DROP COLUMN prospects")
