"""SMS API — inbound Telnyx webhook + a threaded inbox (conversations, send, contacts).

Telnyx delivers inbound SMS by webhook only (no native inbox). This stores both
inbound (`message.received`) and outbound (sent here) messages, groups them into
per-number conversations, lets you send replies, and name numbers via contacts.

Outbound sends are routed by the *from* number's provider: Telnyx numbers go out
over the Telnyx REST API, Twilio numbers over the Twilio REST SDK. Both persist
into the same `sms_messages` table so the thread view stays provider-agnostic.
"""

import asyncio
from datetime import UTC, datetime
from typing import Any

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel
from sqlalchemy import desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

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

# Providers this API can actually send through. Anything else on a phone-number
# row is a config error and is rejected with a 400 (never a 500 at send time).
SMS_SEND_PROVIDERS = ("telnyx", "twilio")
DEFAULT_SMS_PROVIDER = "telnyx"


def _twilio_webhook_url(path: str) -> str:
    """The URL Twilio SIGNED, which is not the URL this process sees.

    Railway terminates TLS, so `request.url` inside the app reports `http://` and
    can carry an internal host. Rebuilding the signed string from it fails EVERY
    signature — which looks exactly like the security working while every inbound
    message is silently refused. X-Forwarded-* is not the fix either: it is
    caller-controlled, so trusting it to validate a signature is circular.

    The canonical public origin is configuration, and it is already set in
    production for the transcript share links.
    """
    base = (settings.PUBLIC_BASE_URL or settings.PUBLIC_URL or "").rstrip("/")
    if not base:
        raise HTTPException(
            status_code=503,
            detail="PUBLIC_BASE_URL is not configured; cannot verify Twilio signatures",
        )
    return f"{base}{path}"


def _verify_twilio_signature(request: Request, params: dict[str, str]) -> None:
    """Refuse anything Twilio did not sign.

    The HMAC key is the account's classic Auth Token specifically — an API-key
    secret does not validate. Validation covers EVERY received parameter, never a
    hand-picked list: Twilio adds parameters without notice, and an allowlist
    starts rejecting real messages the day they do.
    """
    token = settings.TWILIO_AUTH_TOKEN
    if not token:
        raise HTTPException(
            status_code=503, detail="Twilio auth token is not configured"
        )
    signature = request.headers.get("X-Twilio-Signature", "")
    url = _twilio_webhook_url(request.url.path)
    if not RequestValidator(token).validate(url, params, signature):
        logger.warning(
            "twilio_webhook_signature_rejected",
            path=request.url.path,
            has_signature=bool(signature),
            param_count=len(params),
        )
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")


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


async def _resolve_twilio_credentials(
    user_id: int, db: AsyncSession
) -> tuple[str | None, str | None]:
    """Resolve Twilio creds: user-level settings, else the platform env creds."""
    user_uuid = user_id_to_uuid(user_id)
    user_settings = await get_user_api_keys(user_uuid, db, workspace_id=None)
    account_sid = (
        user_settings.twilio_account_sid if user_settings else None
    ) or settings.TWILIO_ACCOUNT_SID
    auth_token = (
        user_settings.twilio_auth_token if user_settings else None
    ) or settings.TWILIO_AUTH_TOKEN
    return account_sid, auth_token


async def _default_from_number(db: AsyncSession) -> str | None:
    """Pick a default 'from' number (a registered number that can send SMS).

    Telnyx is preferred (historical default); Twilio is used when no SMS-capable
    Telnyx number is registered.
    """
    for provider in SMS_SEND_PROVIDERS:
        row = await db.scalar(
            select(PhoneNumber)
            .where(PhoneNumber.provider == provider, PhoneNumber.can_send_sms.is_(True))
            .order_by(desc(PhoneNumber.created_at))
        )
        if row:
            return row.phone_number
    return None


async def _provider_for_number(from_number: str, db: AsyncSession) -> str:
    """Which provider owns this 'from' number (defaults to Telnyx if unregistered)."""
    row = await db.scalar(select(PhoneNumber).where(PhoneNumber.phone_number == from_number))
    provider = (row.provider if row else None) or DEFAULT_SMS_PROVIDER
    provider = provider.lower()
    if provider not in SMS_SEND_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported SMS provider '{provider}' for number {from_number}",
        )
    return provider


