"""SMS API — inbound Telnyx SMS webhook + an authenticated inbox to read them.

Telnyx delivers inbound SMS by webhook only (no native inbox). This stores the
`message.received` events so received texts — e.g. one-time verification codes
sent to our Telnyx number — can be read back via GET /api/v1/sms/inbox.
"""

from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser
from app.core.webhook_security import verify_telnyx_webhook
from app.db.session import get_db
from app.models.sms_message import SmsMessage

router = APIRouter(prefix="/api/v1/sms", tags=["sms"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = structlog.get_logger()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


class SmsMessageResponse(BaseModel):
    """A stored SMS message."""

    id: str
    provider: str
    direction: str
    from_number: str | None
    to_number: str | None
    text: str | None
    num_media: int
    received_at: datetime | None
    created_at: datetime


@webhook_router.post("/telnyx/sms")
async def telnyx_inbound_sms(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Receive inbound SMS from Telnyx (messaging profile webhook target).

    Verifies the Telnyx ed25519 signature, then stores `message.received`
    inbound events. Outbound delivery receipts and other event types are
    acknowledged but not stored. Idempotent on the provider message id.
    """
    # Telnyx signs every webhook (ed25519). Reuses the same verifier as the
    # voice webhooks; raises 403 on a bad/missing signature (skipped in DEBUG).
    await verify_telnyx_webhook(request)

    body = await request.json()
    data = body.get("data", {}) or {}
    event_type = data.get("event_type")
    payload = data.get("payload", {}) or {}

    # Only persist inbound received messages.
    if event_type != "message.received" or payload.get("direction") != "inbound":
        return {"status": "ignored", "event_type": event_type}

    provider_message_id = payload.get("id")

    # Idempotency: Telnyx retries; don't double-store.
    if provider_message_id:
        existing = await db.scalar(
            select(SmsMessage).where(SmsMessage.provider_message_id == provider_message_id)
        )
        if existing:
            return {"status": "duplicate", "id": str(existing.id)}

    from_number = (payload.get("from") or {}).get("phone_number")
    to_list = payload.get("to") or []
    to_number = to_list[0].get("phone_number") if to_list else None

    msg = SmsMessage(
        provider="telnyx",
        provider_message_id=provider_message_id,
        direction="inbound",
        from_number=from_number,
        to_number=to_number,
        text=payload.get("text"),
        messaging_profile_id=payload.get("messaging_profile_id"),
        num_media=len(payload.get("media") or []),
        raw=payload,
        received_at=_parse_dt(payload.get("received_at")),
    )
    db.add(msg)
    await db.commit()

    logger.info(
        "sms_inbound_stored",
        from_number=from_number,
        to_number=to_number,
        provider_message_id=provider_message_id,
    )
    return {"status": "stored", "id": str(msg.id)}


@router.get("/inbox", response_model=list[SmsMessageResponse])
async def list_inbox(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    to_number: str | None = Query(default=None, description="Filter by recipient (our) number"),
    from_number: str | None = Query(default=None, description="Filter by sender number"),
    limit: int = Query(default=20, ge=1, le=200),
) -> list[SmsMessageResponse]:
    """List recent inbound SMS, newest first. Requires authentication."""
    query = select(SmsMessage).where(SmsMessage.direction == "inbound")
    if to_number:
        query = query.where(SmsMessage.to_number == to_number)
    if from_number:
        query = query.where(SmsMessage.from_number == from_number)
    query = query.order_by(desc(SmsMessage.created_at)).limit(limit)

    rows = (await db.scalars(query)).all()
    return [
        SmsMessageResponse(
            id=str(r.id),
            provider=r.provider,
            direction=r.direction,
            from_number=r.from_number,
            to_number=r.to_number,
            text=r.text,
            num_media=r.num_media,
            received_at=r.received_at,
            created_at=r.created_at,
        )
        for r in rows
    ]
