"""to-dos (design §24): to-do types, to-dos, to-do activity, post-session extraction records,
per-day number counters and to-do settings; the old tickets are merged into to-dos of the
"留言" type

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("number_counters", "todo_types", "todos", "todo_events", "todo_extractions")

# 编号的日期按这个时区划分（与迁移前留言的创建日期一致）。
NUMBER_TZ = "Asia/Shanghai"

STATEMENTS = [
    # 按天递增的编号（待办 TD20260930-0001，之后的订单也用它）。
    """
    CREATE TABLE number_counters (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      scope varchar(16) NOT NULL,
      day date NOT NULL,
      value integer NOT NULL DEFAULT 0,
      CONSTRAINT pk_number_counters PRIMARY KEY (tenant_id, scope, day)
    )
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON number_counters TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON number_counters TO edp_platform",
    # 待办类型（设计文档 §24.2）：字段定义、给 AI 的说明、分派规则、时限、提醒与升级、话术。
    """
    CREATE TABLE todo_types (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      code varchar(32) NOT NULL,
      name varchar(32) NOT NULL,
      ai_hint text NOT NULL DEFAULT '',
      examples text[] NOT NULL DEFAULT '{}',
      fields jsonb NOT NULL DEFAULT '[]',
      assign_rule jsonb NOT NULL DEFAULT '{}',
      priority varchar(8) NOT NULL DEFAULT 'normal',
      sla_response_minutes integer,
      sla_resolve_minutes integer,
      sla_resolve_days integer,
      remind_before_minutes integer NOT NULL DEFAULT 120,
      escalate_after_minutes integer NOT NULL DEFAULT 240,
      ai_enabled boolean NOT NULL DEFAULT true,
      handoff boolean NOT NULL DEFAULT false,
      notify_supervisor boolean NOT NULL DEFAULT false,
      promise_text text NOT NULL DEFAULT '',
      done_template text NOT NULL DEFAULT '',
      preset boolean NOT NULL DEFAULT false,
      system boolean NOT NULL DEFAULT false,
      enabled boolean NOT NULL DEFAULT true,
      sort integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_todo_types_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_todo_types_code UNIQUE (tenant_id, code),
      CONSTRAINT ck_todo_types_priority CHECK (priority IN ('urgent', 'high', 'normal', 'low')),
      CONSTRAINT ck_todo_types_sla CHECK (
        (sla_response_minutes IS NULL OR sla_response_minutes > 0)
        AND (sla_resolve_minutes IS NULL OR sla_resolve_minutes > 0)
        AND (sla_resolve_days IS NULL OR sla_resolve_days > 0)
      )
    )
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON todo_types TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON todo_types TO edp_platform",
    # 待办（设计文档 §24.5）。AI 生成的先是待确认（pending），确认后才进入待办列表。
    # 引用的对话消息只保存 ID（消息表是分区表，不建外键）；order_id 在订单表建好后加外键。
    """
    CREATE TABLE todos (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      no varchar(24) NOT NULL,
      type_id uuid NOT NULL,
      title varchar(100) NOT NULL,
      detail text NOT NULL DEFAULT '',
      fields jsonb NOT NULL DEFAULT '{}',
      customer_id uuid,
      session_id uuid,
      order_id uuid,
      source varchar(12) NOT NULL,
      confidence double precision,
      evidence_message_ids uuid[] NOT NULL DEFAULT '{}',
      priority varchar(8) NOT NULL DEFAULT 'normal',
      status varchar(12) NOT NULL DEFAULT 'open',
      assignee_id uuid,
      skill_group_id uuid,
      assigned_by uuid,
      expected_at timestamptz,
      due_at timestamptz,
      respond_due_at timestamptz,
      first_response_at timestamptz,
      confirmed_at timestamptz,
      confirmed_by uuid,
      closed_at timestamptz,
      paused_at timestamptz,
      result text,
      reject_reason varchar(12),
      close_note text,
      progress_note text,
      nudge_count integer NOT NULL DEFAULT 0,
      dedupe_key varchar(64),
      created_by_type varchar(8) NOT NULL,
      created_by uuid,
      notify_reason varchar(12),
      notified_at timestamptz,
      pending_remind_at timestamptz,
      remind_at timestamptz,
      overdue_notified_at timestamptz,
      escalate_at timestamptz,
      escalated_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_todos_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_todos_no UNIQUE (tenant_id, no),
      CONSTRAINT fk_todos_type FOREIGN KEY (tenant_id, type_id)
        REFERENCES todo_types (tenant_id, id),
      CONSTRAINT fk_todos_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_todos_session FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions (tenant_id, id) ON DELETE SET NULL (session_id),
      CONSTRAINT fk_todos_assignee FOREIGN KEY (tenant_id, assignee_id)
        REFERENCES staff (tenant_id, id) ON DELETE SET NULL (assignee_id),
      CONSTRAINT fk_todos_skill_group FOREIGN KEY (tenant_id, skill_group_id)
        REFERENCES skill_groups (tenant_id, id) ON DELETE SET NULL (skill_group_id),
      CONSTRAINT ck_todos_status CHECK (
        status IN ('pending', 'open', 'in_progress', 'waiting', 'done', 'cancelled', 'rejected')
      ),
      CONSTRAINT ck_todos_source CHECK (
        source IN ('ai_chat', 'ai_summary', 'zone', 'copilot', 'sidebar', 'staff', 'visitor',
                   'rule', 'api')
      ),
      CONSTRAINT ck_todos_priority CHECK (priority IN ('urgent', 'high', 'normal', 'low')),
      CONSTRAINT ck_todos_reject_reason CHECK (
        reject_reason IS NULL OR reject_reason IN ('not_real', 'duplicate', 'wrong_info', 'other')
      ),
      CONSTRAINT ck_todos_created_by_type CHECK (
        created_by_type IN ('ai', 'staff', 'system', 'api', 'visitor')
      ),
      CONSTRAINT ck_todos_confidence CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1)
    )
    """,
    "CREATE UNIQUE INDEX uq_todos_dedupe ON todos (tenant_id, dedupe_key)"
    " WHERE dedupe_key IS NOT NULL",
    "CREATE INDEX ix_todos_status_due ON todos (tenant_id, status, due_at)",
    "CREATE INDEX ix_todos_assignee ON todos (tenant_id, assignee_id, status)",
    "CREATE INDEX ix_todos_customer ON todos (tenant_id, customer_id, created_at DESC)",
    "CREATE INDEX ix_todos_session ON todos (tenant_id, session_id)",
    "CREATE INDEX ix_todos_assigned_by ON todos (tenant_id, assigned_by)"
    " WHERE assigned_by IS NOT NULL",
    # 调度进程跨租户扫描：待发送的提醒、到期提醒、逾期和升级。
    "CREATE INDEX ix_todos_notify ON todos (created_at) WHERE notified_at IS NULL",
    "CREATE INDEX ix_todos_pending_remind ON todos (pending_remind_at)"
    " WHERE pending_remind_at IS NOT NULL",
    "CREATE INDEX ix_todos_remind ON todos (remind_at) WHERE remind_at IS NOT NULL",
    "CREATE INDEX ix_todos_overdue ON todos (due_at)"
    " WHERE overdue_notified_at IS NULL AND status IN ('open', 'in_progress')",
    "CREATE INDEX ix_todos_escalate ON todos (escalate_at) WHERE escalate_at IS NOT NULL",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON todos TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON todos TO edp_platform",
    # 待办动态：所有操作完整可追溯（只追加）。
    """
    CREATE TABLE todo_events (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      todo_id uuid NOT NULL,
      type varchar(24) NOT NULL,
      actor_type varchar(8) NOT NULL,
      actor_id uuid,
      payload jsonb NOT NULL DEFAULT '{}',
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, todo_id) REFERENCES todos (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_todo_events_todo ON todo_events (tenant_id, todo_id, created_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON todo_events TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON todo_events TO edp_platform",
    # 会话后解析（设计文档 §24.4）：每个会话解析一次。
    """
    CREATE TABLE todo_extractions (
      session_id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      status varchar(8) NOT NULL,
      created integer NOT NULL DEFAULT 0,
      skipped integer NOT NULL DEFAULT 0,
      extracted_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_todo_extractions_status CHECK (status IN ('done', 'empty', 'failed'))
    )
    """,
    "CREATE INDEX ix_todo_extractions_tenant ON todo_extractions (tenant_id, extracted_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON todo_extractions TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON todo_extractions TO edp_platform",
    # 待办设置：AI 登记频率、会话后解析、待确认再提醒、每日汇总、访客端的服务进度。
    "ALTER TABLE tenant_settings ADD COLUMN todos jsonb NOT NULL DEFAULT '{}'",
]

# 留言并入待办的"留言"类型（与 app/modules/todos/presets.py 的定义一致）。
LEAVE_MESSAGE_TYPE = """
INSERT INTO todo_types (id, tenant_id, code, name, ai_hint, examples, fields, assign_rule,
                        priority, sla_resolve_days, promise_text, done_template, preset, sort)
SELECT gen_random_uuid(), t.id, 'leave_message', '留言',
       '客户留下需要稍后联系或处理的问题，例如非工作时间、客服不在线时的留言。',
       ARRAY['现在没人吗，有空回我一下', '麻烦明天联系我'],
       '[{"key": "contact", "label": "联系方式", "type": "text", "required": false,
          "sensitive": false}]'::jsonb,
       '{"steps": ["owner", "channel_group"], "group_mode": "pool"}'::jsonb,
       'normal', 1,
       '已为您记录留言，客服确认后会尽快联系您。',
       '您好，您的留言我们已经处理：{result}',
       true, 20
FROM tenants t
"""

# 留言（tickets）复制为"留言"类型的待办：保留原来的 ID、状态、处理人和时间。
COPY_TICKETS = f"""
INSERT INTO todos (id, tenant_id, no, type_id, title, detail, fields, customer_id, session_id,
                   source, priority, status, assignee_id, skill_group_id, confirmed_at,
                   closed_at, result, created_by_type, notified_at, created_at, updated_at)
SELECT t.id, t.tenant_id,
       'TD' || to_char(t.created_at AT TIME ZONE '{NUMBER_TZ}', 'YYYYMMDD') || '-'
         || lpad(n::text, greatest(4, length(n::text)), '0'),
       tt.id,
       CASE t.source WHEN 'visitor' THEN '访客留言'
                     WHEN 'off_hours' THEN '非工作时间留言'
                     WHEN 'queue_timeout' THEN '排队超时留言'
                     ELSE 'AI 登记的留言' END,
       t.content,
       CASE WHEN t.contact IS NULL THEN '{{}}'::jsonb
            ELSE jsonb_build_object('contact', t.contact) END,
       t.customer_id, t.session_id,
       CASE t.source WHEN 'visitor' THEN 'visitor' WHEN 'ai' THEN 'ai_chat' ELSE 'rule' END,
       'normal', t.status, t.assignee_id, t.skill_group_id, t.created_at, t.closed_at,
       CASE WHEN t.status = 'done' THEN '已处理' END,
       CASE t.source WHEN 'visitor' THEN 'visitor' WHEN 'ai' THEN 'ai' ELSE 'system' END,
       t.created_at, t.created_at, t.updated_at
FROM (
  SELECT *, row_number() OVER (
           PARTITION BY tenant_id, (created_at AT TIME ZONE '{NUMBER_TZ}')::date
           ORDER BY created_at, id) AS n
  FROM tickets
) t
JOIN todo_types tt ON tt.tenant_id = t.tenant_id AND tt.code = 'leave_message'
"""

TICKET_EVENTS = """
INSERT INTO todo_events (id, tenant_id, todo_id, type, actor_type, payload, created_at)
SELECT gen_random_uuid(), tenant_id, id, 'created', created_by_type,
       '{"migrated_from": "tickets"}'::jsonb, created_at
FROM todos
"""

TICKET_COUNTERS = f"""
INSERT INTO number_counters (tenant_id, scope, day, value)
SELECT tenant_id, 'todo', (created_at AT TIME ZONE '{NUMBER_TZ}')::date, count(*)
FROM tickets GROUP BY 1, 3
"""

# 自定义角色：原来能接待会话（workbench:use）的可以处理留言，升级后同样能查看和处理待办。
# 系统角色的权限以代码为准，不需要迁移。
GRANT_CUSTOM_ROLES = """
UPDATE roles SET permissions = array_cat(permissions, ARRAY['todo:read', 'todo:handle'])
WHERE NOT is_system AND 'workbench:use' = ANY(permissions)
  AND NOT ('todo:read' = ANY(permissions))
"""

TICKETS_TABLE = """
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
  CONSTRAINT ck_tickets_source CHECK (source IN ('queue_timeout', 'off_hours', 'visitor', 'ai'))
)
"""

# 降级：留言类型的待办（有客户的）复制回留言。
RESTORE_TICKETS = """
INSERT INTO tickets (id, tenant_id, customer_id, session_id, source, content, contact, status,
                     assignee_id, skill_group_id, closed_at, created_at, updated_at)
SELECT d.id, d.tenant_id, d.customer_id, d.session_id,
       CASE WHEN d.source = 'visitor' THEN 'visitor'
            WHEN d.source IN ('ai_chat', 'ai_summary', 'zone') THEN 'ai'
            WHEN d.title = '排队超时留言' THEN 'queue_timeout'
            ELSE 'off_hours' END,
       d.detail, left(d.fields ->> 'contact', 128),
       CASE WHEN d.status IN ('done', 'cancelled', 'rejected') THEN 'done' ELSE 'open' END,
       d.assignee_id, d.skill_group_id, d.closed_at, d.created_at, d.updated_at
FROM todos d JOIN todo_types tt ON tt.id = d.type_id
WHERE tt.code = 'leave_message' AND d.customer_id IS NOT NULL
"""


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
    # 迁移以表的所有者执行、没有租户上下文：强制行级安全下所有者读不到留言，先取消强制
    # （留言表最后删除）。新表在复制完数据之后才启用行级安全。
    op.execute("ALTER TABLE tickets NO FORCE ROW LEVEL SECURITY")
    for statement in STATEMENTS:
        op.execute(statement)
    op.execute(LEAVE_MESSAGE_TYPE)
    op.execute(COPY_TICKETS)
    op.execute(TICKET_EVENTS)
    op.execute(TICKET_COUNTERS)
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute(GRANT_CUSTOM_ROLES)
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")
    for table in TENANT_TABLES:
        for statement in _rls(table):
            op.execute(statement)
    op.execute("DROP TABLE tickets")


def downgrade() -> None:
    op.execute(TICKETS_TABLE)
    op.execute(
        "CREATE INDEX ix_tickets_tenant_id_status ON tickets (tenant_id, status, created_at)"
    )
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tickets TO edp_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON tickets TO edp_platform")
    for table in ("todos", "todo_types"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(RESTORE_TICKETS)
    for statement in _rls("tickets"):
        op.execute(statement)
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE roles SET permissions = array(SELECT p FROM unnest(permissions) AS p"
        " WHERE p NOT LIKE 'todo:%') WHERE NOT is_system"
    )
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_settings DROP COLUMN todos")
    op.execute("DROP TABLE todo_extractions, todo_events, todos, todo_types, number_counters")
