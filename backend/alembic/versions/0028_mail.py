"""email channel (design §10.8): mailboxes read over IMAP and answered over SMTP as email
channel accounts; the time the assignee last read a session (server-side unread counts)

Revision ID: 0028
Revises: 0027
Create Date: 2026-10-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ATTACHMENTS = "'image', 'file', 'voice', 'video'"

STATEMENTS = [
    "ALTER TABLE channel_accounts DROP CONSTRAINT ck_channel_accounts_type",
    """
    ALTER TABLE channel_accounts ADD CONSTRAINT ck_channel_accounts_type
      CHECK (type IN ('web', 'wecom_kf', 'wecom_contact', 'email'))
    """,
    # 一个邮箱一行：服务器、登录名、加密的授权码、签名、忽略的发件人、收信位置和状态。
    """
    CREATE TABLE mail_accounts (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      channel_account_id uuid NOT NULL,
      address varchar(254) NOT NULL,
      display_name varchar(64) NOT NULL DEFAULT '',
      provider varchar(16) NOT NULL,
      imap_host varchar(255) NOT NULL,
      imap_port integer NOT NULL,
      imap_security varchar(8) NOT NULL,
      smtp_host varchar(255) NOT NULL,
      smtp_port integer NOT NULL,
      smtp_security varchar(8) NOT NULL,
      username varchar(254) NOT NULL,
      secret_enc text NOT NULL,
      signature text,
      ignore_senders varchar(254)[] NOT NULL DEFAULT '{}',
      status varchar(12) NOT NULL DEFAULT 'active',
      uidvalidity bigint,
      last_uid bigint,
      next_poll_at timestamptz NOT NULL DEFAULT now(),
      last_polled_at timestamptz,
      last_received_at timestamptz,
      failures integer NOT NULL DEFAULT 0,
      last_error text,
      ignored integer NOT NULL DEFAULT 0,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_mail_accounts_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_mail_accounts_channel UNIQUE (tenant_id, channel_account_id),
      CONSTRAINT fk_mail_accounts_channel FOREIGN KEY (tenant_id, channel_account_id)
        REFERENCES channel_accounts (tenant_id, id),
      CONSTRAINT ck_mail_accounts_status CHECK (status IN ('active', 'paused', 'disabled')),
      CONSTRAINT ck_mail_accounts_security CHECK (
        imap_security IN ('ssl', 'starttls', 'none')
        AND smtp_security IN ('ssl', 'starttls', 'none')
      )
    )
    """,
    "CREATE UNIQUE INDEX uq_mail_accounts_address ON mail_accounts (tenant_id, lower(address))",
    # 调度进程跨租户找到期的邮箱。
    "CREATE INDEX ix_mail_accounts_due ON mail_accounts (next_poll_at) WHERE status = 'active'",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON mail_accounts TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON mail_accounts TO edp_platform",
    "ALTER TABLE mail_accounts ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE mail_accounts FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY tenant_isolation ON mail_accounts
      USING (tenant_id = app_current_tenant())
      WITH CHECK (tenant_id = app_current_tenant())
    """,
    "CREATE POLICY platform_access ON mail_accounts TO edp_platform USING (true) WITH CHECK (true)",
    # 服务端记录的未读：接待坐席最后一次查看会话的时间。
    "ALTER TABLE sessions ADD COLUMN read_at timestamptz",
    # 原邮件（.eml）和其他附件一样做病毒扫描、按保留期删除。
    "DROP INDEX IF EXISTS ix_messages_unscanned",
    f"""
    CREATE INDEX ix_messages_unscanned ON messages (tenant_id, sent_at)
      WHERE content_type IN ({ATTACHMENTS}, 'email') AND NOT (content ? 'scan')
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_messages_unscanned")
    op.execute(
        f"""
        CREATE INDEX ix_messages_unscanned ON messages (tenant_id, sent_at)
          WHERE content_type IN ({ATTACHMENTS}) AND NOT (content ? 'scan')
        """
    )
    op.execute("ALTER TABLE sessions DROP COLUMN read_at")
    op.execute("DROP TABLE mail_accounts")
    op.execute("ALTER TABLE channel_accounts DROP CONSTRAINT ck_channel_accounts_type")
    op.execute(
        "ALTER TABLE channel_accounts ADD CONSTRAINT ck_channel_accounts_type"
        " CHECK (type IN ('web', 'wecom_kf', 'wecom_contact'))"
    )
