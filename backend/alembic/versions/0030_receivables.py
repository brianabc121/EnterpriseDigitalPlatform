"""receivables and the finance position (design §28): the customer's promised payment date and
the latest follow-up on orders; finance settings (daily overdue reminder) on tenant_settings

Revision ID: 0030
Revises: 0029
Create Date: 2026-10-02
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


CONSOLES = ("admin", "supervisor", "agent", "finance", "keeper", "worker", "knowledge")


def _console_check(consoles: tuple[str, ...]) -> str:
    values = ", ".join(f"'{c}'" for c in consoles)
    return (
        "ALTER TABLE roles ADD CONSTRAINT ck_roles_console"
        f" CHECK (console IS NULL OR console IN ({values}))"
    )


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE orders
          ADD COLUMN promise_date date,
          ADD COLUMN followed_up_at timestamptz,
          ADD COLUMN follow_up_note text
        """
    )
    op.execute("ALTER TABLE tenant_settings ADD COLUMN finance jsonb NOT NULL DEFAULT '{}'")
    # 自定义角色可以选择的岗位增加"财务"。
    op.execute("ALTER TABLE roles DROP CONSTRAINT ck_roles_console")
    op.execute(_console_check(CONSOLES))


def downgrade() -> None:
    op.execute("ALTER TABLE roles NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE roles SET console = NULL WHERE console = 'finance'")
    op.execute("ALTER TABLE roles FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE roles DROP CONSTRAINT ck_roles_console")
    op.execute(_console_check(tuple(c for c in CONSOLES if c != "finance")))
    op.execute("ALTER TABLE tenant_settings DROP COLUMN finance")
    op.execute(
        "ALTER TABLE orders DROP COLUMN follow_up_note, DROP COLUMN followed_up_at,"
        " DROP COLUMN promise_date"
    )
