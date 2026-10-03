"""Opportunities (design §40): stages; prospects become opportunities; follow-ups form the timeline

意向客户（§35）升级为商机：customer_prospects 改名 opportunities（加名称、阶段、预计金额、
预计成交日、进入阶段的时间、最近动态、关联商品、合同、输单原因分类；follower_id 改名 owner_id），
prospect_followups 改名 opportunity_activities（加种类、标题、属性、关联对象）；新表 pipeline_stages
写入默认的六个阶段，现有记录按状态和跟进次数换算阶段；待办加 opportunity_id；租户设置的 prospects 列
改名 opportunities。

Revision ID: 0045
Revises: 0044
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045"
down_revision: str | None = "0044"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 默认阶段（opportunities/models.py 的 DEFAULT_STAGES）：代码、名称、类型、概率、停滞天数、颜色。
STAGES = (
    ("new", "新线索", "open", 10, 3, "#909399"),
    ("contacted", "已沟通", "open", 30, 7, "#409eff"),
    ("quoted", "已报价", "open", 60, 7, "#e6a23c"),
    ("negotiating", "谈判中", "open", 80, 7, "#f56c6c"),
    ("won", "赢单", "won", 100, None, "#67c23a"),
    ("lost", "输单", "lost", 0, None, "#c0c4cc"),
)
# 迁移里改数据的表：行级安全对表的所有者也生效（FORCE），先放开（按改名前的名称）再恢复
# （按改名后的名称）。
BEFORE = ("customer_prospects", "prospect_followups", "tenant_data_index")
AFTER = ("pipeline_stages", "opportunities", "opportunity_activities", "tenant_data_index")

CREATE_STAGES = """
CREATE TABLE pipeline_stages (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
  code varchar(32) NOT NULL,
  name varchar(32) NOT NULL,
  position integer NOT NULL DEFAULT 0,
  kind varchar(8) NOT NULL DEFAULT 'open',
  probability integer NOT NULL DEFAULT 0,
  stale_days integer,
  color varchar(16),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (tenant_id, id),
  UNIQUE (tenant_id, code),
  CONSTRAINT ck_pipeline_stages_kind CHECK (kind IN ('open', 'won', 'lost')),
  CONSTRAINT ck_pipeline_stages_probability CHECK (probability BETWEEN 0 AND 100)
)
"""

OPPORTUNITIES = [
    "ALTER TABLE customer_prospects RENAME TO opportunities",
    "ALTER TABLE opportunities RENAME COLUMN follower_id TO owner_id",
    "RETRACK opportunities",
    """
    ALTER TABLE opportunities
      ADD COLUMN name varchar(128) NOT NULL DEFAULT '',
      ADD COLUMN stage_id uuid,
      ADD COLUMN amount numeric(14, 2),
      ADD COLUMN expected_close_at date,
      ADD COLUMN probability integer,
      ADD COLUMN stage_entered_at timestamptz NOT NULL DEFAULT now(),
      ADD COLUMN last_activity_at timestamptz,
      ADD COLUMN products jsonb NOT NULL DEFAULT '[]',
      ADD COLUMN position integer NOT NULL DEFAULT 0,
      ADD COLUMN contract_id uuid,
      ADD COLUMN lost_reason_code varchar(16)
    """,
    """
    ALTER TABLE opportunities ADD CONSTRAINT fk_opportunities_stage
      FOREIGN KEY (tenant_id, stage_id) REFERENCES pipeline_stages (tenant_id, id)
    """,
    """
    ALTER TABLE opportunities ADD CONSTRAINT fk_opportunities_contract
      FOREIGN KEY (tenant_id, contract_id) REFERENCES contracts (tenant_id, id)
      ON DELETE SET NULL (contract_id)
    """,
    # 现有记录换算阶段：赢单、输单各自的阶段；跟进中的跟进过就是"已沟通"，否则"新线索"；待确认、
    # 已忽略的是"新线索"。
    """
    UPDATE opportunities o SET stage_id = s.id
    FROM pipeline_stages s
    WHERE s.tenant_id = o.tenant_id AND s.code = CASE o.status
      WHEN 'won' THEN 'won'
      WHEN 'lost' THEN 'lost'
      WHEN 'active' THEN CASE WHEN o.follow_count > 0 THEN 'contacted' ELSE 'new' END
      ELSE 'new' END
    """,
    """
    UPDATE opportunities o
    SET name = COALESCE(NULLIF(left(btrim(o.interest), 40), ''), c.display_name || ' 的商机')
    FROM customers c WHERE c.id = o.customer_id AND o.name = ''
    """,
    """
    UPDATE opportunities SET stage_entered_at = COALESCE(closed_at, opened_at),
      last_activity_at = COALESCE(last_followed_at, updated_at)
    """,
    "UPDATE opportunities SET lost_reason_code = 'other' WHERE status = 'lost'",
    """
    UPDATE opportunities o SET amount = ord.total FROM orders ord
    WHERE ord.id = o.order_id AND o.amount IS NULL
    """,
    "SET CONSTRAINTS ALL IMMEDIATE",
    "ALTER TABLE opportunities ALTER COLUMN stage_id SET NOT NULL",
    "ALTER TABLE opportunities RENAME CONSTRAINT customer_prospects_pkey TO opportunities_pkey",
    """
    ALTER TABLE opportunities RENAME CONSTRAINT customer_prospects_tenant_id_id_key
      TO opportunities_tenant_id_id_key
    """,
    *(
        f"ALTER TABLE opportunities RENAME CONSTRAINT ck_customer_prospects_{c}"
        f" TO ck_opportunities_{c}"
        for c in ("status", "level", "source")
    ),
    "ALTER INDEX uq_customer_prospects_open RENAME TO uq_opportunities_open",
    "ALTER INDEX ix_customer_prospects_due RENAME TO ix_opportunities_due",
    "ALTER INDEX ix_customer_prospects_status RENAME TO ix_opportunities_status",
    "ALTER INDEX ix_customer_prospects_customer RENAME TO ix_opportunities_customer",
    "ALTER INDEX ix_customer_prospects_follower RENAME TO ix_opportunities_owner",
    "ALTER INDEX ix_customer_prospects_session RENAME TO ix_opportunities_session",
    "ALTER INDEX ix_customer_prospects_change_seq RENAME TO ix_opportunities_change_seq",
    "CREATE INDEX ix_opportunities_stage ON opportunities (tenant_id, stage_id, position)",
    """
    CREATE INDEX ix_opportunities_closing ON opportunities (tenant_id, expected_close_at)
      WHERE status = 'active'
    """,
]

ACTIVITIES = [
    "ALTER TABLE prospect_followups RENAME TO opportunity_activities",
    "ALTER TABLE opportunity_activities RENAME COLUMN prospect_id TO opportunity_id",
    "RETRACK opportunity_activities",
    """
    ALTER TABLE opportunity_activities
      ADD COLUMN kind varchar(24) NOT NULL DEFAULT 'followup',
      ADD COLUMN title varchar(200),
      ADD COLUMN properties jsonb NOT NULL DEFAULT '{}',
      ADD COLUMN linked_type varchar(24),
      ADD COLUMN linked_id uuid,
      ALTER COLUMN content DROP NOT NULL
    """,
    # 系统记的"客户又来咨询了"。
    """
    UPDATE opportunity_activities SET kind = 'session'
    WHERE staff_id IS NULL AND session_id IS NOT NULL
    """,
    "SET CONSTRAINTS ALL IMMEDIATE",
    """
    ALTER TABLE opportunity_activities RENAME CONSTRAINT prospect_followups_pkey
      TO opportunity_activities_pkey
    """,
    """
    ALTER TABLE opportunity_activities RENAME CONSTRAINT prospect_followups_tenant_id_id_key
      TO opportunity_activities_tenant_id_id_key
    """,
    """
    ALTER TABLE opportunity_activities RENAME CONSTRAINT ck_prospect_followups_method
      TO ck_opportunity_activities_method
    """,
    "ALTER INDEX ix_prospect_followups_prospect RENAME TO ix_opportunity_activities_opportunity",
    """
    ALTER INDEX ix_prospect_followups_change_seq
      RENAME TO ix_opportunity_activities_change_seq
    """,
    "DROP INDEX uq_prospect_followups_session",
    """
    CREATE UNIQUE INDEX uq_opportunity_activities_session
      ON opportunity_activities (tenant_id, opportunity_id, session_id)
      WHERE kind = 'session' AND session_id IS NOT NULL
    """,
]

OTHERS = [
    "ALTER TABLE todos ADD COLUMN opportunity_id uuid",
    """
    ALTER TABLE todos ADD CONSTRAINT fk_todos_opportunity
      FOREIGN KEY (tenant_id, opportunity_id) REFERENCES opportunities (tenant_id, id)
      ON DELETE SET NULL (opportunity_id)
    """,
    """
    CREATE INDEX ix_todos_opportunity ON todos (tenant_id, opportunity_id)
      WHERE opportunity_id IS NOT NULL
    """,
    "ALTER TABLE tenant_settings RENAME COLUMN prospects TO opportunities",
    "SET CONSTRAINTS ALL IMMEDIATE",
]

# 增量更新索引（§33.9）里的领域名跟着表名改。要在改数据之前改：改名后的触发器在改数据时会按新名称
# 写索引行，这时旧名称的行还在就会撞主键（tenant_id, domain）。
DATA_INDEX = [
    "UPDATE tenant_data_index SET domain = 'opportunities' WHERE domain = 'customer_prospects'",
    """
    UPDATE tenant_data_index SET domain = 'opportunity_activities'
    WHERE domain = 'prospect_followups'
    """,
]


def _retrack(table: str) -> list[str]:
    """改名后重建写增量更新索引的触发器（触发器里记的是建表时的名称）。"""
    return [
        f"DROP TRIGGER IF EXISTS edp_data_index ON {table}",
        f"""
        CREATE CONSTRAINT TRIGGER edp_data_index AFTER INSERT OR UPDATE OR DELETE ON {table}
          DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
          EXECUTE FUNCTION edp_touch_data_index('{table}')
        """,
    ]


def upgrade() -> None:
    op.execute(CREATE_STAGES)
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON pipeline_stages TO edp_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON pipeline_stages TO edp_platform")
    op.execute("ALTER TABLE pipeline_stages ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY tenant_isolation ON pipeline_stages
          USING (tenant_id = app_current_tenant())
          WITH CHECK (tenant_id = app_current_tenant())
        """
    )
    op.execute(
        "CREATE POLICY platform_access ON pipeline_stages TO edp_platform"
        " USING (true) WITH CHECK (true)"
    )
    op.execute("SELECT edp_track_changes('pipeline_stages'::regclass)")
    for table in BEFORE:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    for statement in DATA_INDEX:
        op.execute(statement)
    for position, (code, name, kind, probability, stale_days, color) in enumerate(STAGES):
        stale = "NULL" if stale_days is None else str(stale_days)
        op.execute(
            f"""
            INSERT INTO pipeline_stages
              (id, tenant_id, code, name, position, kind, probability, stale_days, color)
            SELECT gen_random_uuid(), t.id, '{code}', '{name}', {position}, '{kind}', {probability},
              {stale}, '{color}'
            FROM tenants t
            WHERE NOT EXISTS (
              SELECT 1 FROM pipeline_stages s WHERE s.tenant_id = t.id AND s.code = '{code}'
            )
            """
        )
    # 写入阶段触发了延迟的索引触发器，先执行掉再改表结构。
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    for statement in [*OPPORTUNITIES, *ACTIVITIES, *OTHERS]:
        if statement.startswith("RETRACK "):
            for part in _retrack(statement.removeprefix("RETRACK ")):
                op.execute(part)
        else:
            op.execute(statement)
    for table in AFTER:
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in AFTER[1:]:
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("ALTER TABLE tenant_settings RENAME COLUMN opportunities TO prospects")
    op.execute("ALTER TABLE todos DROP COLUMN opportunity_id")
    op.execute("DROP INDEX IF EXISTS uq_opportunity_activities_session")
    op.execute(
        """
        ALTER TABLE opportunity_activities
          DROP COLUMN kind, DROP COLUMN title, DROP COLUMN properties,
          DROP COLUMN linked_type, DROP COLUMN linked_id
        """
    )
    op.execute("UPDATE opportunity_activities SET content = '' WHERE content IS NULL")
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("ALTER TABLE opportunity_activities ALTER COLUMN content SET NOT NULL")
    op.execute("ALTER TABLE opportunity_activities RENAME COLUMN opportunity_id TO prospect_id")
    op.execute("ALTER TABLE opportunity_activities RENAME TO prospect_followups")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_prospect_followups_session
          ON prospect_followups (tenant_id, prospect_id, session_id) WHERE session_id IS NOT NULL
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_opportunities_stage")
    op.execute("DROP INDEX IF EXISTS ix_opportunities_closing")
    op.execute(
        """
        ALTER TABLE opportunities
          DROP COLUMN name, DROP COLUMN stage_id, DROP COLUMN amount, DROP COLUMN expected_close_at,
          DROP COLUMN probability, DROP COLUMN stage_entered_at, DROP COLUMN last_activity_at,
          DROP COLUMN products, DROP COLUMN position, DROP COLUMN contract_id,
          DROP COLUMN lost_reason_code
        """
    )
    op.execute("ALTER TABLE opportunities RENAME COLUMN owner_id TO follower_id")
    op.execute("ALTER TABLE opportunities RENAME TO customer_prospects")
    for part in [*_retrack("customer_prospects"), *_retrack("prospect_followups")]:
        op.execute(part)
    op.execute(
        "UPDATE tenant_data_index SET domain = 'customer_prospects' WHERE domain = 'opportunities'"
    )
    op.execute(
        "UPDATE tenant_data_index SET domain = 'prospect_followups'"
        " WHERE domain = 'opportunity_activities'"
    )
    op.execute("SET CONSTRAINTS ALL IMMEDIATE")
    op.execute("DROP TABLE pipeline_stages")
    for table in ("customer_prospects", "prospect_followups", "tenant_data_index"):
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
