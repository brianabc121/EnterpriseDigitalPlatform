"""Partition large tables: messages by month of sent_at, kb_chunks by tenant hash

- messages: RANGE (sent_at), one partition per month (messages_yYYYYmMM), a messages_history
  partition for rows older than 24 months and a messages_default partition as a safety net.
  Unique keys include sent_at (PostgreSQL requires the partition key in them):
  OpenIM callbacks and reconcile carry the same send time, WeCom messages are checked by msgid
  before insert, and API sends take an advisory lock on client_msg_id.
  edp_ensure_message_partitions(n) creates the partitions for this month and the next n
  months (moving rows that landed in the default partition); the scheduler calls it hourly.
- kb_chunks: HASH (tenant_id), 8 partitions; queries always filter by tenant.
- Every partition gets the same forced RLS policies as its parent. Queries go through the parent
  (whose policies apply); the partitions' own policies only matter if a partition is named
  directly, which the app role has no privileges for anyway.

The migration copies the existing rows (the tables are locked meanwhile). It runs as the table
owner, which in production is not a superuser: under FORCE ROW LEVEL SECURITY the owner only sees
the current tenant's rows (none during a migration). So the copied tables and the tables the new
foreign keys reference stop forcing RLS while rows are copied and foreign keys validated, and force
it again at the end.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-04
"""

from collections.abc import Sequence
from datetime import UTC, date, datetime

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KB_PARTITIONS = 8
MONTHS_AHEAD = 3
HISTORY_MONTHS = 24

MESSAGE_COLUMNS = (
    "id, tenant_id, room_id, channel_account_id, direction, sender_type, sender_id,"
    " content_type, content, text_plain, channel_msg_id, client_msg_id, im_seq, source,"
    " sent_at, created_at, updated_at, session_id, send_status, send_error, ext_msg_id"
)
KB_COLUMNS = "id, tenant_id, item_id, kind, text, terms, embedding, created_at"

MESSAGE_TABLE = """
CREATE TABLE messages (
  id uuid NOT NULL,
  tenant_id uuid NOT NULL DEFAULT app_current_tenant(),
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
  session_id uuid,
  send_status varchar(16),
  send_error text,
  ext_msg_id varchar(128),
  CONSTRAINT ck_messages_direction CHECK (direction IN ('in', 'out')),
  CONSTRAINT ck_messages_sender_type
    CHECK (sender_type IN ('customer', 'agent', 'bot', 'system')),
  CONSTRAINT ck_messages_source CHECK (source IN ('webhook', 'reconcile', 'api', 'channel')),
  CONSTRAINT ck_messages_send_status CHECK (send_status IN ('pending', 'sent', 'failed'))
)
"""

MESSAGE_CONSTRAINTS = [
    "ALTER TABLE messages ADD CONSTRAINT messages_pkey PRIMARY KEY (id, sent_at)",
    "ALTER TABLE messages ADD CONSTRAINT uq_messages_channel_msg"
    " UNIQUE (tenant_id, channel_account_id, channel_msg_id, sent_at)",
    "ALTER TABLE messages ADD CONSTRAINT uq_messages_ext_msg"
    " UNIQUE (tenant_id, channel_account_id, ext_msg_id, sent_at)",
    "ALTER TABLE messages ADD CONSTRAINT messages_tenant_id_fkey"
    " FOREIGN KEY (tenant_id) REFERENCES tenants (id)",
    "ALTER TABLE messages ADD CONSTRAINT fk_messages_room FOREIGN KEY (tenant_id, room_id)"
    " REFERENCES rooms (tenant_id, id) ON DELETE CASCADE",
    "ALTER TABLE messages ADD CONSTRAINT fk_messages_channel"
    " FOREIGN KEY (tenant_id, channel_account_id) REFERENCES channel_accounts (tenant_id, id)",
    "ALTER TABLE messages ADD CONSTRAINT fk_messages_session FOREIGN KEY (tenant_id, session_id)"
    " REFERENCES sessions (tenant_id, id) ON DELETE SET NULL (session_id)",
    "CREATE INDEX ix_messages_tenant_id_room_id_sent_at ON messages (tenant_id, room_id, sent_at)",
    "CREATE INDEX ix_messages_tenant_id_sent_at ON messages (tenant_id, sent_at)",
    "CREATE INDEX ix_messages_tenant_id_session_id ON messages (tenant_id, session_id)",
    # 坐席重试发送时按 client_msg_id 幂等（并发由咨询锁保证，这里兜底完全相同的重复）。
    "CREATE UNIQUE INDEX uq_messages_api_client_msg"
    " ON messages (tenant_id, room_id, sender_id, client_msg_id, sent_at) WHERE source = 'api'",
    """
    CREATE INDEX ix_messages_unscanned ON messages (tenant_id, sent_at)
      WHERE content_type IN ('image', 'file', 'voice', 'video') AND NOT (content ? 'scan')
    """,
    "GRANT SELECT, INSERT, UPDATE ON messages TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON messages TO edp_platform",
]

