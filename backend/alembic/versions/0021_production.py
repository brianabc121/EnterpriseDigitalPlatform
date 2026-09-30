"""production by workers (design §25.11): the worker who claimed an order, per-item progress
(done or out of stock, with the shortage details), when processing finished, whether the order
has out-of-stock items, the to-dos that remind staff to ship or to resolve a shortage, and the
worker role for existing tenants

Revision ID: 0021
Revises: 0020
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    # 订单行的加工进度：待加工、已完成、缺货（缺多少、说明、预计到货日期）。
    "ALTER TABLE order_items ADD COLUMN work_status varchar(12) NOT NULL DEFAULT 'pending'",
    """
    ALTER TABLE order_items ADD CONSTRAINT ck_order_items_work_status
      CHECK (work_status IN ('pending', 'done', 'out_of_stock'))
    """,
    "ALTER TABLE order_items ADD COLUMN done_at timestamptz",
    "ALTER TABLE order_items ADD COLUMN done_by uuid",
    "ALTER TABLE order_items ADD COLUMN shortage_qty integer",
    "ALTER TABLE order_items ADD COLUMN shortage_note text",
    "ALTER TABLE order_items ADD COLUMN restock_date date",
    "ALTER TABLE order_items ADD COLUMN shortage_at timestamptz",
    "ALTER TABLE order_items ADD COLUMN shortage_by uuid",
    # 订单：加工人（领取或被指派）、加工完成、有缺货的商品；提醒客服发货、处理缺货的待办。
    "ALTER TABLE orders ADD COLUMN worker_id uuid",
    """
    ALTER TABLE orders ADD CONSTRAINT fk_orders_worker FOREIGN KEY (tenant_id, worker_id)
      REFERENCES staff (tenant_id, id)
    """,
    "ALTER TABLE orders ADD COLUMN claimed_at timestamptz",
    "ALTER TABLE orders ADD COLUMN processed_at timestamptz",
    "ALTER TABLE orders ADD COLUMN processed_by uuid",
    "ALTER TABLE orders ADD COLUMN shortage_at timestamptz",
    "ALTER TABLE orders ADD COLUMN ship_todo_id uuid",
    "ALTER TABLE orders ADD COLUMN shortage_todo_id uuid",
    # 加工页的"待领取""我的加工"，订单中心的"待发货""缺货"。
    """
    CREATE INDEX ix_orders_production ON orders (tenant_id, worker_id, created_at)
      WHERE status IN ('confirmed', 'fulfilling')
    """,
    """
    CREATE INDEX ix_orders_shortage ON orders (tenant_id, shortage_at)
      WHERE shortage_at IS NOT NULL
    """,
]

# 已有租户补上"工人"系统角色（新租户在开通时创建；权限以代码中的定义为准）。
WORKER_ROLE = """
INSERT INTO roles (id, tenant_id, code, name, permissions, is_system)
SELECT gen_random_uuid(), t.id, 'worker', '工人', '{}', true FROM tenants t
WHERE NOT EXISTS (SELECT 1 FROM roles r WHERE r.tenant_id = t.id AND r.code = 'worker')
"""


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    # 迁移以表的所有者执行、没有租户上下文：强制行级安全下所有者写不进角色表，先取消强制。
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute(WORKER_ROLE)
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    # 员工与角色的关联随角色一起删除（外键 ON DELETE CASCADE）。
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE staff_roles NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM roles WHERE code = 'worker' AND is_system")
    op.execute("ALTER TABLE staff_roles FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")
    op.execute("DROP INDEX ix_orders_shortage")
    op.execute("DROP INDEX ix_orders_production")
    for column in (
        "shortage_todo_id",
        "ship_todo_id",
        "shortage_at",
        "processed_by",
        "processed_at",
        "claimed_at",
        "worker_id",
    ):
        op.execute(f"ALTER TABLE orders DROP COLUMN {column}")
    for column in (
        "shortage_by",
        "shortage_at",
        "restock_date",
        "shortage_note",
        "shortage_qty",
        "done_by",
        "done_at",
        "work_status",
    ):
        op.execute(f"ALTER TABLE order_items DROP COLUMN {column}")
