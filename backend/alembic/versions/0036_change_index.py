"""Per-tenant data change index (design §33.9): change_seq on tenant data, tenant_data_index

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 不记变化的表（与 app/modules/changes/service.py 的 EXCLUDED 一致）：index 本身、缓存和派生数据、
# 队列和发件箱、处理进度、计数器、登录令牌、在线状态、调用日志，以及 AI 唤醒自己的记录。
EXCLUDED = (
    "tenant_data_index",
    "ai_answer_cache",
    "kb_chunks",
    "usage_daily",
    "im_ops",
    "webhook_events",
    "webhook_deliveries",
    "file_scans",
    "kb_extractions",
    "todo_extractions",
    "ai_session_states",
    "number_counters",
    "refresh_tokens",
    "agent_states",
    "llm_calls",
    "wake_runs",
    "wake_findings",
    "wake_check_state",
    "kb_align_marks",
)
# 只有这些列变化时不算数据变化：命中次数、评价等计数，收信、打印机状态、企业微信同步的轮询时间和
# 进度，访客、成员最近出现的时间。
IGNORED_COLUMNS = {
    "kb_items": (
        "hits",
        "last_hit_at",
        "likes",
        "dislikes",
        "visitor_likes",
        "visitor_dislikes",
        "expiry_notified_at",
    ),
    "mail_accounts": ("last_polled_at", "next_poll_at"),
    "printers": ("status_checked_at",),
    "rooms": ("last_active_at",),
    "customer_identities": ("last_seen_at",),
    "assistant_identities": ("last_seen_at",),
    "wecom_kf_accounts": ("cursor", "synced_at"),
    "wecom_members": ("synced_at",),
    "wecom_tags": ("synced_at",),
    "wecom_group_chats": ("synced_at",),
    "wecom_broadcasts": ("polled_at",),
}

FUNCTIONS = [
    # 全库递增的变化编号：取号不加锁，不同事务互不等待。
    "CREATE SEQUENCE data_change_seq",
    "GRANT USAGE, SELECT ON SEQUENCE data_change_seq TO edp_app, edp_platform",
    # 每个企业、每张表最近一次变化的编号和时间（增量更新 index 表）。
    """
    CREATE TABLE tenant_data_index (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      domain varchar(63) NOT NULL,
      seq bigint NOT NULL,
      changed_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant_id, domain)
    )
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON tenant_data_index TO edp_app, edp_platform",
    "ALTER TABLE tenant_data_index ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE tenant_data_index FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY tenant_isolation ON tenant_data_index
      USING (tenant_id = app_current_tenant())
      WITH CHECK (tenant_id = app_current_tenant())
    """,
    "CREATE POLICY platform_access ON tenant_data_index TO edp_platform"
    " USING (true) WITH CHECK (true)",
    # 新增、修改时给这一行写上新的变化编号；参数里的列只有它们变化时保留原来的编号。
    """
    CREATE FUNCTION edp_stamp_change() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'UPDATE' AND TG_NARGS > 0 THEN
        IF (to_jsonb(NEW) - TG_ARGV - 'change_seq' - 'updated_at')
           = (to_jsonb(OLD) - TG_ARGV - 'change_seq' - 'updated_at') THEN
          NEW.change_seq := OLD.change_seq;
          RETURN NEW;
        END IF;
      END IF;
      NEW.change_seq := nextval('data_change_seq');
      RETURN NEW;
    END
    $$
    """,
    # 提交时更新 index 表（延迟触发）：同一个事务对同一个企业的同一张表只更新一次；先取这个企业的
    # 事务锁，同一企业同时提交的事务不会互相死锁；出错只记警告，不影响业务数据的保存。
    # 设置了 edp.data_index_off（删除整个租户的数据时）不更新。
    """
    CREATE FUNCTION edp_touch_data_index() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE
      tenant uuid;
      key text;
      touched text;
    BEGIN
      IF coalesce(current_setting('edp.data_index_off', true), '') = 'on' THEN
        RETURN NULL;
      END IF;
      IF TG_OP = 'DELETE' THEN
        tenant := OLD.tenant_id;
      ELSE
        tenant := NEW.tenant_id;
        IF TG_OP = 'UPDATE' AND NEW.change_seq IS NOT DISTINCT FROM OLD.change_seq THEN
          RETURN NULL;
        END IF;
      END IF;
      IF tenant IS NULL THEN
        RETURN NULL;
      END IF;
      key := tenant::text || ':' || TG_ARGV[0] || ';';
      touched := coalesce(current_setting('edp.data_index', true), '');
      IF position(key IN touched) > 0 THEN
        RETURN NULL;
      END IF;
      PERFORM set_config('edp.data_index', touched || key, true);
      BEGIN
        PERFORM pg_advisory_xact_lock(1010, hashtext(tenant::text));
        INSERT INTO tenant_data_index AS i (tenant_id, domain, seq, changed_at)
        VALUES (tenant, TG_ARGV[0], nextval('data_change_seq'), clock_timestamp())
        ON CONFLICT (tenant_id, domain) DO UPDATE
          SET seq = EXCLUDED.seq, changed_at = EXCLUDED.changed_at;
      EXCEPTION WHEN OTHERS THEN
        RAISE WARNING 'tenant_data_index % % not updated: %', tenant, TG_ARGV[0], SQLERRM;
      END;
      RETURN NULL;
    END
    $$
    """,
    # 给一张企业数据表加上增量字段、索引和两个触发器（以后新建的表也调用它）。
    """
    CREATE FUNCTION edp_track_changes(tbl regclass, ignored text[] DEFAULT '{}')
      RETURNS void LANGUAGE plpgsql AS $$
    DECLARE
      name text := (SELECT relname FROM pg_class WHERE oid = tbl);
      args text := (SELECT coalesce(string_agg(quote_literal(c), ', '), '') FROM unnest(ignored) c);
    BEGIN
      EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS change_seq bigint', tbl);
      EXECUTE format(
        'CREATE INDEX IF NOT EXISTS %I ON %s (tenant_id, change_seq)',
        'ix_' || name || '_change_seq', tbl
      );
      EXECUTE format(
        'CREATE TRIGGER edp_stamp_change BEFORE INSERT OR UPDATE ON %s'
        ' FOR EACH ROW EXECUTE FUNCTION edp_stamp_change(%s)', tbl, args
      );
      EXECUTE format(
        'CREATE CONSTRAINT TRIGGER edp_data_index AFTER INSERT OR UPDATE OR DELETE ON %s'
        ' DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION edp_touch_data_index(%L)',
        tbl, name
      );
    END
    $$
    """,
    """
    CREATE FUNCTION edp_untrack_changes(tbl regclass) RETURNS void LANGUAGE plpgsql AS $$
    DECLARE
      name text := (SELECT relname FROM pg_class WHERE oid = tbl);
    BEGIN
      EXECUTE format('DROP TRIGGER IF EXISTS edp_data_index ON %s', tbl);
      EXECUTE format('DROP TRIGGER IF EXISTS edp_stamp_change ON %s', tbl);
      EXECUTE format('DROP INDEX IF EXISTS %I', 'ix_' || name || '_change_seq');
      EXECUTE format('ALTER TABLE %s DROP COLUMN IF EXISTS change_seq', tbl);
    END
    $$
    """,
]

# 消息分区维护（0017）：从默认分区搬行时删除的行排队了延迟执行的 index 更新，排队的触发事件没执行完
# 时不能 ALTER TABLE 默认分区。搬完先让它们立即执行，再恢复延迟。没有这个触发器（降级后）时
# 照常执行。
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
      FOR fk IN
        SELECT conname, pg_get_constraintdef(oid) AS definition
        FROM pg_constraint WHERE conrelid = 'messages'::regclass AND contype = 'f'
      LOOP
        EXECUTE format(
          'ALTER TABLE %I ADD CONSTRAINT %I %s', partition_name, fk.conname, fk.definition
        );
      END LOOP;
      ALTER TABLE messages_default NO FORCE ROW LEVEL SECURITY;
      EXECUTE format(
        'WITH moved AS (DELETE FROM messages_default WHERE sent_at >= %L AND sent_at < %L'
        ' RETURNING *) INSERT INTO %I SELECT * FROM moved',
        lower_bound, upper_bound, partition_name
      );
      IF EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'edp_data_index') THEN
        SET CONSTRAINTS edp_data_index IMMEDIATE;
        SET CONSTRAINTS edp_data_index DEFERRED;
      END IF;
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

# 带 tenant_id 的表（分区表只取父表）。
TENANT_TABLES_SQL = """
SELECT c.relname
  FROM pg_class c
  JOIN pg_namespace n ON n.oid = c.relnamespace
  JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped
 WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
 ORDER BY c.relname
"""


def _tracked() -> list[str]:
    rows = op.get_bind().execute(text(TENANT_TABLES_SQL)).all()
    return [name for (name,) in rows if name not in EXCLUDED]


def upgrade() -> None:
    for statement in FUNCTIONS:
        op.execute(statement)
    op.execute(ENSURE_PARTITIONS)
    for table in _tracked():
        ignored = IGNORED_COLUMNS.get(table, ())
        array = "ARRAY[" + ", ".join(f"'{c}'" for c in ignored) + "]::text[]" if ignored else "'{}'"
        op.execute(f"SELECT edp_track_changes('\"{table}\"'::regclass, {array})")


def downgrade() -> None:
    for table in _tracked():
        op.execute(f"SELECT edp_untrack_changes('\"{table}\"'::regclass)")
    op.execute("DROP FUNCTION edp_untrack_changes(regclass)")
    op.execute("DROP FUNCTION edp_track_changes(regclass, text[])")
    op.execute("DROP FUNCTION edp_touch_data_index()")
    op.execute("DROP FUNCTION edp_stamp_change()")
    op.execute("DROP TABLE tenant_data_index")
    op.execute("DROP SEQUENCE data_change_seq")
