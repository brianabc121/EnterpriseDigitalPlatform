"""Enterprise token billing (design §37): record which staff member triggered an LLM call

Revision ID: 0040
Revises: 0039
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 触发调用的员工：员工在控制台、AI 助理里的操作记员工；访客咨询时的 AI 接待和定时任务
    # 为空（系统）。不加外键：员工删除后调用记录照样保留，页面上显示"已删除的员工"。
    op.execute("ALTER TABLE llm_calls ADD COLUMN staff_id uuid")


def downgrade() -> None:
    op.execute("ALTER TABLE llm_calls DROP COLUMN staff_id")
