"""profit report (design §30): income and expense entries, orders by confirmation date

Revision ID: 0032
Revises: 0031
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("profit_entries",)

STATEMENTS = [
    # 收支登记：订单以外的费用（支出）和其他收入，按发生日期计入盈利报表。
    """
    CREATE TABLE profit_entries (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      kind varchar(8) NOT NULL,
      category varchar(20) NOT NULL,
      amount numeric(12, 2) NOT NULL,
      occurred_on date NOT NULL,
      note text NOT NULL DEFAULT '',
      recurring boolean NOT NULL DEFAULT false,
      copied_from uuid,
      created_by uuid,
      updated_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_profit_entries_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT ck_profit_entries_kind CHECK (kind IN ('expense', 'income')),
      CONSTRAINT ck_profit_entries_amount CHECK (amount > 0),
      CONSTRAINT ck_profit_entries_category CHECK (char_length(category) BETWEEN 1 AND 20)
    )
    """,
    "CREATE INDEX ix_profit_entries_day ON profit_entries (tenant_id, occurred_on)",
    # 每月固定的收支：同一笔只会被复制到下个月一次。
    """
    CREATE UNIQUE INDEX uq_profit_entries_copied_from ON profit_entries (tenant_id, copied_from)
      WHERE copied_from IS NOT NULL
    """,
    # 盈利报表按确认日期取期间内的订单。
    """
    CREATE INDEX ix_orders_confirmed ON orders (tenant_id, confirmed_at)
      WHERE confirmed_at IS NOT NULL
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


def downgrade() -> None:
    op.execute("DROP INDEX ix_orders_confirmed")
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE {table}")
