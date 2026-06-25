"""SMS API — inbound Telnyx webhook + a threaded inbox (conversations, send, contacts).

Telnyx delivers inbound SMS by webhook only (no native inbox). This stores both
inbound (`message.received`) and outbound (sent here) messages, groups them into
per-number conversations, lets you send replies, and name numbers via contacts.
"""

from datetime import datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.settings import get_user_api_keys
from app.core.auth import CurrentUser, user_id_to_uuid
from app.core.config import settings
from app.core.webhook_security import verify_telnyx_webhook
from app.db.session import get_db
from app.models.phone_number import PhoneNumber
from app.models.sms_contact import SmsContact
from app.models.sms_message import SmsMessage
from app.services.tools.sms_tools import TelnyxSMSTools

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


async def _resolve_telnyx_api_key(user_id: int, db: AsyncSession) -> str | None:
    """Resolve a Telnyx API key: user-level setting, else the platform env key."""
    user_uuid = user_id_to_uuid(user_id)
    user_settings = await get_user_api_keys(user_uuid, db, workspace_id=None)
    key = (user_settings.telnyx_api_key if user_settings else None) or settings.TELNYX_API_KEY
    return key


async def _default_from_number(db: AsyncSession) -> str | None:
    """Pick a default 'from' number (a registered Telnyx number that can send SMS)."""
    row = await db.scalar(
        select(PhoneNumber)
        .where(PhoneNumber.provider == "telnyx", PhoneNumber.can_send_sms.is_(True))
        .order_by(desc(PhoneNumber.created_at))
    )
    return row.phone_number if row else None


# =============================================================================
# Schemas
# =============================================================================


class SmsMessageResponse(BaseModel):
    id: str
    direction: str
    from_number: str | None
    to_number: str | None
    text: str | None
    num_media: int
    received_at: datetime | None
    created_at: datetime


class ConversationResponse(BaseModel):
    contact_number: str
    our_number: str | None
    name: str | None
    last_text: str | None
    last_direction: str | None
    last_at: datetime
    message_count: int


class ContactResponse(BaseModel):
    id: str
    phone_number: str
    name: str
    notes: str | None


class UpsertContactRequest(BaseModel):
    phone_number: str
    name: str
    notes: str | None = None


class SendSmsRequest(BaseModel):
    to: str
    body: str
    from_number: str | None = None


# =============================================================================
# Inbound webhook
# =============================================================================


