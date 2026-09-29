"""compliance: tenant data keys, tenant settings, customer sensitive fields, privacy requests,
attachment scans

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("tenant_keys", "tenant_settings", "privacy_requests", "file_scans")

STATEMENTS = [
    # 租户数据密钥（信封加密，设计文档 §7.1、§15）：随机生成，用主密钥包装后保存；轮换产生新版本。
    """
    CREATE TABLE tenant_keys (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      version integer NOT NULL,
      wrapped_key text NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant_id, version),
      CONSTRAINT ck_tenant_keys_version CHECK (version > 0)
    )
    """,
    "GRANT SELECT, INSERT ON tenant_keys TO edp_app",
    # 更换主密钥时重新包装（rewrap-keys）。
    "GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_keys TO edp_platform",
    # 租户设置：消息与文件保留期等（租户管理员维护）。
    """
    CREATE TABLE tenant_settings (
      tenant_id uuid PRIMARY KEY DEFAULT app_current_tenant() REFERENCES tenants (id),
      retention jsonb NOT NULL DEFAULT '{}',
      updated_by uuid,
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "GRANT SELECT, INSERT, UPDATE ON tenant_settings TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_settings TO edp_platform",
    # 客户敏感字段：用租户密钥加密；*_hash 是按租户密钥计算的盲索引，用于精确查找。
    "ALTER TABLE customers ADD COLUMN phone_enc text",
    "ALTER TABLE customers ADD COLUMN phone_hash varchar(64)",
    "ALTER TABLE customers ADD COLUMN email_enc text",
    "ALTER TABLE customers ADD COLUMN email_hash varchar(64)",
    "ALTER TABLE customers ADD COLUMN company varchar(128)",
    "CREATE INDEX ix_customers_phone_hash ON customers (tenant_id, phone_hash)"
    " WHERE phone_hash IS NOT NULL",
    "CREATE INDEX ix_customers_email_hash ON customers (tenant_id, email_hash)"
    " WHERE email_hash IS NOT NULL",
    # 合并客户时把归属历史改挂到目标客户。
    "GRANT UPDATE (customer_id) ON customer_owner_history TO edp_app",
    # 个人信息查询与删除请求的记录（设计文档 §3.3、§18）。客户删除后记录仍然保留。
    """
    CREATE TABLE privacy_requests (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      customer_name varchar(128) NOT NULL DEFAULT '',
      kind varchar(16) NOT NULL,
      requested_by uuid,
      reason text,
      detail jsonb NOT NULL DEFAULT '{}',
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_privacy_requests_kind CHECK (kind IN ('access', 'erase'))
    )
    """,
    "CREATE INDEX ix_privacy_requests_tenant ON privacy_requests (tenant_id, created_at DESC)",
    "GRANT SELECT, INSERT ON privacy_requests TO edp_app",
    # 聊天附件的病毒扫描结果。
    """
    CREATE TABLE file_scans (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      object_key text NOT NULL,
      status varchar(16) NOT NULL,
      signature text,
      size bigint,
      message_id uuid,
      scanned_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant_id, object_key),
      CONSTRAINT ck_file_scans_status CHECK (status IN ('clean', 'infected', 'error', 'missing'))
    )
    """,
    # 文件下载时按对象 key 查扫描结果（key 以企业代码开头，全局唯一）。
    "CREATE INDEX ix_file_scans_key ON file_scans (object_key)",
    "GRANT SELECT, INSERT, UPDATE ON file_scans TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON file_scans TO edp_platform",
    # 待扫描的附件（扫描后 content 里带 scan）。
    """
    CREATE INDEX ix_messages_unscanned ON messages (tenant_id, sent_at)
      WHERE content_type IN ('image', 'file', 'voice', 'video') AND NOT (content ? 'scan')
    """,
]


def _rls(table: str) -> list[str]:
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
    for statement in STATEMENTS:
        op.execute(statement)
    for table in TENANT_TABLES:
        for statement in _rls(table):
            op.execute(statement)


def downgrade() -> None:
    op.execute("REVOKE UPDATE (customer_id) ON customer_owner_history FROM edp_app")
    op.execute("DROP INDEX IF EXISTS ix_messages_unscanned")
    op.execute("DROP TABLE IF EXISTS file_scans, privacy_requests, tenant_settings, tenant_keys")
    op.execute("DROP INDEX IF EXISTS ix_customers_email_hash")
    op.execute("DROP INDEX IF EXISTS ix_customers_phone_hash")
    for column in ("company", "email_hash", "email_enc", "phone_hash", "phone_enc"):
        op.execute(f"ALTER TABLE customers DROP COLUMN {column}")