async def _send_via_telnyx(
    user_id: int, db: AsyncSession, from_number: str, to: str, body: str
) -> dict[str, Any]:
    """Send over Telnyx. Returns the tools' {success, message_id, error} shape."""
    api_key = await _resolve_telnyx_api_key(user_id, db)
    if not api_key:
        raise HTTPException(status_code=400, detail="No Telnyx API key configured")

    tools = TelnyxSMSTools(api_key=api_key, from_number=from_number)
    try:
        return await tools.send_sms(to=to, body=body)
    finally:
        await tools.close()


async def _send_via_twilio(
    user_id: int, db: AsyncSession, from_number: str, to: str, body: str
) -> dict[str, Any]:
    """Send over the Twilio REST SDK, mirroring the Telnyx result shape.

    The Twilio SDK is synchronous, so the network call runs in a worker thread.
    REST errors are folded into {"success": False, "error": ...} so the caller
    returns the same HTTP error the Telnyx path does (never a bare 500).
    """
    account_sid, auth_token = await _resolve_twilio_credentials(user_id, db)
    if not (account_sid and auth_token):
        raise HTTPException(status_code=400, detail="No Twilio credentials configured")

    client = Client(account_sid, auth_token)
    try:
        message = await asyncio.to_thread(
            client.messages.create, to=to, from_=from_number, body=body
        )
    except TwilioRestException as e:
        logger.warning(
            "sms_twilio_send_failed", status=e.status, code=e.code, error=e.msg or str(e)
        )
        detail = e.msg or str(e)
        if e.code:
            detail = f"{detail} (Twilio error {e.code})"
        return {"success": False, "error": detail}
    except TwilioException as e:
        logger.warning("sms_twilio_send_failed", error=str(e))
        return {"success": False, "error": str(e)}

    return {
        "success": True,
        "message_id": message.sid,
        "to": to,
        "from": from_number,
        "status": getattr(message, "status", None),
    }


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
    # Which carrier carried this one. Two providers are live at once and they do
    # NOT have the same capabilities — until A2P 10DLC is approved, Twilio can
    # receive from US numbers but cannot send to them. Without the provider on
    # the message, a send that will be rejected looks like one that will not.
    provider: str


class OurNumberResponse(BaseModel):
    """One of our own numbers, as something to pick from."""

    number: str
    provider: str
    message_count: int
    last_at: datetime | None
    # Plain language, because "A2P 10DLC pending" is not something anyone should
    # have to hold in their head to know whether a text will actually arrive.
    can_send_to_us: bool
    note: str


class ConversationResponse(BaseModel):
    contact_number: str
    our_number: str | None
    name: str | None
    last_text: str | None
    last_direction: str | None
    last_at: datetime
    message_count: int
    provider: str


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


