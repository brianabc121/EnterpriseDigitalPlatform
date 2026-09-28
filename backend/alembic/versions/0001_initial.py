"""initial schema: tenants, platform users, staff, roles, customers, audit, RLS

Revision ID: 0001
Revises:
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 带 tenant_id 的表都必须出现在这里（tests/test_rls.py 会检查）。
TENANT_TABLES = ("staff", "roles", "staff_roles", "customers", "refresh_tokens", "audit_logs")

# asyncpg 使用预编译语句，一次只能执行一条 SQL，所以这里按语句逐条列出。
SCHEMA_STATEMENTS = [
    """
    DO $$
    BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'edp_app')
         OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'edp_platform') THEN
        RAISE EXCEPTION 'roles edp_app and edp_platform must exist before migrating '
                        '(see deploy/compose/postgres/init/01-roles.sql)';
      END IF;
    END $$
    """,
    # 当前事务的租户。未设置或已重置（空字符串）时返回 NULL，RLS 因此默认拒绝。
    """
    CREATE FUNCTION app_current_tenant() RETURNS uuid
      LANGUAGE sql STABLE
      AS $fn$ SELECT NULLIF(current_setting('app.tenant_id', true), '')::uuid $fn$
    """,
    """
    CREATE TABLE tenants (
      id uuid PRIMARY KEY,
      code varchar(32) NOT NULL,
      name varchar(128) NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'active',
      settings jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_tenants_code UNIQUE (code),
      CONSTRAINT ck_tenants_status CHECK (status IN ('active', 'suspended')),
      CONSTRAINT ck_tenants_code_format CHECK (code ~ '^[a-z][a-z0-9-]{2,31}$')
    )
    """,
    """
    CREATE TABLE platform_users (
      id uuid PRIMARY KEY,
      username varchar(64) NOT NULL,
      display_name varchar(64) NOT NULL,
      password_hash text NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'active',
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_platform_users_username UNIQUE (username),
      CONSTRAINT ck_platform_users_status CHECK (status IN ('active', 'disabled'))
    )
    """,
    """
    CREATE TABLE staff (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      username varchar(64) NOT NULL,
      display_name varchar(64) NOT NULL,
      password_hash text NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'active',
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_staff_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_staff_tenant_id_username UNIQUE (tenant_id, username),
      CONSTRAINT ck_staff_status CHECK (status IN ('active', 'disabled'))
    )
    """,
    """
    CREATE TABLE roles (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      code varchar(32) NOT NULL,
      name varchar(64) NOT NULL,
      permissions text[] NOT NULL DEFAULT '{}',
      is_system boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_roles_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_roles_tenant_id_code UNIQUE (tenant_id, code)
    )
    """,
    # 租户表之间一律使用 (tenant_id, id) 复合外键：外键检查不受 RLS 约束，
    # 只有复合外键才能在数据库层面阻止引用其他租户的行。
    """
    CREATE TABLE staff_roles (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      staff_id uuid NOT NULL,
      role_id uuid NOT NULL,
      CONSTRAINT pk_staff_roles PRIMARY KEY (staff_id, role_id),
      CONSTRAINT fk_staff_roles_staff FOREIGN KEY (tenant_id, staff_id)
        REFERENCES staff (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_staff_roles_role FOREIGN KEY (tenant_id, role_id)
        REFERENCES roles (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_staff_roles_role_id ON staff_roles (role_id)",
    """
    CREATE TABLE customers (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      display_name varchar(128) NOT NULL,
      owner_id uuid,
      source_channel varchar(32) NOT NULL DEFAULT 'manual',
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_customers_owner FOREIGN KEY (tenant_id, owner_id)
        REFERENCES staff (tenant_id, id) ON DELETE SET NULL (owner_id)
    )
    """,
    "CREATE INDEX ix_customers_tenant_id_owner_id ON customers (tenant_id, owner_id)",
    """
    CREATE TABLE refresh_tokens (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      staff_id uuid NOT NULL,
      family_id uuid NOT NULL,
      expires_at timestamptz NOT NULL,
      revoked_at timestamptz,
      replaced_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_refresh_tokens_staff FOREIGN KEY (tenant_id, staff_id)
        REFERENCES staff (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_refresh_tokens_family_id ON refresh_tokens (family_id)",
    "CREATE INDEX ix_refresh_tokens_staff_id ON refresh_tokens (staff_id)",
    """
    CREATE TABLE audit_logs (
      id uuid PRIMARY KEY,
      tenant_id uuid REFERENCES tenants (id),
      actor_type varchar(16) NOT NULL,
      actor_id uuid,
      action varchar(64) NOT NULL,
      resource_type varchar(32),
      resource_id varchar(64),
      detail jsonb NOT NULL DEFAULT '{}'::jsonb,
      ip varchar(64),
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_audit_logs_tenant_id_created_at ON audit_logs (tenant_id, created_at)",
]

GRANT_STATEMENTS = [
    "GRANT USAGE ON SCHEMA public TO edp_app, edp_platform",
    "GRANT SELECT ON tenants TO edp_app",
    """
    GRANT SELECT, INSERT, UPDATE, DELETE
      ON staff, roles, staff_roles, customers, refresh_tokens TO edp_app
    """,
    "GRANT SELECT, INSERT ON audit_logs TO edp_app",
    """
    GRANT SELECT, INSERT, UPDATE, DELETE
      ON tenants, platform_users, staff, roles, staff_roles, customers, refresh_tokens, audit_logs
      TO edp_platform
    """,
]


def _rls_statements(table: str) -> list[str]:
    return [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"""
        CREATE POLICY tenant_isolation ON {table}
          USING (tenant_id = app_current_tenant())
          WITH CHECK (tenant_id = app_current_tenant())
        """,
        f"CREATE POLICY platform_access ON {table} TO edp_platform USING (true) WITH CHECK (true)",
    ]


def upgrade() -> None:
    for statement in SCHEMA_STATEMENTS:
        op.execute(statement)
    for table in TENANT_TABLES:
        for statement in _rls_statements(table):
            op.execute(statement)
    for statement in GRANT_STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute(
        "DROP TABLE IF EXISTS audit_logs, refresh_tokens, customers, staff_roles, roles, staff, "
        "platform_users, tenants"
    )
    op.execute("DROP FUNCTION IF EXISTS app_current_tenant()")
