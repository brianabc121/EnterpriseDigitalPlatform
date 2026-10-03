"""Password reset (design §38): temporary passwords and when a staff password last changed

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 管理员或平台运维人员重置的是临时密码：员工登录后要先设置新密码（§38.5）。
    op.execute("ALTER TABLE staff ADD COLUMN must_change_password boolean NOT NULL DEFAULT false")
    # 密码最近修改（或重置）的时间：在这之前签发的访问令牌失效（§38.6）。为空表示创建以来没有改过。
    op.execute("ALTER TABLE staff ADD COLUMN password_changed_at timestamptz")


def downgrade() -> None:
    op.execute(
        "ALTER TABLE staff DROP COLUMN password_changed_at, DROP COLUMN must_change_password"
    )
