"""transfers: session transfers and customer owner history

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("session_transfers", "customer_owner_history")

STATEMENTS = [
    # 会话转接（设计文档 §14.2）：转给坐席时等对方在 60 秒内接受；转给技能组或强制转接立即生效。
    """
    CREATE TABLE session_transfers (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      session_id uuid NOT NULL,
      from_staff_id uuid,
      to_staff_id uuid,
      to_group_id uuid,
      note text,
      forced boolean NOT NULL DEFAULT false,
      transfer_ownership boolean NOT NULL DEFAULT false,
      status varchar(16) NOT NULL DEFAULT 'pending',
      expires_at timestamptz,
      decided_at timestamptz,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_session_transfers_session FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_session_transfers_from FOREIGN KEY (tenant_id, from_staff_id)
        REFERENCES staff (tenant_id, id) ON DELETE SET NULL (from_staff_id),
      CONSTRAINT fk_session_transfers_to FOREIGN KEY (tenant_id, to_staff_id)
        REFERENCES staff (tenant_id, id) ON DELETE SET NULL (to_staff_id),
      CONSTRAINT fk_session_transfers_group FOREIGN KEY (tenant_id, to_group_id)
        REFERENCES skill_groups (tenant_id, id) ON DELETE SET NULL (to_group_id),
      CONSTRAINT ck_session_transfers_status CHECK (
        status IN ('pending', 'accepted', 'rejected', 'expired', 'cancelled', 'completed')
      )
    )
    """,
    # 每个会话同时最多一个待确认的转接。
    """
    CREATE UNIQUE INDEX uq_session_transfers_pending ON session_transfers (tenant_id, session_id)
      WHERE status = 'pending'
    """,
    """
    CREATE INDEX ix_session_transfers_pending_expiry ON session_transfers (expires_at)
      WHERE status = 'pending'
    """,
    # 客户归属变更记录：会话转接时勾选"同时转移归属"、管理员转移、离职交接。
    """
    CREATE TABLE customer_owner_history (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      from_owner_id uuid,
      to_owner_id uuid,
      actor_id uuid,
      reason varchar(32) NOT NULL,
      note text,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_customer_owner_history_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_customer_owner_history_reason CHECK (
        reason IN ('session_transfer', 'manual', 'handover')
      )
    )
    """,
    """
    CREATE INDEX ix_customer_owner_history_customer
      ON customer_owner_history (tenant_id, customer_id, created_at)
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON session_transfers TO edp_app, edp_platform",
    "GRANT SELECT, INSERT ON customer_owner_history TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON customer_owner_history TO edp_platform",
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
    for table in TABLES:
        for statement in _rls(table):
            op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS customer_owner_history, session_transfers")