# 建本月和之后 months_ahead 个月的分区。之前落进默认分区的行搬到新分区。
# SECURITY DEFINER：调度进程用 edp_platform 连接调用，建表需要表的所有者权限。
ENSURE_PARTITIONS = """
CREATE OR REPLACE FUNCTION edp_ensure_message_partitions(months_ahead integer)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
  first_month timestamp := date_trunc('month', now() AT TIME ZONE 'UTC');
  lower_bound timestamptz;
  upper_bound timestamptz;
  partition_name text;
  fk record;
  created integer := 0;
BEGIN
  FOR i IN 0..months_ahead LOOP
    lower_bound := (first_month + make_interval(months => i)) AT TIME ZONE 'UTC';
    upper_bound := (first_month + make_interval(months => i + 1)) AT TIME ZONE 'UTC';
    partition_name := 'messages_' || to_char(lower_bound AT TIME ZONE 'UTC', '"y"YYYY"m"MM');
    IF to_regclass(partition_name) IS NULL THEN
      EXECUTE format(
        'CREATE TABLE %I (LIKE messages INCLUDING DEFAULTS INCLUDING CONSTRAINTS)',
        partition_name
      );
      -- 外键在搬行之前建好：搬进来的行逐行检查（不受被引用表的强制行级安全影响），挂载分区时
      -- 沿用这些外键，不再整表校验（整表校验时表的所有者看不到被引用表里其他租户的行）。
      FOR fk IN
        SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint WHERE conrelid = 'messages'::regclass AND contype = 'f'
      LOOP
        EXECUTE format(
          'ALTER TABLE %I ADD CONSTRAINT %I %s', partition_name, fk.conname, fk.definition
        );
      END LOOP;
      -- 搬行时默认分区暂不强制行级安全（表的所有者跨租户读写），同一事务内恢复。
      ALTER TABLE messages_default NO FORCE ROW LEVEL SECURITY;
      EXECUTE format(
        'WITH moved AS (DELETE FROM messages_default WHERE sent_at >= %L AND sent_at < %L'
        ' RETURNING *) INSERT INTO %I SELECT * FROM moved',
        lower_bound, upper_bound, partition_name
      );
      ALTER TABLE messages_default FORCE ROW LEVEL SECURITY;
      EXECUTE format(
        'ALTER TABLE messages ATTACH PARTITION %I FOR VALUES FROM (%L) TO (%L)',
        partition_name, lower_bound, upper_bound
      );
      PERFORM edp_protect_partition(partition_name);
      created := created + 1;
    END IF;
  END LOOP;
  RETURN created;
END
$$
"""

# 分区与父表相同的强制行级安全策略（直接访问分区时生效）。
PROTECT_PARTITION = """
CREATE OR REPLACE FUNCTION edp_protect_partition(partition_name text)
RETURNS void
LANGUAGE plpgsql
SET search_path = public, pg_temp
AS $$
BEGIN
  EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', partition_name);
  EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', partition_name);
  EXECUTE format(
    'CREATE POLICY tenant_isolation ON %I USING (tenant_id = app_current_tenant())'
    ' WITH CHECK (tenant_id = app_current_tenant())',
    partition_name
  );
  EXECUTE format(
    'CREATE POLICY platform_access ON %I TO edp_platform USING (true) WITH CHECK (true)',
    partition_name
  );
END
$$
"""

