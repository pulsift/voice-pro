"""Telephony API routes for Twilio and Telnyx integration.

This module provides:
- Webhook endpoints for inbound calls (Twilio/Telnyx)
- Phone number management (list, search, buy, release)
- Outbound call initiation
- Call status callbacks
- WebSocket endpoint for telephony media streaming
"""

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal, cast

import structlog
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ValidationError
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.settings import get_user_api_keys
from app.core.auth import CurrentUser, user_id_to_uuid
from app.core.config import settings
from app.core.limiter import limiter
from app.core.public_id import SHARE_TOKEN_LENGTH, generate_public_id
from app.core.webhook_security import verify_telnyx_webhook, verify_twilio_webhook
from app.db.session import get_db
from app.models.agent import Agent
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.phone_number import PhoneNumber as StoredPhoneNumber
from app.models.workspace import AgentWorkspace, Workspace
from app.services.availability import (
    CALCOM_REQUIRED_BACKEND,
    CALENDAR_BACKEND_VARIABLE,
    missing_calcom_settings,
)
from app.services.call_events import stage_terminal_call_event
from app.services.telephony import recording_policy
from app.services.telephony.media_grant import arm_twilio_media_grant
from app.services.telephony.telnyx_service import (
    TelnyxDialNotStartedError,
    TelnyxService,
    is_unknown_telnyx_dial_outcome,
)
from app.services.telephony.twilio_service import (
    TwilioDialOutcomeUnknownError,
    TwilioService,
)

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

_TWILIO_STATUS_MAP = {
    "queued": CallStatus.INITIATED.value,
    "initiated": CallStatus.INITIATED.value,
    "ringing": CallStatus.RINGING.value,
    "answered": CallStatus.IN_PROGRESS.value,
    "in-progress": CallStatus.IN_PROGRESS.value,
    "completed": CallStatus.COMPLETED.value,
    "busy": CallStatus.BUSY.value,
    "failed": CallStatus.FAILED.value,
    "no-answer": CallStatus.NO_ANSWER.value,
    "canceled": CallStatus.CANCELED.value,
}
_TWILIO_NONTERMINAL_RANK = {
    CallStatus.INITIATED.value: 0,
    CallStatus.RINGING.value: 1,
    CallStatus.IN_PROGRESS.value: 2,
}

_TWILIO_REJECT_TWIML = """<?xml version="1.0" encoding="UTF-8"?>
<Response><Hangup/></Response>"""


def _twilio_reject_response() -> Response:
    """Fail closed without opening a media stream."""
    return Response(content=_TWILIO_REJECT_TWIML, media_type="application/xml")


def _parse_twilio_duration(value: str) -> int | None:
    """Return a non-negative Twilio duration, or None when absent or invalid."""
    if not value:
        return None
    try:
        return max(0, int(value))
    except ValueError:
        return None


def _apply_twilio_lifecycle_status(
    call_record: CallRecord,
    mapped_status: str,
    *,
    event_at: datetime,
    provider_duration: int | None,
) -> bool:
    """Apply one known status monotonically; return the first terminal edge."""
    if call_record.status in _TERMINAL_CALL_STATUSES:
        return False

    if mapped_status in _TERMINAL_CALL_STATUSES:
        call_record.status = mapped_status
        if not call_record.ended_at:
            call_record.ended_at = event_at
        # Twilio's "completed" is only reachable from a real answer - if the
        # answered callback was lost or arrives after this one, the callback's
        # own trusted timestamp is still authenticated carrier evidence that the
        # call was answered. Set it before the call-ended payload is staged so
        # the router never mistakes an answered call for a no-answer redial.
        if mapped_status == CallStatus.COMPLETED.value and not call_record.answered_at:
            call_record.answered_at = event_at
        if provider_duration is not None:
            call_record.duration_seconds = provider_duration
        return True

    incoming_rank = _TWILIO_NONTERMINAL_RANK[mapped_status]
    current_rank = _TWILIO_NONTERMINAL_RANK.get(call_record.status, -1)
    if incoming_rank >= current_rank:
        call_record.status = mapped_status
        if mapped_status == CallStatus.IN_PROGRESS.value and not call_record.answered_at:
            call_record.answered_at = event_at
    return False


async def _find_twilio_lifecycle_record(
    *,
    call_record_id: str,
    call_sid: str,
    from_number: str,
    to_number: str,
    db: AsyncSession,
) -> tuple[CallRecord | None, int]:
    """Lock and reconcile the outbound row authorized by a signed Twilio callback."""
    real_call_sid = _real_provider_call_id(call_sid)
    if real_call_sid is None:
        return None, 0
    call_sid = real_call_sid

    if call_record_id:
        if not from_number or not to_number:
            return None, 0
        try:
            record_id = uuid.UUID(call_record_id)
        except (TypeError, ValueError):
            return None, 0
        statement = select(CallRecord).where(
            CallRecord.id == record_id,
            CallRecord.provider == "twilio",
            CallRecord.direction == CallDirection.OUTBOUND.value,
            CallRecord.from_number == from_number,
            CallRecord.to_number == to_number,
            or_(
                CallRecord.provider_call_id == call_sid,
                CallRecord.provider_call_id.like("pending:%"),
            ),
        )
    else:
        # Rolling compatibility for callback URLs created before call_record_id.
        # An exact provider SID is safe; guessing among pending rows is not.
        statement = select(CallRecord).where(
            CallRecord.provider == "twilio",
            CallRecord.provider_call_id == call_sid,
        )

    result = await db.execute(
        statement.limit(2).with_for_update().execution_options(populate_existing=True)
    )
    candidates = result.scalars().all()
    if len(candidates) != 1:
        return None, len(candidates)

    call_record = candidates[0]
    if call_record.provider_call_id.startswith("pending:"):
        call_record.provider_call_id = call_sid
    _mark_dial_attempt_accepted_by_callback(call_record)
    return call_record, 1


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
    identifiers = {
        provider_id
        for value in identifiers
        if (provider_id := _real_provider_call_id(value)) is not None
    }
    if not identifiers:
        return None, 0
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
            _mark_dial_attempt_accepted_by_callback(candidates[0])
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
                _mark_dial_attempt_accepted_by_callback(record)
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


