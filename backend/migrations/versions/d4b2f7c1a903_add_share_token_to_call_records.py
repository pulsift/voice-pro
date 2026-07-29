"""add share token to call records

Revision ID: d4b2f7c1a903
Revises: a41be9c60d17
Create Date: 2026-07-29 09:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4b2f7c1a903"
down_revision: str | Sequence[str] | None = "a41be9c60d17"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the capability token backing the public transcript share page (B2)."""
    op.add_column(
        "call_records",
        sa.Column("share_token", sa.String(length=32), nullable=True),
    )
    op.create_index(
        "ix_call_records_share_token",
        "call_records",
        ["share_token"],
        unique=True,
    )


def downgrade() -> None:
    """Remove the transcript share token."""
    op.drop_index("ix_call_records_share_token", table_name="call_records")
    op.drop_column("call_records", "share_token")
