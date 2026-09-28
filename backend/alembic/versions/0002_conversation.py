"""conversation: channel accounts, customer identities, rooms, messages

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("channel_accounts", "customer_identities", "rooms", "messages")

SCHEMA_STATEMENTS = [
    # 其他表要以 (tenant_id, id) 复合外键引用客户。
    "ALTER TABLE customers ADD CONSTRAINT uq_customers_tenant_id_id UNIQUE (tenant_id, id)",
    # 渠道账号：一个接入点。public_key 形如 "{租户代码}.{随机串}"，公开嵌入网页，只用于识别渠道。
    """
    CREATE TABLE channel_accounts (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      type varchar(16) NOT NULL,
      name varchar(64) NOT NULL,
      public_key varchar(80) NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'active',
      config jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_channel_accounts_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_channel_accounts_public_key UNIQUE (public_key),
      CONSTRAINT ck_channel_accounts_type CHECK (type IN ('web')),
      CONSTRAINT ck_channel_accounts_status CHECK (status IN ('active', 'disabled'))
    )
    """,
    # 客户在某个渠道上的身份。一个客户可以有多个身份；IM 用户按身份注册。
    """
    CREATE TABLE customer_identities (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      channel_account_id uuid NOT NULL,
      external_id varchar(128) NOT NULL,
      im_user_id varchar(128) NOT NULL,
      im_registered_at timestamptz,
      profile jsonb NOT NULL DEFAULT '{}'::jsonb,
      verified boolean NOT NULL DEFAULT false,
      last_seen_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_customer_identities_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_customer_identities_external
        UNIQUE (tenant_id, channel_account_id, external_id),
      CONSTRAINT uq_customer_identities_im_user UNIQUE (tenant_id, im_user_id),
      CONSTRAINT fk_customer_identities_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_customer_identities_channel FOREIGN KEY (tenant_id, channel_account_id)
        REFERENCES channel_accounts (tenant_id, id)
    )
    """,
    "CREATE INDEX ix_customer_identities_customer_id ON customer_identities (customer_id)",
    # Room：客户身份的长期对话容器，对应一个 OpenIM 服务群。
    # synced_seq 是对账游标：不大于它的 seq 都已核对过（消息已入库，或是无需入库的通知）。
    """
    CREATE TABLE rooms (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      identity_id uuid NOT NULL,
      channel_account_id uuid NOT NULL,
      im_group_id varchar(128) NOT NULL,
      im_ready_at timestamptz,
      synced_seq bigint NOT NULL DEFAULT 0,
      last_message_at timestamptz,
      last_active_at timestamptz NOT NULL DEFAULT now(),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_rooms_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_rooms_tenant_id_identity_id UNIQUE (tenant_id, identity_id),
      CONSTRAINT uq_rooms_tenant_id_im_group_id UNIQUE (tenant_id, im_group_id),
      CONSTRAINT fk_rooms_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_rooms_identity FOREIGN KEY (tenant_id, identity_id)
        REFERENCES customer_identities (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_rooms_channel FOREIGN KEY (tenant_id, channel_account_id)
        REFERENCES channel_accounts (tenant_id, id)
    )
    """,
    "CREATE INDEX ix_rooms_tenant_id_customer_id ON rooms (tenant_id, customer_id)",
    "CREATE INDEX ix_rooms_tenant_id_last_active_at ON rooms (tenant_id, last_active_at)",
    # 消息归档。平台库是消息的业务权威来源。
    # 入站消息按 (渠道账号, 渠道消息 ID) 去重：OpenIM 取 serverMsgID，企业微信取 msgid。
    # im_seq 在回调里拿不到（恒为 0），由对账回填。
    # 上线前改为按月分区；分区表的唯一约束必须包含分区键，届时连同去重键一起调整。
    """
    CREATE TABLE messages (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      room_id uuid NOT NULL,
      channel_account_id uuid NOT NULL,
      direction varchar(8) NOT NULL,
      sender_type varchar(16) NOT NULL,
      sender_id uuid,
      content_type varchar(16) NOT NULL,
      content jsonb NOT NULL,
      text_plain text,
      channel_msg_id varchar(128),
      client_msg_id varchar(128),
      im_seq bigint,
      source varchar(16) NOT NULL,
      sent_at timestamptz NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_messages_channel_msg UNIQUE (tenant_id, channel_account_id, channel_msg_id),
      CONSTRAINT fk_messages_room FOREIGN KEY (tenant_id, room_id)
        REFERENCES rooms (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_messages_channel FOREIGN KEY (tenant_id, channel_account_id)
        REFERENCES channel_accounts (tenant_id, id),
      CONSTRAINT ck_messages_direction CHECK (direction IN ('in', 'out')),
      CONSTRAINT ck_messages_sender_type
        CHECK (sender_type IN ('customer', 'agent', 'bot', 'system')),
      CONSTRAINT ck_messages_source CHECK (source IN ('webhook', 'reconcile', 'api'))
    )
    """,
    "CREATE INDEX ix_messages_tenant_id_room_id_sent_at ON messages (tenant_id, room_id, sent_at)",
    # 已有租户补一个默认 Web 渠道（新租户在开通时创建）。迁移以所有者身份执行，不受 RLS 约束。
    """
    INSERT INTO channel_accounts (id, tenant_id, type, name, public_key)
    SELECT gen_random_uuid(), id, 'web', '官网',
           code || '.' || replace(gen_random_uuid()::text, '-', '')
    FROM tenants
    """,
]

GRANT_STATEMENTS = [
    """
    GRANT SELECT, INSERT, UPDATE, DELETE
      ON channel_accounts, customer_identities, rooms TO edp_app
    """,
    "GRANT SELECT, INSERT, UPDATE ON messages TO edp_app",
    """
    GRANT SELECT, INSERT, UPDATE, DELETE
      ON channel_accounts, customer_identities, rooms, messages TO edp_platform
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
    op.execute("DROP TABLE IF EXISTS messages, rooms, customer_identities, channel_accounts")
    op.execute("ALTER TABLE customers DROP CONSTRAINT IF EXISTS uq_customers_tenant_id_id")