class IdempotentInitiateCallRequest(InitiateCallRequest):
    """Versioned request whose UUID identifies one physical dial attempt."""

    dial_attempt_id: uuid.UUID


class CallResponse(BaseModel):
    """Accepted call response; the legacy route always keeps a concrete call ID."""

    call_id: str
    call_control_id: str | None = None
    from_number: str
    to_number: str
    direction: str
    status: str
    agent_id: str | None = None
    call_record_id: str | None = None
    dial_attempt_id: str | None = None
    dial_attempt_status: Literal["accepted"] | None = None


class DialAttemptPendingResponse(BaseModel):
    """A keyed attempt that must be reconciled, never redialled under a new key."""

    call_id: None = None
    call_control_id: None = None
    from_number: str
    to_number: str
    direction: str
    status: str
    agent_id: str | None = None
    call_record_id: str
    dial_attempt_id: str
    dial_attempt_status: Literal["in_progress", "outcome_unknown"]


class DialAttemptErrorDetail(BaseModel):
    """Sanitized keyed-dial conflict or definitive rejection."""

    code: Literal["dial_attempt_conflict", "dial_attempt_rejected"]
    call_record_id: str | None = None
    dial_attempt_id: str | None = None


class DialAttemptErrorResponse(BaseModel):
    detail: DialAttemptErrorDetail


# Legacy builds used `reserved` while the carrier POST could already be running.
# Never auto-resume that value during a rolling deployment.
_DIAL_RESERVED = "reserved"
_DIAL_READY_V2 = "dispatch_ready_v2"
_DIAL_DISPATCHING = "dispatching"
_DIAL_ACCEPTED = "accepted"
_DIAL_UNKNOWN = "outcome_unknown"
_DIAL_REJECTED = "rejected"
_HTTP_CLIENT_ERROR_MIN = 400
_HTTP_SERVER_ERROR_MIN = 500
_HTTP_REQUEST_TIMEOUT = 408


@dataclass(frozen=True, slots=True)
class _DispatchReadyAttempt:
    """A v2 key proven not to have begun provider dispatch yet."""

    call_record_id: uuid.UUID
    workspace_id: uuid.UUID | None
    provider: str
    variables: dict[str, Any]
    response: JSONResponse


def _real_provider_call_id(value: object) -> str | None:
    """Return one real carrier ID, excluding blanks and our pending sentinel."""
    provider_id = str(value or "").strip()
    if not provider_id or provider_id.startswith("pending:"):
        return None
    return provider_id


def _mark_dial_attempt_accepted_by_callback(record: CallRecord) -> None:
    """Promote keyed state when a verified carrier callback proves acceptance."""
    if not isinstance(getattr(record, "dial_attempt_id", None), uuid.UUID):
        return
    if _real_provider_call_id(record.provider_call_id) is None:
        return
    if record.dial_attempt_state == _DIAL_REJECTED:
        # A signed carrier callback is stronger than our earlier local rejection.
        # Remove the synthetic terminal state before the lifecycle applicator runs.
        record.status = CallStatus.INITIATED.value
        record.answered_at = None
        record.ended_at = None
        record.duration_seconds = 0
    if record.dial_attempt_state != _DIAL_ACCEPTED:
        record.dial_attempt_state = _DIAL_ACCEPTED
        record.dial_attempt_result = None


