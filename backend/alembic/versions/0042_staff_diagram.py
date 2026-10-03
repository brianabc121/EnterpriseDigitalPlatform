"""Staff mind map: diagram branch of each staff card (does not change permissions)

在 feat/staff-mind-map-roles 分支上原来编号 0037；main 已经用 0037–0041（合同、意向客户、企业资料、token 计费、
重置密码），合并时改为 0042–0044。在那个分支上迁移过的数据库，按 0040 的提示先 `alembic stamp 0036` 再升级：
0042–0044 可以重复执行，已有的列、表和角色会跳过。

Revision ID: 0042
Revises: 0041
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE staff ADD COLUMN IF NOT EXISTS diagram_parent_id uuid")
    op.execute("ALTER TABLE staff ADD COLUMN IF NOT EXISTS diagram_direction varchar(8)")
    op.execute("""
        DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_staff_diagram_direction')
          THEN
            ALTER TABLE staff ADD CONSTRAINT ck_staff_diagram_direction CHECK
              (diagram_direction IS NULL OR diagram_direction IN ('left', 'right', 'down'));
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_staff_diagram_branch')
          THEN
            ALTER TABLE staff ADD CONSTRAINT ck_staff_diagram_branch CHECK
              (diagram_parent_id IS NULL OR diagram_direction IS NOT NULL);
          END IF;
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_staff_diagram_parent')
          THEN
            ALTER TABLE staff ADD CONSTRAINT fk_staff_diagram_parent
              FOREIGN KEY (tenant_id, diagram_parent_id) REFERENCES staff (tenant_id, id);
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE staff
          DROP CONSTRAINT fk_staff_diagram_parent,
          DROP CONSTRAINT ck_staff_diagram_branch,
          DROP CONSTRAINT ck_staff_diagram_direction,
          DROP COLUMN diagram_direction,
          DROP COLUMN diagram_parent_id
    """)
