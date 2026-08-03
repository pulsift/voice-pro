"""add durable operator alert outbox

Revision ID: e8a1f2c3d4b6
Revises: d2f3a4b5c6d7
Create Date: 2026-08-03 00:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e8a1f2c3d4b6"
down_revision: str | Sequence[str] | None = "d2f3a4b5c6d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the durable, deduplicated operator-alert (Slack) outbox."""
    op.create_table(
        "operator_alerts",
        sa.Column("dedup_key", sa.String(length=200), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("state", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("dedup_key"),
    )
    op.create_index(
        "ix_operator_alerts_due",
        "operator_alerts",
        ["state", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the operator-alert outbox."""
    op.drop_index("ix_operator_alerts_due", table_name="operator_alerts")
    op.drop_table("operator_alerts")
