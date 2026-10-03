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
    # 在 feat/staff-mind-map-roles 分支上迁移过的数据库：那里的 0037–0039 是员工导图和财务、出纳角色（合并后改为
    # 0042–0044），这个数据库的版本号虽然是 0039，却没有合同、意向客户、企业资料的表。先停下并说明怎样补上。
    op.execute("""
        DO $$ BEGIN
          IF to_regclass('public.contracts') IS NULL THEN
            RAISE EXCEPTION USING MESSAGE =
              '这个数据库按员工导图分支（feat/staff-mind-map-roles）的旧编号迁移过：缺少合同、意向客户、'
              '企业资料的表。请先执行 alembic stamp 0036，再执行 alembic upgrade head'
              '（员工导图的迁移已改为 0042–0044，可以重复执行）。';
          END IF;
        END $$
    """)
    # 触发调用的员工：员工在控制台、AI 助理里的操作记员工；访客咨询时的 AI 接待和定时任务
    # 为空（系统）。不加外键：员工删除后调用记录照样保留，页面上显示"已删除的员工"。
    op.execute("ALTER TABLE llm_calls ADD COLUMN staff_id uuid")


def downgrade() -> None:
    op.execute("ALTER TABLE llm_calls DROP COLUMN staff_id")
