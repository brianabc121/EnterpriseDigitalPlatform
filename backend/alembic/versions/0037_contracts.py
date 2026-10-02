"""Contract management (design §34): categories, templates, contracts

Revision ID: 0037
Revises: 0036
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("contract_categories", "contract_templates", "contracts")
HISTORY_TYPES = ("order", "requisition", "receipt", "todo", "goods", "material")

STATEMENTS = [
    # 合同设置（contracts/settings.py 的 ContractSettings）：我方信息、编号前缀、快到期天数。
    "ALTER TABLE tenant_settings ADD COLUMN contracts jsonb NOT NULL DEFAULT '{}'",
    # 多层级分类（最多 5 层）：同一上级下名称不重复；有下级、模板或合同的分类不能删除。
    """
    CREATE TABLE contract_categories (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      parent_id uuid,
      name varchar(64) NOT NULL,
      sort integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      CONSTRAINT uq_contract_categories_name UNIQUE NULLS NOT DISTINCT (tenant_id, parent_id, name),
      FOREIGN KEY (tenant_id, parent_id) REFERENCES contract_categories (tenant_id, id),
      CONSTRAINT ck_contract_categories_parent CHECK (parent_id IS DISTINCT FROM id)
    )
    """,
    # 模板：正文是简单的 Markdown，填写项写成 {{名称}}；fields 是填写项的说明和默认值。
    """
    CREATE TABLE contract_templates (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      category_id uuid,
      name varchar(128) NOT NULL,
      description text,
      body text NOT NULL DEFAULT '',
      fields jsonb NOT NULL DEFAULT '[]',
      status varchar(12) NOT NULL DEFAULT 'active',
      file_key text,
      file_name varchar(200),
      used_count integer NOT NULL DEFAULT 0,
      created_by uuid,
      updated_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      FOREIGN KEY (tenant_id, category_id) REFERENCES contract_categories (tenant_id, id),
      CONSTRAINT ck_contract_templates_status CHECK (status IN ('active', 'disabled'))
    )
    """,
    "CREATE INDEX ix_contract_templates_category ON contract_templates (tenant_id, category_id)",
    # 合同：编号按天递增；正文里还没填的 {{名称}} 填完才能定稿。ai 记下 AI 起草的依据。
    """
    CREATE TABLE contracts (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      no varchar(32) NOT NULL,
      title varchar(200) NOT NULL,
      category_id uuid,
      template_id uuid,
      customer_id uuid,
      order_id uuid,
      owner_id uuid,
      status varchar(12) NOT NULL DEFAULT 'draft',
      amount numeric(14, 2),
      sign_date date,
      start_date date,
      end_date date,
      requirement text,
      body text NOT NULL DEFAULT '',
      field_values jsonb NOT NULL DEFAULT '{}',
      ai jsonb,
      void_reason text,
      scan_key text,
      scan_name varchar(200),
      created_by uuid,
      finalized_by uuid,
      finalized_at timestamptz,
      signed_by uuid,
      signed_at timestamptz,
      voided_by uuid,
      voided_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      CONSTRAINT uq_contracts_no UNIQUE (tenant_id, no),
      FOREIGN KEY (tenant_id, category_id) REFERENCES contract_categories (tenant_id, id),
      FOREIGN KEY (tenant_id, template_id) REFERENCES contract_templates (tenant_id, id)
        ON DELETE SET NULL (template_id),
      FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id)
        ON DELETE SET NULL (customer_id),
      FOREIGN KEY (tenant_id, order_id) REFERENCES orders (tenant_id, id)
        ON DELETE SET NULL (order_id),
      FOREIGN KEY (tenant_id, owner_id) REFERENCES staff (tenant_id, id)
        ON DELETE SET NULL (owner_id),
      CONSTRAINT ck_contracts_status CHECK (status IN ('draft', 'final', 'signed', 'void')),
      CONSTRAINT ck_contracts_dates CHECK (end_date IS NULL OR start_date IS NULL
        OR end_date >= start_date)
    )
    """,
    "CREATE INDEX ix_contracts_category ON contracts (tenant_id, category_id)",
    "CREATE INDEX ix_contracts_customer ON contracts (tenant_id, customer_id)",
    "CREATE INDEX ix_contracts_order ON contracts (tenant_id, order_id)",
    "CREATE INDEX ix_contracts_owner ON contracts (tenant_id, owner_id)",
    "CREATE INDEX ix_contracts_updated ON contracts (tenant_id, updated_at DESC)",
    "CREATE INDEX ix_contracts_expiring ON contracts (tenant_id, end_date) WHERE status = 'signed'",
]


def _history_types(types: Sequence[str]) -> list[str]:
    values = ", ".join(f"'{t}'" for t in types)
    return [
        "ALTER TABLE record_versions DROP CONSTRAINT ck_record_versions_type",
        "ALTER TABLE record_versions ADD CONSTRAINT ck_record_versions_type"
        f" CHECK (record_type IN ({values}))",
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
    # 修改历史：合同和模板（§34.7）。
    for statement in _history_types((*HISTORY_TYPES, "contract", "contract_tpl")):
        op.execute(statement)


def downgrade() -> None:
    op.execute("DELETE FROM record_versions WHERE record_type IN ('contract', 'contract_tpl')")
    for statement in _history_types(HISTORY_TYPES):
        op.execute(statement)
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE tenant_settings DROP COLUMN contracts")