def _parse_requested_workspace_id(workspace_id: str | None) -> uuid.UUID | None:
    if not workspace_id:
        return None
    try:
        return uuid.UUID(workspace_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid workspace_id format") from exc


def _dial_request_sha256(
    call_request: InitiateCallRequest,
    requested_workspace_id: uuid.UUID | None,
) -> str:
    """Hash only the immutable client transport contract."""
    variables = {
        key: value
        for key, value in (call_request.variables or {}).items()
        if key
        not in {
            "dialAttemptId",
            "dial_attempt_id",
            CALENDAR_BACKEND_VARIABLE,
        }
    }
    canonical = json.dumps(
        {
            "agent_id": call_request.agent_id,
            "from_number": call_request.from_number,
            "to_number": call_request.to_number,
            "variables": variables,
            "requested_workspace_id": str(requested_workspace_id) if requested_workspace_id else "",
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


async def _load_dial_attempt(
    dial_attempt_id: uuid.UUID,
    db: AsyncSession,
    *,
    for_update: bool = False,
) -> CallRecord | None:
    statement = (
        select(CallRecord)
        .where(CallRecord.dial_attempt_id == dial_attempt_id)
        .limit(2)
        .execution_options(populate_existing=True)
    )
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    candidates = result.scalars().all()
    if len(candidates) > 1:
        logger.error("dial_attempt_unique_invariant_broken")
        raise HTTPException(status_code=409, detail={"code": "dial_attempt_conflict"})
    return candidates[0] if candidates else None


def _dial_attempt_matches(
    record: CallRecord,
    *,
    owner_user_id: uuid.UUID,
    request_sha256: str,
) -> bool:
    return record.user_id == owner_user_id and record.dial_request_sha256 == request_sha256


def _accepted_response_from_record(record: CallRecord) -> CallResponse:
    stored = record.dial_attempt_result
    if record.dial_attempt_state == _DIAL_ACCEPTED and isinstance(stored, dict):
        try:
            accepted = CallResponse.model_validate(stored)
        except ValidationError:
            pass
        else:
            if _real_provider_call_id(accepted.call_id) is not None:
                return accepted

    provider_id = _real_provider_call_id(record.provider_call_id)
    if provider_id is None:
        raise ValueError("accepted dial attempt has no provider call ID")
    return CallResponse(
        call_id=provider_id,
        call_control_id=provider_id if record.provider == "twilio" else None,
        from_number=record.from_number,
        to_number=record.to_number,
        direction=record.direction,
        status=record.status,
        agent_id=str(record.agent_id) if record.agent_id else None,
        call_record_id=str(record.id),
        dial_attempt_id=str(record.dial_attempt_id) if record.dial_attempt_id else None,
        dial_attempt_status=_DIAL_ACCEPTED,
    )


def _pending_dial_response(record: CallRecord, state: str) -> JSONResponse:
    pending_state: Literal["in_progress", "outcome_unknown"] = (
        "outcome_unknown" if state == _DIAL_UNKNOWN else "in_progress"
    )
    body = DialAttemptPendingResponse(
        from_number=record.from_number,
        to_number=record.to_number,
        direction=record.direction,
        status=CallStatus.INITIATED.value,
        agent_id=str(record.agent_id) if record.agent_id else None,
        call_record_id=str(record.id),
        dial_attempt_id=str(record.dial_attempt_id),
        dial_attempt_status=pending_state,
    )
    return JSONResponse(status_code=202, content=body.model_dump(mode="json"))


def _dial_attempt_rejected_error(record: CallRecord) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "dial_attempt_rejected",
            "call_record_id": str(record.id),
            "dial_attempt_id": str(record.dial_attempt_id),
        },
    )


async def _replay_dial_attempt(
    *,
    dial_attempt_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request_sha256: str,
    db: AsyncSession,
) -> CallResponse | JSONResponse | _DispatchReadyAttempt | None:
    """Return persisted state, or a resumable pre-dispatch reservation."""
    record = await _load_dial_attempt(dial_attempt_id, db, for_update=True)
    if record is None:
        return None
    if not _dial_attempt_matches(
        record,
        owner_user_id=owner_user_id,
        request_sha256=request_sha256,
    ):
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "dial_attempt_conflict"})

    provider_id = _real_provider_call_id(record.provider_call_id)
    if provider_id is not None:
        accepted_response = _accepted_response_from_record(record)
        record.dial_attempt_state = _DIAL_ACCEPTED
        record.dial_attempt_result = accepted_response.model_dump(mode="json")
        await db.commit()
        return accepted_response

    if record.dial_attempt_state == _DIAL_REJECTED:
        error = _dial_attempt_rejected_error(record)
        await db.commit()
        raise error

    if (
        record.dial_attempt_state == _DIAL_READY_V2
        and record.provider_call_id.startswith("pending:")
    ):
        resume = _DispatchReadyAttempt(
            call_record_id=record.id,
            workspace_id=record.workspace_id,
            provider=record.provider,
            variables=dict(record.variables or {}),
            response=_pending_dial_response(record, "in_progress"),
        )
        await db.commit()
        return resume

    state = _DIAL_UNKNOWN if record.dial_attempt_state == _DIAL_UNKNOWN else "in_progress"
    pending_response = _pending_dial_response(record, state)
    await db.commit()
    return pending_response


async def _claim_dispatch_ready_dial_attempt(
    *,
    call_record_id: uuid.UUID,
    dial_attempt_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    request_sha256: str,
    db: AsyncSession,
) -> CallRecord | CallResponse | JSONResponse:
    """Atomically let exactly one v2 reservation reach the carrier."""
    claimed = await db.execute(
        update(CallRecord)
        .where(
            CallRecord.id == call_record_id,
            CallRecord.dial_attempt_id == dial_attempt_id,
            CallRecord.user_id == owner_user_id,
            CallRecord.dial_request_sha256 == request_sha256,
            CallRecord.dial_attempt_state == _DIAL_READY_V2,
            CallRecord.provider_call_id.like("pending:%"),
        )
        .values(dial_attempt_state=_DIAL_DISPATCHING)
        .returning(CallRecord)
        .execution_options(populate_existing=True)
    )
    record = claimed.scalar_one_or_none()
    if record is not None:
        await db.commit()
        return record

    # Another worker or a signed callback won. End this transaction, then return
    # the winner's authoritative state; never infer that a second POST is safe.
    await db.rollback()
    replay = await _replay_dial_attempt(
        dial_attempt_id=dial_attempt_id,
        owner_user_id=owner_user_id,
        request_sha256=request_sha256,
        db=db,
    )
    if replay is None:
        raise HTTPException(status_code=409, detail={"code": "dial_attempt_conflict"})
    if isinstance(replay, _DispatchReadyAttempt):
        return replay.response
    return replay


