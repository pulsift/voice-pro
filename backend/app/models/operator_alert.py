"""Durable, deduplicated operator alerts (the one Slack-bound notification lane).

Distinct from the technical logs lane (structlog + each outbox row's own
`last_error`): a row here holds only plain-English copy for a non-technical
operator to act on. `dedup_key` is the primary key, so staging the same
incident twice is a no-op - callers pick a key that identifies the exact
thing gone wrong (e.g. one call, or one stuck handover's current alert
window) so retries and repeats never spam Slack.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class OperatorAlert(Base):
    """One leased, at-least-once Slack delivery per deduplicated incident."""

    __tablename__ = "operator_alerts"

    dedup_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending"
    )
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )
