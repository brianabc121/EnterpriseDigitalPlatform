"""Persist employee diagram branches without changing permissions."""

from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE staff
          ADD COLUMN diagram_parent_id uuid,
          ADD COLUMN diagram_direction varchar(8),
          ADD CONSTRAINT ck_staff_diagram_direction CHECK
            (diagram_direction IS NULL OR diagram_direction IN ('left', 'right', 'down')),
          ADD CONSTRAINT ck_staff_diagram_branch CHECK
            (diagram_parent_id IS NULL OR diagram_direction IS NOT NULL),
          ADD CONSTRAINT fk_staff_diagram_parent FOREIGN KEY (tenant_id, diagram_parent_id)
            REFERENCES staff (tenant_id, id)
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
