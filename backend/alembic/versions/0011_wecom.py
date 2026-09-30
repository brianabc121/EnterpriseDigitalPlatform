"""wecom: provider authorization, 微信客服 channel, contacts, groups, transfers, sidebar

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "wecom_corps",
    "wecom_kf_accounts",
    "wecom_members",
    "wecom_contact_follows",
    "wecom_tags",
    "wecom_group_chats",
    "wecom_group_members",
    "wecom_transfers",
    "wecom_sidebar_messages",
)

STATEMENTS = [
    # 员工绑定的企业微信成员（扫码登录、企业微信内免登、应用消息、客户的默认归属坐席）。
    "ALTER TABLE staff ADD COLUMN wecom_userid varchar(64)",
    "ALTER TABLE staff ADD CONSTRAINT uq_staff_wecom_userid UNIQUE (tenant_id, wecom_userid)",
    # 渠道类型：微信客服账号（每个客服账号一个渠道）、企业微信客户联系（每个授权企业一个）。
    "ALTER TABLE channel_accounts DROP CONSTRAINT ck_channel_accounts_type",
    """
    ALTER TABLE channel_accounts ADD CONSTRAINT ck_channel_accounts_type
      CHECK (type IN ('web', 'wecom_kf', 'wecom_contact'))
    """,
    # 消息在外部渠道（企业微信）的 ID：入站消息按它去重，出站消息记录渠道返回的 ID。
    # 服务群里的镜像消息仍以 OpenIM 的 serverMsgID 记在 channel_msg_id。
    "ALTER TABLE messages ADD COLUMN ext_msg_id varchar(128)",
    """
    ALTER TABLE messages ADD CONSTRAINT uq_messages_ext_msg
      UNIQUE (tenant_id, channel_account_id, ext_msg_id)
    """,
    "ALTER TABLE messages DROP CONSTRAINT ck_messages_source",
    """
    ALTER TABLE messages ADD CONSTRAINT ck_messages_source
      CHECK (source IN ('webhook', 'reconcile', 'api', 'channel'))
    """,
    # 发件箱：投递到外部渠道（先投递、成功后镜像到服务群）；把渠道的入站消息镜像到服务群。
    "ALTER TABLE im_ops DROP CONSTRAINT ck_im_ops_op",
    """
    ALTER TABLE im_ops ADD CONSTRAINT ck_im_ops_op CHECK (
      op IN ('invite', 'kick', 'notice', 'signal', 'bot_message', 'channel_send', 'mirror')
    )
    """,
    # 客户转移同步到企业微信（在职继承）的状态：waiting、success、failed。
    # 归属历史仍然只能追加，只放开这一列的更新（结果异步回收）。
    "ALTER TABLE customer_owner_history ADD COLUMN wecom_sync_status varchar(16)",
    "GRANT UPDATE (wecom_sync_status) ON customer_owner_history TO edp_app",
    # 归属原因：企业微信里添加客户的员工成为默认归属坐席（设计 §10.4）。
    "ALTER TABLE customer_owner_history DROP CONSTRAINT ck_customer_owner_history_reason",
    """
    ALTER TABLE customer_owner_history ADD CONSTRAINT ck_customer_owner_history_reason
      CHECK (reason IN ('session_transfer', 'manual', 'handover', 'wecom'))
    """,
    # 授权企业（设计 §7.4）：每个租户最多绑定一个企业，一个企业只能绑定一个租户。
    # 永久授权码（代开发应用的 Secret）加密保存。
    """
    CREATE TABLE wecom_corps (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      corp_id varchar(64) NOT NULL,
      corp_name varchar(128) NOT NULL DEFAULT '',
      agent_id integer,
      permanent_code_enc text NOT NULL,
      auth_info jsonb NOT NULL DEFAULT '{}',
      auth_user_id varchar(64),
      status varchar(16) NOT NULL DEFAULT 'active',
      settings jsonb NOT NULL DEFAULT '{}',
      sync_state jsonb NOT NULL DEFAULT '{}',
      authorized_at timestamptz NOT NULL DEFAULT now(),
      cancelled_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_wecom_corps_tenant_id UNIQUE (tenant_id, id),
      CONSTRAINT ck_wecom_corps_status CHECK (status IN ('active', 'cancelled'))
    )
    """,
    "CREATE UNIQUE INDEX uq_wecom_corps_corp ON wecom_corps (corp_id) WHERE status = 'active'",
    "CREATE UNIQUE INDEX uq_wecom_corps_tenant ON wecom_corps (tenant_id) WHERE status = 'active'",
    # 微信客服账号：每个账号对应一个渠道；sync_msg 的游标按账号保存。
    """
    CREATE TABLE wecom_kf_accounts (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      corp_id varchar(64) NOT NULL,
      open_kfid varchar(64) NOT NULL,
      name varchar(128) NOT NULL DEFAULT '',
      avatar text,
      channel_account_id uuid NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'active',
      cursor varchar(128),
      synced_at timestamptz,
      contact_url text,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_wecom_kf_accounts UNIQUE (tenant_id, open_kfid),
      CONSTRAINT fk_wecom_kf_accounts_channel FOREIGN KEY (tenant_id, channel_account_id)
        REFERENCES channel_accounts (tenant_id, id),
      CONSTRAINT ck_wecom_kf_accounts_status CHECK (status IN ('active', 'removed'))
    )
    """,
    # 企业成员（通讯录同步）：与平台员工按 staff.wecom_userid 绑定。
    """
    CREATE TABLE wecom_members (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      userid varchar(64) NOT NULL,
      name varchar(128) NOT NULL DEFAULT '',
      departments integer[] NOT NULL DEFAULT '{}',
      follow boolean NOT NULL DEFAULT false,
      status varchar(16) NOT NULL DEFAULT 'active',
      synced_at timestamptz NOT NULL DEFAULT now(),
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_wecom_members UNIQUE (tenant_id, userid),
      CONSTRAINT ck_wecom_members_status CHECK (status IN ('active', 'left'))
    )
    """,
    # 客户联系：外部联系人与添加他的成员（一位客户可以被多位成员添加）。
    """
    CREATE TABLE wecom_contact_follows (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      external_userid varchar(64) NOT NULL,
      userid varchar(64) NOT NULL,
      remark varchar(128),
      description text,
      tag_ids text[] NOT NULL DEFAULT '{}',
      add_way integer,
      state varchar(64),
      added_at timestamptz,
      deleted_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_wecom_contact_follows UNIQUE (tenant_id, external_userid, userid),
      CONSTRAINT fk_wecom_contact_follows_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_wecom_contact_follows_customer ON wecom_contact_follows"
    " (tenant_id, customer_id)",
    # 企业标签（客户标签按名称与企业标签对应，平台上打的标签写回企业微信）。
    """
    CREATE TABLE wecom_tags (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      tag_id varchar(64) NOT NULL,
      name varchar(64) NOT NULL,
      group_id varchar(64),
      group_name varchar(64),
      sort integer NOT NULL DEFAULT 0,
      deleted boolean NOT NULL DEFAULT false,
      synced_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant_id, tag_id)
    )
    """,
    # 客户群与群成员：外部成员关联客户档案，在客户 360 视图中展示。
    """
    CREATE TABLE wecom_group_chats (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      chat_id varchar(64) NOT NULL,
      name varchar(128) NOT NULL DEFAULT '',
      owner_userid varchar(64),
      notice text,
      member_count integer NOT NULL DEFAULT 0,
      status varchar(16) NOT NULL DEFAULT 'normal',
      created_time timestamptz,
      synced_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_wecom_group_chats UNIQUE (tenant_id, chat_id),
      CONSTRAINT ck_wecom_group_chats_status CHECK (status IN ('normal', 'dismissed'))
    )
    """,
    """
    CREATE TABLE wecom_group_members (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      chat_id varchar(64) NOT NULL,
      member_id varchar(64) NOT NULL,
      type smallint NOT NULL,
      name varchar(128),
      customer_id uuid,
      join_time timestamptz,
      join_scene smallint,
      PRIMARY KEY (tenant_id, chat_id, member_id),
      CONSTRAINT fk_wecom_group_members_chat FOREIGN KEY (tenant_id, chat_id)
        REFERENCES wecom_group_chats (tenant_id, chat_id) ON DELETE CASCADE,
      CONSTRAINT fk_wecom_group_members_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE SET NULL (customer_id),
      CONSTRAINT ck_wecom_group_members_type CHECK (type IN (1, 2))
    )
    """,
    "CREATE INDEX ix_wecom_group_members_customer ON wecom_group_members (tenant_id, customer_id)",
    # 在职继承（设计 §14.3）：客户转移同步变更企业微信里的添加人，结果异步回收。
    """
    CREATE TABLE wecom_transfers (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      history_id uuid,
      external_userid varchar(64) NOT NULL,
      handover_userid varchar(64) NOT NULL,
      takeover_userid varchar(64) NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'waiting',
      errcode integer,
      error text,
      created_by uuid,
      takeover_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_wecom_transfers_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_wecom_transfers_status CHECK (status IN ('waiting', 'success', 'failed'))
    )
    """,
    "CREATE INDEX ix_wecom_transfers_status ON wecom_transfers (tenant_id, status, created_at)",
    # 员工在企业微信聊天工具栏（侧边栏）发出的内容（设计 §10.5，渠道记为 wecom_sidebar）。
    """
    CREATE TABLE wecom_sidebar_messages (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      staff_id uuid NOT NULL,
      customer_id uuid,
      external_userid varchar(64),
      chat_id varchar(64),
      content text NOT NULL,
      origin varchar(16) NOT NULL DEFAULT 'manual',
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_wecom_sidebar_messages_staff FOREIGN KEY (tenant_id, staff_id)
        REFERENCES staff (tenant_id, id),
      CONSTRAINT fk_wecom_sidebar_messages_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE SET NULL (customer_id),
      CONSTRAINT ck_wecom_sidebar_messages_origin
        CHECK (origin IN ('manual', 'quick_reply', 'suggestion', 'knowledge'))
    )
    """,
    "CREATE INDEX ix_wecom_sidebar_messages_created ON wecom_sidebar_messages"
    " (tenant_id, created_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON wecom_corps, wecom_kf_accounts, wecom_members,"
    " wecom_contact_follows, wecom_tags, wecom_group_chats, wecom_group_members, wecom_transfers,"
    " wecom_sidebar_messages TO edp_app, edp_platform",
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
    op.execute(
        "DROP TABLE IF EXISTS wecom_sidebar_messages, wecom_transfers, wecom_group_members,"
        " wecom_group_chats, wecom_tags, wecom_contact_follows, wecom_members,"
        " wecom_kf_accounts, wecom_corps"
    )
    op.execute("DELETE FROM customer_owner_history WHERE reason = 'wecom'")
    op.execute(
        "ALTER TABLE customer_owner_history DROP CONSTRAINT ck_customer_owner_history_reason"
    )
    op.execute(
        "ALTER TABLE customer_owner_history ADD CONSTRAINT ck_customer_owner_history_reason"
        " CHECK (reason IN ('session_transfer', 'manual', 'handover'))"
    )
    op.execute("REVOKE UPDATE (wecom_sync_status) ON customer_owner_history FROM edp_app")
    op.execute("ALTER TABLE customer_owner_history DROP COLUMN wecom_sync_status")
    op.execute("DELETE FROM im_ops WHERE op IN ('channel_send', 'mirror')")
    op.execute("ALTER TABLE im_ops DROP CONSTRAINT ck_im_ops_op")
    op.execute(
        "ALTER TABLE im_ops ADD CONSTRAINT ck_im_ops_op"
        " CHECK (op IN ('invite', 'kick', 'notice', 'signal', 'bot_message'))"
    )
    op.execute("ALTER TABLE messages DROP CONSTRAINT ck_messages_source")
    op.execute(
        "ALTER TABLE messages ADD CONSTRAINT ck_messages_source"
        " CHECK (source IN ('webhook', 'reconcile', 'api'))"
    )
    op.execute("ALTER TABLE messages DROP CONSTRAINT uq_messages_ext_msg")
    op.execute("ALTER TABLE messages DROP COLUMN ext_msg_id")
    op.execute("ALTER TABLE channel_accounts DROP CONSTRAINT ck_channel_accounts_type")
    op.execute(
        "ALTER TABLE channel_accounts ADD CONSTRAINT ck_channel_accounts_type"
        " CHECK (type IN ('web'))"
    )
    op.execute("ALTER TABLE staff DROP CONSTRAINT uq_staff_wecom_userid")
    op.execute("ALTER TABLE staff DROP COLUMN wecom_userid")
