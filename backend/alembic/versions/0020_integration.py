"""integration with enterprise systems (design §25.8): API keys for the open API, webhook
endpoints, the webhook outbox (events written in the same transaction as the change) and
delivery records with retries and dead letters, plus the enterprise reference on to-dos and an
index for incremental order sync

Revision ID: 0020
Revises: 0019
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("api_keys", "webhook_endpoints", "webhook_events", "webhook_deliveries")

STATEMENTS = [
    # 接口密钥：只保存哈希；prefix 是明文的一部分，用来查找密钥和在界面上辨认。
    """
    CREATE TABLE api_keys (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      name varchar(64) NOT NULL,
      prefix varchar(16) NOT NULL,
      key_hash varchar(64) NOT NULL,
      scopes text[] NOT NULL DEFAULT '{}',
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      last_used_at timestamptz,
      revoked_at timestamptz,
      revoked_by uuid,
      CONSTRAINT uq_api_keys_tenant_id_id UNIQUE (tenant_id, id)
    )
    """,
    "CREATE UNIQUE INDEX uq_api_keys_prefix ON api_keys (prefix)",
    "CREATE INDEX ix_api_keys_tenant ON api_keys (tenant_id, created_at DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON api_keys TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON api_keys TO edp_platform",
    # 推送地址：签名密钥用租户数据密钥加密保存。
    """
    CREATE TABLE webhook_endpoints (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      name varchar(64) NOT NULL,
      url varchar(1024) NOT NULL,
      secret_enc text NOT NULL,
      events text[] NOT NULL DEFAULT '{}',
      enabled boolean NOT NULL DEFAULT true,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_webhook_endpoints_tenant_id_id UNIQUE (tenant_id, id)
    )
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_endpoints TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_endpoints TO edp_platform",
    # 推送事件（发件箱）：与订单、待办的变化在同一个事务里写入，调度进程再按订阅分发。
    """
    CREATE TABLE webhook_events (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      event varchar(32) NOT NULL,
      resource_type varchar(16) NOT NULL,
      resource_id uuid NOT NULL,
      data jsonb NOT NULL DEFAULT '{}',
      actor_type varchar(8),
      created_at timestamptz NOT NULL DEFAULT now(),
      dispatched_at timestamptz,
      CONSTRAINT uq_webhook_events_tenant_id_id UNIQUE (tenant_id, id)
    )
    """,
    "CREATE INDEX ix_webhook_events_pending ON webhook_events (created_at)"
    " WHERE dispatched_at IS NULL",
    "CREATE INDEX ix_webhook_events_dispatched ON webhook_events (dispatched_at)"
    " WHERE dispatched_at IS NOT NULL",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_events TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_events TO edp_platform",
    # 每个推送地址的每次推送：失败按退避重试（仍为 pending），多次失败后为 dead（死信）。
    """
    CREATE TABLE webhook_deliveries (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      endpoint_id uuid NOT NULL,
      event_id uuid,
      event varchar(32) NOT NULL,
      body text NOT NULL,
      status varchar(12) NOT NULL DEFAULT 'pending',
      attempts smallint NOT NULL DEFAULT 0,
      next_attempt_at timestamptz,
      last_status smallint,
      last_error text,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      delivered_at timestamptz,
      CONSTRAINT uq_webhook_deliveries_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT fk_webhook_deliveries_endpoint FOREIGN KEY (tenant_id, endpoint_id)
        REFERENCES webhook_endpoints (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_webhook_deliveries_event FOREIGN KEY (tenant_id, event_id)
        REFERENCES webhook_events (tenant_id, id) ON DELETE SET NULL (event_id),
      CONSTRAINT ck_webhook_deliveries_status CHECK (status IN ('pending', 'succeeded', 'dead'))
    )
    """,
    "CREATE INDEX ix_webhook_deliveries_due ON webhook_deliveries (next_attempt_at)"
    " WHERE status = 'pending'",
    "CREATE INDEX ix_webhook_deliveries_endpoint ON webhook_deliveries"
    " (tenant_id, endpoint_id, created_at DESC)",
    "CREATE INDEX ix_webhook_deliveries_dead ON webhook_deliveries (created_at DESC)"
    " WHERE status = 'dead'",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_deliveries TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON webhook_deliveries TO edp_platform",
    # 企业系统创建的待办带上它自己的单号，推送"待办完成"时一起带回。
    "ALTER TABLE todos ADD COLUMN external_ref varchar(64)",
    "CREATE UNIQUE INDEX uq_todos_external_ref ON todos (tenant_id, external_ref)"
    " WHERE external_ref IS NOT NULL",
    # 企业系统按更新时间增量同步订单。
    "CREATE INDEX ix_orders_updated ON orders (tenant_id, updated_at, id)",
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
    op.execute("DROP INDEX ix_orders_updated")
    op.execute("ALTER TABLE todos DROP COLUMN external_ref")
    op.execute("DROP TABLE webhook_deliveries, webhook_events, webhook_endpoints, api_keys")
