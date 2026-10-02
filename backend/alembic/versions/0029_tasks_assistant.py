"""personal to-dos and the AI company assistant (design §27): staff_tasks; assistant bots,
IM identities, recorded groups and their messages, staff conversations with the assistant;
task and assistant settings; group chats as a knowledge candidate source

Revision ID: 0029
Revises: 0028
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "staff_tasks",
    "assistant_bots",
    "assistant_identities",
    "assistant_groups",
    "assistant_group_messages",
    "assistant_messages",
)
PROVIDERS = "'wecom', 'dingtalk', 'feishu', 'telegram', 'whatsapp'"

STATEMENTS = [
    # ---- 个人待办（设计文档 §27.2）：每个员工自己的事项，管理员看全员 ----
    """
    CREATE TABLE staff_tasks (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      no varchar(24) NOT NULL,
      owner_id uuid NOT NULL,
      title varchar(100) NOT NULL,
      note text NOT NULL DEFAULT '',
      priority varchar(8) NOT NULL DEFAULT 'normal',
      status varchar(12) NOT NULL DEFAULT 'open',
      source varchar(12) NOT NULL DEFAULT 'self',
      due_at timestamptz,
      remind_before_minutes integer,
      reminded_at timestamptz,
      overdue_notified_at timestamptz,
      link text,
      done_note text,
      done_at timestamptz,
      cancelled_at timestamptz,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_staff_tasks_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_staff_tasks_no UNIQUE (tenant_id, no),
      CONSTRAINT fk_staff_tasks_owner FOREIGN KEY (tenant_id, owner_id)
        REFERENCES staff (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_staff_tasks_priority CHECK (priority IN ('urgent', 'high', 'normal', 'low')),
      CONSTRAINT ck_staff_tasks_status CHECK (status IN ('open', 'done', 'cancelled')),
      CONSTRAINT ck_staff_tasks_source
        CHECK (source IN ('self', 'assigned', 'assistant', 'system'))
    )
    """,
    "CREATE INDEX ix_staff_tasks_owner ON staff_tasks (tenant_id, owner_id, status, due_at)",
    # 调度进程跨租户找要提醒的事项。
    """
    CREATE INDEX ix_staff_tasks_due ON staff_tasks (due_at)
      WHERE status = 'open' AND due_at IS NOT NULL
    """,
    "ALTER TABLE tenant_settings ADD COLUMN tasks jsonb NOT NULL DEFAULT '{}'",
    # ---- AI 公司助理（设计文档 §27.3） ----
    "ALTER TABLE tenant_settings ADD COLUMN assistant jsonb NOT NULL DEFAULT '{}'",
    # 机器人：平台、非密配置、加密的密钥、回调令牌、状态。
    f"""
    CREATE TABLE assistant_bots (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      provider varchar(12) NOT NULL,
      name varchar(64) NOT NULL,
      config jsonb NOT NULL DEFAULT '{{}}',
      secrets_enc text NOT NULL DEFAULT '',
      webhook_token varchar(64) NOT NULL,
      status varchar(12) NOT NULL DEFAULT 'active',
      last_received_at timestamptz,
      last_sent_at timestamptz,
      last_error text,
      failures integer NOT NULL DEFAULT 0,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_assistant_bots_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT ck_assistant_bots_provider CHECK (provider IN ({PROVIDERS})),
      CONSTRAINT ck_assistant_bots_status CHECK (status IN ('active', 'disabled'))
    )
    """,
    # IM 账号与员工的对应：未绑定时 staff_id 为空（只记录出现过的账号）。
    """
    CREATE TABLE assistant_identities (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      bot_id uuid NOT NULL,
      external_user_id varchar(128) NOT NULL,
      display_name varchar(128) NOT NULL DEFAULT '',
      chat_id varchar(128),
      staff_id uuid,
      bound_at timestamptz,
      last_seen_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_assistant_identities_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_assistant_identities_user UNIQUE (tenant_id, bot_id, external_user_id),
      CONSTRAINT fk_assistant_identities_bot FOREIGN KEY (tenant_id, bot_id)
        REFERENCES assistant_bots (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_assistant_identities_staff FOREIGN KEY (tenant_id, staff_id)
        REFERENCES staff (tenant_id, id) ON DELETE SET NULL
    )
    """,
    "CREATE INDEX ix_assistant_identities_staff ON assistant_identities (tenant_id, staff_id)",
    # 助理所在的群：默认只记录不说话。
    """
    CREATE TABLE assistant_groups (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      bot_id uuid NOT NULL,
      external_chat_id varchar(128) NOT NULL,
      name varchar(128) NOT NULL DEFAULT '',
      recording boolean NOT NULL DEFAULT true,
      reply_mode varchar(12),
      extract boolean NOT NULL DEFAULT true,
      message_count integer NOT NULL DEFAULT 0,
      last_message_at timestamptz,
      last_extracted_at timestamptz,
      extracted_candidates integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_assistant_groups_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_assistant_groups_chat UNIQUE (tenant_id, bot_id, external_chat_id),
      CONSTRAINT fk_assistant_groups_bot FOREIGN KEY (tenant_id, bot_id)
        REFERENCES assistant_bots (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_assistant_groups_reply_mode
        CHECK (reply_mode IS NULL OR reply_mode IN ('silent', 'mentioned'))
    )
    """,
    # 群消息：按平台消息 ID 去重；提炼过的记下时间。
    """
    CREATE TABLE assistant_group_messages (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      group_id uuid NOT NULL,
      external_message_id varchar(128) NOT NULL,
      sender_external_id varchar(128) NOT NULL,
      sender_name varchar(128) NOT NULL DEFAULT '',
      staff_id uuid,
      text text NOT NULL,
      sent_at timestamptz NOT NULL,
      extracted_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_assistant_group_messages_external
        UNIQUE (tenant_id, group_id, external_message_id),
      CONSTRAINT fk_assistant_group_messages_group FOREIGN KEY (tenant_id, group_id)
        REFERENCES assistant_groups (tenant_id, id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX ix_assistant_group_messages_group
      ON assistant_group_messages (tenant_id, group_id, sent_at)
    """,
    # 员工与助理的对话（IM 私聊和控制台）：上下文、审计。
    """
    CREATE TABLE assistant_messages (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      bot_id uuid,
      staff_id uuid NOT NULL,
      role varchar(12) NOT NULL,
      text text NOT NULL,
      tools jsonb NOT NULL DEFAULT '[]',
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_assistant_messages_staff FOREIGN KEY (tenant_id, staff_id)
        REFERENCES staff (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_assistant_messages_bot FOREIGN KEY (tenant_id, bot_id)
        REFERENCES assistant_bots (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_assistant_messages_role CHECK (role IN ('user', 'assistant'))
    )
    """,
    """
    CREATE INDEX ix_assistant_messages_staff
      ON assistant_messages (tenant_id, staff_id, bot_id, created_at)
    """,
    # 知识候选的来源：助理记录的内部群聊；提示词版本放得下 group_extract@builtin。
    "ALTER TABLE kb_candidates ALTER COLUMN prompt_version TYPE varchar(40)",
    "ALTER TABLE kb_candidates DROP CONSTRAINT ck_kb_candidates_source",
    """
    ALTER TABLE kb_candidates ADD CONSTRAINT ck_kb_candidates_source
      CHECK (source IN ('session', 'sidebar', 'zone', 'group'))
    """,
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)
    for table in TENANT_TABLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edp_app")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO edp_platform")
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
              USING (tenant_id = app_current_tenant())
              WITH CHECK (tenant_id = app_current_tenant())
            """
        )
        op.execute(
            f"CREATE POLICY platform_access ON {table} TO edp_platform"
            " USING (true) WITH CHECK (true)"
        )


def downgrade() -> None:
    op.execute("DELETE FROM kb_candidates WHERE source = 'group'")
    op.execute("ALTER TABLE kb_candidates ALTER COLUMN prompt_version TYPE varchar(16)")
    op.execute("ALTER TABLE kb_candidates DROP CONSTRAINT ck_kb_candidates_source")
    op.execute(
        "ALTER TABLE kb_candidates ADD CONSTRAINT ck_kb_candidates_source"
        " CHECK (source IN ('session', 'sidebar', 'zone'))"
    )
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE tenant_settings DROP COLUMN assistant")
    op.execute("ALTER TABLE tenant_settings DROP COLUMN tasks")
