"""Mind-map cards can exist before a login account is created."""

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE staff_diagram_nodes (
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
    op.execute("CREATE INDEX ix_staff_diagram_nodes_tenant ON staff_diagram_nodes(tenant_id)")
    for role in ("edp_app", "edp_platform"):
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON staff_diagram_nodes TO {role}")
    op.execute("ALTER TABLE staff_diagram_nodes ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE staff_diagram_nodes FORCE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY tenant_isolation ON staff_diagram_nodes
          USING (tenant_id = app_current_tenant()) WITH CHECK (tenant_id = app_current_tenant())
    """)
    op.execute("""
        CREATE POLICY platform_access ON staff_diagram_nodes TO edp_platform
          USING (true) WITH CHECK (true)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE staff_diagram_nodes")