@webhook_router.post("/telnyx/sms")
async def telnyx_inbound_sms(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Receive inbound SMS from Telnyx (messaging profile webhook target).

    Verifies the ed25519 signature, stores `message.received` inbound events,
    idempotent on the provider message id. Other event types are acknowledged.
    """
    await verify_telnyx_webhook(request)

    body = await request.json()
    data = body.get("data", {}) or {}
    event_type = data.get("event_type")
    payload = data.get("payload", {}) or {}

    if event_type != "message.received" or payload.get("direction") != "inbound":
        return {"status": "ignored", "event_type": event_type}

    provider_message_id = payload.get("id")
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
    logger.info("sms_inbound_stored", from_number=from_number, to_number=to_number)
    return {"status": "stored", "id": str(msg.id)}


# =============================================================================
# Inbox / conversations
# =============================================================================


@router.get("/inbox", response_model=list[SmsMessageResponse])
async def list_inbox(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[SmsMessageResponse]:
    """Flat list of recent inbound messages (newest first)."""
    rows = (
        await db.scalars(
            select(SmsMessage)
            .where(SmsMessage.direction == "inbound")
            .order_by(desc(SmsMessage.created_at))
            .limit(limit)
        )
    ).all()
    return [_to_message_response(r) for r in rows]


@router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ConversationResponse]:
    """Group all messages into per-contact conversations, newest activity first."""
    rows = (await db.scalars(select(SmsMessage).order_by(SmsMessage.created_at))).all()

    # name lookup
    contacts = (await db.scalars(select(SmsContact))).all()
    names = {c.phone_number: c.name for c in contacts}

    convos: dict[str, dict[str, Any]] = {}
    for m in rows:
        contact_number = m.from_number if m.direction == "inbound" else m.to_number
        our_number = m.to_number if m.direction == "inbound" else m.from_number
        if not contact_number:
            continue
        when = m.received_at or m.created_at
        c = convos.get(contact_number)
        if not c:
            convos[contact_number] = {
                "contact_number": contact_number,
                "our_number": our_number,
                "name": names.get(contact_number),
                "last_text": m.text,
                "last_direction": m.direction,
                "last_at": when,
                "message_count": 1,
            }
        else:
            c["message_count"] += 1
            if when >= c["last_at"]:
                c["last_at"] = when
                c["last_text"] = m.text
                c["last_direction"] = m.direction
                c["our_number"] = our_number

    ordered = sorted(convos.values(), key=lambda c: c["last_at"], reverse=True)
    return [ConversationResponse(**c) for c in ordered]


@router.get("/conversations/{contact_number}/messages", response_model=list[SmsMessageResponse])
async def conversation_messages(
    contact_number: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=200, ge=1, le=500),
) -> list[SmsMessageResponse]:
    """All messages exchanged with one number, oldest first (thread order)."""
    rows = (
        await db.scalars(
            select(SmsMessage)
            .where(
                or_(
                    SmsMessage.from_number == contact_number,
                    SmsMessage.to_number == contact_number,
                )
            )
            .order_by(SmsMessage.created_at)
            .limit(limit)
        )
    ).all()
    return [_to_message_response(r) for r in rows]


@router.post("/send", response_model=SmsMessageResponse)
async def send_sms(
    payload: SendSmsRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> SmsMessageResponse:
    """Send an SMS via Telnyx and store it as an outbound message in the thread."""
    api_key = await _resolve_telnyx_api_key(current_user.id, db)
    if not api_key:
        raise HTTPException(status_code=400, detail="No Telnyx API key configured")

    from_number = payload.from_number or await _default_from_number(db)
    if not from_number:
        raise HTTPException(status_code=400, detail="No SMS-capable Telnyx number available")

    tools = TelnyxSMSTools(api_key=api_key, from_number=from_number)
    try:
        result = await tools.send_sms(to=payload.to, body=payload.body)
    finally:
        await tools.close()

    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("error", "Send failed"))

    msg = SmsMessage(
        provider="telnyx",
        provider_message_id=result.get("message_id"),
        direction="outbound",
        from_number=from_number,
        to_number=payload.to,
        text=payload.body,
        num_media=0,
    )
    db.add(msg)
    await db.commit()
    logger.info("sms_outbound_sent", to=payload.to, from_number=from_number)
    return _to_message_response(msg)


# =============================================================================
# Contacts (name a number)
# =============================================================================


@router.get("/contacts", response_model=list[ContactResponse])
async def list_contacts(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ContactResponse]:
    rows = (await db.scalars(select(SmsContact).order_by(SmsContact.name))).all()
    return [
        ContactResponse(id=str(c.id), phone_number=c.phone_number, name=c.name, notes=c.notes)
        for c in rows
    ]


@router.put("/contacts", response_model=ContactResponse)
async def upsert_contact(
    payload: UpsertContactRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ContactResponse:
    """Create or update the name/notes for a phone number (keyed by number)."""
    contact = await db.scalar(
        select(SmsContact).where(SmsContact.phone_number == payload.phone_number)
    )
    if contact:
        contact.name = payload.name
        contact.notes = payload.notes
    else:
        contact = SmsContact(
            phone_number=payload.phone_number, name=payload.name, notes=payload.notes
        )
        db.add(contact)
    await db.commit()
    return ContactResponse(
        id=str(contact.id),
        phone_number=contact.phone_number,
        name=contact.name,
        notes=contact.notes,
    )


def _to_message_response(r: SmsMessage) -> SmsMessageResponse:
    return SmsMessageResponse(
        id=str(r.id),
        direction=r.direction,
        from_number=r.from_number,
        to_number=r.to_number,
        text=r.text,
        num_media=r.num_media,
        received_at=r.received_at,
        created_at=r.created_at,
    )
