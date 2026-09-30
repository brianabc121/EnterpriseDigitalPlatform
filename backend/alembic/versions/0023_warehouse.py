"""warehouse (design §25.13): materials and finished goods in one item library (category, unit,
decimal stock, ready-made goods shipped straight from stock), bills of materials, material
requisitions and production receipts confirmed by the warehouse keeper, and the warehouse
settings (keeper, whether documents need confirming)

Revision ID: 0023
Revises: 0022
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD_KINDS = (
    "import_set",
    "import_add",
    "adjust_set",
    "adjust_add",
    "adjust_remove",
    "untrack",
    "order_out",
    "order_return",
    "api_set",
)
KINDS = (*OLD_KINDS, "requisition", "receipt")
QTY = "numeric(14, 3)"


def _kind_check(kinds: tuple[str, ...]) -> str:
    listed = ", ".join(f"'{k}'" for k in kinds)
    return (
        "ALTER TABLE stock_movements ADD CONSTRAINT ck_stock_movements_kind"
        f" CHECK (kind IN ({listed}))"
    )


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
    # 商品库里的类别：成品（可以销售）或材料（只用于生产，不给 AI、不能下单）；单位；现货（直接
    # 从成品库存发货，不需要加工）。库存改为最多三位小数（材料可以是 2.5 米）。
    "ALTER TABLE products ADD COLUMN kind varchar(12) NOT NULL DEFAULT 'goods'",
    "ALTER TABLE products ADD CONSTRAINT ck_products_kind CHECK (kind IN ('goods', 'material'))",
    "ALTER TABLE products ADD COLUMN unit varchar(16) NOT NULL DEFAULT ''",
    "ALTER TABLE products ADD COLUMN ready_made boolean NOT NULL DEFAULT false",
    f"ALTER TABLE products ALTER COLUMN stock TYPE {QTY}",
    f"ALTER TABLE products ALTER COLUMN stock_alert TYPE {QTY}",
    "CREATE INDEX ix_products_kind ON products (tenant_id, kind, category, name)",
    # 配方：每一件成品用多少材料。
    f"""
    CREATE TABLE product_materials (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      product_id uuid NOT NULL,
      material_id uuid NOT NULL,
      quantity {QTY} NOT NULL,
      sort integer NOT NULL DEFAULT 0,
      CONSTRAINT fk_product_materials_product FOREIGN KEY (tenant_id, product_id)
        REFERENCES products (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_product_materials_material FOREIGN KEY (tenant_id, material_id)
        REFERENCES products (tenant_id, id),
      CONSTRAINT uq_product_materials_line UNIQUE (tenant_id, product_id, material_id),
      CONSTRAINT ck_product_materials_quantity CHECK (quantity > 0),
      CONSTRAINT ck_product_materials_self CHECK (product_id <> material_id)
    )
    """,
    "CREATE INDEX ix_product_materials_material ON product_materials (tenant_id, material_id)",
    *_rls("product_materials"),
    # 仓库单据：领料单（扣减材料库存）和入库单（增加成品库存），仓管确认后生效。
    """
    CREATE TABLE stock_documents (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      kind varchar(12) NOT NULL,
      no varchar(24) NOT NULL,
      status varchar(12) NOT NULL DEFAULT 'pending',
      order_id uuid,
      note text NOT NULL DEFAULT '',
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      submitted_at timestamptz NOT NULL DEFAULT now(),
      confirmed_by uuid,
      confirmed_at timestamptz,
      rejected_by uuid,
      rejected_at timestamptz,
      reject_reason text,
      voided_by uuid,
      voided_at timestamptz,
      void_reason text,
      CONSTRAINT uq_stock_documents_tenant_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_stock_documents_no UNIQUE (tenant_id, no),
      CONSTRAINT fk_stock_documents_order FOREIGN KEY (tenant_id, order_id)
        REFERENCES orders (tenant_id, id) ON DELETE SET NULL (order_id),
      CONSTRAINT ck_stock_documents_kind CHECK (kind IN ('requisition', 'receipt')),
      CONSTRAINT ck_stock_documents_status
        CHECK (status IN ('pending', 'confirmed', 'rejected', 'voided'))
    )
    """,
    "CREATE INDEX ix_stock_documents_list ON stock_documents"
    " (tenant_id, kind, status, created_at DESC)",
    "CREATE INDEX ix_stock_documents_order ON stock_documents (tenant_id, order_id)"
    " WHERE order_id IS NOT NULL",
    *_rls("stock_documents"),
    f"""
    CREATE TABLE stock_document_lines (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      document_id uuid NOT NULL,
      product_id uuid NOT NULL,
      code varchar(64),
      name varchar(128) NOT NULL,
      spec varchar(128) NOT NULL DEFAULT '',
      unit varchar(16) NOT NULL DEFAULT '',
      planned {QTY},
      quantity {QTY} NOT NULL,
      stock_before {QTY},
      stock_after {QTY},
      sort integer NOT NULL DEFAULT 0,
      CONSTRAINT fk_stock_document_lines_document FOREIGN KEY (tenant_id, document_id)
        REFERENCES stock_documents (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_stock_document_lines_product FOREIGN KEY (tenant_id, product_id)
        REFERENCES products (tenant_id, id),
      CONSTRAINT ck_stock_document_lines_quantity CHECK (quantity > 0)
    )
    """,
    "CREATE INDEX ix_stock_document_lines_document ON stock_document_lines"
    " (tenant_id, document_id, sort)",
    "CREATE INDEX ix_stock_document_lines_product ON stock_document_lines (tenant_id, product_id)",
    *_rls("stock_document_lines"),
    # 库存记录：数量改为小数，关联单据；新增领料、生产入库两种变化。
    f"ALTER TABLE stock_movements ALTER COLUMN delta TYPE {QTY}",
    f"ALTER TABLE stock_movements ALTER COLUMN stock_before TYPE {QTY}",
    f"ALTER TABLE stock_movements ALTER COLUMN stock_after TYPE {QTY}",
    "ALTER TABLE stock_movements ADD COLUMN document_id uuid",
    """
    ALTER TABLE stock_movements ADD CONSTRAINT fk_stock_movements_document
      FOREIGN KEY (tenant_id, document_id) REFERENCES stock_documents (tenant_id, id)
      ON DELETE SET NULL (document_id)
    """,
    "ALTER TABLE stock_movements DROP CONSTRAINT ck_stock_movements_kind",
    _kind_check(KINDS),
    "CREATE INDEX ix_stock_movements_recent ON stock_movements (tenant_id, created_at DESC)",
    # 仓库设置（仓管、单据是否需要仓管确认）。
    "ALTER TABLE tenant_settings ADD COLUMN warehouse jsonb NOT NULL DEFAULT '{}'",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("ALTER TABLE tenant_settings DROP COLUMN warehouse")
    op.execute("DROP INDEX ix_stock_movements_recent")
    op.execute("DELETE FROM stock_movements WHERE kind IN ('requisition', 'receipt')")
    op.execute("ALTER TABLE stock_movements DROP CONSTRAINT ck_stock_movements_kind")
    op.execute(_kind_check(OLD_KINDS))
    op.execute("ALTER TABLE stock_movements DROP CONSTRAINT fk_stock_movements_document")
    op.execute("ALTER TABLE stock_movements DROP COLUMN document_id")
    # 小数的库存四舍五入为整数。
    for column in ("delta", "stock_before", "stock_after"):
        op.execute(
            f"ALTER TABLE stock_movements ALTER COLUMN {column} TYPE integer USING round({column})"
        )
    op.execute("DROP TABLE stock_document_lines")
    op.execute("DROP TABLE stock_documents")
    op.execute("DROP TABLE product_materials")
    op.execute("DROP INDEX ix_products_kind")
    op.execute(
        "ALTER TABLE products ALTER COLUMN stock_alert TYPE integer USING round(stock_alert)"
    )
    op.execute("ALTER TABLE products ALTER COLUMN stock TYPE integer USING round(stock)")
    op.execute("ALTER TABLE products DROP COLUMN ready_made")
    op.execute("ALTER TABLE products DROP COLUMN unit")
    op.execute("ALTER TABLE products DROP CONSTRAINT ck_products_kind")
    op.execute("ALTER TABLE products DROP COLUMN kind")
