"""commerce and ops: plans, subscriptions, invoices, tenant lifecycle, exports, deletions,
support access, LLM providers, platform settings, platform MFA

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 删除记录只有平台连接可以访问；也启用 RLS，保持"带 tenant_id 的表都受 RLS 约束"。
TENANT_TABLES = (
    "subscriptions",
    "invoices",
    "tenant_exports",
    "support_grants",
    "tenant_deletions",
)

STATEMENTS = [
    # 套餐（平台级，设计 §7.3）：价格（分/月）、额度、功能开关、超额策略、试用天数。
    """
    CREATE TABLE plans (
      id uuid PRIMARY KEY,
      code varchar(32) NOT NULL,
      name varchar(64) NOT NULL,
      description text,
      price_monthly integer NOT NULL DEFAULT 0,
      limits jsonb NOT NULL DEFAULT '{}',
      features jsonb NOT NULL DEFAULT '{}',
      overage jsonb NOT NULL DEFAULT '{}',
      trial_days integer NOT NULL DEFAULT 0,
      public boolean NOT NULL DEFAULT false,
      status varchar(16) NOT NULL DEFAULT 'active',
      sort integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_plans_code UNIQUE (code),
      CONSTRAINT ck_plans_status CHECK (status IN ('active', 'archived')),
      CONSTRAINT ck_plans_price CHECK (price_monthly >= 0),
      CONSTRAINT ck_plans_trial CHECK (trial_days BETWEEN 0 AND 365)
    )
    """,
    "GRANT SELECT ON plans TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON plans TO edp_platform",
    # 订阅：租户在每个计费周期使用的套餐（试用 → 正式 → 续费或升级；到期后停用）。
    """
    CREATE TABLE subscriptions (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      plan_id uuid NOT NULL REFERENCES plans (id),
      status varchar(16) NOT NULL,
      period_start date NOT NULL,
      period_end date NOT NULL,
      note text,
      created_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_subscriptions_status
        CHECK (status IN ('trial', 'active', 'expired', 'cancelled')),
      CONSTRAINT ck_subscriptions_period CHECK (period_end >= period_start)
    )
    """,
    "CREATE INDEX ix_subscriptions_tenant ON subscriptions (tenant_id, created_at DESC)",
    # 账单（一期只展示，不在线支付）：按月生成，平台运营标记已收款或作废。
    """
    CREATE TABLE invoices (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      number varchar(64) NOT NULL,
      period_start date NOT NULL,
      period_end date NOT NULL,
      plan_id uuid REFERENCES plans (id),
      plan_name varchar(64) NOT NULL DEFAULT '',
      items jsonb NOT NULL DEFAULT '[]',
      amount integer NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'issued',
      issued_at timestamptz NOT NULL DEFAULT now(),
      paid_at timestamptz,
      note text,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_invoices_number UNIQUE (number),
      CONSTRAINT uq_invoices_period UNIQUE (tenant_id, period_start),
      CONSTRAINT ck_invoices_status CHECK (status IN ('issued', 'paid', 'void'))
    )
    """,
    # 数据导出（注销前或随时）：打包成 ZIP 放在对象存储，管理员下载。
    """
    CREATE TABLE tenant_exports (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      status varchar(16) NOT NULL DEFAULT 'pending',
      requested_by uuid,
      object_key text,
      size bigint,
      tables jsonb NOT NULL DEFAULT '{}',
      error text,
      created_at timestamptz NOT NULL DEFAULT now(),
      finished_at timestamptz,
      expires_at timestamptz,
      CONSTRAINT ck_tenant_exports_status
        CHECK (status IN ('pending', 'running', 'done', 'failed'))
    )
    """,
    # 租户授权平台运维访问业务数据（设计 §7.5）：有效期内运营人员可以只读查看会话，全程审计。
    """
    CREATE TABLE support_grants (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      granted_by uuid,
      reason text NOT NULL,
      expires_at timestamptz NOT NULL,
      revoked_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "GRANT SELECT ON subscriptions, invoices TO edp_app",
    "GRANT SELECT, INSERT, UPDATE ON tenant_exports, support_grants TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON subscriptions, invoices, tenant_exports,"
    " support_grants TO edp_platform",
    # 租户生命周期：注销申请后按保留期删除数据（先导出），删除后留下删除记录。
    "ALTER TABLE tenants ADD COLUMN closing_requested_at timestamptz",
    "ALTER TABLE tenants ADD COLUMN deletion_scheduled_at timestamptz",
    "ALTER TABLE tenants ADD COLUMN purged_at timestamptz",
    "ALTER TABLE tenants DROP CONSTRAINT ck_tenants_status",
    """
    ALTER TABLE tenants ADD CONSTRAINT ck_tenants_status
      CHECK (status IN ('active', 'suspended', 'closed'))
    """,
    """
    CREATE TABLE tenant_deletions (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL REFERENCES tenants (id),
      code varchar(32) NOT NULL,
      name varchar(128) NOT NULL,
      requested_at timestamptz,
      scheduled_at timestamptz,
      purged_at timestamptz NOT NULL DEFAULT now(),
      export_id uuid,
      counts jsonb NOT NULL DEFAULT '{}',
      digest varchar(64) NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "GRANT SELECT, INSERT ON tenant_deletions TO edp_platform",
    # 删除租户数据时平台连接需要删除所有租户表的行（平时只有运营接口用到的表可以删除）。
    """
    DO $$
    DECLARE t text;
    BEGIN
      FOR t IN
        SELECT c.table_name FROM information_schema.columns c
        JOIN information_schema.tables x
          ON x.table_name = c.table_name AND x.table_schema = c.table_schema
        WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
          AND x.table_type = 'BASE TABLE'
      LOOP
        EXECUTE format('GRANT SELECT, DELETE ON %I TO edp_platform', t);
      END LOOP;
    END $$
    """,
    # 大模型供应商（平台级，设计 §7.5、§11.5）：运营在后台配置，接口密钥加密保存。
    """
    CREATE TABLE llm_providers (
      id uuid PRIMARY KEY,
      name varchar(64) NOT NULL,
      base_url text NOT NULL,
      api_key_enc text NOT NULL DEFAULT '',
      chat_model varchar(128) NOT NULL,
      fast_model varchar(128) NOT NULL DEFAULT '',
      embed_model varchar(128) NOT NULL DEFAULT '',
      embed_dim integer NOT NULL DEFAULT 1024,
      send_dimensions boolean NOT NULL DEFAULT false,
      prices jsonb NOT NULL DEFAULT '{}',
      is_default boolean NOT NULL DEFAULT false,
      enabled boolean NOT NULL DEFAULT true,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "CREATE UNIQUE INDEX uq_llm_providers_default ON llm_providers (is_default) WHERE is_default",
    "GRANT SELECT ON llm_providers TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON llm_providers TO edp_platform",
    # 平台设置（全局敏感词、按场景的模型路由等）。
    """
    CREATE TABLE platform_settings (
      key varchar(64) PRIMARY KEY,
      value jsonb NOT NULL DEFAULT '{}',
      updated_by uuid,
      updated_at timestamptz NOT NULL DEFAULT now()
    )
    """,
    "GRANT SELECT ON platform_settings TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON platform_settings TO edp_platform",
    # 租户使用的模型：运营指定的平台供应商，或租户自带的接口密钥（加密保存）。
    "ALTER TABLE ai_settings ADD COLUMN llm_provider_id uuid REFERENCES llm_providers (id)"
    " ON DELETE SET NULL",
    "ALTER TABLE ai_settings ADD COLUMN byo_llm jsonb",
    # 运营账号的二次验证（TOTP）。
    "ALTER TABLE platform_users ADD COLUMN totp_secret_enc text",
    "ALTER TABLE platform_users ADD COLUMN mfa_enabled_at timestamptz",
    # 默认套餐：试用版、标准版、旗舰版（运营可以在后台修改）。价格单位：分/月。
    """
    INSERT INTO plans (id, code, name, description, price_monthly, limits, features, overage,
                       trial_days, public, sort)
    VALUES
      ('01928000-0000-7000-8000-000000000001', 'trial', '试用版', '14 天免费试用，功能完整',
       0, '{"seats": 5, "ai_replies_monthly": 1000, "kb_items": 500, "channels": 3}',
       '{"ai": true, "wecom": true, "broadcast": true, "extraction": true, "zone": false}',
       '{"policy": "degrade"}', 14, true, 0),
      ('01928000-0000-7000-8000-000000000002', 'standard', '标准版',
       '中小团队：AI 接待、企业微信、知识沉淀', 199900,
       '{"seats": 20, "ai_replies_monthly": 20000, "kb_items": 5000, "channels": 10}',
       '{"ai": true, "wecom": true, "broadcast": true, "extraction": true, "zone": false}',
       '{"policy": "degrade", "ai_reply_price": 5}', 0, true, 1),
      ('01928000-0000-7000-8000-000000000003', 'enterprise', '旗舰版',
       '大型团队：不限坐席，含数据与智能专区', 999900,
       '{"ai_replies_monthly": 200000}',
       '{"ai": true, "wecom": true, "broadcast": true, "extraction": true, "zone": true}',
       '{"policy": "warn", "ai_reply_price": 3}', 0, true, 2)
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
    op.execute("ALTER TABLE platform_users DROP COLUMN mfa_enabled_at")
    op.execute("ALTER TABLE platform_users DROP COLUMN totp_secret_enc")
    op.execute("ALTER TABLE ai_settings DROP COLUMN byo_llm")
    op.execute("ALTER TABLE ai_settings DROP COLUMN llm_provider_id")
    op.execute("DROP TABLE IF EXISTS platform_settings, llm_providers, tenant_deletions")
    op.execute("UPDATE tenants SET status = 'suspended' WHERE status = 'closed'")
    op.execute("ALTER TABLE tenants DROP CONSTRAINT ck_tenants_status")
    op.execute(
        "ALTER TABLE tenants ADD CONSTRAINT ck_tenants_status"
        " CHECK (status IN ('active', 'suspended'))"
    )
    op.execute("ALTER TABLE tenants DROP COLUMN purged_at")
    op.execute("ALTER TABLE tenants DROP COLUMN deletion_scheduled_at")
    op.execute("ALTER TABLE tenants DROP COLUMN closing_requested_at")
    op.execute("DROP TABLE IF EXISTS support_grants, tenant_exports, invoices, subscriptions, plans")
