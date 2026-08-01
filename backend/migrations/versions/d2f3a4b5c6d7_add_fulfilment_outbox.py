"""add durable fulfilment intent outbox

Revision ID: d2f3a4b5c6d7
Revises: c92f7d1a4e08
Create Date: 2026-08-01 12:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f3a4b5c6d7"
down_revision: str | Sequence[str] | None = "c92f7d1a4e08"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the pre-booking intent and leased delivery outbox."""
    op.create_table(
        "fulfilment_outbox",
        sa.Column("intent_key", sa.String(length=64), nullable=False),
        sa.Column(
            "state",
            sa.String(length=24),
            server_default="awaiting_booking",
            nullable=False,
        ),
        sa.Column("booking_id", sa.String(length=255), nullable=True),
        sa.Column("booking_start", sa.String(length=64), nullable=False),
        sa.Column("booking_email", sa.String(length=320), nullable=False),
        sa.Column("cal_event_type_id", sa.Integer(), nullable=False),
        sa.Column("intent_body", sa.Text(), nullable=False),
        sa.Column("intent_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload_body", sa.Text(), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=True),
        sa.Column("booking_dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reconcile_until", sa.DateTime(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint("intent_key"),
        sa.UniqueConstraint("booking_id", name="uq_fulfilment_outbox_booking_id"),
    )
    op.create_index(
        "ix_fulfilment_outbox_due",
        "fulfilment_outbox",
        ["state", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove the fulfilment outbox."""
    op.drop_index("ix_fulfilment_outbox_due", table_name="fulfilment_outbox")
    op.drop_table("fulfilment_outbox")
