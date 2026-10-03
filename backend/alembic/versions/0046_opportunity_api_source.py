"""Opportunities from the open API (design §40.6): allow source 'api'

企业系统通过开放接口创建的线索：商机的来源多一个 api。

Revision ID: 0046
Revises: 0045
Create Date: 2026-10-03
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE opportunities DROP CONSTRAINT ck_opportunities_source")
    op.execute(
        "ALTER TABLE opportunities ADD CONSTRAINT ck_opportunities_source"
        " CHECK (source IN ('ai', 'staff', 'api'))"
    )


def downgrade() -> None:
    op.execute("UPDATE opportunities SET source = 'staff' WHERE source = 'api'")
    op.execute("ALTER TABLE opportunities DROP CONSTRAINT ck_opportunities_source")
    op.execute(
        "ALTER TABLE opportunities ADD CONSTRAINT ck_opportunities_source"
        " CHECK (source IN ('ai', 'staff'))"
    )
