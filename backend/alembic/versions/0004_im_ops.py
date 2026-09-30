"""IM outbox: IM operations written together with session state, executed after commit

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    # 分配、结束、转接等业务状态变化需要在 OpenIM 里拉人、踢人、发提示和信令。
    # 这些操作与业务状态在同一个事务里写入这张表，提交后按 Room 顺序执行，失败的按退避重试。
    """
    CREATE TABLE im_ops (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      room_id uuid NOT NULL,
      op varchar(16) NOT NULL,
      payload jsonb NOT NULL DEFAULT '{}'::jsonb,
      status varchar(16) NOT NULL DEFAULT 'pending',
      attempts integer NOT NULL DEFAULT 0,
      next_attempt_at timestamptz NOT NULL DEFAULT now(),
      last_error text,
      created_at timestamptz NOT NULL DEFAULT now(),
      done_at timestamptz,
      CONSTRAINT fk_im_ops_room FOREIGN KEY (tenant_id, room_id)
        REFERENCES rooms (tenant_id, id) ON DELETE CASCADE,
      CONSTRAINT ck_im_ops_op CHECK (op IN ('invite', 'kick', 'notice', 'signal')),
      CONSTRAINT ck_im_ops_status CHECK (status IN ('pending', 'done', 'failed'))
    )
    """,
    "CREATE INDEX ix_im_ops_pending ON im_ops (next_attempt_at) WHERE status = 'pending'",
    "CREATE INDEX ix_im_ops_room ON im_ops (tenant_id, room_id, id)",
    "ALTER TABLE im_ops ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE im_ops FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY tenant_isolation ON im_ops
      USING (tenant_id = app_current_tenant())
      WITH CHECK (tenant_id = app_current_tenant())
    """,
    "CREATE POLICY platform_access ON im_ops TO edp_platform USING (true) WITH CHECK (true)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON im_ops TO edp_app, edp_platform",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS im_ops")