@webhook_router.post("/twilio/sms")
async def twilio_inbound_sms(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Receive inbound SMS on the Twilio number.

    This closes a hole that has been open, silently, for as long as the number
    has existed: `+16693694746` is the caller ID the voice agent dials prospects
    from, and its `sms_url` at Twilio was never set. A prospect who sees a missed
    call and texts back reaches nothing — Twilio accepts the message and drops
    it. Nobody sees it and nobody is told.

    Receiving needs no A2P 10DLC registration; only US-bound SENDING does. So
    this is safe to ship on its own, ahead of any decision about which number
    Sami texts from.

    Twilio expects TwiML or an empty 204. A JSON body makes it log warning 12300
    on every single message, which pollutes the very debugger you read when a
    webhook misbehaves.
    """
    form = await request.form()
    params = {key: str(value) for key, value in form.items()}
    _verify_twilio_signature(request, params)

    provider_message_id = params.get("MessageSid") or params.get("SmsSid")
    if provider_message_id:
        # Twilio retries on any non-2xx or timeout, so a repeat is ordinary
        # traffic rather than an error.
        existing = await db.scalar(
            select(SmsMessage).where(SmsMessage.provider_message_id == provider_message_id)
        )
        if existing:
            return Response(status_code=204)

    msg = SmsMessage(
        provider="twilio",
        provider_message_id=provider_message_id,
        direction="inbound",
        from_number=params.get("From"),
        to_number=params.get("To"),
        text=params.get("Body"),
        messaging_profile_id=params.get("MessagingServiceSid"),
        num_media=int(params.get("NumMedia") or 0),
        raw=params,
        received_at=datetime.now(UTC),
    )
    db.add(msg)
    await db.commit()
    logger.info(
        "sms_inbound_stored",
        provider="twilio",
        from_number=params.get("From"),
        to_number=params.get("To"),
    )
    return Response(status_code=204)


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


@router.get("/numbers", response_model=list[OurNumberResponse])
async def list_our_numbers(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[OurNumberResponse]:
    """Our own numbers, as a thing to pick from, busiest first.

    Sami's design: pick a number, see that number's history, with the provider
    shown. Two providers run at once and they are not interchangeable — Twilio
    receives from US numbers today but cannot SEND to them until A2P 10DLC is
    approved, and a rejected send (carrier error 30034) is indistinguishable from
    a delivered one unless you already know which number you used.
    """
    rows = (await db.scalars(select(SmsMessage))).all()

    numbers: dict[str, dict[str, Any]] = {}
    for message in rows:
        our_number = message.to_number if message.direction == "inbound" else message.from_number
        if not our_number:
            continue
        when = message.received_at or message.created_at
        entry = numbers.setdefault(
            our_number,
            {"number": our_number, "provider": message.provider, "message_count": 0,
             "last_at": None},
        )
        entry["message_count"] += 1
        if entry["last_at"] is None or when > entry["last_at"]:
            entry["last_at"] = when
            entry["provider"] = message.provider

    return [
        OurNumberResponse(
            **entry,
            can_send_to_us=entry["provider"] != "twilio",
            note=(
                "Receiving works. Sending to US numbers is rejected by the carriers "
                "until A2P 10DLC registration is approved."
                if entry["provider"] == "twilio"
                else "Sending and receiving both work."
            ),
        )
        for entry in sorted(
            numbers.values(), key=lambda item: item["message_count"], reverse=True
        )
    ]


@router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    our_number: str | None = Query(
        default=None,
        description="Show only conversations that ran through one of our numbers.",
    ),
) -> list[ConversationResponse]:
    """Group all messages into per-contact conversations, newest activity first.

    `our_number` filters to one of our own numbers — the same thread list, seen
    through one line instead of all of them.
    """
    rows = (await db.scalars(select(SmsMessage).order_by(SmsMessage.created_at))).all()

    # name lookup
    contacts = (await db.scalars(select(SmsContact))).all()
    names = {c.phone_number: c.name for c in contacts}

    convos: dict[str, dict[str, Any]] = {}
    for m in rows:
        contact_number = m.from_number if m.direction == "inbound" else m.to_number
        line = m.to_number if m.direction == "inbound" else m.from_number
        if not contact_number:
            continue
        if our_number and line != our_number:
            continue
        when = m.received_at or m.created_at
        c = convos.get(contact_number)
        if not c:
            convos[contact_number] = {
                "contact_number": contact_number,
                "our_number": line,
                "name": names.get(contact_number),
                "last_text": m.text,
                "last_direction": m.direction,
                "last_at": when,
                "message_count": 1,
                "provider": m.provider,
            }
        else:
            c["message_count"] += 1
            if when >= c["last_at"]:
                c["last_at"] = when
                c["last_text"] = m.text
                c["last_direction"] = m.direction
                c["our_number"] = line
                c["provider"] = m.provider

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
    """Send an SMS (Telnyx or Twilio, by the from-number's provider) and store it."""
    from_number = payload.from_number or await _default_from_number(db)
    if not from_number:
        raise HTTPException(status_code=400, detail="No SMS-capable phone number available")

    provider = await _provider_for_number(from_number, db)

    if provider == "twilio":
        result = await _send_via_twilio(current_user.id, db, from_number, payload.to, payload.body)
    else:
        result = await _send_via_telnyx(current_user.id, db, from_number, payload.to, payload.body)

    if not result.get("success"):
        raise HTTPException(status_code=502, detail=result.get("error", "Send failed"))

    msg = SmsMessage(
        provider=provider,
        provider_message_id=result.get("message_id"),
        direction="outbound",
        from_number=from_number,
        to_number=payload.to,
        text=payload.body,
        num_media=0,
    )
    db.add(msg)
    await db.commit()
    logger.info("sms_outbound_sent", to=payload.to, from_number=from_number, provider=provider)
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
        provider=r.provider,
    )
