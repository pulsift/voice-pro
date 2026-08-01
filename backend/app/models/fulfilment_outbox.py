"""Durable pre-booking intent and fulfilment-webhook delivery state."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class FulfilmentOutbox(Base):
    """One immutable fulfilment intent per attendee and Cal.com slot."""

    __tablename__ = "fulfilment_outbox"
    __table_args__ = (
        Index(
            "ix_fulfilment_outbox_due",
            "state",
            "next_attempt_at",
        ),
    )

    intent_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="awaiting_booking", server_default="awaiting_booking"
    )
    booking_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    booking_start: Mapped[str] = mapped_column(String(64), nullable=False)
    booking_email: Mapped[str] = mapped_column(String(320), nullable=False)
    cal_event_type_id: Mapped[int] = mapped_column(Integer, nullable=False)
    intent_body: Mapped[str] = mapped_column(Text, nullable=False)
    intent_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Null proves no Cal.com create was authorized for this deterministic intent.
    # Once set, every process is reconciliation-only until a bounded not-found
    # sweep explicitly cancels the intent and a later user action restages it.
    booking_dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reconcile_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
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