KB_TABLE = """
CREATE TABLE kb_chunks (
  id uuid NOT NULL,
  tenant_id uuid NOT NULL DEFAULT app_current_tenant(),
  item_id uuid NOT NULL,
  kind varchar(12) NOT NULL,
  text text NOT NULL,
  terms text[] NOT NULL DEFAULT '{}',
  embedding vector(1024),
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_kb_chunks_kind CHECK (kind IN ('question', 'passage'))
) PARTITION BY HASH (tenant_id)
"""

KB_CONSTRAINTS = [
    "ALTER TABLE kb_chunks ADD CONSTRAINT kb_chunks_pkey PRIMARY KEY (tenant_id, id)",
    "ALTER TABLE kb_chunks ADD CONSTRAINT kb_chunks_tenant_id_fkey"
    " FOREIGN KEY (tenant_id) REFERENCES tenants (id)",
    "ALTER TABLE kb_chunks ADD CONSTRAINT fk_kb_chunks_item FOREIGN KEY (tenant_id, item_id)"
    " REFERENCES kb_items (tenant_id, id) ON DELETE CASCADE",
    "CREATE INDEX ix_kb_chunks_item ON kb_chunks (tenant_id, item_id)",
    "CREATE INDEX ix_kb_chunks_terms ON kb_chunks USING gin (terms)",
    "CREATE INDEX ix_kb_chunks_embedding ON kb_chunks USING hnsw (embedding vector_cosine_ops)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON kb_chunks TO edp_app, edp_platform",
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


def _month(value: date, offset: int) -> date:
    index = value.year * 12 + value.month - 1 + offset
    return date(index // 12, index % 12 + 1, 1)


def _utc(value: date) -> str:
    return f"{value.isoformat()} 00:00:00+00"


def _message_partitions(oldest: datetime | None) -> list[str]:
    """历史分区和已有数据所在月份到上个月的分区（本月及之后由 edp_ensure_message_partitions 建）。

    24 个月以前的数据都放进 messages_history。
    """
    today = datetime.now(UTC).date()
    current = date(today.year, today.month, 1)
    first = current
    if oldest is not None:
        first = min(current, max(_month(current, -HISTORY_MONTHS), _month_of(oldest)))
    statements = [
        "CREATE TABLE messages_history PARTITION OF messages"
        f" FOR VALUES FROM (MINVALUE) TO ('{_utc(first)}')"
    ]
    month = first
    while month < current:
        upper = _month(month, 1)
        statements.append(
            f"CREATE TABLE messages_y{month.year}m{month.month:02d} PARTITION OF messages"
            f" FOR VALUES FROM ('{_utc(month)}') TO ('{_utc(upper)}')"
        )
        month = upper
    return statements


def _month_of(value: datetime) -> date:
    utc = value.astimezone(UTC)
    return date(utc.year, utc.month, 1)


# 新表的外键引用的租户表（强制行级安全）。
REFERENCED = ("rooms", "channel_accounts", "sessions", "kb_items")


def _force_rls(*tables: str, force: bool = True) -> None:
    """取消强制时，表的所有者（执行迁移的账号）能读到所有租户的行：整表复制、校验外键都需要。"""
    for table in tables:
        op.execute(f"ALTER TABLE {table} {'' if force else 'NO '}FORCE ROW LEVEL SECURITY")


def upgrade() -> None:
    _force_rls("messages", "kb_chunks", *REFERENCED, force=False)
    bind = op.get_bind()
    oldest = bind.exec_driver_sql("SELECT min(sent_at) FROM messages").scalar()

    op.execute("ALTER TABLE messages RENAME TO messages_unpartitioned")
    op.execute(MESSAGE_TABLE.rstrip() + " PARTITION BY RANGE (sent_at)")
    op.execute("CREATE TABLE messages_default PARTITION OF messages DEFAULT")
    for statement in _message_partitions(oldest):
        op.execute(statement)
    op.execute(PROTECT_PARTITION)
    op.execute("REVOKE ALL ON FUNCTION edp_protect_partition(text) FROM PUBLIC")
    _protect_partitions("messages")
    op.execute(ENSURE_PARTITIONS)
    op.execute(f"SELECT edp_ensure_message_partitions({MONTHS_AHEAD})")
    op.execute(
        f"INSERT INTO messages ({MESSAGE_COLUMNS})"
        f" SELECT {MESSAGE_COLUMNS} FROM messages_unpartitioned"
    )
    op.execute("DROP TABLE messages_unpartitioned")
    for statement in MESSAGE_CONSTRAINTS:
        op.execute(statement)
    for statement in _rls("messages"):
        op.execute(statement)
    # 系统健康与指标读取默认分区里的行数（正常时为空）。
    op.execute("GRANT SELECT ON messages_default TO edp_platform")
    op.execute("REVOKE ALL ON FUNCTION edp_ensure_message_partitions(integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION edp_ensure_message_partitions(integer) TO edp_platform")

    op.execute("ALTER TABLE kb_chunks RENAME TO kb_chunks_unpartitioned")
    op.execute(KB_TABLE)
    for remainder in range(KB_PARTITIONS):
        op.execute(
            f"CREATE TABLE kb_chunks_p{remainder} PARTITION OF kb_chunks"
            f" FOR VALUES WITH (MODULUS {KB_PARTITIONS}, REMAINDER {remainder})"
        )
    op.execute(
        f"INSERT INTO kb_chunks ({KB_COLUMNS}) SELECT {KB_COLUMNS} FROM kb_chunks_unpartitioned"
    )
    op.execute("DROP TABLE kb_chunks_unpartitioned")
    for statement in KB_CONSTRAINTS:
        op.execute(statement)
    for statement in _rls("kb_chunks"):
        op.execute(statement)
    _protect_partitions("kb_chunks")
    _force_rls(*REFERENCED)


def _protect_partitions(parent: str) -> None:
    """给已有分区加上与父表相同的强制行级安全策略。"""
    op.execute(
        f"""
        SELECT edp_protect_partition(c.relname)
        FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
        WHERE i.inhparent = '{parent}'::regclass
        """
    )


def downgrade() -> None:
    _force_rls("messages", "kb_chunks", *REFERENCED, force=False)
    op.execute("ALTER TABLE kb_chunks RENAME TO kb_chunks_partitioned")
    op.execute(KB_TABLE.replace(" PARTITION BY HASH (tenant_id)", ""))
    op.execute(
        f"INSERT INTO kb_chunks ({KB_COLUMNS}) SELECT {KB_COLUMNS} FROM kb_chunks_partitioned"
    )
    op.execute("DROP TABLE kb_chunks_partitioned")
    for statement in KB_CONSTRAINTS:
        op.execute(statement.replace("PRIMARY KEY (tenant_id, id)", "PRIMARY KEY (id)"))
    for statement in _rls("kb_chunks"):
        op.execute(statement)

    op.execute("DROP FUNCTION IF EXISTS edp_ensure_message_partitions(integer)")
    op.execute("DROP FUNCTION IF EXISTS edp_protect_partition(text)")
    op.execute("ALTER TABLE messages RENAME TO messages_partitioned")
    op.execute(MESSAGE_TABLE)
    op.execute(
        f"INSERT INTO messages ({MESSAGE_COLUMNS})"
        f" SELECT {MESSAGE_COLUMNS} FROM messages_partitioned"
    )
    op.execute("DROP TABLE messages_partitioned")
    for statement in MESSAGE_CONSTRAINTS:
        op.execute(
            statement.replace("PRIMARY KEY (id, sent_at)", "PRIMARY KEY (id)")
            .replace("channel_msg_id, sent_at)", "channel_msg_id)")
            .replace("ext_msg_id, sent_at)", "ext_msg_id)")
            .replace("client_msg_id, sent_at)", "client_msg_id)")
        )
    for statement in _rls("messages"):
        op.execute(statement)
    _force_rls(*REFERENCED)
