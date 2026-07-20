"""add variables to call records

Revision ID: a41be9c60d17
Revises: 7c11d4a930f2
Create Date: 2026-07-20 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a41be9c60d17"
down_revision: str | Sequence[str] | None = "7c11d4a930f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable per-call variables (lead/offer data) to existing call records."""
    op.add_column(
        "call_records",
        sa.Column("variables", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    """Remove per-call variables."""
    op.drop_column("call_records", "variables")
