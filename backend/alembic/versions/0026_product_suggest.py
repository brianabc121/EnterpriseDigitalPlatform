"""product suggestions while entering documents (design §25.16): products.search_key, the
normalized fields plus pinyin and initials of the name and aliases, backfilled for existing products

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.modules.products.suggest import build_search_key

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

BATCH = 500


def upgrade() -> None:
    op.execute("ALTER TABLE products ADD COLUMN search_key text NOT NULL DEFAULT ''")
    # 检索键是由商品字段算出来的（以后算法变化时重新计算即可）。迁移以表的所有者执行、没有
    # 租户上下文：强制行级安全下读不到商品，补算时先取消强制。
    op.execute("ALTER TABLE products NO FORCE ROW LEVEL SECURITY")
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, name, aliases, code, model, spec, category FROM products")
    ).all()
    update = sa.text("UPDATE products SET search_key = :key WHERE id = :id")
    for start in range(0, len(rows), BATCH):
        connection.execute(
            update,
            [
                {
                    "id": row.id,
                    "key": build_search_key(
                        row.name, row.aliases or [], row.code, row.model, row.spec, row.category
                    ),
                }
                for row in rows[start : start + BATCH]
            ],
        )
    op.execute("ALTER TABLE products FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE products DROP COLUMN search_key")
