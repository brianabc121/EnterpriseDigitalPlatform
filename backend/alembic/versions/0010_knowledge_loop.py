"""knowledge loop: versions, candidates from conversations, must-read, feedback, digests

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 1024

TENANT_TABLES = (
    "kb_item_versions",
    "kb_candidates",
    "kb_extractions",
    "kb_reads",
    "kb_feedback",
    "kb_digests",
    "ai_suggestions",
)

STATEMENTS = [
    # 必读、点赞与点踩计数（由 kb_feedback 汇总）、下线时间。
    "ALTER TABLE kb_items ADD COLUMN must_read boolean NOT NULL DEFAULT false",
    "ALTER TABLE kb_items ADD COLUMN likes integer NOT NULL DEFAULT 0",
    "ALTER TABLE kb_items ADD COLUMN dislikes integer NOT NULL DEFAULT 0",
    "ALTER TABLE kb_items ADD COLUMN archived_at timestamptz",
    # 知识沉淀开关：自动从会话提炼候选；自动把相似问法并入已有问答（默认关闭，设计 §12.5）。
    "ALTER TABLE ai_settings ADD COLUMN extraction_enabled boolean NOT NULL DEFAULT true",
    "ALTER TABLE ai_settings ADD COLUMN auto_merge_similar boolean NOT NULL DEFAULT false",
    # 每次发布的内容快照（设计 §12.5 版本管理）：可以查看历史、回滚到任意版本；也是知识动态的来源。
    """
    CREATE TABLE kb_item_versions (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      item_id uuid NOT NULL,
      version integer NOT NULL,
      change varchar(16) NOT NULL,
      note text,
      title text NOT NULL,
      content text NOT NULL,
      questions text[] NOT NULL DEFAULT '{}',
      category varchar(64) NOT NULL DEFAULT '',
      tags text[] NOT NULL DEFAULT '{}',
      visibility varchar(16) NOT NULL,
      valid_from timestamptz,
      valid_to timestamptz,
      must_read boolean NOT NULL DEFAULT false,
      published_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_kb_item_versions UNIQUE (tenant_id, item_id, version),
      CONSTRAINT fk_kb_item_versions_item FOREIGN KEY (tenant_id, item_id)
        REFERENCES kb_items (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_kb_item_versions_change
        CHECK (change IN ('created', 'updated', 'restored', 'merged'))
    )
    """,
    "CREATE INDEX ix_kb_item_versions_feed ON kb_item_versions (tenant_id, created_at)",
    # 从会话提炼的候选（设计 §12.4）：新问题、已有问答的相似问法、与已有答案冲突、知识缺口。
    # 同类候选按问题相似度聚类，occurrences 为出现次数；evidence 为脱敏后的证据对话（最多 10 段）。
    f"""
    CREATE TABLE kb_candidates (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      kind varchar(12) NOT NULL,
      status varchar(12) NOT NULL DEFAULT 'pending',
      question text NOT NULL,
      answer text,
      category varchar(64) NOT NULL DEFAULT '',
      target_item_id uuid,
      similarity double precision,
      confidence double precision,
      time_sensitive boolean NOT NULL DEFAULT false,
      occurrences integer NOT NULL DEFAULT 1,
      terms text[] NOT NULL DEFAULT '{{}}',
      embedding vector({EMBED_DIM}),
      evidence jsonb NOT NULL DEFAULT '[]',
      first_seen_at timestamptz NOT NULL DEFAULT now(),
      last_seen_at timestamptz NOT NULL DEFAULT now(),
      model varchar(128),
      prompt_version varchar(16),
      review_note text,
      reviewed_by uuid,
      reviewed_at timestamptz,
      result_item_id uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_kb_candidates_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT fk_kb_candidates_target FOREIGN KEY (tenant_id, target_item_id)
        REFERENCES kb_items (tenant_id, id) ON DELETE SET NULL (target_item_id),
      CONSTRAINT ck_kb_candidates_kind CHECK (kind IN ('new', 'similar', 'conflict', 'gap')),
      CONSTRAINT ck_kb_candidates_status
        CHECK (status IN ('pending', 'approved', 'merged', 'rejected'))
    )
    """,
    "CREATE INDEX ix_kb_candidates_queue ON kb_candidates (tenant_id, status, kind, occurrences)",
    "CREATE INDEX ix_kb_candidates_terms ON kb_candidates USING gin (terms)",
    # 会话的提炼记录：保证每个会话只提炼一次，失败的最多重试 3 次。
    """
    CREATE TABLE kb_extractions (
      session_id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      status varchar(12) NOT NULL,
      pairs integer NOT NULL DEFAULT 0,
      gaps integer NOT NULL DEFAULT 0,
      attempts integer NOT NULL DEFAULT 1,
      error text,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_kb_extractions_session FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_kb_extractions_status CHECK (status IN ('done', 'skipped', 'failed'))
    )
    """,
    # 必读确认：按版本记录，知识更新后需要重新确认（设计 §12.6）。
    """
    CREATE TABLE kb_reads (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      item_id uuid NOT NULL,
      version integer NOT NULL,
      staff_id uuid NOT NULL,
      read_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant_id, item_id, version, staff_id),
      CONSTRAINT fk_kb_reads_item FOREIGN KEY (tenant_id, item_id)
        REFERENCES kb_items (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_kb_reads_staff FOREIGN KEY (tenant_id, staff_id)
        REFERENCES staff (tenant_id, id) ON DELETE CASCADE
    )
    """,
    # 员工对知识的评价：有用（1）/ 没用（-1），每人每条一票（设计 §12.7）。
    """
    CREATE TABLE kb_feedback (
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      item_id uuid NOT NULL,
      staff_id uuid NOT NULL,
      value smallint NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (tenant_id, item_id, staff_id),
      CONSTRAINT fk_kb_feedback_item FOREIGN KEY (tenant_id, item_id)
        REFERENCES kb_items (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_kb_feedback_staff FOREIGN KEY (tenant_id, staff_id)
        REFERENCES staff (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_kb_feedback_value CHECK (value IN (-1, 1))
    )
    """,
    # 知识周报（设计 §12.6）：每周一汇总上一周。
    """
    CREATE TABLE kb_digests (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      week_start date NOT NULL,
      data jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_kb_digests_week UNIQUE (tenant_id, week_start)
    )
    """,
    # 坐席助手的每次建议（用于统计采纳率）。
    """
    CREATE TABLE ai_suggestions (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      session_id uuid NOT NULL,
      staff_id uuid,
      suggestions jsonb NOT NULL DEFAULT '[]',
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_ai_suggestions_session FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_ai_suggestions_created ON ai_suggestions (tenant_id, created_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON kb_item_versions, kb_candidates, kb_extractions,"
    " kb_reads, kb_feedback, kb_digests, ai_suggestions TO edp_app, edp_platform",
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
        "DROP TABLE IF EXISTS ai_suggestions, kb_digests, kb_feedback, kb_reads, kb_extractions,"
        " kb_candidates, kb_item_versions"
    )
    op.execute(
        "ALTER TABLE ai_settings DROP COLUMN auto_merge_similar, DROP COLUMN extraction_enabled"
    )
    op.execute(
        "ALTER TABLE kb_items DROP COLUMN archived_at, DROP COLUMN dislikes, DROP COLUMN likes,"
        " DROP COLUMN must_read"
    )
