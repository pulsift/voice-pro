"""add durable call-event outbox

Revision ID: c92f7d1a4e08
Revises: b7d9c2e4f601
Create Date: 2026-08-01 08:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c92f7d1a4e08"
down_revision: str | Sequence[str] | None = "b7d9c2e4f601"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add only nullable/additive call state plus a new outbox table."""
    op.add_column(
        "call_records",
        sa.Column("media_finalized_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "call_event_outbox",
        sa.Column("call_id", sa.Uuid(), nullable=False),
        sa.Column(
            "state",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("carrier_terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("payload_body", sa.Text(), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["call_id"], ["call_records.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("call_id"),
    )
    op.create_index(
        "ix_call_event_outbox_due",
        "call_event_outbox",
        ["state", "next_attempt_at", "available_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the call-event outbox and media finalization marker."""
    op.drop_index("ix_call_event_outbox_due", table_name="call_event_outbox")
    op.drop_table("call_event_outbox")
    op.drop_column("call_records", "media_finalized_at")
