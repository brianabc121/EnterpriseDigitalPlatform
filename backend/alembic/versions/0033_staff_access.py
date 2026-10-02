"""per-staff pages and permissions (design §31)

Revision ID: 0033
Revises: 0032
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    # 按员工设置的页面和权限：只记录和角色的差别（多给的、去掉的权限）；menus 为空表示按岗位。
    """
    ALTER TABLE staff
      ADD COLUMN extra_permissions text[] NOT NULL DEFAULT '{}',
      ADD COLUMN revoked_permissions text[] NOT NULL DEFAULT '{}',
      ADD COLUMN menus text[],
      ADD COLUMN home_menu varchar(16)
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE staff
          DROP COLUMN home_menu,
          DROP COLUMN menus,
          DROP COLUMN revoked_permissions,
          DROP COLUMN extra_permissions
        """
    )
