"""wecom extras: resigned transfers, group chat transfers, join ways, broadcasts, zone results

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "wecom_group_transfers",
    "wecom_join_ways",
    "wecom_broadcasts",
    "wecom_broadcast_results",
    "wecom_zone_results",
)

STATEMENTS = [
    # 客户继承分在职继承（onjob）和离职继承（resigned），结果查询的接口不同（设计 §14.3）。
    "ALTER TABLE wecom_transfers ADD COLUMN kind varchar(16) NOT NULL DEFAULT 'onjob'",
    """
    ALTER TABLE wecom_transfers ADD CONSTRAINT ck_wecom_transfers_kind
      CHECK (kind IN ('onjob', 'resigned'))
    """,
    # 客户群成员经哪个"加入群聊"二维码进群（state），用于统计活码效果。
    "ALTER TABLE wecom_group_members ADD COLUMN state varchar(64)",
    # 侧边栏发出内容对应的客户问题（员工粘贴的），用于从侧边栏的问答沉淀知识；
    # extracted_at 记录已经提炼过（提炼任务只处理还没提炼的）。
    "ALTER TABLE wecom_sidebar_messages ADD COLUMN question text",
    "ALTER TABLE wecom_sidebar_messages ADD COLUMN extracted_at timestamptz",
    "CREATE INDEX ix_wecom_sidebar_messages_pending ON wecom_sidebar_messages (tenant_id)"
    " WHERE question IS NOT NULL AND extracted_at IS NULL",
    # 客户群继承：员工离职或调岗时，把他作为群主的客户群转给接替的员工。结果同步返回。
    """
    CREATE TABLE wecom_group_transfers (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      chat_id varchar(64) NOT NULL,
      handover_userid varchar(64) NOT NULL,
      takeover_userid varchar(64) NOT NULL,
      kind varchar(16) NOT NULL,
      status varchar(16) NOT NULL,
      errcode integer,
      error text,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_wecom_group_transfers_kind CHECK (kind IN ('onjob', 'resigned')),
      CONSTRAINT ck_wecom_group_transfers_status CHECK (status IN ('success', 'failed'))
    )
    """,
    "CREATE INDEX ix_wecom_group_transfers_created ON wecom_group_transfers"
    " (tenant_id, created_at DESC)",
    # "加入群聊"二维码（客户群活码）：扫码进群，群满后可以自动建新群（设计 §10.4）。
    """
    CREATE TABLE wecom_join_ways (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      config_id varchar(64) NOT NULL,
      name varchar(64) NOT NULL,
      chat_ids varchar(64)[] NOT NULL DEFAULT '{}',
      auto_create_room boolean NOT NULL DEFAULT true,
      room_base_name varchar(40),
      room_base_id integer,
      state varchar(30) NOT NULL,
      qr_code text,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_wecom_join_ways_config UNIQUE (tenant_id, config_id),
      CONSTRAINT uq_wecom_join_ways_state UNIQUE (tenant_id, state)
    )
    """,
    # 群发任务：平台创建，员工或群主在企业微信里确认后发出，结果定时回收（设计 §10.4）。
    """
    CREATE TABLE wecom_broadcasts (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      kind varchar(16) NOT NULL,
      title varchar(128) NOT NULL,
      content text NOT NULL,
      link jsonb,
      audience jsonb NOT NULL DEFAULT '{}',
      target_count integer NOT NULL DEFAULT 0,
      msgids varchar(64)[] NOT NULL DEFAULT '{}',
      status varchar(16) NOT NULL,
      fail_list jsonb NOT NULL DEFAULT '[]',
      error text,
      stats jsonb NOT NULL DEFAULT '{}',
      created_by uuid,
      polled_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_wecom_broadcasts_tenant_id UNIQUE (tenant_id, id),
      CONSTRAINT ck_wecom_broadcasts_kind CHECK (kind IN ('single', 'group')),
      CONSTRAINT ck_wecom_broadcasts_status CHECK (status IN ('created', 'failed', 'cancelled'))
    )
    """,
    "CREATE INDEX ix_wecom_broadcasts_created ON wecom_broadcasts (tenant_id, created_at DESC)",
    # 群发结果：每位成员对每位客户（或每个客户群）的发送情况。
    """
    CREATE TABLE wecom_broadcast_results (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      broadcast_id uuid NOT NULL,
      msgid varchar(64) NOT NULL,
      userid varchar(64) NOT NULL,
      external_userid varchar(64),
      chat_id varchar(64),
      status smallint NOT NULL,
      send_time timestamptz,
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_wecom_broadcast_results_broadcast FOREIGN KEY (tenant_id, broadcast_id)
        REFERENCES wecom_broadcasts (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT uq_wecom_broadcast_results UNIQUE NULLS NOT DISTINCT
        (tenant_id, broadcast_id, userid, external_userid, chat_id)
    )
    """,
    # 数据与智能专区（可选，设计 §10.6）：专区内的分析程序只返回结果（摘要、情绪、问答候选）。
    """
    CREATE TABLE wecom_zone_results (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      chat_id varchar(64) NOT NULL,
      kind varchar(24) NOT NULL,
      payload jsonb NOT NULL DEFAULT '{}',
      window_start timestamptz,
      window_end timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_wecom_zone_results_kind
        CHECK (kind IN ('summary', 'sentiment', 'qa_candidates', 'tags'))
    )
    """,
    "CREATE INDEX ix_wecom_zone_results_chat ON wecom_zone_results"
    " (tenant_id, chat_id, kind, created_at DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON wecom_group_transfers, wecom_join_ways,"
    " wecom_broadcasts, wecom_broadcast_results, wecom_zone_results TO edp_app, edp_platform",
    # 知识候选的来源：专区返回的群聊问答候选（没有证据消息）。
    "ALTER TABLE kb_candidates ADD COLUMN source varchar(16) NOT NULL DEFAULT 'session'",
    """
    ALTER TABLE kb_candidates ADD CONSTRAINT ck_kb_candidates_source
      CHECK (source IN ('session', 'sidebar', 'zone'))
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
    op.execute("ALTER TABLE kb_candidates DROP CONSTRAINT ck_kb_candidates_source")
    op.execute("ALTER TABLE kb_candidates DROP COLUMN source")
    op.execute(
        "DROP TABLE IF EXISTS wecom_zone_results, wecom_broadcast_results, wecom_broadcasts,"
        " wecom_join_ways, wecom_group_transfers"
    )
    op.execute("ALTER TABLE wecom_sidebar_messages DROP COLUMN extracted_at")
    op.execute("ALTER TABLE wecom_sidebar_messages DROP COLUMN question")
    op.execute("ALTER TABLE wecom_group_members DROP COLUMN state")
    op.execute("ALTER TABLE wecom_transfers DROP CONSTRAINT ck_wecom_transfers_kind")
    op.execute("ALTER TABLE wecom_transfers DROP COLUMN kind")
