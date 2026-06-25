"""SMS message model — stores inbound (and optionally outbound) SMS.

Used by the Telnyx inbound-SMS webhook so received texts (e.g. one-time
verification codes sent to a Telnyx number) are persisted and readable via the
authenticated SMS inbox endpoint, instead of being dropped (Telnyx delivers
inbound SMS by webhook only — there is no native inbox to read).
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SmsMessage(Base):
    """A single SMS message (inbound by default)."""

    __tablename__ = "sms_messages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)

    provider: Mapped[str] = mapped_column(
        String(50), nullable=False, default="telnyx", comment="Provider: telnyx or twilio"
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        unique=True,
        comment="Provider message id (for idempotency)",
    )

    direction: Mapped[str] = mapped_column(
        String(20), nullable=False, default="inbound", comment="inbound or outbound"
    )
    from_number: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True, comment="Sender phone number"
    )
    to_number: Mapped[str | None] = mapped_column(
        String(50), nullable=True, index=True, comment="Recipient phone number (our Telnyx number)"
    )
    text: Mapped[str | None] = mapped_column(Text, nullable=True, comment="Message body")

    messaging_profile_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Telnyx messaging profile id"
    )
    num_media: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Number of media attachments (MMS)"
    )
    raw: Mapped[dict[str, Any] | None] = mapped_column(
        JSON, nullable=True, comment="Full provider payload for debugging"
    )

    received_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="When the provider received the message"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
        index=True,
        comment="When we stored the message",
    )

    def __repr__(self) -> str:
        return (
            f"<SmsMessage(id={self.id}, direction={self.direction}, "
            f"from={self.from_number}, to={self.to_number})>"
        )
