"""inventory (design §25.12): product stock (empty = not tracked) and warning level, the stock
movement log, when an order's goods left the stock, and whether a product import counts the
stock column as a stocktake or as goods received

Revision ID: 0022
Revises: 0021
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KINDS = (
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

STATEMENTS = [
    # 商品的现有库存（为空表示不管理库存）和预警值：可用库存（现有减去已确认、还没发货的订单占用）
    # 不高于预警值时算库存不足。库存可以是负数（发货多于记录的库存，提示需要盘点）。
    "ALTER TABLE products ADD COLUMN stock integer",
    "ALTER TABLE products ADD COLUMN stock_alert integer",
    """
    ALTER TABLE products ADD CONSTRAINT ck_products_stock_alert
      CHECK (stock_alert IS NULL OR stock_alert >= 0)
    """,
    # 订单的商品出库的时间（发货时，没有发货环节的在完成时）：只扣一次；发货后被企业系统取消时退回。
    "ALTER TABLE orders ADD COLUMN stock_out_at timestamptz",
    # 导入商品表格时"库存"列的算法：盘点（表格里的数就是现有库存）或入库（加到现有库存上）。
    "ALTER TABLE product_imports ADD COLUMN stock_mode varchar(8) NOT NULL DEFAULT 'set'",
    """
    ALTER TABLE product_imports ADD CONSTRAINT ck_product_imports_stock_mode
      CHECK (stock_mode IN ('set', 'add'))
    """,
    # 库存记录：每一次变化（导入、手动调整、订单出库和退回、企业系统同步）和变化前后的数量。
    f"""
    CREATE TABLE stock_movements (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      product_id uuid NOT NULL,
      kind varchar(16) NOT NULL,
      delta integer NOT NULL,
      stock_before integer,
      stock_after integer,
      order_id uuid,
      import_id uuid,
      note text NOT NULL DEFAULT '',
      actor_type varchar(8) NOT NULL,
      actor_id uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_stock_movements_product FOREIGN KEY (tenant_id, product_id)
        REFERENCES products (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_stock_movements_order FOREIGN KEY (tenant_id, order_id)
        REFERENCES orders (tenant_id, id) ON DELETE SET NULL (order_id),
      CONSTRAINT ck_stock_movements_kind CHECK (kind IN ({", ".join(f"'{k}'" for k in KINDS)})),
      CONSTRAINT ck_stock_movements_actor_type CHECK (actor_type IN ('staff', 'api', 'system'))
    )
    """,
    "CREATE INDEX ix_stock_movements_product ON stock_movements"
    " (tenant_id, product_id, created_at DESC)",
    "CREATE INDEX ix_stock_movements_order ON stock_movements (tenant_id, order_id)"
    " WHERE order_id IS NOT NULL",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON stock_movements TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON stock_movements TO edp_platform",
    "ALTER TABLE stock_movements ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE stock_movements FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY tenant_isolation ON stock_movements
      USING (tenant_id = app_current_tenant())
      WITH CHECK (tenant_id = app_current_tenant())
    """,
    "CREATE POLICY platform_access ON stock_movements TO edp_platform USING (true)"
    " WITH CHECK (true)",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE stock_movements")
    op.execute("ALTER TABLE product_imports DROP CONSTRAINT ck_product_imports_stock_mode")
    op.execute("ALTER TABLE product_imports DROP COLUMN stock_mode")
    op.execute("ALTER TABLE orders DROP COLUMN stock_out_at")
    op.execute("ALTER TABLE products DROP CONSTRAINT ck_products_stock_alert")
    op.execute("ALTER TABLE products DROP COLUMN stock_alert")
    op.execute("ALTER TABLE products DROP COLUMN stock")
