"""AI wake-up (design §33): wake runs, findings, policy knowledge and alignment marks

Revision ID: 0035
Revises: 0034
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("wake_runs", "wake_findings", "wake_check_state", "kb_align_marks")

CANDIDATE_KINDS = "'new', 'similar', 'conflict', 'gap', 'phrase'"
CANDIDATE_SOURCES = "'session', 'sidebar', 'zone', 'group'"

STATEMENTS = [
    # 租户的唤醒设置（wake/settings.py 的 WakeSettings）。
    "ALTER TABLE tenant_settings ADD COLUMN wake jsonb NOT NULL DEFAULT '{}'",
    # 每次唤醒：同一类型、同一时段只唤醒一次（定时的时段是小时、日期或周，其他触发是随机串）。
    """
    CREATE TABLE wake_runs (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      kind varchar(8) NOT NULL,
      trigger varchar(12) NOT NULL,
      slot varchar(40) NOT NULL,
      status varchar(12) NOT NULL DEFAULT 'queued',
      not_before timestamptz NOT NULL DEFAULT now(),
      lease_until timestamptz,
      attempts smallint NOT NULL DEFAULT 0,
      started_at timestamptz,
      finished_at timestamptz,
      stats jsonb NOT NULL DEFAULT '{}',
      summary text,
      error text,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      CONSTRAINT uq_wake_runs_slot UNIQUE (tenant_id, kind, slot),
      CONSTRAINT ck_wake_runs_kind CHECK (kind IN ('hourly', 'daily', 'kb')),
      CONSTRAINT ck_wake_runs_trigger
        CHECK (trigger IN ('schedule', 'event', 'manual', 'continue')),
      CONSTRAINT ck_wake_runs_status
        CHECK (status IN ('queued', 'running', 'done', 'failed', 'skipped'))
    )
    """,
    # 实时消费进程跨租户领取排队的唤醒；页面按时间倒序列出。
    "CREATE INDEX ix_wake_runs_queued ON wake_runs (not_before) WHERE status = 'queued'",
    "CREATE INDEX ix_wake_runs_running ON wake_runs (lease_until) WHERE status = 'running'",
    "CREATE INDEX ix_wake_runs_created ON wake_runs (tenant_id, created_at DESC)",
    # 巡检发现的问题：同一个检查项、同一个对象只有一条（fingerprint）。
    """
    CREATE TABLE wake_findings (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      check_code varchar(32) NOT NULL,
      fingerprint varchar(160) NOT NULL,
      category varchar(16) NOT NULL,
      severity varchar(8) NOT NULL,
      status varchar(8) NOT NULL DEFAULT 'open',
      title text NOT NULL,
      detail text,
      link text,
      entity_type varchar(16),
      entity_id uuid,
      data jsonb NOT NULL DEFAULT '{}',
      assignee_ids uuid[] NOT NULL DEFAULT '{}',
      first_seen_at timestamptz NOT NULL DEFAULT now(),
      last_seen_at timestamptz NOT NULL DEFAULT now(),
      seen_count integer NOT NULL DEFAULT 1,
      notified_at timestamptz,
      notified_severity varchar(8),
      escalated_at timestamptz,
      resolved_at timestamptz,
      resolved_by uuid,
      resolve_note text,
      ignored_by uuid,
      ignored_until timestamptz,
      ignore_note text,
      run_id uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      UNIQUE (tenant_id, id),
      CONSTRAINT uq_wake_findings_fingerprint UNIQUE (tenant_id, fingerprint),
      CONSTRAINT ck_wake_findings_severity CHECK (severity IN ('info', 'warning', 'critical')),
      CONSTRAINT ck_wake_findings_status CHECK (status IN ('open', 'ignored', 'resolved'))
    )
    """,
    "CREATE INDEX ix_wake_findings_status ON wake_findings (tenant_id, status, check_code)",
    "CREATE INDEX ix_wake_findings_assignees ON wake_findings USING gin (assignee_ids)",
    # 每个检查项上次检查时涉及的数据的变化编号（增量更新索引，§33.9）、口径数字和下一个可能出现
    # 新问题的时刻：数据没变、时间也没到时跳过这个检查项。
    """
    CREATE TABLE wake_check_state (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      check_code varchar(32) NOT NULL,
      data_seq bigint NOT NULL DEFAULT 0,
      params varchar(64) NOT NULL DEFAULT '',
      next_due_at timestamptz,
      checked_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant_id, check_code)
    )
    """,
    # 规章制度（§33.7.1）：知识库整理时作为依据。
    "ALTER TABLE kb_items ADD COLUMN policy boolean NOT NULL DEFAULT false",
    "CREATE INDEX ix_kb_items_policy ON kb_items (tenant_id) WHERE policy",
    # 知识库整理的核对记录：一条知识、一段制度、一对重复的知识；签名不变时不再核对。
    """
    CREATE TABLE kb_align_marks (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      key varchar(160) NOT NULL,
      signature varchar(64) NOT NULL,
      verdict varchar(16) NOT NULL,
      candidate_id uuid,
      item_seq bigint,
      policy_sig varchar(40),
      checked_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant_id, key)
    )
    """,
    # 审核台：重复的知识（种类）、制度对齐（来源）。
    "ALTER TABLE kb_candidates DROP CONSTRAINT ck_kb_candidates_kind",
    "ALTER TABLE kb_candidates ADD CONSTRAINT ck_kb_candidates_kind"
    f" CHECK (kind IN ({CANDIDATE_KINDS}, 'duplicate'))",
    "ALTER TABLE kb_candidates DROP CONSTRAINT ck_kb_candidates_source",
    "ALTER TABLE kb_candidates ADD CONSTRAINT ck_kb_candidates_source"
    f" CHECK (source IN ({CANDIDATE_SOURCES}, 'policy'))",
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
    op.execute("DELETE FROM kb_candidates WHERE kind = 'duplicate' OR source = 'policy'")
    op.execute("ALTER TABLE kb_candidates DROP CONSTRAINT ck_kb_candidates_source")
    op.execute(
        "ALTER TABLE kb_candidates ADD CONSTRAINT ck_kb_candidates_source"
        f" CHECK (source IN ({CANDIDATE_SOURCES}))"
    )
    op.execute("ALTER TABLE kb_candidates DROP CONSTRAINT ck_kb_candidates_kind")
    op.execute(
        "ALTER TABLE kb_candidates ADD CONSTRAINT ck_kb_candidates_kind"
        f" CHECK (kind IN ({CANDIDATE_KINDS}))"
    )
    op.execute("DROP INDEX ix_kb_items_policy")
    op.execute("ALTER TABLE kb_items DROP COLUMN policy")
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE tenant_settings DROP COLUMN wake")
