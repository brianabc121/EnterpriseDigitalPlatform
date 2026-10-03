"""Staff mind map: cards that exist before a login account is created

在 feat/staff-mind-map-roles 分支上原来编号 0038（见 0042 的说明），可以重复执行。

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS staff_diagram_nodes (
          id uuid PRIMARY KEY,
          tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants(id),
          parent_id uuid,
          direction varchar(8) NOT NULL,
          staff_id uuid,
          created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE (tenant_id, staff_id),
          CONSTRAINT ck_staff_diagram_nodes_direction
            CHECK (direction IN ('left', 'right', 'down')),
          FOREIGN KEY (tenant_id, staff_id) REFERENCES staff(tenant_id, id) ON DELETE CASCADE
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_staff_diagram_nodes_tenant ON staff_diagram_nodes(tenant_id)"
    )
    for role in ("edp_app", "edp_platform"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON staff_diagram_nodes TO {role}")
    op.execute("ALTER TABLE staff_diagram_nodes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE staff_diagram_nodes FORCE ROW LEVEL SECURITY")
    op.execute("""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE tablename = 'staff_diagram_nodes' AND policyname = 'tenant_isolation'
          ) THEN
            CREATE POLICY tenant_isolation ON staff_diagram_nodes
              USING (tenant_id = app_current_tenant())
              WITH CHECK (tenant_id = app_current_tenant());
          END IF;
          IF NOT EXISTS (
            SELECT 1 FROM pg_policies
            WHERE tablename = 'staff_diagram_nodes' AND policyname = 'platform_access'
          ) THEN
            CREATE POLICY platform_access ON staff_diagram_nodes TO edp_platform
              USING (true) WITH CHECK (true);
          END IF;
        END $$
    """)
    # 增量更新索引（§33.9）：分支上的迁移没有加，重复执行时跳过已经加过的。
    op.execute("""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_trigger
            WHERE tgrelid = 'staff_diagram_nodes'::regclass AND tgname = 'edp_stamp_change'
          ) THEN
            PERFORM edp_track_changes('staff_diagram_nodes'::regclass);
          END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute("DROP TABLE staff_diagram_nodes")