def _provider_rejection_is_definitive(exc: Exception) -> bool:
    """Only explicit no-dispatch evidence or provider 4xx proves rejection."""
    if isinstance(exc, TelnyxDialNotStartedError):
        return True
    response = getattr(exc, "response", None)
    raw_status = (
        getattr(exc, "status", None)
        or getattr(exc, "status_code", None)
        or getattr(response, "status_code", None)
    )
    try:
        status = int(str(raw_status))
    except (TypeError, ValueError):
        return False
    return (
        _HTTP_CLIENT_ERROR_MIN <= status < _HTTP_SERVER_ERROR_MIN
        and status != _HTTP_REQUEST_TIMEOUT
    )


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
    without silently switching providers. Returns None when the selected provider is
    invalid or unavailable.
    """
    pref = (preferred or "twilio").lower()
    if pref == "telnyx":
        return "telnyx" if has_telnyx else None
    if pref == "twilio":
        return "twilio" if has_twilio else None
    return None


def resolve_recording_flag(*, agent_enabled: bool, to_number: str) -> tuple[bool, str | None, str]:
    """Decide whether this outbound call may be recorded.

    Three independent gates, ALL of which must agree:
      1. `settings.CALL_RECORDING_ENABLED` — the platform-wide kill switch.
      2. `agent_enabled` (Agent.enable_recording) — the operator's intent.
      3. The legal-consent policy — one-party-consent US states only, fail-safe OFF
         for unknown area codes, US territories and non-US numbers.

    Returns `(record, consent_state, consent_reason)` so the caller can log WHY a
    recording was or wasn't made without re-deriving the state.
    """
    consent_allowed, consent_state, consent_reason = recording_policy.recording_decision(to_number)
    record = bool(settings.CALL_RECORDING_ENABLED) and agent_enabled and consent_allowed
    return record, consent_state, consent_reason


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


async def require_owned_caller_id(
    *,
    from_number: str,
    workspace_id: uuid.UUID | None,
    owner_user_id: int,
    db: AsyncSession,
) -> None:
    """Refuse outbound caller IDs not registered to the resolved owner.

    The security property is OWNERSHIP: the caller ID must be a number this
    user has registered. Workspace scoping is an ADDITIONAL constraint, applied
    only when a workspace was actually resolved.

    It must not be a precondition. `PhoneNumber.workspace_id` is nullable by
    design, and this deployment is single-tenant with no workspace rows at all,
    so demanding a workspace made the gate unsatisfiable: it 403'd every
    outbound call, including every call the reply machine placed. Found by a
    live seeded call on 2026-08-03, hours after the gate first shipped —
    no test caught it because the suites never populate this table.
    """
    conditions = [
        StoredPhoneNumber.phone_number == from_number,
        StoredPhoneNumber.user_id == user_id_to_uuid(owner_user_id),
    ]
    if workspace_id is not None:
        conditions.append(StoredPhoneNumber.workspace_id == workspace_id)

    result = await db.execute(
        select(StoredPhoneNumber.id).where(*conditions).limit(1)
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=403,
            detail="Caller ID is not owned by the selected workspace",
        )


async def _lock_twilio_outbound_answer_record(
    *,
    db: AsyncSession,
    call_record_id: str,
    call_sid: str,
    agent_id: str,
    workspace_id: str,
    from_number: str,
    to_number: str,
) -> CallRecord | None:
    """Correlate a signed Twilio answer to one precommitted outbound call."""
    real_call_sid = _real_provider_call_id(call_sid)
    if real_call_sid is None or not from_number or not to_number:
        return None
    call_sid = real_call_sid
    try:
        agent_uuid = uuid.UUID(agent_id)
        workspace_uuid = uuid.UUID(workspace_id) if workspace_id else None
        record_uuid = uuid.UUID(call_record_id) if call_record_id else None
    except ValueError:
        return None

    workspace_filter = (
        CallRecord.workspace_id == workspace_uuid
        if workspace_uuid is not None
        else CallRecord.workspace_id.is_(None)
    )
    scope = (
        CallRecord.provider == "twilio",
        CallRecord.direction == CallDirection.OUTBOUND.value,
        CallRecord.agent_id == agent_uuid,
        workspace_filter,
        CallRecord.from_number == from_number,
        CallRecord.to_number == to_number,
    )

    if record_uuid is not None:
        result = await db.execute(
            select(CallRecord)
            .where(
                CallRecord.id == record_uuid,
                *scope,
                or_(
                    CallRecord.provider_call_id == call_sid,
                    CallRecord.provider_call_id.like("pending:%"),
                ),
            )
            .limit(2)
            .with_for_update()
        )
        candidates = result.scalars().all()
    else:
        result = await db.execute(
            select(CallRecord)
            .where(*scope, CallRecord.provider_call_id == call_sid)
            .limit(2)
            .with_for_update()
        )
        candidates = result.scalars().all()
        if not candidates:
            pending = await db.execute(
                select(CallRecord)
                .where(
                    *scope,
                    CallRecord.provider_call_id.like("pending:%"),
                    CallRecord.created_at >= datetime.now(UTC) - timedelta(minutes=2),
                    CallRecord.ended_at.is_(None),
                )
                .order_by(CallRecord.created_at.desc())
                .limit(2)
                .with_for_update()
            )
            candidates = pending.scalars().all()

    if len(candidates) != 1:
        return None
    record = candidates[0]
    record.provider_call_id = call_sid
    _mark_dial_attempt_accepted_by_callback(record)
    return record


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
    workspace_id: str | None = Query(
        None, description="Workspace ID (optional; falls back to account-level keys)"
    ),
) -> CallResponse | JSONResponse:
    """Legacy unkeyed call route retained for rolling compatibility."""
    return await _initiate_call_impl(
        call_request,
        request,
        current_user,
        db,
        workspace_id=workspace_id,
    )


@router.post(
    "/calls/idempotent",
    response_model=CallResponse,
    responses={
        202: {"model": DialAttemptPendingResponse},
        409: {"model": DialAttemptErrorResponse},
    },
)
async def initiate_call_idempotent(
    call_request: IdempotentInitiateCallRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    workspace_id: str | None = Query(
        None, description="Workspace ID (optional; falls back to account-level keys)"
    ),
) -> CallResponse | JSONResponse:
    """Replay first; rate-limit only a genuinely unseen physical dial key."""
    legacy_request = InitiateCallRequest.model_validate(
        call_request.model_dump(exclude={"dial_attempt_id"})
    )
    requested_workspace_id = _parse_requested_workspace_id(workspace_id)
    replay = await _replay_dial_attempt(
        dial_attempt_id=call_request.dial_attempt_id,
        owner_user_id=user_id_to_uuid(current_user.id),
        request_sha256=_dial_request_sha256(legacy_request, requested_workspace_id),
        db=db,
    )
    if isinstance(replay, _DispatchReadyAttempt):
        return await _resume_keyed_call(
            legacy_request,
            request,
            current_user,
            db,
            workspace_id=workspace_id,
            dial_attempt_id=call_request.dial_attempt_id,
            resume_attempt=replay,
        )
    if replay is not None:
        return replay
    new_attempt = await _initiate_new_keyed_call(
        legacy_request,
        request,
        current_user,
        db,
        workspace_id=workspace_id,
        dial_attempt_id=call_request.dial_attempt_id,
    )
    return cast("CallResponse | JSONResponse", new_attempt)


@limiter.limit("30/minute")
async def _initiate_new_keyed_call(
    call_request: InitiateCallRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession,
    *,
    workspace_id: str | None,
    dial_attempt_id: uuid.UUID,
) -> CallResponse | JSONResponse:
    """Apply the cost limiter only after the key was absent from durable state."""
    return await _initiate_call_impl(
        call_request,
        request,
        current_user,
        db,
        workspace_id=workspace_id,
        dial_attempt_id=dial_attempt_id,
        skip_dial_attempt_replay=True,
    )


async def _resume_keyed_call(
    call_request: InitiateCallRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession,
    *,
    workspace_id: str | None,
    dial_attempt_id: uuid.UUID,
    resume_attempt: _DispatchReadyAttempt,
) -> CallResponse | JSONResponse:
    """Resume a provably pre-dispatch reservation without reusing the limiter."""
    return await _initiate_call_impl(
        call_request,
        request,
        current_user,
        db,
        workspace_id=workspace_id,
        dial_attempt_id=dial_attempt_id,
        skip_dial_attempt_replay=True,
        resume_attempt=resume_attempt,
    )


async def _initiate_call_impl(  # noqa: PLR0911, PLR0912, PLR0915
    call_request: InitiateCallRequest,
    request: Request,
    current_user: CurrentUser,
    db: AsyncSession,
    *,
    workspace_id: str | None,
    dial_attempt_id: uuid.UUID | None = None,
    skip_dial_attempt_replay: bool = False,
    resume_attempt: _DispatchReadyAttempt | None = None,
) -> CallResponse | JSONResponse:
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

    # The keyed replay fingerprint uses the transport workspace selection, not the
    # mutable workspace that happens to be the user's default when a replay arrives.
    requested_workspace_uuid = _parse_requested_workspace_id(workspace_id)

    # These values are server-owned. Hash them before any mutable new-dial preflight,
    # so an accepted attempt remains replayable after agent/config ownership changes.
    call_variables = dict(call_request.variables or {})
    call_variables[CALENDAR_BACKEND_VARIABLE] = CALCOM_REQUIRED_BACKEND
    owner_user_id = user_id_to_uuid(current_user.id)
    request_sha256: str | None = None
    if dial_attempt_id is not None:
        request_sha256 = _dial_request_sha256(
            call_request,
            requested_workspace_uuid,
        )
        if not skip_dial_attempt_replay:
            replay = await _replay_dial_attempt(
                dial_attempt_id=dial_attempt_id,
                owner_user_id=owner_user_id,
                request_sha256=request_sha256,
                db=db,
            )
            if isinstance(replay, _DispatchReadyAttempt):
                resume_attempt = replay
            elif replay is not None:
                return replay
        if resume_attempt is not None:
            call_variables = dict(resume_attempt.variables)
        else:
            # The transport copy is authoritative; callers cannot spoof a different key
            # inside variables and poison call-ended correlation.
            call_variables["dialAttemptId"] = str(dial_attempt_id)
            call_variables["dial_attempt_id"] = str(dial_attempt_id)

    # Only a new attempt passes mutable authorization and dependency gates.
    # Agent.user_id is the INTEGER users.id (not the UUID) — comparing it to a UUID
    # throws "operator does not exist: integer = uuid".
    result = await db.execute(
        select(Agent).where(
            Agent.id == uuid.UUID(call_request.agent_id),
            Agent.user_id == current_user.id,
        )
    )
    agent = result.scalar_one_or_none()

    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    workspace_uuid = (
        resume_attempt.workspace_id
        if resume_attempt is not None
        else await resolve_outbound_workspace_id(
            agent_id=agent.id,
            owner_user_id=current_user.id,
            requested_workspace_id=requested_workspace_uuid,
            db=db,
        )
    )
    await require_owned_caller_id(
        from_number=call_request.from_number,
        workspace_id=workspace_uuid,
        owner_user_id=current_user.id,
        db=db,
    )

    missing_calendar_settings = missing_calcom_settings()
    if missing_calendar_settings:
        log.error(
            "outbound_call_calendar_unavailable",
            missing_settings=missing_calendar_settings,
        )
        raise HTTPException(
            status_code=503,
            detail={"code": "calendar_unavailable"},
        )

    # Select exactly the configured outbound provider. Telnyx is dormant unless it is
    # explicitly selected; missing credentials must fail closed rather than rerouting.
    telnyx_service = await get_telnyx_service(current_user.id, db, workspace_id=workspace_uuid)
    twilio_service = await get_twilio_service(current_user.id, db, workspace_id=workspace_uuid)

    preferred = (
        resume_attempt.provider
        if resume_attempt is not None
        else (settings.TELEPHONY_OUTBOUND_PROVIDER or "twilio").lower()
    )
    provider = select_outbound_provider(
        preferred, has_telnyx=telnyx_service is not None, has_twilio=twilio_service is not None
    )

    if provider is None:
        log.error("selected_telephony_provider_unavailable", provider=preferred)
        raise HTTPException(
            status_code=503,
            detail={"code": "telephony_provider_unavailable", "provider": preferred},
        )

    # Build webhook URL (forward per-call variables as base64-JSON in ?cv= so the
    # answer webhook -> media WS can personalize the prompt + fill the booking attendee)
    call_record_id = (
        resume_attempt.call_record_id if resume_attempt is not None else uuid.uuid4()
    )
    base_url = str(request.base_url).rstrip("/")
    webhook_url = f"{base_url}/webhooks/{provider}/answer?agent_id={call_request.agent_id}"
    if provider == "twilio":
        webhook_url = f"{webhook_url}&call_record_id={call_record_id}"
    if workspace_uuid:
        webhook_url = f"{webhook_url}&workspace_id={workspace_uuid}"
    if call_variables:
        import base64
        import json as _json

        cv = base64.urlsafe_b64encode(_json.dumps(call_variables).encode()).decode()
        webhook_url = f"{webhook_url}&cv={cv}"

    # Recording gate: the (previously dead) per-agent toggle ANDed with the legal
    # consent policy, under a platform-wide kill switch. All three must agree.
    # getattr keeps the safe default (no recording) if the attribute is ever absent.
    agent_wants_recording = bool(getattr(agent, "enable_recording", False))
    record_flag, consent_state, consent_reason = resolve_recording_flag(
        agent_enabled=agent_wants_recording, to_number=call_request.to_number
    )
    log.info(
        "call_recording_decision",
        record=record_flag,
        agent_enabled=agent_wants_recording,
        platform_enabled=bool(settings.CALL_RECORDING_ENABLED),
        consent_state=consent_state,
        consent_reason=consent_reason,
    )
    if agent_wants_recording and not record_flag:
        log.warning(
            "call_recording_denied_by_policy",
            consent_state=consent_state,
            consent_reason=consent_reason,
        )

    # Commit a correlation row BEFORE dialing (both providers). An immediate status
    # callback can then reconcile by the unique pending From/To record instead of
    # being lost in the POST-before-record race.
    call_record: CallRecord | None = None
    if resume_attempt is None:
        call_record = CallRecord(
            id=call_record_id,
            user_id=owner_user_id,
            workspace_id=workspace_uuid,
            provider=provider,
            provider_call_id=f"pending:{uuid.uuid4()}",
            agent_id=agent.id,
            direction=CallDirection.OUTBOUND.value,
            status=CallStatus.INITIATED.value,
            from_number=call_request.from_number,
            to_number=call_request.to_number,
            # Persisted so the terminal call-ended event can echo the lead context back
            # to the reply-router even from the bare status-callback path.
            variables=call_variables,
            dial_attempt_id=dial_attempt_id,
            dial_request_sha256=request_sha256,
            dial_attempt_state=_DIAL_READY_V2 if dial_attempt_id else None,
        )
        db.add(call_record)
        try:
            await db.commit()
        except IntegrityError:
            if dial_attempt_id is None or request_sha256 is None:
                raise
            await db.rollback()
            replay = await _replay_dial_attempt(
                dial_attempt_id=dial_attempt_id,
                owner_user_id=owner_user_id,
                request_sha256=request_sha256,
                db=db,
            )
            if replay is None:
                raise
            if isinstance(replay, _DispatchReadyAttempt):
                return await _resume_keyed_call(
                    call_request,
                    request,
                    current_user,
                    db,
                    workspace_id=workspace_id,
                    dial_attempt_id=dial_attempt_id,
                    resume_attempt=replay,
                )
            return replay

    if dial_attempt_id is not None and request_sha256 is not None:
        dispatch_claim = await _claim_dispatch_ready_dial_attempt(
            call_record_id=call_record_id,
            dial_attempt_id=dial_attempt_id,
            owner_user_id=owner_user_id,
            request_sha256=request_sha256,
            db=db,
        )
        if not isinstance(dispatch_claim, CallRecord):
            return dispatch_claim
        call_record = dispatch_claim

    if call_record is None:  # pragma: no cover - defensive legacy invariant
        raise RuntimeError("call record missing before provider dispatch")

    # Recording is a Twilio-only capability, so it is passed on the concrete Twilio
    # branch rather than through the abstract provider contract.
    recording_callback_url = f"{base_url}/webhooks/twilio/recording" if record_flag else None
    try:
        if provider == "twilio" and twilio_service is not None:
            call_info = await twilio_service.initiate_call(
                to_number=call_request.to_number,
                from_number=call_request.from_number,
                webhook_url=webhook_url,
                agent_id=call_request.agent_id,
                record=record_flag,
                recording_callback_url=recording_callback_url,
            )
        else:
            service = telnyx_service if provider == "telnyx" else twilio_service
            call_info = await service.initiate_call(  # type: ignore[union-attr]
                to_number=call_request.to_number,
                from_number=call_request.from_number,
                webhook_url=webhook_url,
                agent_id=call_request.agent_id,
            )
    except Exception as exc:
        twilio_unknown = isinstance(exc, TwilioDialOutcomeUnknownError)
        telnyx_unknown = provider == "telnyx" and is_unknown_telnyx_dial_outcome(exc)

        if dial_attempt_id is not None:
            locked = await db.execute(
                select(CallRecord)
                .where(CallRecord.id == call_record.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            attempt_record = locked.scalar_one()
            if _real_provider_call_id(attempt_record.provider_call_id) is not None:
                # The signed carrier callback is stronger evidence than a lost SDK
                # response: Twilio/Telnyx accepted the call.
                accepted_response = _accepted_response_from_record(attempt_record)
                attempt_record.dial_attempt_state = _DIAL_ACCEPTED
                attempt_record.dial_attempt_result = accepted_response.model_dump(mode="json")
                await db.commit()
                return accepted_response

            if twilio_unknown or telnyx_unknown or not _provider_rejection_is_definitive(exc):
                attempt_record.dial_attempt_state = _DIAL_UNKNOWN
                pending_response = _pending_dial_response(attempt_record, _DIAL_UNKNOWN)
                record_id = str(attempt_record.id)
                await db.commit()
                log.warning(
                    "dial_attempt_outcome_unknown",
                    record_id=record_id,
                    provider=provider,
                    error_type=type(exc).__name__,
                )
                return pending_response

            attempt_record.dial_attempt_state = _DIAL_REJECTED
            attempt_record.dial_attempt_result = {
                "code": "dial_attempt_rejected",
                "error_type": type(exc).__name__,
            }
            attempt_record.status = CallStatus.FAILED.value
            attempt_record.ended_at = datetime.now(UTC)
            error = _dial_attempt_rejected_error(attempt_record)
            await db.commit()
            raise error from exc

        if twilio_unknown:
            log.warning(
                "twilio_dial_outcome_unknown",
                record_id=str(call_record.id),
                error_type=type(exc.__cause__).__name__,
            )
            raise

        # Telnyx-only: an unknown dial outcome must NOT be marked failed (the call may
        # still be live); surface it and let reconciliation settle the record.
        if telnyx_unknown:
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

    provider_call_id = _real_provider_call_id(call_info.call_id)
    if dial_attempt_id is not None and provider_call_id is None:
        locked = await db.execute(
            select(CallRecord)
            .where(CallRecord.id == call_record.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        attempt_record = locked.scalar_one()
        if _real_provider_call_id(attempt_record.provider_call_id) is not None:
            accepted_response = _accepted_response_from_record(attempt_record)
            attempt_record.dial_attempt_state = _DIAL_ACCEPTED
            attempt_record.dial_attempt_result = accepted_response.model_dump(mode="json")
            await db.commit()
            return accepted_response
        attempt_record.dial_attempt_state = _DIAL_UNKNOWN
        pending_response = _pending_dial_response(attempt_record, _DIAL_UNKNOWN)
        record_id = str(attempt_record.id)
        await db.commit()
        log.warning(
            "dial_attempt_missing_provider_id",
            record_id=record_id,
            provider=provider,
        )
        return pending_response

    locked = await db.execute(
        select(CallRecord)
        .where(CallRecord.id == call_record.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    call_record = locked.scalar_one()
    call_record.provider_call_id = provider_call_id or call_info.call_id
    response = CallResponse(
        call_id=provider_call_id or call_info.call_id,
        call_control_id=call_info.call_control_id,
        from_number=call_info.from_number,
        to_number=call_info.to_number,
        direction=call_info.direction.value,
        status=call_info.status.value,
        agent_id=call_info.agent_id,
        call_record_id=str(call_record.id),
        dial_attempt_id=str(dial_attempt_id) if dial_attempt_id else None,
        dial_attempt_status=_DIAL_ACCEPTED if dial_attempt_id else None,
    )
    if dial_attempt_id is not None:
        call_record.dial_attempt_state = _DIAL_ACCEPTED
        call_record.dial_attempt_result = response.model_dump(mode="json")
    await db.commit()

    log.info("call_initiated", call_id=call_info.call_id, provider=provider)
    log.info("call_record_created", record_id=str(call_record.id))

    return response


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

    existing = await db.execute(
        select(CallRecord)
        .where(
            CallRecord.provider == "twilio",
            CallRecord.provider_call_id == call_sid,
        )
        .limit(2)
        .with_for_update()
    )
    candidates = existing.scalars().all()
    if len(candidates) > 1:
        log.warning("twilio_inbound_call_ambiguous")
        await db.rollback()
        return _twilio_reject_response()
    if candidates:
        call_record = candidates[0]
        if (
            call_record.direction != CallDirection.INBOUND.value
            or call_record.agent_id != agent.id
            or call_record.workspace_id != agent_workspace_id
            or call_record.from_number != from_number
            or call_record.to_number != to_number
        ):
            log.warning("twilio_inbound_call_scope_mismatch")
            await db.rollback()
            return _twilio_reject_response()
    else:
        call_record = CallRecord(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"pulsift:twilio:{call_sid}"),
            user_id=user_id_to_uuid(agent.user_id),
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

    media_grant = arm_twilio_media_grant(call_record, "")
    if media_grant is None:
        log.warning("twilio_inbound_media_grant_refused")
        await db.rollback()
        return _twilio_reject_response()
    await db.commit()
    log.info("call_record_created", record_id=str(call_record.id))

    # Build WebSocket URL for media streaming
    base_url = str(request.base_url).rstrip("/")
    ws_url = base_url.replace("http://", "wss://").replace("https://", "wss://")
    stream_url = f"{ws_url}/ws/telephony/twilio/{agent_id}"

    # Generate TwiML to connect to our WebSocket
    twilio_service = TwilioService("", "")  # Just need TwiML generation
    twiml = twilio_service.generate_answer_response(
        stream_url,
        agent_id,
        custom_parameters={
            "media_grant": media_grant,
            "workspace_id": str(call_record.workspace_id) if call_record.workspace_id else "",
        },
    )

    log.info("twilio_twiml_generated", agent_id=agent_id)

    return Response(content=twiml, media_type="application/xml")


@webhook_router.post("/twilio/status")
async def twilio_status_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    call_record_id: str = Query(default=""),
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

    call_record, candidate_count = await _find_twilio_lifecycle_record(
        call_record_id=call_record_id,
        call_sid=call_sid,
        from_number=from_number,
        to_number=to_number,
        db=db,
    )

    if call_record:
        callback_at = datetime.now(UTC)
        normalized_status = call_status.strip().lower().replace("_", "-")
        mapped_status = _TWILIO_STATUS_MAP.get(normalized_status)
        if mapped_status is None:
            log.warning("twilio_status_unrecognized")
        else:
            # Still applied. Only the "did it just become terminal" ANSWER is gone,
            # and that existed solely to decide whether to update a campaign
            # contact. The lifecycle write itself is untouched.
            _apply_twilio_lifecycle_status(
                call_record,
                mapped_status,
                event_at=callback_at,
                provider_duration=_parse_twilio_duration(call_duration),
            )

        if (
            mapped_status in _TERMINAL_CALL_STATUSES
            and call_record.direction == CallDirection.OUTBOUND.value
        ):
            await stage_terminal_call_event(db, call_record, observed_at=callback_at)

        await db.commit()
        log.info("call_record_updated", record_id=str(call_record.id), status=call_record.status)
    else:
        await db.rollback()
        log.warning(
            "call_record_not_found_or_ambiguous",
            call_sid=call_sid,
            candidate_count=candidate_count,
            has_call_record_id=bool(call_record_id),
        )

    return {"status": "received"}


@webhook_router.post("/twilio/answer", response_class=HTMLResponse)
async def twilio_answer_webhook(
    request: Request,
    agent_id: str = Query(default=""),
    cv: str = Query(default=""),
    workspace_id: str = Query(default=""),
    call_record_id: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
    call_sid: str = Form(default="", alias="CallSid"),
    from_number: str = Form(default="", alias="From"),
    to_number: str = Form(default="", alias="To"),
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

    call_record = await _lock_twilio_outbound_answer_record(
        db=db,
        call_record_id=call_record_id,
        call_sid=call_sid,
        agent_id=agent_id,
        workspace_id=workspace_id,
        from_number=from_number,
        to_number=to_number,
    )
    if call_record is None:
        log.warning("twilio_outbound_call_not_correlated")
        await db.rollback()
        return _twilio_reject_response()

    media_grant = arm_twilio_media_grant(call_record, cv)
    if media_grant is None:
        log.warning("twilio_outbound_media_grant_refused", record_id=str(call_record.id))
        await db.rollback()
        return _twilio_reject_response()
    await db.commit()

    base_url = str(request.base_url).rstrip("/")
    ws_url = base_url.replace("http://", "wss://").replace("https://", "wss://")
    stream_url = f"{ws_url}/ws/telephony/twilio/{agent_id}"
    twilio_service = TwilioService("", "")
    twiml = twilio_service.generate_answer_response(
        stream_url,
        agent_id,
        custom_parameters={
            "cv": cv,
            "workspace_id": str(call_record.workspace_id) if call_record.workspace_id else "",
            "media_grant": media_grant,
        },
    )

    return Response(content=twiml, media_type="application/xml")


@webhook_router.post("/twilio/recording")
async def twilio_recording_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
    recording_sid: str = Form(default="", alias="RecordingSid"),
    recording_url: str = Form(default="", alias="RecordingUrl"),
    recording_status: str = Form(default="", alias="RecordingStatus"),
    call_sid: str = Form(default="", alias="CallSid"),
) -> dict[str, str]:
    """Persist the recording URL once Twilio finishes writing the file.

    Fired by ``recording_status_callback`` (event: completed). This is the only
    moment the recording URL becomes knowable, so it is the only place
    ``CallRecord.recording_url`` is written.

    Always answers 200 -- an unknown CallSid is logged, not raised, because a 4xx/5xx
    here makes Twilio retry the same dead callback repeatedly for no gain.
    """
    # Validate Twilio signature
    await verify_twilio_webhook(request)

    log = logger.bind(
        webhook="twilio_recording",
        call_sid=call_sid,
        recording_sid=recording_sid,
        recording_status=recording_status,
    )
    log.info("twilio_recording_callback")

    if not call_sid or not recording_url:
        log.warning("twilio_recording_callback_incomplete")
        return {"status": "ignored"}

    result = await db.execute(select(CallRecord).where(CallRecord.provider_call_id == call_sid))
    call_record = result.scalar_one_or_none()

    if not call_record:
        # Never make Twilio retry-spam: acknowledge, log, move on.
        log.warning("call_record_not_found_for_recording", call_sid=call_sid)
        return {"status": "ignored"}

    # Twilio's RecordingUrl is extension-less; appending ".mp3" is the documented way
    # to request the compressed audio rather than raw WAV.
    call_record.recording_url = f"{recording_url}.mp3"
    # The share token is what lets this audio be played without anyone being
    # asked for credentials. It is normally minted alongside the transcript, but
    # a call can be recorded and leave no transcript (an instant hang-up), so
    # mint one here too rather than stranding the recording behind an auth box.
    if not call_record.share_token:
        call_record.share_token = generate_public_id(prefix="tr",
                                                     length=SHARE_TOKEN_LENGTH)
    await db.commit()

    log.info("call_recording_saved", record_id=str(call_record.id))

    return {"status": "received"}


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
        user_id=user_id_to_uuid(agent.user_id),
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
        lifecycle_at = event_at or datetime.now(UTC)
        _apply_telnyx_lifecycle_event(
            call_record,
            event_type,
            payload,
            event_at=lifecycle_at,
            provider_duration=provider_duration,
        )

        if event_type == "call.hangup" and call_record.direction == CallDirection.OUTBOUND.value:
            await stage_terminal_call_event(db, call_record, observed_at=lifecycle_at)

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
