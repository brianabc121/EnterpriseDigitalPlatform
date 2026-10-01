"""form-filling knowledge base (design §25.18): aliases, material usage and companions learned
from every submitted form, the learning record of each submission (judged by the worker), the
alias evidence and the change log of every entry; tenant setting for auto-activation

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

QTY = "numeric(14, 3)"
ZERO = "'00000000-0000-0000-0000-000000000000'::uuid"


def _rls(table: str) -> list[str]:
    return [
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edp_app",
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edp_platform",
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY tenant_isolation ON {table}
          USING (tenant_id = app_current_tenant())
          WITH CHECK (tenant_id = app_current_tenant())
        """,
        f"CREATE POLICY platform_access ON {table} TO edp_platform USING (true) WITH CHECK (true)",
    ]


STATEMENTS = [
    # 表单知识：叫法（text → product，适用于所有表单，form 为空）、用量（领料单：product 每件用
    # related 多少）、搭配（某种表单里 product 常和 related 一起开）。同一类、同一表单、同一文字和
    # 商品只有一条。
    f"""
    CREATE TABLE form_kb_entries (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      kind varchar(12) NOT NULL,
      form varchar(12),
      text varchar(64) NOT NULL DEFAULT '',
      label varchar(64) NOT NULL DEFAULT '',
      product_id uuid NOT NULL,
      related_id uuid,
      value {QTY},
      status varchar(12) NOT NULL DEFAULT 'observing',
      source varchar(12) NOT NULL DEFAULT 'learned',
      locked boolean NOT NULL DEFAULT false,
      review varchar(12),
      review_note text,
      evidence integer NOT NULL DEFAULT 0,
      share double precision,
      stats jsonb NOT NULL DEFAULT '{{}}',
      hits integer NOT NULL DEFAULT 0,
      last_hit_at timestamptz,
      learned_at timestamptz,
      created_by uuid,
      updated_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_form_kb_entries_tenant_id UNIQUE (tenant_id, id),
      CONSTRAINT fk_form_kb_entries_product FOREIGN KEY (tenant_id, product_id)
        REFERENCES products (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_form_kb_entries_related FOREIGN KEY (tenant_id, related_id)
        REFERENCES products (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_form_kb_entries_kind CHECK (kind IN ('alias', 'usage', 'companion')),
      CONSTRAINT ck_form_kb_entries_form
        CHECK (form IS NULL OR form IN ('order', 'requisition', 'receipt')),
      CONSTRAINT ck_form_kb_entries_status CHECK (status IN ('observing', 'active', 'disabled')),
      CONSTRAINT ck_form_kb_entries_source CHECK (source IN ('learned', 'manual')),
      CONSTRAINT ck_form_kb_entries_review
        CHECK (review IS NULL OR review IN ('activate', 'conflict', 'recipe')),
      CONSTRAINT ck_form_kb_entries_shape CHECK (
        (kind = 'alias' AND form IS NULL AND text <> '' AND related_id IS NULL)
        OR (kind = 'usage' AND form = 'requisition' AND text = '' AND related_id IS NOT NULL
            AND related_id <> product_id)
        OR (kind = 'companion' AND form IS NOT NULL AND text = '' AND related_id IS NOT NULL
            AND related_id <> product_id)
      )
    )
    """,
    "CREATE UNIQUE INDEX uq_form_kb_entries_key ON form_kb_entries"
    f" (tenant_id, kind, COALESCE(form, ''), text, product_id, COALESCE(related_id, {ZERO}))",
    # 开单时查叫法（输入一致或是叫法的开头）。
    "CREATE INDEX ix_form_kb_entries_text ON form_kb_entries"
    " (tenant_id, kind, text varchar_pattern_ops) WHERE kind = 'alias'",
    "CREATE INDEX ix_form_kb_entries_product ON form_kb_entries (tenant_id, kind, product_id)",
    "CREATE INDEX ix_form_kb_entries_list ON form_kb_entries (tenant_id, status, updated_at DESC)",
    *_rls("form_kb_entries"),
    # 学习记录：每次提交表单一条，和表单同一个事务写入；实时消费进程领取后判断。
    """
    CREATE TABLE form_kb_submissions (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      form varchar(12) NOT NULL,
      event varchar(12) NOT NULL,
      record_id uuid NOT NULL,
      record_no varchar(32) NOT NULL DEFAULT '',
      actor_id uuid,
      payload jsonb NOT NULL DEFAULT '{}',
      status varchar(12) NOT NULL DEFAULT 'pending',
      due_at timestamptz DEFAULT now(),
      attempts integer NOT NULL DEFAULT 0,
      error text,
      result jsonb NOT NULL DEFAULT '[]',
      created_at timestamptz NOT NULL DEFAULT now(),
      processed_at timestamptz,
      CONSTRAINT uq_form_kb_submissions_tenant_id UNIQUE (tenant_id, id),
      CONSTRAINT ck_form_kb_submissions_form CHECK (form IN ('order', 'requisition', 'receipt')),
      CONSTRAINT ck_form_kb_submissions_event CHECK (event IN ('created', 'updated', 'confirmed')),
      CONSTRAINT ck_form_kb_submissions_status CHECK (status IN ('pending', 'done', 'failed'))
    )
    """,
    "CREATE INDEX ix_form_kb_submissions_due ON form_kb_submissions (due_at)"
    " WHERE status = 'pending'",
    "CREATE INDEX ix_form_kb_submissions_list ON form_kb_submissions"
    " (tenant_id, created_at DESC, id DESC)",
    *_rls("form_kb_submissions"),
    # 叫法的证据。
    """
    CREATE TABLE form_kb_signals (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      submission_id uuid NOT NULL,
      text varchar(64) NOT NULL,
      label varchar(64) NOT NULL DEFAULT '',
      product_id uuid NOT NULL,
      via varchar(12) NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_form_kb_signals_submission FOREIGN KEY (tenant_id, submission_id)
        REFERENCES form_kb_submissions (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_form_kb_signals_product FOREIGN KEY (tenant_id, product_id)
        REFERENCES products (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT uq_form_kb_signals_once UNIQUE (tenant_id, submission_id, text, product_id),
      CONSTRAINT ck_form_kb_signals_via CHECK (via IN ('typed', 'missed', 'mapped', 'hit'))
    )
    """,
    "CREATE INDEX ix_form_kb_signals_text ON form_kb_signals (tenant_id, text, created_at DESC)",
    "CREATE INDEX ix_form_kb_signals_product ON form_kb_signals (tenant_id, product_id)",
    *_rls("form_kb_signals"),
    # 每条知识的变化记录。
    """
    CREATE TABLE form_kb_log (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      entry_id uuid NOT NULL,
      submission_id uuid,
      action varchar(16) NOT NULL,
      note text NOT NULL DEFAULT '',
      before jsonb,
      after jsonb,
      actor_type varchar(8) NOT NULL,
      actor_id uuid,
      created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
      CONSTRAINT fk_form_kb_log_entry FOREIGN KEY (tenant_id, entry_id)
        REFERENCES form_kb_entries (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_form_kb_log_entry ON form_kb_log (tenant_id, entry_id, created_at DESC)",
    *_rls("form_kb_log"),
    # 设置：学到的知识是否自动生效、三类知识是否学习。
    "ALTER TABLE tenant_settings ADD COLUMN form_kb jsonb NOT NULL DEFAULT '{}'",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_settings DROP COLUMN form_kb")
    for table in ("form_kb_log", "form_kb_signals", "form_kb_submissions", "form_kb_entries"):
        op.execute(f"DROP TABLE {table}")
