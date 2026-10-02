"""AI intent judgment (design §32): session_intents, tenant settings, decision-model providers

Revision ID: 0034
Revises: 0033
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = ("session_intents",)

ALERT_KINDS = "'negative', 'escalation', 'sensitive_info', 'promise', 'price_probe'"

STATEMENTS = [
    # 每个会话最新的意图判断、这次会话的变化，以及待判断时间（兼作租约）。不存消息原文。
    """
    CREATE TABLE session_intents (
      session_id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      due_at timestamptz,
      message_id uuid,
      message_at timestamptz,
      judged_at timestamptz,
      stage smallint,
      stage_probability double precision,
      purchase_probability double precision,
      score double precision,
      intent varchar(32),
      intent_probability double precision,
      concerns text[] NOT NULL DEFAULT '{}',
      emotion double precision,
      human double precision,
      route varchar(32),
      source varchar(12),
      model varchar(128),
      answers jsonb NOT NULL DEFAULT '{}',
      peak_stage smallint,
      peak_at timestamptz,
      ready_alerted boolean NOT NULL DEFAULT false,
      judgments integer NOT NULL DEFAULT 0,
      failures smallint NOT NULL DEFAULT 0,
      history jsonb NOT NULL DEFAULT '[]',
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_session_intents_stage CHECK (stage BETWEEN 0 AND 4),
      CONSTRAINT ck_session_intents_peak CHECK (peak_stage BETWEEN 0 AND 4),
      CONSTRAINT ck_session_intents_source CHECK (source IN ('judge', 'llm'))
    )
    """,
    # 实时消费进程跨租户领取到期的判断。
    "CREATE INDEX ix_session_intents_due ON session_intents (due_at) WHERE due_at IS NOT NULL",
    # 租户的设置：判断开关、AI 回复是否参考、高意向转人工的阶段、自定义意图。
    "ALTER TABLE ai_settings ADD COLUMN intent_enabled boolean NOT NULL DEFAULT true",
    "ALTER TABLE ai_settings ADD COLUMN intent_in_reply boolean NOT NULL DEFAULT true",
    "ALTER TABLE ai_settings ADD COLUMN intent_handoff_stage smallint",
    "ALTER TABLE ai_settings ADD COLUMN custom_intents jsonb NOT NULL DEFAULT '[]'",
    "ALTER TABLE ai_settings ADD CONSTRAINT ck_ai_settings_intent_handoff"
    " CHECK (intent_handoff_stage IN (3, 4))",
    # 供应商的接口类型：OpenAI 兼容，或者 TypeSafe 判断模型（Jev）。
    "ALTER TABLE llm_providers ADD COLUMN protocol varchar(16) NOT NULL DEFAULT 'openai'",
    "ALTER TABLE llm_providers ADD CONSTRAINT ck_llm_providers_protocol"
    " CHECK (protocol IN ('openai', 'typesafe'))",
    # 坐席助手的"客户准备下单"提醒。
    "ALTER TABLE copilot_alerts DROP CONSTRAINT ck_copilot_alerts_kind",
    "ALTER TABLE copilot_alerts ADD CONSTRAINT ck_copilot_alerts_kind"
    f" CHECK (kind IN ({ALERT_KINDS}, 'purchase_ready'))",
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
    op.execute("DELETE FROM copilot_alerts WHERE kind = 'purchase_ready'")
    op.execute("ALTER TABLE copilot_alerts DROP CONSTRAINT ck_copilot_alerts_kind")
    op.execute(
        "ALTER TABLE copilot_alerts ADD CONSTRAINT ck_copilot_alerts_kind"
        f" CHECK (kind IN ({ALERT_KINDS}))"
    )
    # 判断模型的供应商在降级后无法使用：先去掉引用它们的路由和租户指定，再删除。
    op.execute(
        """
        UPDATE platform_settings
           SET value = jsonb_set(value, '{routes}', COALESCE((
             SELECT jsonb_object_agg(r.key, r.value)
               FROM jsonb_each(value -> 'routes') AS r
              WHERE r.value #>> '{}' NOT IN (
                SELECT id::text FROM llm_providers WHERE protocol = 'typesafe')
           ), '{}'::jsonb))
         WHERE key = 'llm_routes' AND value ? 'routes'
        """
    )
    op.execute("DELETE FROM llm_providers WHERE protocol = 'typesafe'")
    op.execute("ALTER TABLE llm_providers DROP CONSTRAINT ck_llm_providers_protocol")
    op.execute("ALTER TABLE llm_providers DROP COLUMN protocol")
    op.execute("ALTER TABLE ai_settings DROP CONSTRAINT ck_ai_settings_intent_handoff")
    for column in ("custom_intents", "intent_handoff_stage", "intent_in_reply", "intent_enabled"):
        op.execute(f"ALTER TABLE ai_settings DROP COLUMN {column}")
    for table in reversed(TENANT_TABLES):
        op.execute(f"DROP TABLE {table}")
