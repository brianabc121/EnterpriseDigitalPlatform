"""orders (design §25): the product catalog with Excel imports and product gaps, orders with
items, payments, revisions and activity, AI security events, order settings, the link from
to-dos to orders, and the AI reception state for product lookups and price probes

Revision ID: 0019
Revises: 0018
Create Date: 2026-09-30
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 1024
TENANT_TABLES = (
    "products",
    "product_imports",
    "product_gaps",
    "orders",
    "order_items",
    "order_payments",
    "order_revisions",
    "order_events",
    "ai_security_events",
)
MONEY = "numeric(12, 2)"

STATEMENTS = [
    "ALTER TABLE tenant_settings ADD COLUMN orders jsonb NOT NULL DEFAULT '{}'",
    # 商品库（设计文档 §25.2）。成本价和备注只给有权限的员工，永远不进入 AI 的上下文。
    # terms 是用于关键词检索的词项（名称、别名、型号、规格、分类、代码），embedding 为稠密向量。
    f"""
    CREATE TABLE products (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      code varchar(64),
      name varchar(128) NOT NULL,
      model varchar(64) NOT NULL DEFAULT '',
      spec varchar(128) NOT NULL DEFAULT '',
      category varchar(128) NOT NULL DEFAULT '',
      image_url varchar(1024),
      cost_price {MONEY},
      retail_price {MONEY},
      remark text NOT NULL DEFAULT '',
      aliases text[] NOT NULL DEFAULT '{{}}',
      status varchar(8) NOT NULL DEFAULT 'on',
      terms text[] NOT NULL DEFAULT '{{}}',
      embedding vector({EMBED_DIM}),
      created_by uuid,
      updated_by uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_products_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT ck_products_status CHECK (status IN ('on', 'off')),
      CONSTRAINT ck_products_prices CHECK (
        (cost_price IS NULL OR cost_price >= 0) AND (retail_price IS NULL OR retail_price >= 0)
      )
    )
    """,
    "CREATE UNIQUE INDEX uq_products_code ON products (tenant_id, code) WHERE code IS NOT NULL",
    "CREATE INDEX ix_products_status ON products (tenant_id, status, category, name)",
    "CREATE INDEX ix_products_terms ON products USING gin (terms)",
    "CREATE INDEX ix_products_embedding ON products USING hnsw (embedding vector_cosine_ops)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON products TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON products TO edp_platform",
    # Excel 导入：上传后先预览（逐行校验），确认后才写入商品库。
    """
    CREATE TABLE product_imports (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      file_name varchar(256) NOT NULL,
      status varchar(12) NOT NULL DEFAULT 'preview',
      rows jsonb NOT NULL DEFAULT '[]',
      total integer NOT NULL DEFAULT 0,
      invalid integer NOT NULL DEFAULT 0,
      created integer NOT NULL DEFAULT 0,
      updated integer NOT NULL DEFAULT 0,
      skipped integer NOT NULL DEFAULT 0,
      created_by uuid,
      applied_by uuid,
      applied_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT ck_product_imports_status CHECK (status IN ('preview', 'done', 'cancelled'))
    )
    """,
    "CREATE INDEX ix_product_imports_created ON product_imports (tenant_id, created_at DESC)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON product_imports TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON product_imports TO edp_platform",
    # 商品缺口：客户问到、商品库里匹配不到的商品（按规范化后的说法汇总）。
    """
    CREATE TABLE product_gaps (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      term varchar(128) NOT NULL,
      sample text NOT NULL DEFAULT '',
      count integer NOT NULL DEFAULT 1,
      first_seen_at timestamptz NOT NULL DEFAULT now(),
      last_seen_at timestamptz NOT NULL DEFAULT now(),
      resolved_at timestamptz,
      CONSTRAINT uq_product_gaps_term UNIQUE (tenant_id, term)
    )
    """,
    "CREATE INDEX ix_product_gaps_rank ON product_gaps (tenant_id, count DESC)"
    " WHERE resolved_at IS NULL",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON product_gaps TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON product_gaps TO edp_platform",
    # 订单（设计文档 §25.4）。收货信息加密保存（{"enc", "masked"}）；version 用于并发修改检查。
    # 跟踪链接的令牌全局唯一（公开页面只凭令牌查询）。
    f"""
    CREATE TABLE orders (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      no varchar(24) NOT NULL,
      status varchar(16) NOT NULL DEFAULT 'draft',
      source varchar(12) NOT NULL,
      customer_id uuid,
      session_id uuid,
      assignee_id uuid,
      skill_group_id uuid,
      receiver jsonb NOT NULL DEFAULT '{{}}',
      payment_method varchar(8),
      payment_hint varchar(8),
      deposit_amount {MONEY},
      credit_due_date date,
      credit_approved_by uuid,
      credit_approved_at timestamptz,
      payment_status varchar(8) NOT NULL DEFAULT 'unpaid',
      items_amount {MONEY} NOT NULL DEFAULT 0,
      discount {MONEY} NOT NULL DEFAULT 0,
      total {MONEY} NOT NULL DEFAULT 0,
      paid_amount {MONEY} NOT NULL DEFAULT 0,
      refunded_amount {MONEY} NOT NULL DEFAULT 0,
      price_pending boolean NOT NULL DEFAULT false,
      expected_at timestamptz,
      shipping_company varchar(64),
      tracking_no varchar(64),
      submitted_at timestamptz,
      confirmed_at timestamptz,
      confirmed_by uuid,
      started_at timestamptz,
      shipped_at timestamptz,
      completed_at timestamptz,
      cancelled_at timestamptz,
      cancel_reason text,
      tracking_token varchar(64) NOT NULL,
      tracking_expires_at timestamptz,
      external_no varchar(64),
      version integer NOT NULL DEFAULT 1,
      modified boolean NOT NULL DEFAULT false,
      ai_error boolean NOT NULL DEFAULT false,
      confirm_message_id uuid,
      evidence_message_ids uuid[] NOT NULL DEFAULT '{{}}',
      customer_note text NOT NULL DEFAULT '',
      internal_note text NOT NULL DEFAULT '',
      dedupe_key varchar(64),
      created_by_type varchar(8) NOT NULL,
      created_by uuid,
      review_todo_id uuid,
      collection_todo_id uuid,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT uq_orders_tenant_id_id UNIQUE (tenant_id, id),
      CONSTRAINT uq_orders_no UNIQUE (tenant_id, no),
      CONSTRAINT uq_orders_tracking_token UNIQUE (tracking_token),
      CONSTRAINT fk_orders_customer FOREIGN KEY (tenant_id, customer_id)
        REFERENCES customers (tenant_id, id) ON DELETE SET NULL (customer_id),
      CONSTRAINT fk_orders_session FOREIGN KEY (tenant_id, session_id)
        REFERENCES sessions (tenant_id, id) ON DELETE SET NULL (session_id),
      CONSTRAINT fk_orders_assignee FOREIGN KEY (tenant_id, assignee_id)
        REFERENCES staff (tenant_id, id) ON DELETE SET NULL (assignee_id),
      CONSTRAINT fk_orders_skill_group FOREIGN KEY (tenant_id, skill_group_id)
        REFERENCES skill_groups (tenant_id, id) ON DELETE SET NULL (skill_group_id),
      CONSTRAINT fk_orders_review_todo FOREIGN KEY (tenant_id, review_todo_id)
        REFERENCES todos (tenant_id, id) ON DELETE SET NULL (review_todo_id),
      CONSTRAINT fk_orders_collection_todo FOREIGN KEY (tenant_id, collection_todo_id)
        REFERENCES todos (tenant_id, id) ON DELETE SET NULL (collection_todo_id),
      CONSTRAINT ck_orders_status CHECK (
        status IN ('draft', 'pending_review', 'confirmed', 'fulfilling', 'shipped', 'completed',
                   'cancelled')
      ),
      CONSTRAINT ck_orders_source CHECK (
        source IN ('ai_chat', 'copilot', 'sidebar', 'staff', 'api')
      ),
      CONSTRAINT ck_orders_payment_method CHECK (
        payment_method IS NULL OR payment_method IN ('online', 'cod', 'deposit', 'credit')
      ),
      CONSTRAINT ck_orders_payment_hint CHECK (
        payment_hint IS NULL OR payment_hint IN ('online', 'cod', 'deposit', 'credit')
      ),
      CONSTRAINT ck_orders_payment_status CHECK (
        payment_status IN ('unpaid', 'deposit', 'partial', 'paid', 'refunded')
      ),
      CONSTRAINT ck_orders_amounts CHECK (
        items_amount >= 0 AND discount >= 0 AND total >= 0 AND paid_amount >= 0
        AND refunded_amount >= 0 AND (deposit_amount IS NULL OR deposit_amount > 0)
      ),
      CONSTRAINT ck_orders_created_by_type CHECK (
        created_by_type IN ('ai', 'staff', 'system', 'api')
      )
    )
    """,
    "CREATE UNIQUE INDEX uq_orders_dedupe ON orders (tenant_id, dedupe_key)"
    " WHERE dedupe_key IS NOT NULL",
    "CREATE UNIQUE INDEX uq_orders_external_no ON orders (tenant_id, external_no)"
    " WHERE external_no IS NOT NULL",
    "CREATE INDEX ix_orders_status ON orders (tenant_id, status, created_at DESC)",
    "CREATE INDEX ix_orders_customer ON orders (tenant_id, customer_id, created_at DESC)",
    "CREATE INDEX ix_orders_assignee ON orders (tenant_id, assignee_id, status)",
    "CREATE INDEX ix_orders_session ON orders (tenant_id, session_id)",
    # 调度进程跨租户扫描到期未收清的暂欠订单（生成催收待办）。
    "CREATE INDEX ix_orders_credit_due ON orders (credit_due_date)"
    " WHERE payment_method = 'credit' AND collection_todo_id IS NULL",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON orders TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON orders TO edp_platform",
    # 订单行：商品信息是下单时的快照；匹配不到商品库的保留客户的原话（raw_text），由员工处理。
    f"""
    CREATE TABLE order_items (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      order_id uuid NOT NULL,
      product_id uuid,
      code varchar(64),
      name varchar(128) NOT NULL,
      model varchar(64) NOT NULL DEFAULT '',
      spec varchar(128) NOT NULL DEFAULT '',
      image_url varchar(1024),
      raw_text varchar(200),
      quantity integer NOT NULL,
      list_price {MONEY},
      unit_price {MONEY},
      cost_price {MONEY},
      amount {MONEY} NOT NULL DEFAULT 0,
      sort integer NOT NULL DEFAULT 0,
      FOREIGN KEY (tenant_id, order_id) REFERENCES orders (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT fk_order_items_product FOREIGN KEY (tenant_id, product_id)
        REFERENCES products (tenant_id, id) ON DELETE SET NULL (product_id),
      CONSTRAINT ck_order_items_quantity CHECK (quantity > 0),
      CONSTRAINT ck_order_items_prices CHECK (
        (unit_price IS NULL OR unit_price >= 0) AND amount >= 0
      )
    )
    """,
    "CREATE INDEX ix_order_items_order ON order_items (tenant_id, order_id, sort)",
    "CREATE INDEX ix_order_items_product ON order_items (tenant_id, product_id)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON order_items TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON order_items TO edp_platform",
    # 收款记录（设计文档 §25.5）：平台只登记，不直接收款。作废保留记录并写明原因。
    f"""
    CREATE TABLE order_payments (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      order_id uuid NOT NULL,
      kind varchar(8) NOT NULL DEFAULT 'payment',
      amount {MONEY} NOT NULL,
      channel varchar(8) NOT NULL,
      paid_at timestamptz NOT NULL,
      reference_no varchar(64),
      proof_url varchar(1024),
      note text,
      recorded_by_type varchar(8) NOT NULL DEFAULT 'staff',
      recorded_by uuid,
      voided_at timestamptz,
      voided_by uuid,
      void_reason text,
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, order_id) REFERENCES orders (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_order_payments_kind CHECK (kind IN ('payment', 'refund')),
      CONSTRAINT ck_order_payments_channel CHECK (
        channel IN ('wechat', 'alipay', 'bank', 'cash', 'other')
      ),
      CONSTRAINT ck_order_payments_amount CHECK (amount > 0)
    )
    """,
    "CREATE INDEX ix_order_payments_order ON order_payments (tenant_id, order_id, created_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON order_payments TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON order_payments TO edp_platform",
    # 修改记录：每次修改生成一个版本（前后差异、原因、修改后的完整内容），只追加。
    """
    CREATE TABLE order_revisions (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      order_id uuid NOT NULL,
      version integer NOT NULL,
      kind varchar(12) NOT NULL,
      actor_type varchar(8) NOT NULL,
      actor_id uuid,
      reason varchar(20),
      note text,
      changes jsonb NOT NULL DEFAULT '{}',
      snapshot jsonb NOT NULL,
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, order_id) REFERENCES orders (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT uq_order_revisions_version UNIQUE (tenant_id, order_id, version),
      CONSTRAINT ck_order_revisions_kind CHECK (kind IN ('created', 'edit', 'status', 'payment')),
      CONSTRAINT ck_order_revisions_reason CHECK (
        reason IS NULL OR reason IN ('customer_request', 'ai_error', 'price_adjust',
                                     'substitution', 'other')
      )
    )
    """,
    "GRANT SELECT, INSERT, UPDATE, DELETE ON order_revisions TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON order_revisions TO edp_platform",
    # 订单动态：所有操作；public 的（提交、确认、发货、完成等）也显示在客户的跟踪页上。
    """
    CREATE TABLE order_events (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      order_id uuid NOT NULL,
      type varchar(24) NOT NULL,
      actor_type varchar(8) NOT NULL,
      actor_id uuid,
      payload jsonb NOT NULL DEFAULT '{}',
      public boolean NOT NULL DEFAULT false,
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, order_id) REFERENCES orders (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_order_events_order ON order_events (tenant_id, order_id, created_at)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON order_events TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON order_events TO edp_platform",
    # AI 安全事件（设计文档 §25.2）：套价识别、回复拦截。
    """
    CREATE TABLE ai_security_events (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      kind varchar(16) NOT NULL,
      session_id uuid,
      customer_id uuid,
      detail jsonb NOT NULL DEFAULT '{}',
      created_at timestamptz NOT NULL DEFAULT now(),
      FOREIGN KEY (tenant_id, session_id) REFERENCES sessions (tenant_id, id)
        ON DELETE SET NULL (session_id),
      FOREIGN KEY (tenant_id, customer_id) REFERENCES customers (tenant_id, id)
        ON DELETE SET NULL (customer_id),
      CONSTRAINT ck_ai_security_events_kind CHECK (kind IN ('price_probe', 'reply_blocked'))
    )
    """,
    "CREATE INDEX ix_ai_security_events_created ON ai_security_events (tenant_id, created_at)",
    "CREATE INDEX ix_ai_security_events_session ON ai_security_events (tenant_id, session_id)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ai_security_events TO edp_app",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ai_security_events TO edp_platform",
    # 待办关联的订单（订单审核、催收、订单相关的退换货）。所有待办的 order_id 此前都是空的。
    "ALTER TABLE todos ADD CONSTRAINT fk_todos_order FOREIGN KEY (tenant_id, order_id)"
    " REFERENCES orders (tenant_id, id) ON DELETE SET NULL (order_id)",
    "CREATE INDEX ix_todos_order ON todos (tenant_id, order_id) WHERE order_id IS NOT NULL",
    # AI 接待：连续几次在商品库里找不到客户要的商品（两次时转人工）；坐席助手的"套价"提醒。
    "ALTER TABLE ai_session_states ADD COLUMN product_misses smallint NOT NULL DEFAULT 0",
    "ALTER TABLE copilot_alerts DROP CONSTRAINT ck_copilot_alerts_kind",
    "ALTER TABLE copilot_alerts ADD CONSTRAINT ck_copilot_alerts_kind"
    " CHECK (kind IN ('negative', 'escalation', 'sensitive_info', 'promise', 'price_probe'))",
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
    op.execute("DELETE FROM copilot_alerts WHERE kind = 'price_probe'")
    op.execute("ALTER TABLE copilot_alerts DROP CONSTRAINT ck_copilot_alerts_kind")
    op.execute(
        "ALTER TABLE copilot_alerts ADD CONSTRAINT ck_copilot_alerts_kind"
        " CHECK (kind IN ('negative', 'escalation', 'sensitive_info', 'promise'))"
    )
    op.execute("ALTER TABLE ai_session_states DROP COLUMN product_misses")
    op.execute("DROP INDEX ix_todos_order")
    op.execute("ALTER TABLE todos DROP CONSTRAINT fk_todos_order")
    op.execute(
        "DROP TABLE ai_security_events, order_events, order_revisions, order_payments,"
        " order_items, orders, product_gaps, product_imports, products"
    )
    op.execute("ALTER TABLE tenant_settings DROP COLUMN orders")
