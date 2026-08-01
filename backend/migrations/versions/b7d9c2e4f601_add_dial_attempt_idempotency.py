"""add dial-attempt idempotency state

Revision ID: b7d9c2e4f601
Revises: f3e8a1c2d4b5
Create Date: 2026-08-01 03:00:00

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d9c2e4f601"
down_revision: str | Sequence[str] | None = "f3e8a1c2d4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add nullable keyed-dial state for rolling compatibility."""
    op.add_column(
        "call_records",
        sa.Column("dial_attempt_id", sa.Uuid(), nullable=True),
    )
    op.add_column(
        "call_records",
        sa.Column("dial_request_sha256", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "call_records",
        sa.Column("dial_attempt_state", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "call_records",
        sa.Column("dial_attempt_result", sa.JSON(), nullable=True),
    )
    op.create_index(
        "uq_call_records_dial_attempt_id",
        "call_records",
        ["dial_attempt_id"],
        unique=True,
        postgresql_where=sa.text("dial_attempt_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove keyed-dial state."""
    op.drop_index("uq_call_records_dial_attempt_id", table_name="call_records")
    op.drop_column("call_records", "dial_attempt_result")
    op.drop_column("call_records", "dial_attempt_state")
    op.drop_column("call_records", "dial_request_sha256")
    op.drop_column("call_records", "dial_attempt_id")
