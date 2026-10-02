"""cloud printers (design §29): printers and print jobs

Revision ID: 0031
Revises: 0030
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("printers", "print_jobs")

STATEMENTS = [
    # 打印机：厂商、开发者账号和加密的密钥、编号、用途、份数、状态。
    """
    CREATE TABLE printers (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      name varchar(64) NOT NULL,
      brand varchar(12) NOT NULL,
      account varchar(128) NOT NULL,
      key_enc text NOT NULL,
      sn varchar(64) NOT NULL,
      device_key varchar(64) NOT NULL DEFAULT '',
      uses text[] NOT NULL DEFAULT '{}',
      copies integer NOT NULL DEFAULT 1,
      enabled boolean NOT NULL DEFAULT true,
      status varchar(16) NOT NULL DEFAULT 'unknown',
      status_checked_at timestamptz,
      last_error text,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_printers_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_printers_sn UNIQUE (tenant_id, brand, sn),
      CONSTRAINT ck_printers_brand CHECK (brand IN ('xpyun', 'feie')),
      CONSTRAINT ck_printers_copies CHECK (copies BETWEEN 1 AND 3),
      CONSTRAINT ck_printers_status
        CHECK (status IN ('unknown', 'online', 'offline', 'abnormal', 'misconfigured'))
    )
    """,
    # 打印任务：一张小票的一次打印，排好的内容、第几次、谁打印的、发送和重试的状态。
    """
    CREATE TABLE print_jobs (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      printer_id uuid,
      printer_name varchar(64) NOT NULL DEFAULT '',
      kind varchar(12) NOT NULL,
      ref_id uuid,
      ref_no varchar(32) NOT NULL DEFAULT '',
      seq integer NOT NULL DEFAULT 1,
      copies integer NOT NULL DEFAULT 1,
      source varchar(12) NOT NULL,
      requested_by uuid,
      requested_by_name varchar(64) NOT NULL DEFAULT '',
      layout jsonb NOT NULL DEFAULT '[]',
      status varchar(12) NOT NULL DEFAULT 'queued',
      attempts integer NOT NULL DEFAULT 0,
      next_attempt_at timestamptz,
      cloud_order_id varchar(64),
      last_error text,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      sent_at timestamptz,
      printed_at timestamptz,
      CONSTRAINT uq_print_jobs_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT ck_print_jobs_kind CHECK (kind IN ('order', 'requisition', 'test')),
      CONSTRAINT ck_print_jobs_status
        CHECK (status IN ('queued', 'sent', 'printed', 'retrying', 'dead')),
      CONSTRAINT ck_print_jobs_source
        CHECK (source IN ('claim', 'assign', 'requisition', 'manual', 'test'))
    )
    """,
    "CREATE INDEX ix_print_jobs_ref ON print_jobs (tenant_id, kind, ref_id, created_at)",
    "CREATE INDEX ix_print_jobs_list ON print_jobs (tenant_id, created_at)",
    # 调度进程跨租户找要发送、要确认的任务。
    """
    CREATE INDEX ix_print_jobs_due ON print_jobs (next_attempt_at)
      WHERE status IN ('queued', 'retrying')
    """,
    "CREATE INDEX ix_print_jobs_sent ON print_jobs (sent_at) WHERE status = 'sent'",
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
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE {table}")
