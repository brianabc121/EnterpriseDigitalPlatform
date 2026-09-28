"""sessions and routing: skill groups, routing policies, agent states, sessions, tickets

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "skill_groups",
    "skill_group_members",
    "routing_policies",
    "agent_states",
    "sessions",
    "session_events",
    "tickets",
)

SCHEMA_STATEMENTS = [
    # 技能组。组长（is_lead）可以查看和转接本组的会话、查看组员的客户。
    """
    CREATE TABLE skill_groups (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      name varchar(64) NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_skill_groups_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_skill_groups_tenant_id_name UNIQUE (tenant_id, name)
    )
    """,
    """
    CREATE TABLE skill_group_members (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      skill_group_id uuid NOT NULL,
      staff_id uuid NOT NULL,
      is_lead boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT pk_skill_group_members PRIMARY KEY (skill_group_id, staff_id),
      CONSTRAINT fk_skill_group_members_group FOREIGN KEY (tenant_id, skill_group_id)
        REFERENCES skill_groups (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_skill_group_members_staff FOREIGN KEY (tenant_id, staff_id)
        REFERENCES staff (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_skill_group_members_staff_id ON skill_group_members (staff_id)",
    # 路由策略：渠道账号引用一套策略；没有引用时使用租户的默认策略（is_default）。
    # business_hours 为空表示全天服务，
    # 否则形如 {"tz": "Asia/Shanghai", "days": {"1": [["09:00", "18:00"]]}}（1 为周一）。
    """
    CREATE TABLE routing_policies (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      name varchar(64) NOT NULL,
      is_default boolean NOT NULL DEFAULT false,
      mode varchar(16) NOT NULL DEFAULT 'human_first',
      default_skill_group_id uuid,
      owner_first boolean NOT NULL DEFAULT true,
      max_wait_seconds integer NOT NULL DEFAULT 300,
      idle_close_minutes integer NOT NULL DEFAULT 30,
      business_hours jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_routing_policies_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT fk_routing_policies_group FOREIGN KEY (tenant_id, default_skill_group_id)
        REFERENCES skill_groups (tenant_id, id) ON DELETE SET NULL (default_skill_group_id),
      CONSTRAINT ck_routing_policies_mode CHECK (mode IN ('ai_first', 'human_first')),
      CONSTRAINT ck_routing_policies_max_wait CHECK (max_wait_seconds BETWEEN 10 AND 86400),
      CONSTRAINT ck_routing_policies_idle CHECK (idle_close_minutes BETWEEN 1 AND 1440)
    )
    """,
    """
    CREATE UNIQUE INDEX uq_routing_policies_default ON routing_policies (tenant_id)
      WHERE is_default
    """,
    "ALTER TABLE channel_accounts ADD COLUMN routing_policy_id uuid",
    """
    ALTER TABLE channel_accounts ADD CONSTRAINT fk_channel_accounts_routing_policy
      FOREIGN KEY (tenant_id, routing_policy_id) REFERENCES routing_policies (tenant_id, id)
      ON DELETE SET NULL (routing_policy_id)
    """,
    # 坐席状态与并发上限。im_ready_at：员工的 IM 用户已注册并与系统用户互为好友（可以收信令）。
    """
    CREATE TABLE agent_states (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      staff_id uuid PRIMARY KEY,
      status varchar(16) NOT NULL DEFAULT 'offline',
      max_concurrency integer NOT NULL DEFAULT 5,
      status_changed_at timestamptz NOT NULL DEFAULT now(),
      last_seen_at timestamptz,
      last_assigned_at timestamptz,
      im_ready_at timestamptz,
      CONSTRAINT fk_agent_states_staff FOREIGN KEY (tenant_id, staff_id)
        REFERENCES staff (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_agent_states_status CHECK (status IN ('online', 'busy', 'away', 'offline')),
      CONSTRAINT ck_agent_states_concurrency CHECK (max_concurrency BETWEEN 1 AND 50)
    )
    """,
    # 会话：Room 中的一次服务过程。同一个 Room 同时只有一个未结束的会话。
    """
    CREATE TABLE sessions (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      room_id uuid NOT NULL,
      customer_id uuid NOT NULL,
      channel_account_id uuid NOT NULL,
      status varchar(16) NOT NULL,
      assignee_id uuid,
      skill_group_id uuid,
      priority smallint NOT NULL DEFAULT 0,
      queued_at timestamptz,
      assigned_at timestamptz,
      first_response_at timestamptz,
      closed_at timestamptz,
      close_reason varchar(32),
      handoff_reason varchar(64),
      ai_summary text,
      csat smallint,
      csat_comment text,
      last_customer_message_at timestamptz,
      last_agent_message_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_sessions_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT fk_sessions_room FOREIGN KEY (tenant_id, room_id)
        REFERENCES rooms (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_sessions_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_sessions_channel FOREIGN KEY (tenant_id, channel_account_id)
        REFERENCES channel_accounts (tenant_id, id),
      CONSTRAINT fk_sessions_assignee FOREIGN KEY (tenant_id, assignee_id)
        REFERENCES staff (tenant_id, id) ON DELETE SET NULL (assignee_id),
      CONSTRAINT fk_sessions_skill_group FOREIGN KEY (tenant_id, skill_group_id)
        REFERENCES skill_groups (tenant_id, id) ON DELETE SET NULL (skill_group_id),
      CONSTRAINT ck_sessions_status CHECK (
        status IN ('ai_serving', 'queued', 'human_serving', 'transferring', 'closed')
      ),
      CONSTRAINT ck_sessions_csat CHECK (csat BETWEEN 1 AND 5)
    )
    """,
    """
    CREATE UNIQUE INDEX uq_sessions_open_room ON sessions (tenant_id, room_id)
      WHERE status <> 'closed'
    """,
    "CREATE INDEX ix_sessions_tenant_id_status ON sessions (tenant_id, status, queued_at)",
    """
    CREATE INDEX ix_sessions_open_assignee ON sessions (tenant_id, assignee_id)
      WHERE status <> 'closed'
    """,
    "CREATE INDEX ix_sessions_tenant_id_customer_id ON sessions (tenant_id, customer_id)",
    """
    CREATE TABLE session_events (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      session_id uuid NOT NULL,
      type varchar(32) NOT NULL,
      actor_type varchar(16) NOT NULL,
      actor_id uuid,
      payload jsonb NOT NULL DEFAULT '{}'::jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_session_events_session FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_session_events_actor CHECK (actor_type IN ('system', 'staff', 'visitor', 'ai'))
    )
    """,
    """
    CREATE INDEX ix_session_events_session ON session_events (tenant_id, session_id, created_at)
    """,
    # 留言与跟进任务：排队超时、非工作时间或访客主动留言时创建。
    """
    CREATE TABLE tickets (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      session_id uuid,
      source varchar(16) NOT NULL,
      content text NOT NULL,
      contact varchar(128),
      status varchar(16) NOT NULL DEFAULT 'open',
      assignee_id uuid,
      skill_group_id uuid,
      closed_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_tickets_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_tickets_session FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions (tenant_id, id) ON DELETE SET NULL (session_id),
      CONSTRAINT fk_tickets_assignee FOREIGN KEY (tenant_id, assignee_id)
        REFERENCES staff (tenant_id, id) ON DELETE SET NULL (assignee_id),
      CONSTRAINT fk_tickets_skill_group FOREIGN KEY (tenant_id, skill_group_id)
        REFERENCES skill_groups (tenant_id, id) ON DELETE SET NULL (skill_group_id),
      CONSTRAINT ck_tickets_status CHECK (status IN ('open', 'done')),
      CONSTRAINT ck_tickets_source CHECK (source IN ('queue_timeout', 'off_hours', 'visitor'))
    )
    """,
    "CREATE INDEX ix_tickets_tenant_id_status ON tickets (tenant_id, status, created_at)",
    "ALTER TABLE messages ADD COLUMN session_id uuid",
    """
    ALTER TABLE messages ADD CONSTRAINT fk_messages_session FOREIGN KEY (tenant_id, session_id)
      REFERENCES sessions (tenant_id, id) ON DELETE SET NULL (session_id)
    """,
    "CREATE INDEX ix_messages_tenant_id_session_id ON messages (tenant_id, session_id)",
    # 已有租户：补默认路由策略、主管角色（新租户在开通时创建）。
    # 迁移以所有者身份执行，不受 RLS 约束。
    """
    INSERT INTO routing_policies (id, tenant_id, name, is_default)
    SELECT gen_random_uuid(), id, '默认策略', true FROM tenants
    """,
    """
    INSERT INTO roles (id, tenant_id, code, name, permissions, is_system)
    SELECT gen_random_uuid(), t.id, 'supervisor', '主管', '{}', true FROM tenants t
    WHERE NOT EXISTS (SELECT 1 FROM roles r WHERE r.tenant_id = t.id AND r.code = 'supervisor')
    """,
]

GRANT_STATEMENTS = [
    """
    GRANT SELECT, INSERT, UPDATE, DELETE
      ON skill_groups, skill_group_members, routing_policies, agent_states, sessions, tickets
      TO edp_app
    """,
    "GRANT SELECT, INSERT ON session_events TO edp_app",
    f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(TENANT_TABLES)} TO edp_platform",
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
    op.execute("DELETE FROM roles WHERE code = 'supervisor' AND is_system")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS session_id")
    op.execute("ALTER TABLE channel_accounts DROP COLUMN IF EXISTS routing_policy_id")
    op.execute(
        "DROP TABLE IF EXISTS tickets, session_events, sessions, agent_states, "
        "routing_policies, skill_group_members, skill_groups"
    )
