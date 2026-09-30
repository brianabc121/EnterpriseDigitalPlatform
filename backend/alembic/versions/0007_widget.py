"""widget: resume window on routing policies

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 会话续接（设计文档 §8.2）：会话结束后这么多分钟内客户再来咨询，
    # 优先分配给上次接待的坐席；0 表示关闭。
    op.execute(
        "ALTER TABLE routing_policies ADD COLUMN resume_window_minutes integer NOT NULL DEFAULT 10"
    )
    op.execute(
        """
        ALTER TABLE routing_policies ADD CONSTRAINT ck_routing_policies_resume
          CHECK (resume_window_minutes BETWEEN 0 AND 1440)
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE routing_policies DROP COLUMN IF EXISTS resume_window_minutes")
