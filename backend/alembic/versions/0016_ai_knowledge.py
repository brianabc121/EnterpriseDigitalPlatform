"""AI and knowledge enhancements: prompt versions, answer cache, cost and concurrency, AI tools,
per-channel AI settings, visitor ratings, Copilot alerts and session summaries, knowledge spaces,
categories, owners and audiences, import jobs, staff notifications, phrase candidates

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 1024

TENANT_TABLES = (
    "ai_answer_cache",
    "customer_lead_drafts",
    "ai_message_feedback",
    "copilot_alerts",
    "session_summaries",
    "kb_spaces",
    "kb_categories",
    "kb_import_jobs",
    "staff_notifications",
)

STATEMENTS = [
    # ---- 大模型网关（设计文档 §11.5） ----
    # 供应商：重排序模型、能力标签（工具调用、JSON Schema、上下文长度）。
    "ALTER TABLE llm_providers ADD COLUMN rerank_model varchar(128) NOT NULL DEFAULT ''",
    "ALTER TABLE llm_providers ADD COLUMN capabilities jsonb NOT NULL DEFAULT '{}'",
    # 调用记账：估算费用（分）和使用的提示词版本。
    "ALTER TABLE llm_calls ADD COLUMN cost double precision NOT NULL DEFAULT 0",
    "ALTER TABLE llm_calls ADD COLUMN prompt_version varchar(40)",
    # 提示词版本（平台级）：每个场景最多一个启用的版本，没有启用时用内置的提示词。
    """
    CREATE TABLE prompt_templates (
      id uuid PRIMARY KEY,
      key varchar(32) NOT NULL,
      version integer NOT NULL,
      content text NOT NULL,
      note text,
      active boolean NOT NULL DEFAULT false,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_prompt_templates_key_version UNIQUE (key, version)
    )
    """,
    "CREATE UNIQUE INDEX uq_prompt_templates_active ON prompt_templates (key) WHERE active",
    "GRANT SELECT ON prompt_templates TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON prompt_templates TO edp_platform",
    # AI 设置：问题改写、答案缓存、工具调用、分段发送；平台给租户设置的大模型并发上限。
    "ALTER TABLE ai_settings ADD COLUMN rewrite_enabled boolean NOT NULL DEFAULT true",
    "ALTER TABLE ai_settings ADD COLUMN answer_cache boolean NOT NULL DEFAULT true",
    "ALTER TABLE ai_settings ADD COLUMN tools_enabled boolean NOT NULL DEFAULT false",
    "ALTER TABLE ai_settings ADD COLUMN segment_replies boolean NOT NULL DEFAULT true",
    "ALTER TABLE ai_settings ADD COLUMN llm_concurrency integer"
    " CHECK (llm_concurrency IS NULL OR llm_concurrency BETWEEN 1 AND 200)",
    # 语义缓存：同一个问题（向量相似）直接用之前的回答；知识或 AI 设置变化时清空。
    f"""
    CREATE TABLE ai_answer_cache (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      question text NOT NULL,
      embedding vector({EMBED_DIM}) NOT NULL,
      answer text NOT NULL,
      confidence double precision NOT NULL,
      knowledge jsonb NOT NULL DEFAULT '[]',
      hits integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      expires_at timestamptz NOT NULL
    )
    """,
    "CREATE INDEX ix_ai_answer_cache_embedding ON ai_answer_cache"
    " USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX ix_ai_answer_cache_expires ON ai_answer_cache (tenant_id, expires_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ai_answer_cache TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ai_answer_cache TO edp_platform",
    # AI 工具 save_lead_info 登记的线索：由坐席确认后写入客户档案。
    """
    CREATE TABLE customer_lead_drafts (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      session_id uuid,
      fields jsonb NOT NULL DEFAULT '{}',
      status varchar(12) NOT NULL DEFAULT 'pending',
      created_at timestamptz NOT NULL DEFAULT now(),
      decided_by uuid,
      decided_at timestamptz,
      FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_customer_lead_drafts_status
        CHECK (status IN ('pending', 'confirmed', 'discarded'))
    )
    """,
    "CREATE INDEX ix_customer_lead_drafts_customer"
    " ON customer_lead_drafts (tenant_id, customer_id, created_at DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON customer_lead_drafts TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON customer_lead_drafts TO edp_platform",
    # 智能客服"正在输入"（发给在线的访客，失败不重试）。
    "ALTER TABLE im_ops DROP CONSTRAINT ck_im_ops_op",
    """
    ALTER TABLE im_ops ADD CONSTRAINT ck_im_ops_op CHECK (
      op IN ('invite', 'kick', 'notice', 'signal', 'bot_message', 'channel_send', 'mirror',
             'typing')
    )
    """,
    # AI 工具 create_ticket 建的留言。
    "ALTER TABLE tickets DROP CONSTRAINT ck_tickets_source",
    "ALTER TABLE tickets ADD CONSTRAINT ck_tickets_source"
    " CHECK (source IN ('queue_timeout', 'off_hours', 'visitor', 'ai'))",
    # ---- 渠道：AI 参数覆盖、AI 使用的知识空间（为空表示全部） ----
    "ALTER TABLE channel_accounts ADD COLUMN ai_overrides jsonb NOT NULL DEFAULT '{}'",
    "ALTER TABLE channel_accounts ADD COLUMN kb_space_ids uuid[] NOT NULL DEFAULT '{}'",
    # ---- 访客评价 AI 回答（设计文档 §12.7：单条知识的满意度） ----
    """
    CREATE TABLE ai_message_feedback (
      message_id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      session_id uuid NOT NULL,
      value smallint NOT NULL,
      item_ids uuid[] NOT NULL DEFAULT '{}',
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_ai_message_feedback_value CHECK (value IN (-1, 1))
    )
    """,
    "CREATE INDEX ix_ai_message_feedback_created ON ai_message_feedback (tenant_id, created_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ai_message_feedback TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ai_message_feedback TO edp_platform",
    "ALTER TABLE kb_items ADD COLUMN visitor_likes integer NOT NULL DEFAULT 0",
    "ALTER TABLE kb_items ADD COLUMN visitor_dislikes integer NOT NULL DEFAULT 0",
    # ---- Copilot（设计文档 §11.4）：实时提醒、会话小结 ----
    """
    CREATE TABLE copilot_alerts (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      session_id uuid NOT NULL,
      staff_id uuid,
      message_id uuid,
      kind varchar(24) NOT NULL,
      detail jsonb NOT NULL DEFAULT '{}',
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_copilot_alerts_kind
        CHECK (kind IN ('negative', 'escalation', 'sensitive_info', 'promise'))
    )
    """,
    "CREATE INDEX ix_copilot_alerts_session ON copilot_alerts (tenant_id, session_id, created_at)",
    "CREATE INDEX ix_copilot_alerts_created ON copilot_alerts (tenant_id, created_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON copilot_alerts TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON copilot_alerts TO edp_platform",
    """
    CREATE TABLE session_summaries (
      session_id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      customer_id uuid NOT NULL,
      summary text NOT NULL,
      tags text[] NOT NULL DEFAULT '{}',
      status varchar(12) NOT NULL DEFAULT 'draft',
      generated_at timestamptz NOT NULL DEFAULT now(),
      confirmed_by uuid,
      confirmed_at timestamptz,
      FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id) ON DELETE CASCADE,
      FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_session_summaries_status CHECK (status IN ('draft', 'confirmed', 'discarded'))
    )
    """,
    "CREATE INDEX ix_session_summaries_customer"
    " ON session_summaries (tenant_id, customer_id, generated_at DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON session_summaries TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON session_summaries TO edp_platform",
    # ---- 知识空间与分类树（设计文档 §12.1） ----
    """
    CREATE TABLE kb_spaces (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      name varchar(64) NOT NULL,
      description text,
      sort integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_kb_spaces_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_kb_spaces_name UNIQUE (tenant_id, name)
    )
    """,
    """
    CREATE TABLE kb_categories (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      space_id uuid NOT NULL,
      parent_id uuid,
      name varchar(64) NOT NULL,
      sort integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_kb_categories_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_kb_categories_name
        UNIQUE NULLS NOT DISTINCT (tenant_id, space_id, parent_id, name),
      FOREIGN KEY (tenant_id, space_id) REFERENCES kb_spaces (tenant_id, id) ON DELETE CASCADE,
      FOREIGN KEY (tenant_id, parent_id) REFERENCES kb_categories (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_kb_categories_parent CHECK (parent_id IS DISTINCT FROM id)
    )
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON kb_spaces TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON kb_spaces TO edp_platform",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON kb_categories TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON kb_categories TO edp_platform",
    # 条目：所属空间和分类、负责人、推送的技能组（为空表示全员）、到期提醒时间、来源地址。
    "ALTER TABLE kb_items ADD COLUMN space_id uuid",
    "ALTER TABLE kb_items ADD COLUMN category_id uuid",
    "ALTER TABLE kb_items ADD COLUMN owner_id uuid",
    "ALTER TABLE kb_items ADD COLUMN audience_group_ids uuid[] NOT NULL DEFAULT '{}'",
    "ALTER TABLE kb_items ADD COLUMN expiry_notified_at timestamptz",
    "ALTER TABLE kb_items ADD COLUMN source_url text",
    """
    ALTER TABLE kb_items ADD CONSTRAINT fk_kb_items_space FOREIGN KEY (tenant_id, space_id)
      REFERENCES kb_spaces (tenant_id, id) ON DELETE SET NULL (space_id)
    """,
    """
    ALTER TABLE kb_items ADD CONSTRAINT fk_kb_items_category FOREIGN KEY (tenant_id, category_id)
      REFERENCES kb_categories (tenant_id, id) ON DELETE SET NULL (category_id)
    """,
    """
    ALTER TABLE kb_items ADD CONSTRAINT fk_kb_items_owner FOREIGN KEY (tenant_id, owner_id)
      REFERENCES staff (tenant_id, id) ON DELETE SET NULL (owner_id)
    """,
    "CREATE INDEX ix_kb_items_space ON kb_items (tenant_id, space_id, category_id)",
    "ALTER TABLE kb_items DROP CONSTRAINT ck_kb_items_source",
    "ALTER TABLE kb_items ADD CONSTRAINT ck_kb_items_source"
    " CHECK (source IN ('manual', 'import', 'extracted', 'document', 'crawl'))",
    # 优秀话术候选（设计文档 §12.4）：审核通过后成为公共快捷话术。
    "ALTER TABLE kb_candidates DROP CONSTRAINT ck_kb_candidates_kind",
    "ALTER TABLE kb_candidates ADD CONSTRAINT ck_kb_candidates_kind"
    " CHECK (kind IN ('new', 'similar', 'conflict', 'gap', 'phrase'))",
    # 高满意度会话挖掘优秀话术的时间（客户可能在提炼之后才评价，之后再补充挖掘）。
    "ALTER TABLE kb_extractions ADD COLUMN phrases_at timestamptz",
    # ---- 知识导入任务：文档、Excel、官网抓取 ----
    """
    CREATE TABLE kb_import_jobs (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      kind varchar(12) NOT NULL,
      status varchar(12) NOT NULL DEFAULT 'pending',
      params jsonb NOT NULL DEFAULT '{}',
      result jsonb NOT NULL DEFAULT '{}',
      error text,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      started_at timestamptz,
      finished_at timestamptz,
      CONSTRAINT ck_kb_import_jobs_kind CHECK (kind IN ('document', 'excel', 'crawl')),
      CONSTRAINT ck_kb_import_jobs_status CHECK (status IN ('pending', 'running', 'done', 'failed'))
    )
    """,
    "CREATE INDEX ix_kb_import_jobs_status ON kb_import_jobs (status, created_at)",
    "CREATE INDEX ix_kb_import_jobs_tenant ON kb_import_jobs (tenant_id, created_at DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON kb_import_jobs TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON kb_import_jobs TO edp_platform",
    # ---- 站内信：知识到期提醒、知识周报等 ----
    """
    CREATE TABLE staff_notifications (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      staff_id uuid NOT NULL,
      kind varchar(32) NOT NULL,
      title text NOT NULL,
      body text,
      link text,
      created_at timestamptz NOT NULL DEFAULT now(),
      read_at timestamptz,
      FOREIGN KEY (tenant_id, staff_id) REFERENCES staff (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_staff_notifications_staff"
    " ON staff_notifications (tenant_id, staff_id, created_at DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON staff_notifications TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON staff_notifications TO edp_platform",
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
        "DROP TABLE IF EXISTS staff_notifications, kb_import_jobs, session_summaries,"
        " copilot_alerts, ai_message_feedback, customer_lead_drafts, ai_answer_cache,"
        " prompt_templates"
    )
    op.execute("ALTER TABLE kb_extractions DROP COLUMN phrases_at")
    op.execute("DELETE FROM kb_candidates WHERE kind = 'phrase'")
    op.execute("ALTER TABLE kb_candidates DROP CONSTRAINT ck_kb_candidates_kind")
    op.execute(
        "ALTER TABLE kb_candidates ADD CONSTRAINT ck_kb_candidates_kind"
        " CHECK (kind IN ('new', 'similar', 'conflict', 'gap'))"
    )
    op.execute("ALTER TABLE kb_items DROP CONSTRAINT ck_kb_items_source")
    op.execute(
        "ALTER TABLE kb_items ADD CONSTRAINT ck_kb_items_source"
        " CHECK (source IN ('manual', 'import', 'extracted'))"
    )
    for constraint in ("fk_kb_items_owner", "fk_kb_items_category", "fk_kb_items_space"):
        op.execute(f"ALTER TABLE kb_items DROP CONSTRAINT {constraint}")
    op.execute("DROP TABLE IF EXISTS kb_categories, kb_spaces")
    for column in (
        "source_url",
        "expiry_notified_at",
        "audience_group_ids",
        "owner_id",
        "category_id",
        "space_id",
        "visitor_dislikes",
        "visitor_likes",
    ):
        op.execute(f"ALTER TABLE kb_items DROP COLUMN {column}")
    for column in ("kb_space_ids", "ai_overrides"):
        op.execute(f"ALTER TABLE channel_accounts DROP COLUMN {column}")
    op.execute("DELETE FROM im_ops WHERE op = 'typing'")
    op.execute("ALTER TABLE im_ops DROP CONSTRAINT ck_im_ops_op")
    op.execute(
        "ALTER TABLE im_ops ADD CONSTRAINT ck_im_ops_op CHECK (op IN ('invite', 'kick', 'notice',"
        " 'signal', 'bot_message', 'channel_send', 'mirror'))"
    )
    op.execute("ALTER TABLE tickets DROP CONSTRAINT ck_tickets_source")
    op.execute(
        "ALTER TABLE tickets ADD CONSTRAINT ck_tickets_source"
        " CHECK (source IN ('queue_timeout', 'off_hours', 'visitor'))"
    )
    for column in (
        "llm_concurrency",
        "segment_replies",
        "tools_enabled",
        "answer_cache",
        "rewrite_enabled",
    ):
        op.execute(f"ALTER TABLE ai_settings DROP COLUMN {column}")
    for column in ("prompt_version", "cost"):
        op.execute(f"ALTER TABLE llm_calls DROP COLUMN {column}")
    for column in ("capabilities", "rerank_model"):
        op.execute(f"ALTER TABLE llm_providers DROP COLUMN {column}")
