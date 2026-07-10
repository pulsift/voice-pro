"""add booking attempts to call records

Revision ID: 7c11d4a930f2
Revises: 2aeb78a98185
Create Date: 2026-07-10 21:20:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7c11d4a930f2"
down_revision: str | Sequence[str] | None = "2aeb78a98185"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable structured booking diagnostics to existing call records."""
    op.add_column(
        "call_records",
        sa.Column("booking_attempts", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Remove structured booking diagnostics."""
    op.drop_column("call_records", "booking_attempts")
