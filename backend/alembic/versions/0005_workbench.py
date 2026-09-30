"""workbench: agent message send status, customer notes and tags, quick replies

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-28
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATEMENTS = [
    # 平台发出的消息（坐席经 API 发送）先写库再发往 IM：pending → sent / failed。
    # 回调和对账入库的消息为空。
    "ALTER TABLE messages ADD COLUMN send_status varchar(16)",
    "ALTER TABLE messages ADD COLUMN send_error text",
    """
    ALTER TABLE messages ADD CONSTRAINT ck_messages_send_status
      CHECK (send_status IN ('pending', 'sent', 'failed'))
    """,
    # 坐席重试发送时按 client_msg_id 幂等。
    """
    CREATE UNIQUE INDEX uq_messages_api_client_msg
      ON messages (tenant_id, room_id, sender_id, client_msg_id) WHERE source = 'api'
    """,
    "ALTER TABLE customers ADD COLUMN notes text",
    "ALTER TABLE customers ADD COLUMN tags text[] NOT NULL DEFAULT '{}'",
    # 快捷话术：owner_id 为空表示全员共享（需要 quick_reply:manage 权限维护）。
    """
    CREATE TABLE quick_replies (
      id uuid PRIMARY KEY,
      tenant_id uuid NOT NULL DEFAULT app_current_tenant() REFERENCES tenants (id),
      owner_id uuid,
      category varchar(32) NOT NULL DEFAULT '',
      title varchar(64) NOT NULL,
      content text NOT NULL,
      sort integer NOT NULL DEFAULT 0,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CONSTRAINT fk_quick_replies_owner FOREIGN KEY (tenant_id, owner_id)
        REFERENCES staff (tenant_id, id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX ix_quick_replies_owner ON quick_replies (tenant_id, owner_id, category, sort)",
    "ALTER TABLE quick_replies ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE quick_replies FORCE ROW LEVEL SECURITY",
    """
    CREATE POLICY tenant_isolation ON quick_replies
      USING (tenant_id = app_current_tenant())
      WITH CHECK (tenant_id = app_current_tenant())
    """,
    "CREATE POLICY platform_access ON quick_replies TO edp_platform USING (true) WITH CHECK (true)",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON quick_replies TO edp_app, edp_platform",
]


def upgrade() -> None:
    for statement in STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS quick_replies")
    op.execute("ALTER TABLE customers DROP COLUMN IF EXISTS tags, DROP COLUMN IF EXISTS notes")
    op.execute("DROP INDEX IF EXISTS uq_messages_api_client_msg")
    op.execute(
        "ALTER TABLE messages DROP COLUMN IF EXISTS send_error, DROP COLUMN IF EXISTS send_status"
    )
