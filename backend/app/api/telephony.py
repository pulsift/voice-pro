"""Telephony API routes for Twilio and Telnyx integration.

This module provides:
- Webhook endpoints for inbound calls (Twilio/Telnyx)
- Phone number management (list, search, buy, release)
- Outbound call initiation
- Call status callbacks
- WebSocket endpoint for telephony media streaming
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.settings import get_user_api_keys
from app.core.auth import CurrentUser, user_id_to_uuid
from app.core.config import settings
from app.core.limiter import limiter
from app.core.webhook_security import verify_telnyx_webhook, verify_twilio_webhook
from app.db.session import get_db
from app.models.agent import Agent
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.campaign import Campaign, CampaignContact, CampaignContactStatus
from app.models.workspace import AgentWorkspace, Workspace
from app.services.telephony.telnyx_service import TelnyxService, is_unknown_telnyx_dial_outcome
from app.services.telephony.twilio_service import TwilioService

if TYPE_CHECKING:
    from app.services.telephony.base import PhoneNumber

router = APIRouter(prefix="/api/v1/telephony", tags=["telephony"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])

logger = structlog.get_logger()

_TERMINAL_CALL_STATUSES = {
    CallStatus.COMPLETED.value,
    CallStatus.FAILED.value,
    CallStatus.BUSY.value,
    CallStatus.NO_ANSWER.value,
    CallStatus.CANCELED.value,
}


def _parse_telnyx_timestamp(value: Any) -> datetime | None:
    """Parse an optional Telnyx event timestamp without trusting local time."""
    if not value:
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(float(value), tz=UTC)
        text = str(value).strip()
        if text.isdigit():
            return datetime.fromtimestamp(float(text), tz=UTC)
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _parse_telnyx_duration(value: Any) -> int | None:
    """Return a non-negative provider duration, or None when absent/invalid."""
    if value in (None, ""):
        return None
    try:
        return max(0, int(float(str(value))))
    except (TypeError, ValueError):
        return None


def _telnyx_form_event(form: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Translate Telnyx TeXML's TwiML-style form fields to one lifecycle event."""
    raw_status = str(form.get("CallStatus") or form.get("call_status") or "").lower()
    normalized = raw_status.replace("-", "_")
    event_map = {
        "queued": "call.initiated",
        "initiated": "call.initiated",
        "ringing": "call.ringing",
        "answered": "call.answered",
        "in_progress": "call.answered",
        "completed": "call.hangup",
        "busy": "call.hangup",
        "no_answer": "call.hangup",
        "failed": "call.hangup",
        "canceled": "call.hangup",
        "cancelled": "call.hangup",
    }
    payload = dict(form)
    cause_map = {
        "busy": "USER_BUSY",
        "no_answer": "NO_ANSWER",
        "failed": "FAILED",
        "canceled": "ORIGINATOR_CANCEL",
        "cancelled": "ORIGINATOR_CANCEL",
    }
    if normalized in cause_map and not payload.get("hangup_cause"):
        payload["hangup_cause"] = cause_map[normalized]
    return event_map.get(normalized, ""), payload


def _telnyx_terminal_status(payload: dict[str, Any]) -> str:
    """Map a Telnyx hangup cause to the durable CallRecord status."""
    hangup_cause = (
        str(payload.get("hangup_cause") or payload.get("HangupCause") or "").strip().upper()
    )
    if hangup_cause == "USER_BUSY":
        return CallStatus.BUSY.value
    if hangup_cause == "NO_ANSWER":
        return CallStatus.NO_ANSWER.value
    if hangup_cause in ("CALL_REJECTED", "ORIGINATOR_CANCEL"):
        return CallStatus.CANCELED.value
    if hangup_cause and hangup_cause not in ("NORMAL_CLEARING", "NORMAL_RELEASE"):
        return CallStatus.FAILED.value
    return CallStatus.COMPLETED.value


def _apply_telnyx_lifecycle_event(
    call_record: CallRecord,
    event_type: str,
    payload: dict[str, Any],
    *,
    event_at: datetime,
    provider_duration: int | None,
) -> None:
    """Apply one Telnyx lifecycle event idempotently."""
    was_terminal = call_record.status in _TERMINAL_CALL_STATUSES
    if event_type == "call.initiated" and not was_terminal:
        call_record.status = CallStatus.INITIATED.value
    elif event_type == "call.ringing" and not was_terminal:
        call_record.status = CallStatus.RINGING.value
    elif event_type == "call.answered":
        if not call_record.answered_at:
            call_record.answered_at = event_at
        if not was_terminal:
            call_record.status = CallStatus.IN_PROGRESS.value
    elif event_type == "call.hangup":
        if not call_record.ended_at:
            call_record.ended_at = event_at
        terminal_status = _telnyx_terminal_status(payload)
        # Media stop can establish generic completion before the signed callback.
        # Let a specific carrier outcome refine it, but never let a later generic
        # completion erase busy/no-answer/canceled/failed evidence.
        if not was_terminal or (
            call_record.status == CallStatus.COMPLETED.value
            and terminal_status != CallStatus.COMPLETED.value
        ):
            call_record.status = terminal_status

        if provider_duration is not None:
            call_record.duration_seconds = provider_duration
        elif call_record.answered_at and call_record.ended_at:
            elapsed = (call_record.ended_at - call_record.answered_at).total_seconds()
            call_record.duration_seconds = max(0, int(elapsed))


def _telnyx_phone_number(value: Any) -> str:
    """Extract an E.164-like number from either TeXML or Call Control shapes."""
    if isinstance(value, dict):
        value = value.get("phone_number") or value.get("number")
    return str(value or "")


