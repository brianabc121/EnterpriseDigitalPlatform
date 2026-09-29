"""ai: knowledge base, AI reception state and decisions, LLM call log, evaluations

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-29
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 向量维度与 EDP_LLM_EMBED_DIM 一致（bge-m3、text-embedding-v3 等常用模型支持 1024 维）。
EMBED_DIM = 1024

TENANT_TABLES = (
    "ai_settings",
    "kb_items",
    "kb_chunks",
    "ai_session_states",
    "ai_decisions",
    "llm_calls",
    "ai_eval_runs",
)

STATEMENTS = [
    # 生产环境由 DBA 预先创建扩展；开发和测试环境以所有者身份执行迁移时创建。
    "CREATE EXTENSION IF NOT EXISTS vector",
    # 租户的 AI 接待设置（设计文档 §11）。没有记录时按默认值、不启用。
    """
    CREATE TABLE ai_settings (
      tenant_id uuid PRIMARY KEY DEFAULT app_current_tenant() REFERENCES tenants (id),
      enabled boolean NOT NULL DEFAULT false,
      bot_name varchar(32) NOT NULL DEFAULT '智能客服',
      persona text,
      handoff_threshold real NOT NULL DEFAULT 0.6,
      max_turns integer NOT NULL DEFAULT 8,
      relevance_threshold real NOT NULL DEFAULT 0.55,
      handoff_keywords text[] NOT NULL DEFAULT '{}',
      sensitive_keywords text[] NOT NULL DEFAULT '{}',
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_ai_settings_threshold CHECK (handoff_threshold > 0 AND handoff_threshold <= 2),
      CONSTRAINT ck_ai_settings_turns CHECK (max_turns BETWEEN 1 AND 50),
      CONSTRAINT ck_ai_settings_relevance CHECK (relevance_threshold BETWEEN 0 AND 1)
    )
    """,
    # 知识条目（设计文档 §12.1）：FAQ（标准问 + 相似问 + 答案）或文档（标题 + 正文）。
    """
    CREATE TABLE kb_items (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      kind varchar(8) NOT NULL,
      title text NOT NULL,
      content text NOT NULL,
      questions text[] NOT NULL DEFAULT '{}',
      category varchar(64) NOT NULL DEFAULT '',
      tags text[] NOT NULL DEFAULT '{}',
      status varchar(16) NOT NULL DEFAULT 'draft',
      visibility varchar(16) NOT NULL DEFAULT 'public',
      valid_from timestamptz,
      valid_to timestamptz,
      source varchar(16) NOT NULL DEFAULT 'manual',
      version integer NOT NULL DEFAULT 1,
      hits integer NOT NULL DEFAULT 0,
      last_hit_at timestamptz,
      created_by uuid,
      updated_by uuid,
      published_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_kb_items_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT ck_kb_items_kind CHECK (kind IN ('faq', 'doc')),
      CONSTRAINT ck_kb_items_status CHECK (status IN ('draft', 'published', 'archived')),
      CONSTRAINT ck_kb_items_visibility CHECK (visibility IN ('public', 'agent', 'admin')),
      CONSTRAINT ck_kb_items_source CHECK (source IN ('manual', 'import', 'extracted'))
    )
    """,
    "CREATE INDEX ix_kb_items_tenant_status ON kb_items (tenant_id, status, updated_at)",
    # 检索单元：FAQ 的每个问法、文档的每个切片。只为已发布的条目生成。
    # terms 是用于关键词检索的词项（中文按字的二元组，英文和数字按词），embedding 为稠密向量。
    f"""
    CREATE TABLE kb_chunks (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      item_id uuid NOT NULL,
      kind varchar(12) NOT NULL,
      text text NOT NULL,
      terms text[] NOT NULL DEFAULT '{{}}',
      embedding vector({EMBED_DIM}),
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_kb_chunks_item FOREIGN KEY (tenant_id, item_id)
        REFERENCES kb_items (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_kb_chunks_kind CHECK (kind IN ('question', 'passage'))
    )
    """,
    "CREATE INDEX ix_kb_chunks_item ON kb_chunks (tenant_id, item_id)",
    "CREATE INDEX ix_kb_chunks_terms ON kb_chunks USING gin (terms)",
    "CREATE INDEX ix_kb_chunks_embedding ON kb_chunks USING hnsw (embedding vector_cosine_ops)",
    # AI 接待中的会话：待回复时间（客户连续发消息时合并后再回复）、轮数、护栏失败与重复提问计数。
    """
    CREATE TABLE ai_session_states (
      session_id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      due_at timestamptz,
      turns integer NOT NULL DEFAULT 0,
      guard_failures integer NOT NULL DEFAULT 0,
      repeats integer NOT NULL DEFAULT 0,
      last_question text,
      answered_until timestamptz,
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_ai_session_states_session FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_ai_session_states_due ON ai_session_states (due_at) WHERE due_at IS NOT NULL",
    # 每次 AI 回复或转人工的判定留痕（设计文档 §11.2），也是知识缺口的来源。
    """
    CREATE TABLE ai_decisions (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      session_id uuid NOT NULL,
      question text NOT NULL,
      action varchar(12) NOT NULL,
      reason varchar(32),
      score real NOT NULL DEFAULT 0,
      signals jsonb NOT NULL DEFAULT '{}',
      reply text,
      knowledge jsonb NOT NULL DEFAULT '[]',
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_ai_decisions_session FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_ai_decisions_action CHECK (action IN ('reply', 'handoff'))
    )
    """,
    "CREATE INDEX ix_ai_decisions_session ON ai_decisions (tenant_id, session_id, created_at)",
    "CREATE INDEX ix_ai_decisions_created ON ai_decisions (tenant_id, created_at)",
    # 大模型调用记账（设计文档 §11.5）：用量计量与成本分析。
    """
    CREATE TABLE llm_calls (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      scene varchar(24) NOT NULL,
      provider varchar(64) NOT NULL,
      model varchar(128) NOT NULL,
      prompt_tokens integer NOT NULL DEFAULT 0,
      completion_tokens integer NOT NULL DEFAULT 0,
      latency_ms integer NOT NULL DEFAULT 0,
      status varchar(12) NOT NULL,
      error text,
      session_id uuid,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_llm_calls_tenant_created ON llm_calls (tenant_id, created_at)",
    # 评测记录：评测集、结果与指标。
    """
    CREATE TABLE ai_eval_runs (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      created_by uuid,
      cases integer NOT NULL,
      answer_accuracy real,
      handoff_accuracy real,
      results jsonb NOT NULL DEFAULT '[]',
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_ai_eval_runs_tenant ON ai_eval_runs (tenant_id, created_at)",
    # AI 回复经发件箱以机器人身份发到服务群（与系统提示一样按 Room 顺序发送、失败重试）。
    "ALTER TABLE im_ops DROP CONSTRAINT ck_im_ops_op",
    """
    ALTER TABLE im_ops ADD CONSTRAINT ck_im_ops_op
      CHECK (op IN ('invite', 'kick', 'notice', 'signal', 'bot_message'))
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ai_settings, kb_items, kb_chunks, ai_session_states,"
    " ai_decisions, llm_calls, ai_eval_runs TO edp_app, edp_platform",
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
    op.execute("DELETE FROM im_ops WHERE op = 'bot_message'")
    op.execute("ALTER TABLE im_ops DROP CONSTRAINT ck_im_ops_op")
    op.execute(
        "ALTER TABLE im_ops ADD CONSTRAINT ck_im_ops_op"
        " CHECK (op IN ('invite', 'kick', 'notice', 'signal'))"
    )
    op.execute(
        "DROP TABLE IF EXISTS ai_eval_runs, llm_calls, ai_decisions, ai_session_states,"
        " kb_chunks, kb_items, ai_settings"
    )
