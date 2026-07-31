"""add single-use Twilio media grants

Revision ID: f3e8a1c2d4b5
Revises: d4b2f7c1a903
Create Date: 2026-08-01 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f3e8a1c2d4b5"
down_revision: str | Sequence[str] | None = "d4b2f7c1a903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable grant state so old application code remains compatible."""
    op.add_column(
        "call_records",
        sa.Column("media_grant_cv_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "call_records",
        sa.Column("media_grant_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "call_records",
        sa.Column("media_grant_consumed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Remove Twilio media grant state."""
    op.drop_column("call_records", "media_grant_consumed_at")
    op.drop_column("call_records", "media_grant_expires_at")
    op.drop_column("call_records", "media_grant_cv_sha256")