async def _find_telnyx_lifecycle_record(
    *,
    identifiers: set[str],
    from_number: str,
    to_number: str,
    db: AsyncSession,
) -> tuple[CallRecord | None, int]:
    """Lock an exact record or reconcile one pre-dial pending row with bounded retries."""
    candidate_count = 0
    for delay in (0.0, 0.05, 0.1, 0.2, 0.4):
        if delay:
            await db.rollback()
            await asyncio.sleep(delay)

        exact = await db.execute(
            select(CallRecord)
            .where(
                CallRecord.provider == "telnyx",
                or_(*(CallRecord.provider_call_id == value for value in identifiers)),
            )
            .limit(2)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        candidates = exact.scalars().all()
        candidate_count = len(candidates)
        if candidate_count == 1:
            return candidates[0], candidate_count

        if candidate_count == 0 and from_number and to_number:
            pending = await db.execute(
                select(CallRecord.id)
                .where(
                    CallRecord.provider == "telnyx",
                    CallRecord.provider_call_id.like("pending:%"),
                    CallRecord.from_number == from_number,
                    CallRecord.to_number == to_number,
                    CallRecord.created_at >= datetime.now(UTC) - timedelta(minutes=2),
                    CallRecord.ended_at.is_(None),
                )
                .order_by(CallRecord.created_at.desc())
                .limit(2)
            )
            pending_ids = pending.scalars().all()
            candidate_count = len(pending_ids)
            if candidate_count == 1:
                locked = await db.execute(
                    select(CallRecord)
                    .where(CallRecord.id == pending_ids[0])
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                record = locked.scalar_one()
                if record.provider_call_id.startswith("pending:"):
                    record.provider_call_id = sorted(identifiers)[0]
                return record, candidate_count

    return None, candidate_count


# =============================================================================
# Pydantic Models
# =============================================================================


class PhoneNumberResponse(BaseModel):
    """Phone number response."""

    id: str
    phone_number: str
    friendly_name: str | None = None
    provider: str
    capabilities: dict[str, bool] | None = None
    assigned_agent_id: str | None = None


class SearchPhoneNumbersRequest(BaseModel):
    """Request to search for phone numbers."""

    provider: str  # "twilio" or "telnyx"
    country: str = "US"
    area_code: str | None = None
    contains: str | None = None
    limit: int = 10


class PurchasePhoneNumberRequest(BaseModel):
    """Request to purchase a phone number."""

    provider: str  # "twilio" or "telnyx"
    phone_number: str


class InitiateCallRequest(BaseModel):
    """Request to initiate an outbound call."""

    to_number: str
    from_number: str
    agent_id: str
    # Per-call lead/offer data (leadName, company, offer_name, leadEmail, tzName, ...)
    # forwarded to the agent via the answer-webhook Url (?cv=) to personalize the call.
    variables: dict[str, Any] | None = None


class CallResponse(BaseModel):
    """Call response."""

    call_id: str
    call_control_id: str | None = None
    from_number: str
    to_number: str
    direction: str
    status: str
    agent_id: str | None = None


# =============================================================================
# Helper Functions
# =============================================================================


async def get_twilio_service(
    user_id: int, db: AsyncSession, workspace_id: uuid.UUID | None = None
) -> TwilioService | None:
    """Get Twilio service for a user.

    Args:
        user_id: User ID (int)
        db: Database session
        workspace_id: Workspace UUID (required for workspace-specific API keys)
    """
    user_uuid = user_id_to_uuid(user_id)
    user_settings = await get_user_api_keys(user_uuid, db, workspace_id=workspace_id)

    account_sid = user_settings.twilio_account_sid if user_settings else None
    auth_token = user_settings.twilio_auth_token if user_settings else None

    # Fall back to the user-level creds, then the platform env creds (single-tenant
    # own-tool; mirrors get_telnyx_service). This is what makes Twilio resolvable from
    # settings.TWILIO_ACCOUNT_SID + settings.TWILIO_AUTH_TOKEN.
    if (not account_sid or not auth_token) and workspace_id:
        ul = await get_user_api_keys(user_uuid, db, workspace_id=None)
        if ul and ul.twilio_account_sid and ul.twilio_auth_token:
            account_sid, auth_token = ul.twilio_account_sid, ul.twilio_auth_token
    if not account_sid or not auth_token:
        account_sid = account_sid or settings.TWILIO_ACCOUNT_SID
        auth_token = auth_token or settings.TWILIO_AUTH_TOKEN

    if not account_sid or not auth_token:
        return None

    return TwilioService(account_sid=account_sid, auth_token=auth_token)


async def get_telnyx_service(
    user_id: int, db: AsyncSession, workspace_id: uuid.UUID | None = None
) -> TelnyxService | None:
    """Get Telnyx service for a user.

    Args:
        user_id: User ID (int)
        db: Database session
        workspace_id: Workspace UUID (required for workspace-specific API keys)
    """
    user_uuid = user_id_to_uuid(user_id)
    user_settings = await get_user_api_keys(user_uuid, db, workspace_id=workspace_id)
    api_key = user_settings.telnyx_api_key if user_settings else None
    public_key = user_settings.telnyx_public_key if user_settings else None

    # Fall back to the user-level key, then the platform env key (single-tenant own-tool;
    # keys live at account level, and there may be no workspace).
    if not api_key and workspace_id:
        ul = await get_user_api_keys(user_uuid, db, workspace_id=None)
        if ul and ul.telnyx_api_key:
            api_key, public_key = ul.telnyx_api_key, ul.telnyx_public_key
    if not api_key:
        api_key = settings.TELNYX_API_KEY
        public_key = public_key or settings.TELNYX_PUBLIC_KEY

    if not api_key:
        return None

    return TelnyxService(api_key=api_key, public_key=public_key)


def select_outbound_provider(
    preferred: str | None, *, has_telnyx: bool, has_twilio: bool
) -> str | None:
    """Pick the outbound telephony provider.

    Honours the configured preference (`TELEPHONY_OUTBOUND_PROVIDER`, default "twilio")
    and falls back to the other provider only if the preferred one isn't configured.
    Returns None when neither is available. This is what keeps Telnyx dormant while
    Twilio is present, and re-enables Telnyx by flipping the preference.
    """
    pref = (preferred or "twilio").lower()
    if pref == "telnyx":
        return "telnyx" if has_telnyx else ("twilio" if has_twilio else None)
    return "twilio" if has_twilio else ("telnyx" if has_telnyx else None)


async def get_agent_by_phone_number(phone_number: str, db: AsyncSession) -> Agent | None:
    """Find agent by assigned phone number."""
    # Remove + prefix for comparison if present
    normalized = phone_number.lstrip("+")

    result = await db.execute(
        select(Agent).where(
            (Agent.phone_number_id == phone_number)
            | (Agent.phone_number_id == normalized)
            | (Agent.phone_number_id == f"+{normalized}")
        )
    )
    return result.scalar_one_or_none()


async def get_agent_workspace_id(agent_id: uuid.UUID, db: AsyncSession) -> uuid.UUID | None:
    """Get the workspace ID for an agent.

    Args:
        agent_id: Agent UUID
        db: Database session

    Returns:
        Workspace UUID if agent belongs to a workspace, None otherwise
    """
    result = await db.execute(select(AgentWorkspace).where(AgentWorkspace.agent_id == agent_id))
    memberships = result.scalars().all()
    if len(memberships) == 1:
        return memberships[0].workspace_id
    defaults = [membership.workspace_id for membership in memberships if membership.is_default]
    return defaults[0] if len(defaults) == 1 else None


async def resolve_outbound_workspace_id(
    *,
    agent_id: uuid.UUID,
    owner_user_id: int,
    requested_workspace_id: uuid.UUID | None,
    db: AsyncSession,
) -> uuid.UUID | None:
    """Resolve one owner-scoped workspace, refusing ambiguous multi-workspace calls."""
    result = await db.execute(
        select(AgentWorkspace)
        .join(Workspace, Workspace.id == AgentWorkspace.workspace_id)
        .where(
            AgentWorkspace.agent_id == agent_id,
            Workspace.user_id == owner_user_id,
        )
    )
    memberships = result.scalars().all()
    if requested_workspace_id is not None:
        if not any(row.workspace_id == requested_workspace_id for row in memberships):
            raise HTTPException(status_code=400, detail="Agent does not belong to that workspace")
        return requested_workspace_id
    if len(memberships) <= 1:
        return memberships[0].workspace_id if memberships else None
    defaults = [row.workspace_id for row in memberships if row.is_default]
    if len(defaults) == 1:
        return defaults[0]
    raise HTTPException(
        status_code=400,
        detail="workspace_id is required because the agent has multiple workspaces",
    )


async def update_campaign_contact_from_call(
    call_record: CallRecord,
    call_status: str,
    duration_seconds: int,
    db: AsyncSession,
) -> None:
    """Update campaign contact status based on call outcome.

    Args:
        call_record: Call record that just completed
        call_status: Final call status (completed, busy, failed, no-answer, etc.)
        duration_seconds: Call duration in seconds
        db: Database session
    """
    # A manual call has no authoritative campaign identity. Never infer one from a
    # shared phone number or agent because that can mutate an unrelated campaign.
    if call_record.direction != CallDirection.OUTBOUND.value or call_record.contact_id is None:
        return

    result = await db.execute(
        select(CampaignContact)
        .join(Campaign)
        .where(
            CampaignContact.status == CampaignContactStatus.CALLING.value,
            CampaignContact.contact_id == call_record.contact_id,
            Campaign.agent_id == call_record.agent_id,
        )
        .limit(2)
        .with_for_update()
    )
    campaign_contacts = result.scalars().all()

    if len(campaign_contacts) != 1:
        logger.warning(
            "campaign_contact_not_found_or_ambiguous",
            call_record_id=str(call_record.id),
            contact_id=call_record.contact_id,
            candidate_count=len(campaign_contacts),
        )
        return

    cc = campaign_contacts[0]
    log = logger.bind(
        campaign_contact_id=str(cc.id),
        contact_id=call_record.contact_id,
        call_status=call_status,
    )
    status_map = {
        CallStatus.COMPLETED.value: CampaignContactStatus.COMPLETED.value,
        CallStatus.BUSY.value: CampaignContactStatus.BUSY.value,
        CallStatus.FAILED.value: CampaignContactStatus.FAILED.value,
        CallStatus.NO_ANSWER.value: CampaignContactStatus.NO_ANSWER.value,
        CallStatus.CANCELED.value: CampaignContactStatus.FAILED.value,
    }
    new_status = status_map.get(call_status, CampaignContactStatus.COMPLETED.value)
    cc.status = new_status
    cc.last_call_id = call_record.id
    cc.last_call_duration_seconds = duration_seconds
    cc.last_call_outcome = call_status

    campaign_result = await db.execute(
        select(Campaign).where(Campaign.id == cc.campaign_id).with_for_update()
    )
    campaign = campaign_result.scalar_one_or_none()
    if campaign:
        campaign.total_call_duration_seconds += duration_seconds
        if new_status == CampaignContactStatus.COMPLETED.value:
            campaign.contacts_completed += 1
        elif new_status in (
            CampaignContactStatus.FAILED.value,
            CampaignContactStatus.BUSY.value,
            CampaignContactStatus.NO_ANSWER.value,
        ):
            if cc.attempts < campaign.max_attempts_per_contact:
                from datetime import timedelta

                cc.status = CampaignContactStatus.PENDING.value
                cc.next_attempt_at = datetime.now(UTC) + timedelta(
                    minutes=campaign.retry_delay_minutes
                )
                log.info("Scheduling retry", next_attempt=cc.next_attempt_at.isoformat())
            else:
                campaign.contacts_failed += 1

    log.info("Campaign contact updated", new_status=new_status)


# =============================================================================
# Phone Number Management Endpoints
# =============================================================================


@router.get("/phone-numbers", response_model=list[PhoneNumberResponse])
async def list_phone_numbers(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    provider: str = Query("twilio", description="Provider: twilio or telnyx"),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> list[PhoneNumberResponse]:
    """List all phone numbers for the user's account.

    Args:
        provider: Telephony provider (twilio or telnyx)
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        List of phone numbers
    """
    log = logger.bind(user_id=current_user.id, provider=provider, workspace_id=workspace_id)
    log.info("listing_phone_numbers")

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    numbers: list[PhoneNumber] = []

    if provider == "twilio":
        twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)
        if not twilio_service:
            # Return empty list when credentials not configured (not an error)
            return []
        numbers = await twilio_service.list_phone_numbers()

    elif provider == "telnyx":
        telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
        if not telnyx_service:
            # Return empty list when credentials not configured (not an error)
            return []
        numbers = await telnyx_service.list_phone_numbers()

    else:
        raise HTTPException(status_code=400, detail="Invalid provider. Use 'twilio' or 'telnyx'.")

    # Map to response model
    return [
        PhoneNumberResponse(
            id=n.id,
            phone_number=n.phone_number,
            friendly_name=n.friendly_name,
            provider=n.provider,
            capabilities=n.capabilities,
            assigned_agent_id=n.assigned_agent_id,
        )
        for n in numbers
    ]


@router.post("/phone-numbers/search", response_model=list[PhoneNumberResponse])
async def search_phone_numbers(
    request: SearchPhoneNumbersRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> list[PhoneNumberResponse]:
    """Search for available phone numbers to purchase.

    Args:
        request: Search parameters
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        List of available phone numbers
    """
    log = logger.bind(user_id=current_user.id, provider=request.provider, workspace_id=workspace_id)
    log.info("searching_phone_numbers", country=request.country, area_code=request.area_code)

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    numbers: list[PhoneNumber] = []

    if request.provider == "twilio":
        twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)
        if not twilio_service:
            raise HTTPException(
                status_code=400,
                detail="Twilio credentials not configured. Please add them in Settings.",
            )
        numbers = await twilio_service.search_phone_numbers(
            country=request.country,
            area_code=request.area_code,
            contains=request.contains,
            limit=request.limit,
        )

    elif request.provider == "telnyx":
        telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
        if not telnyx_service:
            raise HTTPException(
                status_code=400,
                detail="Telnyx credentials not configured. Please add them in Settings.",
            )
        numbers = await telnyx_service.search_phone_numbers(
            country=request.country,
            area_code=request.area_code,
            contains=request.contains,
            limit=request.limit,
        )

    else:
        raise HTTPException(status_code=400, detail="Invalid provider. Use 'twilio' or 'telnyx'.")

    return [
        PhoneNumberResponse(
            id=n.id,
            phone_number=n.phone_number,
            friendly_name=n.friendly_name,
            provider=n.provider,
            capabilities=n.capabilities,
        )
        for n in numbers
    ]


async def _configure_webhook_for_provider(
    service: TwilioService | TelnyxService,
    number_id: str,
    provider: str,
    log: structlog.stdlib.BoundLogger,
) -> None:
    """Configure webhook for a purchased phone number."""
    public_url = settings.PUBLIC_URL
    if not public_url or not number_id:
        return

    voice_url = f"{public_url}/webhooks/{provider}/voice"
    webhook_success = await service.configure_phone_number_webhook(
        phone_number_id=number_id,
        voice_url=voice_url,
    )
    if webhook_success:
        log.info("webhook_configured", provider=provider, voice_url=voice_url)
    else:
        log.warning("webhook_config_failed", provider=provider, phone_number_id=number_id)


@router.post("/phone-numbers/purchase", response_model=PhoneNumberResponse)
@limiter.limit("5/minute")  # Strict rate limit for phone number purchases (costs money!)
async def purchase_phone_number(
    purchase_request: PurchasePhoneNumberRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> PhoneNumberResponse:
    """Purchase a phone number.

    Args:
        purchase_request: Purchase request with provider and phone number
        request: HTTP request (for rate limiting)
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        Purchased phone number details
    """
    log = logger.bind(
        user_id=current_user.id, provider=purchase_request.provider, workspace_id=workspace_id
    )
    log.info("purchasing_phone_number", phone_number=purchase_request.phone_number)

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    number: PhoneNumber

    # Get public URL for webhook configuration
    if not settings.PUBLIC_URL:
        log.warning("PUBLIC_URL not configured, webhooks will not be set up automatically")

    if purchase_request.provider == "twilio":
        twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)
        if not twilio_service:
            raise HTTPException(
                status_code=400,
                detail="Twilio credentials not configured. Please add them in Settings.",
            )
        number = await twilio_service.purchase_phone_number(purchase_request.phone_number)
        await _configure_webhook_for_provider(twilio_service, number.id, "twilio", log)

    elif purchase_request.provider == "telnyx":
        telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
        if not telnyx_service:
            raise HTTPException(
                status_code=400,
                detail="Telnyx credentials not configured. Please add them in Settings.",
            )
        number = await telnyx_service.purchase_phone_number(purchase_request.phone_number)
        await _configure_webhook_for_provider(telnyx_service, number.id, "telnyx", log)

    else:
        raise HTTPException(status_code=400, detail="Invalid provider. Use 'twilio' or 'telnyx'.")

    return PhoneNumberResponse(
        id=number.id,
        phone_number=number.phone_number,
        friendly_name=number.friendly_name,
        provider=number.provider,
        capabilities=number.capabilities,
    )


@router.delete("/phone-numbers/{phone_number_id}")
async def release_phone_number(
    phone_number_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    provider: str = Query(..., description="Provider: twilio or telnyx"),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> dict[str, str]:
    """Release a phone number.

    Args:
        phone_number_id: Phone number ID to release
        provider: Telephony provider
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        Success message
    """
    log = logger.bind(user_id=current_user.id, provider=provider, workspace_id=workspace_id)
    log.info("releasing_phone_number", phone_number_id=phone_number_id)

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    success = False

    if provider == "twilio":
        twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)
        if not twilio_service:
            raise HTTPException(
                status_code=400,
                detail="Twilio credentials not configured.",
            )
        success = await twilio_service.release_phone_number(phone_number_id)

    elif provider == "telnyx":
        telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
        if not telnyx_service:
            raise HTTPException(
                status_code=400,
                detail="Telnyx credentials not configured.",
            )
        success = await telnyx_service.release_phone_number(phone_number_id)

    else:
        raise HTTPException(status_code=400, detail="Invalid provider.")

    if not success:
        raise HTTPException(status_code=500, detail="Failed to release phone number.")

    return {"message": "Phone number released successfully"}


# =============================================================================
# Outbound Call Endpoints
# =============================================================================


@router.post("/calls", response_model=CallResponse)
@limiter.limit("30/minute")  # Rate limit outbound call initiation (costs money!)
async def initiate_call(  # noqa: PLR0915
    call_request: InitiateCallRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: str | None = Query(
        None, description="Workspace ID (optional; falls back to account-level keys)"
    ),
) -> CallResponse:
    """Initiate an outbound call.

    Args:
        call_request: Call initiation request
        request: HTTP request (for rate limiting and building webhook URLs)
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        Call details
    """
    log = logger.bind(
        user_id=current_user.id, agent_id=call_request.agent_id, workspace_id=workspace_id
    )
    log.info("initiating_call", to=call_request.to_number, from_=call_request.from_number)

    # Parse workspace_id (optional - single-tenant falls back to account-level keys)
    workspace_uuid: uuid.UUID | None = None
    if workspace_id:
        try:
            workspace_uuid = uuid.UUID(workspace_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    # Load agent to get provider preference (verify user owns agent).
    # Agent.user_id is the INTEGER users.id (not the UUID) — comparing it to a UUID
    # throws "operator does not exist: integer = uuid".
    result = await db.execute(
        select(Agent).where(
            Agent.id == uuid.UUID(call_request.agent_id),
            Agent.user_id == current_user.id,  # Ensure user owns the agent
        )
    )
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    workspace_uuid = await resolve_outbound_workspace_id(
        agent_id=agent.id,
        owner_user_id=current_user.id,
        requested_workspace_id=workspace_uuid,
        db=db,
    )

    # Select the outbound provider: honour TELEPHONY_OUTBOUND_PROVIDER, falling back to
    # the other only if the preferred one isn't configured. Twilio is first-class;
    # Telnyx is dormant (used only when preferred, or as a fallback).
    telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
    twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)

    preferred = (settings.TELEPHONY_OUTBOUND_PROVIDER or "twilio").lower()
    provider = select_outbound_provider(
        preferred, has_telnyx=telnyx_service is not None, has_twilio=twilio_service is not None
    )

    if provider is None:
        raise HTTPException(
            status_code=400,
            detail="No telephony provider configured. Please add Twilio or Telnyx credentials in Settings.",
        )
    if provider != preferred:
        log.warning("telephony_provider_fallback", preferred=preferred, using=provider)

    # Build webhook URL (forward per-call variables as base64-JSON in ?cv= so the
    # answer webhook -> media WS can personalize the prompt + fill the booking attendee)
    base_url = str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/webhooks/{provider}/answer?agent_id={call_request.agent_id}"
    if workspace_uuid:
        webhook_url = f"{webhook_url}&workspace_id={workspace_uuid}"
    if call_request.variables:
        import base64
        import json as _json

        cv = base64.urlsafe_b64encode(_json.dumps(call_request.variables).encode()).decode()
        webhook_url = f"{webhook_url}&cv={cv}"

    # Commit a correlation row BEFORE dialing (both providers). An immediate status
    # callback can then reconcile by the unique pending From/To record instead of
    # being lost in the POST-before-record race.
    call_record = CallRecord(
        user_id=user_id_to_uuid(current_user.id),
        workspace_id=workspace_uuid,
        provider=provider,
        provider_call_id=f"pending:{uuid.uuid4()}",
        agent_id=agent.id,
        direction=CallDirection.OUTBOUND.value,
        status=CallStatus.INITIATED.value,
        from_number=call_request.from_number,
        to_number=call_request.to_number,
    )
    db.add(call_record)
    await db.commit()

    service = telnyx_service if provider == "telnyx" else twilio_service
    try:
        call_info = await service.initiate_call(
            to_number=call_request.to_number,
            from_number=call_request.from_number,
            webhook_url=webhook_url,
            agent_id=call_request.agent_id,
        )
    except Exception as exc:
        # Telnyx-only: an unknown dial outcome must NOT be marked failed (the call may
        # still be live); surface it and let reconciliation settle the record.
        if provider == "telnyx" and is_unknown_telnyx_dial_outcome(exc):
            log.warning(
                "telnyx_dial_outcome_unknown",
                record_id=str(call_record.id),
                error_type=type(exc).__name__,
            )
            raise
        locked = await db.execute(
            select(CallRecord)
            .where(CallRecord.id == call_record.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        failed_record = locked.scalar_one()
        failed_record.status = CallStatus.FAILED.value
        failed_record.ended_at = datetime.now(UTC)
        await db.commit()
        raise

    locked = await db.execute(
        select(CallRecord)
        .where(CallRecord.id == call_record.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    call_record = locked.scalar_one()
    call_record.provider_call_id = call_info.call_id
    await db.commit()

    log.info("call_initiated", call_id=call_info.call_id, provider=provider)
    log.info("call_record_created", record_id=str(call_record.id))

    return CallResponse(
        call_id=call_info.call_id,
        call_control_id=call_info.call_control_id,
        from_number=call_info.from_number,
        to_number=call_info.to_number,
        direction=call_info.direction.value,
        status=call_info.status.value,
        agent_id=call_info.agent_id,
    )


@router.post("/calls/{call_id}/hangup")
async def hangup_call(
    call_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    provider: str = Query(..., description="Provider: twilio or telnyx"),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
) -> dict[str, str]:
    """Hang up an active call.

    Args:
        call_id: Call ID to hang up
        provider: Telephony provider
        current_user: Authenticated user
        db: Database session
        workspace_id: Workspace ID for workspace-specific API keys

    Returns:
        Success message
    """
    log = logger.bind(
        user_id=current_user.id, call_id=call_id, provider=provider, workspace_id=workspace_id
    )
    log.info("hanging_up_call")

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    success = False

    if provider == "twilio":
        twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)
        if twilio_service:
            success = await twilio_service.hangup_call(call_id)

    elif provider == "telnyx":
        telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
        if telnyx_service:
            success = await telnyx_service.hangup_call(call_id)

    if not success:
        raise HTTPException(status_code=500, detail="Failed to hang up call")

    return {"message": "Call ended successfully"}


# =============================================================================
# Twilio Webhook Endpoints
# =============================================================================


@webhook_router.post("/twilio/voice", response_class=HTMLResponse)
async def twilio_voice_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    call_sid: str = Form(default="", alias="CallSid"),
    from_number: str = Form(default="", alias="From"),
    to_number: str = Form(default="", alias="To"),
    call_status: str = Form(default="", alias="CallStatus"),
) -> Response:
    """Handle incoming Twilio voice calls.

    This webhook is called when a call comes in to a Twilio phone number.
    It returns TwiML to connect the call to our WebSocket for AI handling.
    """
    # Validate Twilio signature
    await verify_twilio_webhook(request)

    log = logger.bind(
        webhook="twilio_voice",
        call_sid=call_sid,
        from_number=from_number,
        to_number=to_number,
        status=call_status,
    )
    log.info("twilio_incoming_call")

    # Find agent by phone number
    agent = await get_agent_by_phone_number(to_number, db)
    agent_id = str(agent.id) if agent else None

    if not agent:
        log.warning("no_agent_for_number", to_number=to_number)
        # Return TwiML that says no agent is available
        return Response(
            content="""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, no agent is configured for this number. Goodbye.</Say>
    <Hangup/>
</Response>""",
            media_type="application/xml",
        )

    # Get workspace for the agent
    agent_workspace_id = await get_agent_workspace_id(agent.id, db)

    # Create call record for inbound call
    call_record = CallRecord(
        user_id=agent.user_id,
        workspace_id=agent_workspace_id,
        provider="twilio",
        provider_call_id=call_sid,
        agent_id=agent.id,
        direction=CallDirection.INBOUND.value,
        status=CallStatus.RINGING.value,
        from_number=from_number,
        to_number=to_number,
    )
    db.add(call_record)
    await db.commit()
    log.info("call_record_created", record_id=str(call_record.id))

    # Build WebSocket URL for media streaming
    base_url = str(request.base_url).rstrip("/")
    ws_url = base_url.replace("http://", "wss://").replace("https://", "wss://")
    stream_url = f"{ws_url}/ws/telephony/twilio/{agent_id}"

    # Generate TwiML to connect to our WebSocket
    twilio_service = TwilioService("", "")  # Just need TwiML generation
    twiml = twilio_service.generate_answer_response(stream_url, agent_id)

    log.info("twilio_twiml_generated", agent_id=agent_id)

    return Response(content=twiml, media_type="application/xml")


@webhook_router.post("/twilio/status")
async def twilio_status_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    call_sid: str = Form(default="", alias="CallSid"),
    call_status: str = Form(default="", alias="CallStatus"),
    call_duration: str = Form(default="0", alias="CallDuration"),
    from_number: str = Form(default="", alias="From"),
    to_number: str = Form(default="", alias="To"),
) -> dict[str, str]:
    """Handle Twilio call status callbacks.

    Called when call status changes (initiated, ringing, answered, completed).
    """
    # Validate Twilio signature
    await verify_twilio_webhook(request)

    log = logger.bind(
        webhook="twilio_status",
        call_sid=call_sid,
        status=call_status,
        duration=call_duration,
    )
    log.info("twilio_status_update")

    # Find and update call record
    result = await db.execute(select(CallRecord).where(CallRecord.provider_call_id == call_sid))
    call_record = result.scalar_one_or_none()

    if call_record:
        # Map Twilio status to our status
        status_map = {
            "initiated": CallStatus.INITIATED.value,
            "ringing": CallStatus.RINGING.value,
            "in-progress": CallStatus.IN_PROGRESS.value,
            "completed": CallStatus.COMPLETED.value,
            "busy": CallStatus.BUSY.value,
            "failed": CallStatus.FAILED.value,
            "no-answer": CallStatus.NO_ANSWER.value,
            "canceled": CallStatus.CANCELED.value,
        }

        call_record.status = status_map.get(call_status, call_status)

        # Update timestamps based on status
        if call_status == "in-progress" and not call_record.answered_at:
            call_record.answered_at = datetime.now(UTC)
        elif call_status in ("completed", "busy", "failed", "no-answer", "canceled"):
            call_record.ended_at = datetime.now(UTC)
            if call_duration:
                call_record.duration_seconds = int(call_duration)

            # Update campaign contact status if this was a campaign call
            await update_campaign_contact_from_call(
                call_record=call_record,
                call_status=call_record.status,
                duration_seconds=call_record.duration_seconds or 0,
                db=db,
            )

        await db.commit()
        log.info("call_record_updated", record_id=str(call_record.id), status=call_status)
    else:
        log.warning("call_record_not_found", call_sid=call_sid)

    return {"status": "received"}


@webhook_router.post("/twilio/answer", response_class=HTMLResponse)
async def twilio_answer_webhook(
    request: Request,
    agent_id: str = Query(default=""),
    cv: str = Query(default=""),
    workspace_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Handle Twilio outbound call connection.

    Called when an outbound call is answered by the recipient. Returns TwiML to connect
    to our WebSocket. Per-call variables (base64 JSON in `cv`, set on the outbound call's
    Url) are forwarded to the media WS so the session can personalize the prompt + fill
    the booking attendee — mirroring the Telnyx path.
    """
    # Validate Twilio signature
    await verify_twilio_webhook(request)

    log = logger.bind(webhook="twilio_answer", agent_id=agent_id, has_cv=bool(cv))
    log.info("twilio_outbound_answered")

    # Build WebSocket URL (forward the per-call variables blob if present)
    base_url = str(request.base_url).rstrip("/")
    ws_url = base_url.replace("http://", "wss://").replace("https://", "wss://")
    stream_url = f"{ws_url}/ws/telephony/twilio/{agent_id}"
    query_parts: list[str] = []
    if workspace_id:
        from urllib.parse import quote

        query_parts.append(f"workspace_id={quote(workspace_id, safe='')}")
    if cv:
        from urllib.parse import quote

        query_parts.append(f"cv={quote(cv, safe='')}")
    if query_parts:
        stream_url = f"{stream_url}?{'&'.join(query_parts)}"

    twilio_service = TwilioService("", "")
    twiml = twilio_service.generate_answer_response(stream_url, agent_id)

    return Response(content=twiml, media_type="application/xml")


# =============================================================================
# Telnyx Webhook Endpoints
# =============================================================================


@webhook_router.post("/telnyx/voice", response_class=HTMLResponse)
async def telnyx_voice_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Handle incoming Telnyx voice calls.

    This webhook is called when a call comes in to a Telnyx phone number.
    It returns TeXML to connect the call to our WebSocket for AI handling.
    """
    # Validate Telnyx signature
    await verify_telnyx_webhook(request)

    body = await request.json()
    data = body.get("data", {})
    payload = data.get("payload", {})

    call_control_id = payload.get("call_control_id", "")
    from_number = payload.get("from", "")
    to_number = payload.get("to", "")
    event_type = data.get("event_type", "")

    log = logger.bind(
        webhook="telnyx_voice",
        call_control_id=call_control_id,
        from_number=from_number,
        to_number=to_number,
        event_type=event_type,
    )
    log.info("telnyx_incoming_call")

    # Find agent by phone number
    agent = await get_agent_by_phone_number(to_number, db)
    agent_id = str(agent.id) if agent else None

    if not agent:
        log.warning("no_agent_for_number", to_number=to_number)
        return Response(
            content="""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Say>Sorry, no agent is configured for this number. Goodbye.</Say>
    <Hangup/>
</Response>""",
            media_type="application/xml",
        )

    # Get workspace for the agent
    agent_workspace_id = await get_agent_workspace_id(agent.id, db)

    # Create call record for inbound call
    call_record = CallRecord(
        user_id=agent.user_id,
        workspace_id=agent_workspace_id,
        provider="telnyx",
        provider_call_id=call_control_id,
        agent_id=agent.id,
        direction=CallDirection.INBOUND.value,
        status=CallStatus.RINGING.value,
        from_number=from_number,
        to_number=to_number,
    )
    db.add(call_record)
    await db.commit()
    log.info("call_record_created", record_id=str(call_record.id))

    # Build WebSocket URL
    base_url = str(request.base_url).rstrip("/")
    ws_url = base_url.replace("http://", "wss://").replace("https://", "wss://")
    stream_url = f"{ws_url}/ws/telephony/telnyx/{agent_id}"

    telnyx_service = TelnyxService("")
    texml = telnyx_service.generate_answer_response(stream_url, agent_id)

    log.info("telnyx_texml_generated", agent_id=agent_id)

    return Response(content=texml, media_type="application/xml")


@webhook_router.post("/telnyx/answer", response_class=HTMLResponse)
async def telnyx_answer_webhook(
    request: Request,
    agent_id: str = Query(default=""),
    cv: str = Query(default=""),
    workspace_id: str = Query(default=""),
) -> Response:
    """Handle Telnyx outbound call connection.

    Called when an outbound call is answered by the recipient.
    Returns TeXML to connect to our WebSocket. Per-call variables (base64 JSON in `cv`,
    set on the outbound call's Url) are forwarded to the media WS so the session can
    personalize the prompt + fill the booking attendee.
    """
    # Validate Telnyx signature
    await verify_telnyx_webhook(request)

    log = logger.bind(webhook="telnyx_answer", agent_id=agent_id, has_cv=bool(cv))
    log.info("telnyx_outbound_answered")

    # Build WebSocket URL (forward the per-call variables blob if present)
    base_url = str(request.base_url).rstrip("/")
    ws_url = base_url.replace("http://", "wss://").replace("https://", "wss://")
    stream_url = f"{ws_url}/ws/telephony/telnyx/{agent_id}"
    query_parts: list[str] = []
    if workspace_id:
        from urllib.parse import quote

        query_parts.append(f"workspace_id={quote(workspace_id, safe='')}")
    if cv:
        from urllib.parse import quote

        query_parts.append(f"cv={quote(cv, safe='')}")
    if query_parts:
        stream_url = f"{stream_url}?{'&'.join(query_parts)}"

    telnyx_service = TelnyxService("")
    texml = telnyx_service.generate_answer_response(stream_url, agent_id)

    return Response(content=texml, media_type="application/xml")


@webhook_router.post("/telnyx/status")
async def telnyx_status_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Handle Telnyx call status callbacks.

    Called when call events occur (call.initiated, call.answered, call.hangup, etc).
    """
    # Validate Telnyx signature
    await verify_telnyx_webhook(request)

    event_type = ""
    payload: dict[str, Any] = {}
    identifiers: set[str] = set()
    from_number = ""
    to_number = ""
    event_at: datetime | None = None
    provider_duration: int | None = None

    # Telnyx Call Control sends JSON; TeXML applications send TwiML-style form data.
    # Support both because outbound calls in this service use the TeXML endpoint.
    try:
        body = await request.json()
    except Exception:
        form = dict(await request.form())
        event_type, payload = _telnyx_form_event(form)
        identifiers.update(
            str(value)
            for value in (
                form.get("CallSid"),
                form.get("call_sid"),
                form.get("CallControlId"),
                form.get("call_control_id"),
            )
            if value
        )
        event_at = _parse_telnyx_timestamp(form.get("Timestamp") or form.get("timestamp"))
        provider_duration = _parse_telnyx_duration(
            form.get("CallDuration") or form.get("call_duration") or form.get("duration_seconds")
        )
        from_number = _telnyx_phone_number(form.get("From") or form.get("from"))
        to_number = _telnyx_phone_number(form.get("To") or form.get("to"))
    else:
        data = body.get("data", {}) if isinstance(body, dict) else {}
        event_type = str(data.get("event_type", ""))
        raw_payload = data.get("payload", {})
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        identifiers.update(
            str(value)
            for value in (
                payload.get("call_control_id"),
                payload.get("call_leg_id"),
                payload.get("call_session_id"),
                payload.get("call_sid"),
            )
            if value
        )
        event_at = _parse_telnyx_timestamp(
            data.get("occurred_at") or payload.get("occurred_at") or payload.get("timestamp")
        )
        provider_duration = _parse_telnyx_duration(
            payload.get("duration_secs")
            or payload.get("duration_seconds")
            or payload.get("call_duration")
        )
        from_number = _telnyx_phone_number(payload.get("from") or payload.get("from_number"))
        to_number = _telnyx_phone_number(payload.get("to") or payload.get("to_number"))

    if not event_type or not identifiers:
        logger.warning(
            "telnyx_status_unusable",
            event_type=event_type,
            identifier_count=len(identifiers),
        )
        return {"status": "received"}

    call_identifier = sorted(identifiers)[0]

    log = logger.bind(
        webhook="telnyx_status",
        event_type=event_type,
        call_identifier=call_identifier,
    )
    log.info("telnyx_status_update")

    call_record, candidate_count = await _find_telnyx_lifecycle_record(
        identifiers=identifiers,
        from_number=from_number,
        to_number=to_number,
        db=db,
    )

    if call_record:
        _apply_telnyx_lifecycle_event(
            call_record,
            event_type,
            payload,
            event_at=event_at or datetime.now(UTC),
            provider_duration=provider_duration,
        )

        # The updater itself only accepts a campaign contact still in CALLING state,
        # so retry callbacks cannot increment campaign totals twice. Calling it for
        # every hangup also covers a media-stop fallback that arrived first.
        if event_type == "call.hangup":
            await update_campaign_contact_from_call(
                call_record=call_record,
                call_status=call_record.status,
                duration_seconds=call_record.duration_seconds or 0,
                db=db,
            )

        await db.commit()
        log.info(
            "call_record_updated",
            record_id=str(call_record.id),
            lifecycle_event=event_type,
        )
    else:
        log.warning(
            "call_record_not_found_or_ambiguous",
            call_identifier=call_identifier,
            candidate_count=candidate_count,
        )

    return {"status": "received"}
