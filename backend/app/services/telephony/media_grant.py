"""Short-lived, DB-authorized, single-use grants for Twilio Media Streams."""

import base64
import hashlib
import hmac
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.call_record import CallRecord

_TOKEN_PREFIX = "mg1"
_TOKEN_PARTS = 3
_SIGNING_CONTEXT = b"twilio-media-v1:"
_GRANT_TTL = timedelta(seconds=120)


def cv_sha256(cv: str) -> str:
    """Hash the exact base64 transport value carried by Twilio."""
    return hashlib.sha256(cv.encode("utf-8")).hexdigest()


def _signature(record_id: uuid.UUID) -> str:
    digest = hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        _SIGNING_CONTEXT + record_id.bytes,
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def create_twilio_media_grant_token(record_id: uuid.UUID) -> str:
    """Return the deterministic opaque capability for one internal call record."""
    return f"{_TOKEN_PREFIX}.{record_id.hex}.{_signature(record_id)}"


def _record_id_from_token(token: object) -> uuid.UUID | None:
    if not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != _TOKEN_PARTS or parts[0] != _TOKEN_PREFIX:
        return None
    try:
        record_id = uuid.UUID(hex=parts[1])
    except (TypeError, ValueError):
        return None
    if not hmac.compare_digest(parts[2], _signature(record_id)):
        return None
    return record_id


def arm_twilio_media_grant(
    record: CallRecord,
    cv: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Arm or refresh one unconsumed grant; a consumed grant is never reissued."""
    if (
        record.provider != "twilio"
        or not record.provider_call_id
        or record.provider_call_id.startswith("pending:")
        or record.agent_id is None
        or record.media_grant_consumed_at is not None
    ):
        return None

    digest = cv_sha256(cv)
    existing_digest = record.media_grant_cv_sha256
    if existing_digest is not None and not hmac.compare_digest(existing_digest, digest):
        return None

    current = now or datetime.now(UTC)
    record.media_grant_cv_sha256 = digest
    record.media_grant_expires_at = current + _GRANT_TTL
    return create_twilio_media_grant_token(record.id)


async def consume_twilio_media_grant(
    *,
    db: AsyncSession,
    token: str,
    call_sid: str,
    agent_id: str,
    workspace_id: str,
    cv: str,
    now: datetime | None = None,
) -> CallRecord | None:
    """Atomically consume an exact Twilio start-frame grant and return its call."""
    record_id = _record_id_from_token(token)
    if record_id is None or not call_sid:
        return None
    try:
        agent_uuid = uuid.UUID(agent_id)
        workspace_uuid = uuid.UUID(workspace_id) if workspace_id else None
    except (TypeError, ValueError):
        return None

    current = now or datetime.now(UTC)
    workspace_filter = (
        CallRecord.workspace_id == workspace_uuid
        if workspace_uuid is not None
        else CallRecord.workspace_id.is_(None)
    )
    statement = (
        update(CallRecord)
        .where(
            CallRecord.id == record_id,
            CallRecord.provider == "twilio",
            CallRecord.provider_call_id == call_sid,
            CallRecord.agent_id == agent_uuid,
            workspace_filter,
            CallRecord.media_grant_cv_sha256 == cv_sha256(cv),
            CallRecord.media_grant_expires_at.is_not(None),
            CallRecord.media_grant_expires_at > current,
            CallRecord.media_grant_consumed_at.is_(None),
        )
        .values(
            media_grant_consumed_at=current,
            # Consuming an authenticated media grant is independent proof the
            # call was answered - same evidence-of-answer principle as
            # fcf2d99's carrier-callback fix, applied to this signal too. Only
            # fill it if no earlier signal already did.
            answered_at=func.coalesce(CallRecord.answered_at, current),
        )
        .returning(CallRecord)
    )
    result = await db.execute(statement)
    record = result.scalar_one_or_none()
    if record is None:
        await db.rollback()
        return None
    await db.commit()
    return record
