"""SMS contact — an optional friendly name/profile for a phone number.

Lets the Messages inbox show "Acme Solar" instead of +1408…; the raw number is
always still available. Keyed by E.164 phone number.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import DateTime, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class SmsContact(Base):
    """A named profile for a phone number used in the SMS inbox."""

    __tablename__ = "sms_contacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    phone_number: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True, comment="E.164 phone number"
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, comment="Display name")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<SmsContact(phone={self.phone_number}, name={self.name})>"
