"""Telephony API routes for Twilio and Telnyx integration.

This module provides:
- Webhook endpoints for inbound calls (Twilio/Telnyx)
- Phone number management (list, search, buy, release)
- Outbound call initiation
- Call status callbacks
- WebSocket endpoint for telephony media streaming
"""

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.settings import get_user_api_keys
from app.core.auth import CurrentUser, user_id_to_uuid
from app.core.config import settings
from app.core.limiter import limiter
from app.core.webhook_security import verify_telnyx_webhook, verify_twilio_webhook
from app.db.session import get_db
from app.models.agent import Agent
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.campaign import Campaign, CampaignContact, CampaignContactStatus
from app.models.workspace import AgentWorkspace
from app.services.telephony.telnyx_service import TelnyxService
from app.services.telephony.twilio_service import TwilioService

if TYPE_CHECKING:
    from app.models.contact import Contact
    from app.services.telephony.base import PhoneNumber

router = APIRouter(prefix="/api/v1/telephony", tags=["telephony"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])

logger = structlog.get_logger()


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

    if (
        not user_settings
        or not user_settings.twilio_account_sid
        or not user_settings.twilio_auth_token
    ):
        return None

    return TwilioService(
        account_sid=user_settings.twilio_account_sid,
        auth_token=user_settings.twilio_auth_token,
    )


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

    if not user_settings or not user_settings.telnyx_api_key:
        return None

    return TelnyxService(
        api_key=user_settings.telnyx_api_key,
        public_key=user_settings.telnyx_public_key,
    )


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
    result = await db.execute(
        select(AgentWorkspace.workspace_id).where(AgentWorkspace.agent_id == agent_id).limit(1)
    )
    row = result.scalar_one_or_none()
    return row


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
    # Only process outbound calls (campaigns make outbound calls)
    if call_record.direction != CallDirection.OUTBOUND.value:
        return

    # Find campaign contact that is currently being called to this number
    # from this campaign's agent (use selectinload to avoid N+1 queries)
    result = await db.execute(
        select(CampaignContact)
        .join(Campaign)
        .options(selectinload(CampaignContact.contact))
        .where(
            CampaignContact.status == CampaignContactStatus.CALLING.value,
            Campaign.agent_id == call_record.agent_id,
        )
    )
    campaign_contacts = result.scalars().all()

    if not campaign_contacts:
        return

    # Find the matching campaign contact by phone number
    for cc in campaign_contacts:
        # Contact is already loaded via selectinload
        contact: Contact | None = cc.contact

        if not contact:
            continue

        # Check if phone numbers match (normalize both)
        contact_phone = contact.phone_number.lstrip("+").replace("-", "").replace(" ", "")
        to_phone = call_record.to_number.lstrip("+").replace("-", "").replace(" ", "")

        if contact_phone != to_phone:
            continue

        # Found the matching campaign contact - update its status
        log = logger.bind(
            campaign_contact_id=str(cc.id),
            contact_id=contact.id,
            call_status=call_status,
        )

        # Map call status to campaign contact status
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

        # Get the campaign to update stats
        campaign_result = await db.execute(select(Campaign).where(Campaign.id == cc.campaign_id))
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
                # Check if we should retry
                if cc.attempts < campaign.max_attempts_per_contact:
                    # Schedule retry
                    from datetime import timedelta

                    cc.status = CampaignContactStatus.PENDING.value
                    cc.next_attempt_at = datetime.now(UTC) + timedelta(
                        minutes=campaign.retry_delay_minutes
                    )
                    log.info(
                        "Scheduling retry",
                        next_attempt=cc.next_attempt_at.isoformat(),
                    )
                else:
                    campaign.contacts_failed += 1

        log.info("Campaign contact updated", new_status=new_status)
        break  # Only update one campaign contact per call


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
async def initiate_call(
    call_request: InitiateCallRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: str = Query(..., description="Workspace ID for API key isolation"),
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

    # Parse workspace_id
    try:
        workspace_uuid = uuid.UUID(workspace_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from e

    # Load agent to get provider preference (verify user owns agent)
    user_uuid = user_id_to_uuid(current_user.id)
    result = await db.execute(
        select(Agent).where(
            Agent.id == uuid.UUID(call_request.agent_id),
            Agent.user_id == user_uuid,  # Ensure user owns the agent
        )
    )
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Determine provider from agent's phone number configuration
    # Default to Telnyx if not specified
    provider = "telnyx"

    # Try Telnyx first
    telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
    twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)

    if not telnyx_service and not twilio_service:
        raise HTTPException(
            status_code=400,
            detail="No telephony provider configured. Please add Twilio or Telnyx credentials in Settings.",
        )

    # Build webhook URL
    base_url = str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/webhooks/{'telnyx' if telnyx_service else 'twilio'}/answer?agent_id={call_request.agent_id}"

    if telnyx_service:
        provider = "telnyx"
        call_info = await telnyx_service.initiate_call(
            to_number=call_request.to_number,
            from_number=call_request.from_number,
            webhook_url=webhook_url,
            agent_id=call_request.agent_id,
        )
    elif twilio_service:
        provider = "twilio"
        call_info = await twilio_service.initiate_call(
            to_number=call_request.to_number,
            from_number=call_request.from_number,
            webhook_url=webhook_url,
            agent_id=call_request.agent_id,
        )
    else:
        raise HTTPException(status_code=500, detail="Failed to initialize telephony service")

    log.info("call_initiated", call_id=call_info.call_id, provider=provider)

    # Create call record for outbound call (workspace_uuid already available from query param)
    call_record = CallRecord(
        user_id=user_id_to_uuid(current_user.id),
        workspace_id=workspace_uuid,
        provider=provider,
        provider_call_id=call_info.call_id,
        agent_id=uuid.UUID(call_request.agent_id),
        direction=CallDirection.OUTBOUND.value,
        status=CallStatus.INITIATED.value,
        from_number=call_request.from_number,
        to_number=call_request.to_number,
    )
    db.add(call_record)
    await db.commit()
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
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Handle Twilio outbound call connection.

    Called when an outbound call is answered by the recipient.
    Returns TwiML to connect to our WebSocket.
    """
    # Validate Twilio signature
    await verify_twilio_webhook(request)

    log = logger.bind(webhook="twilio_answer", agent_id=agent_id)
    log.info("twilio_outbound_answered")

    # Build WebSocket URL
    base_url = str(request.base_url).rstrip("/")
    ws_url = base_url.replace("http://", "wss://").replace("https://", "wss://")
    stream_url = f"{ws_url}/ws/telephony/twilio/{agent_id}"

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
    if cv:
        from urllib.parse import quote

        stream_url = f"{stream_url}?cv={quote(cv, safe='')}"

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

    # TeXML status callbacks are form-encoded (TwiML-style), not the Call Control JSON
    # this handler expects — don't 500 on them. Call status is tracked via the media-WS
    # lifecycle for now. (TODO: map TeXML status fields CallSid/CallStatus.)
    try:
        body = await request.json()
    except Exception:
        logger.info("telnyx_status_non_json_ignored")
        return {"status": "received"}
    data = body.get("data", {})
    event_type = data.get("event_type", "")
    payload = data.get("payload", {})
    call_control_id = payload.get("call_control_id", "")

    log = logger.bind(
        webhook="telnyx_status",
        event_type=event_type,
        call_control_id=call_control_id,
    )
    log.info("telnyx_status_update")

    # Find and update call record
    result = await db.execute(
        select(CallRecord).where(CallRecord.provider_call_id == call_control_id)
    )
    call_record = result.scalar_one_or_none()

    if call_record:
        # Map Telnyx event types to our status
        event_status_map = {
            "call.initiated": CallStatus.INITIATED.value,
            "call.ringing": CallStatus.RINGING.value,
            "call.answered": CallStatus.IN_PROGRESS.value,
            "call.hangup": CallStatus.COMPLETED.value,
            "call.machine.detection.ended": None,  # Don't change status
        }

        new_status = event_status_map.get(event_type)
        if new_status:
            call_record.status = new_status

        # Update timestamps based on event
        if event_type == "call.answered" and not call_record.answered_at:
            call_record.answered_at = datetime.now(UTC)
        elif event_type == "call.hangup":
            call_record.ended_at = datetime.now(UTC)
            # Calculate duration if we have answered_at
            if call_record.answered_at:
                duration = (call_record.ended_at - call_record.answered_at).total_seconds()
                call_record.duration_seconds = int(duration)

            # Check hangup cause for failed calls
            hangup_cause = payload.get("hangup_cause", "")
            if hangup_cause == "USER_BUSY":
                call_record.status = CallStatus.BUSY.value
            elif hangup_cause == "NO_ANSWER":
                call_record.status = CallStatus.NO_ANSWER.value
            elif hangup_cause in ("CALL_REJECTED", "ORIGINATOR_CANCEL"):
                call_record.status = CallStatus.CANCELED.value
            elif hangup_cause and hangup_cause not in ("NORMAL_CLEARING", "NORMAL_RELEASE"):
                call_record.status = CallStatus.FAILED.value

            # Update campaign contact status if this was a campaign call
            await update_campaign_contact_from_call(
                call_record=call_record,
                call_status=call_record.status,
                duration_seconds=call_record.duration_seconds or 0,
                db=db,
            )

        await db.commit()
        log.info("call_record_updated", record_id=str(call_record.id), event=event_type)
    else:
        log.warning("call_record_not_found", call_control_id=call_control_id)

    return {"status": "received"}
