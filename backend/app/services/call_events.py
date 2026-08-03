"""Durable signed call-ended delivery for outbound calls.

Without this, nothing downstream ever learns what happened after a call: a
no-answer never becomes a retry, and an answered-but-unbooked lead is stranded.
Carrier callbacks and media teardown upsert one database outbox row per call.
Delivery waits until both signals exist, or until a bounded one-signal grace
expires. A leased worker then locks and rereads the CallRecord, persists the exact
serialized body before HTTP, and retries those immutable bytes until a 2xx.

Authenticity: each request is signed with `X-VoicePro-Signature: sha256=<hex>` — an
HMAC-SHA256 over the raw JSON bytes — keyed by CALL_EVENTS_SECRET. Unset secret
= send unsigned, with a one-time warning.
"""

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

import structlog
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.call_event_outbox import CallEventOutbox
from app.models.call_record import CallDirection, CallRecord
from app.services.durable_events import (
    DEFAULT_LEASE_SECONDS,
    DeliveryDisposition,
    classify_delivery_status,
    exponential_backoff,
    lease_one,
    post_once,
    run_worker_loop,
    start_worker_task,
    stop_worker_task,
    transition_claim,
)

logger = structlog.get_logger()

_TIMEOUT_SECONDS = 10.0
_WORKER_POLL_SECONDS = 1.0
_LEASE_SECONDS = DEFAULT_LEASE_SECONDS
# Preserve the existing provider/media race grace. Both signals make a row
# immediately eligible; either signal alone waits this long before fallback.
FALLBACK_DELAY_SECONDS = 20.0
_MISSING_URL_ERROR = "CALL_EVENTS_URL not configured"

# Booking-attempt categories that prove a real Cal.com booking exists (a direct
# create success, or a transient POST later reconciled as landed).
_BOOKED_CATEGORIES = {"success", "reconciled_success"}

# Public transcript page served by app/api/transcripts.py (B2).
_TRANSCRIPT_PATH_PREFIX = "/api/public/transcripts"

# AMD verdicts (C2) that mean no human ever heard the pitch.
_MACHINE_AMD_VERDICTS = {"machine-vm", "machine-ivr"}

_worker_task: asyncio.Task[None] | None = None

# Warn only once per process when the event goes out unsigned.
_warned_unsigned = False
_warned_missing_url = False


def extract_booking_outcome(
    booking_attempts: list[Any] | None,
) -> tuple[bool, str | None]:
    """Return (booked, booking_uid) from the call's booking-attempt diagnostics.

    A call is booked when any create attempt succeeded — including a transient
    POST that a reconcile attempt later confirmed landed (category
    "reconciled_success"), which is a real booking too.
    """
    for attempt in booking_attempts or []:
        if not isinstance(attempt, dict):
            continue
        uid = str(attempt.get("uid") or "").strip()
        if uid and attempt.get("category") in _BOOKED_CATEGORIES:
            return True, uid
    return False, None


def build_transcript_url(share_token: str | None) -> str | None:
    """Public, no-auth transcript link for this call - None when there's nothing to link.

    The origin comes from PUBLIC_BASE_URL, falling back to PUBLIC_URL (the origin
    telephony webhooks already use). With neither configured we send None rather
    than a broken relative link.
    """
    if not share_token:
        return None
    base = settings.PUBLIC_BASE_URL or settings.PUBLIC_URL
    if not base:
        return None
    return f"{base.rstrip('/')}{_TRANSCRIPT_PATH_PREFIX}/{share_token}"


def build_recording_url(share_token: str | None) -> str | None:
    """Playable link to the call recording, or None when there is nothing to play.

    Points at our own proxy rather than the provider's media URL: the provider
    URL is behind HTTP Basic auth, so a browser opening it asks for credentials
    no human has. Same token, same expiry as the transcript link.
    """
    page = build_transcript_url(share_token)
    return f"{page}/recording" if page else None


def build_call_ended_payload(record: CallRecord) -> dict[str, Any]:
    """Build the call-ended event body from the record's in-memory state."""
    booked, booking_uid = extract_booking_outcome(record.booking_attempts)
    variables = record.variables if isinstance(record.variables, dict) else {}
    return {
        "call_id": str(record.id),
        "provider_call_id": record.provider_call_id,
        "dial_attempt_id": (
            str(record.dial_attempt_id)
            if isinstance(getattr(record, "dial_attempt_id", None), uuid.UUID)
            else None
        ),
        "to_number": record.to_number,
        "status": record.status,
        "answered": record.answered_at is not None,
        "duration_seconds": record.duration_seconds or 0,
        "booked": booked,
        "booking_uid": booking_uid,
        "variables": variables,
        # A voicemail/IVR pickup is "answered" to the carrier but nobody heard the
        # pitch - the receiver needs that distinction to decide on a retry.
        "voicemail": str(variables.get("amd") or "") in _MACHINE_AMD_VERDICTS,
        "transcript_url": build_transcript_url(getattr(record, "share_token", None)),
    }


def _serialize_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


def _headers_for_body(body: bytes) -> dict[str, str]:
    """Sign the exact persisted bytes that will be sent."""
    global _warned_unsigned
    headers = {"Content-Type": "application/json"}
    secret = settings.CALL_EVENTS_SECRET
    if secret:
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-VoicePro-Signature"] = f"sha256={digest}"
    elif not _warned_unsigned:
        _warned_unsigned = True
        logger.warning(
            "call_ended_event_unsigned",
            reason="CALL_EVENTS_SECRET unset - sending without X-VoicePro-Signature",
        )
    return headers


def _signed_request_parts(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """Compatibility helper used by direct signature tests."""
    body = _serialize_payload(payload)
    return body, _headers_for_body(body)


def _outbox_upsert(
    db: AsyncSession,
    *,
    call_id: uuid.UUID,
    observed_at: datetime,
    carrier_terminal_at: datetime | None,
) -> Any:
    values = {
        "call_id": call_id,
        "state": "pending",
        "carrier_terminal_at": carrier_terminal_at,
        "available_at": observed_at + timedelta(seconds=FALLBACK_DELAY_SECONDS),
        "next_attempt_at": observed_at,
        "attempts": 0,
        "last_error": None if settings.CALL_EVENTS_URL else _MISSING_URL_ERROR,
    }
    if db.get_bind().dialect.name == "sqlite":
        sqlite_statement = sqlite_insert(CallEventOutbox).values(**values)
        return sqlite_statement.on_conflict_do_update(
            index_elements=[CallEventOutbox.call_id],
            set_={
                "carrier_terminal_at": func.coalesce(
                    CallEventOutbox.carrier_terminal_at,
                    sqlite_statement.excluded.carrier_terminal_at,
                ),
                "updated_at": func.now(),
            },
        )

    postgresql_statement = postgresql_insert(CallEventOutbox).values(**values)
    return postgresql_statement.on_conflict_do_update(
        index_elements=[CallEventOutbox.call_id],
        set_={
            "carrier_terminal_at": func.coalesce(
                CallEventOutbox.carrier_terminal_at,
                postgresql_statement.excluded.carrier_terminal_at,
            ),
            "updated_at": func.now(),
        },
    )


async def stage_terminal_call_event(
    db: AsyncSession,
    record: CallRecord,
    *,
    observed_at: datetime,
) -> None:
    """Stage the carrier signal in the caller's terminal-state transaction."""
    if record.direction != CallDirection.OUTBOUND.value:
        return
    await db.execute(
        _outbox_upsert(
            db,
            call_id=record.id,
            observed_at=observed_at,
            carrier_terminal_at=observed_at,
        )
    )


async def stage_media_finalized_call_event(
    db: AsyncSession,
    record: CallRecord,
    *,
    observed_at: datetime,
) -> None:
    """Persist media finalization and its outbox signal in one transaction."""
    if record.media_finalized_at is None:
        record.media_finalized_at = observed_at
    if record.direction != CallDirection.OUTBOUND.value:
        return
    await db.execute(
        _outbox_upsert(
            db,
            call_id=record.id,
            observed_at=observed_at,
            carrier_terminal_at=None,
        )
    )


class _Claim(NamedTuple):
    call_id: uuid.UUID
    token: uuid.UUID
    attempts: int


class _PayloadIntegrityError(RuntimeError):
    """The immutable serialized body no longer matches its persisted digest."""


async def _claim_due_event(*, now: datetime | None = None) -> _Claim | None:
    now = now or datetime.now(UTC)
    expired_before = now - timedelta(seconds=_LEASE_SECONDS)
    statement = (
        select(CallEventOutbox)
        .join(CallRecord, CallRecord.id == CallEventOutbox.call_id)
        .where(
            or_(
                and_(
                    CallEventOutbox.state == "pending",
                    CallEventOutbox.next_attempt_at <= now,
                ),
                and_(
                    CallEventOutbox.state == "sending",
                    or_(
                        CallEventOutbox.claimed_at.is_(None),
                        CallEventOutbox.claimed_at <= expired_before,
                    ),
                ),
            ),
            or_(
                and_(
                    CallEventOutbox.carrier_terminal_at.is_not(None),
                    CallRecord.media_finalized_at.is_not(None),
                ),
                CallEventOutbox.available_at <= now,
            ),
        )
        .order_by(CallEventOutbox.next_attempt_at, CallEventOutbox.created_at)
        .limit(1)
        .with_for_update(of=CallEventOutbox, skip_locked=True)
    )
    return await lease_one(
        AsyncSessionLocal,
        statement,
        now=now,
        claimed_state="sending",
        claim_factory=lambda row, token, attempts: _Claim(row.call_id, token, attempts),
    )


async def _materialize_claimed_body(claim: _Claim) -> bytes | None:
    """Lock call then outbox, and freeze one exact body before any HTTP."""
    async with AsyncSessionLocal() as db, db.begin():
        # Signal paths already hold CallRecord before their outbox UPSERT. Use
        # that same explicit order here so Postgres cannot form a lock cycle.
        record_result = await db.execute(
            select(CallRecord)
            .where(CallRecord.id == claim.call_id)
            .with_for_update(of=CallRecord)
            .execution_options(populate_existing=True)
        )
        record = record_result.scalar_one_or_none()
        if record is None:
            return None

        outbox_result = await db.execute(
            select(CallEventOutbox)
            .where(
                CallEventOutbox.call_id == claim.call_id,
                CallEventOutbox.state == "sending",
                CallEventOutbox.claim_token == claim.token,
            )
            .with_for_update(of=CallEventOutbox)
            .execution_options(populate_existing=True)
        )
        outbox = outbox_result.scalar_one_or_none()
        if outbox is None:
            return None
        if outbox.payload_body is None:
            body = _serialize_payload(build_call_ended_payload(record))
            outbox.payload_body = body.decode("utf-8")
            outbox.payload_sha256 = hashlib.sha256(body).hexdigest()
            return body

        body = outbox.payload_body.encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        if outbox.payload_sha256 != digest:
            raise _PayloadIntegrityError("persisted call-event body hash mismatch")
        return body


async def _ack_claim(claim: _Claim) -> bool:
    return await transition_claim(
        AsyncSessionLocal,
        CallEventOutbox,
        identity_conditions=(CallEventOutbox.call_id == claim.call_id,),
        token=claim.token,
        expected_state="sending",
        target_state="sent",
        mark_sent=True,
    )


async def _retry_claim(claim: _Claim, error: str, *, delay_seconds: int) -> bool:
    return await transition_claim(
        AsyncSessionLocal,
        CallEventOutbox,
        identity_conditions=(CallEventOutbox.call_id == claim.call_id,),
        token=claim.token,
        expected_state="sending",
        target_state="pending",
        error=error,
        delay_seconds=delay_seconds,
    )


async def _block_claim(claim: _Claim, error: str) -> bool:
    """Permanently expose an immutable-payload or integrity conflict."""
    return await transition_claim(
        AsyncSessionLocal,
        CallEventOutbox,
        identity_conditions=(CallEventOutbox.call_id == claim.call_id,),
        token=claim.token,
        expected_state="sending",
        target_state="blocked",
        error=error,
    )


async def _mark_missing_url() -> None:
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(CallEventOutbox)
            .where(
                CallEventOutbox.state.in_(("pending", "sending")),
                or_(
                    CallEventOutbox.last_error.is_(None),
                    CallEventOutbox.last_error != _MISSING_URL_ERROR,
                ),
            )
            .values(last_error=_MISSING_URL_ERROR, updated_at=datetime.now(UTC))
        )


def _retry_delay(attempts: int) -> int:
    return exponential_backoff(attempts)


async def _post_once(url: str, body: bytes, headers: dict[str, str]) -> int:
    return await post_once(url, body, headers, timeout_seconds=_TIMEOUT_SECONDS)


async def dispatch_due_call_event() -> bool:  # noqa: PLR0911
    """Attempt one due row; return whether a row was claimed."""
    global _warned_missing_url
    base_url = settings.CALL_EVENTS_URL
    if not base_url:
        if not _warned_missing_url:
            _warned_missing_url = True
            logger.error(
                "call_ended_event_worker_unconfigured",
                reason=_MISSING_URL_ERROR,
            )
        await _mark_missing_url()
        return False
    _warned_missing_url = False

    claim = await _claim_due_event()
    if claim is None:
        return False

    log = logger.bind(component="call_events", call_id=str(claim.call_id))
    try:
        body = await _materialize_claimed_body(claim)
        if body is None:
            log.warning("call_ended_event_claim_lost_before_materialize")
            return True
        status_code = await _post_once(
            base_url.rstrip("/") + "/webhooks/call-ended",
            body,
            _headers_for_body(body),
        )
    except asyncio.CancelledError:
        raise
    except _PayloadIntegrityError as exc:
        error = f"{type(exc).__name__}: {exc}"
        blocked = await _block_claim(claim, error)
        log.exception(
            "call_ended_event_payload_integrity_blocked",
            error=error,
            attempt=claim.attempts,
            claim_blocked=blocked,
        )
        return True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        await _retry_claim(claim, error, delay_seconds=_retry_delay(claim.attempts))
        log.warning("call_ended_event_delivery_error", error=error, attempt=claim.attempts)
        return True

    disposition = classify_delivery_status(
        status_code,
        permanent_client_errors=False,
    )
    if disposition is DeliveryDisposition.ACK:
        acknowledged = await _ack_claim(claim)
        log.info(
            "call_ended_event_delivered",
            attempt=claim.attempts,
            claim_acknowledged=acknowledged,
        )
        return True

    if disposition is DeliveryDisposition.BLOCK:
        error = "HTTP 409: immutable call-event payload conflict"
        blocked = await _block_claim(claim, error)
        log.error(
            "call_ended_event_payload_conflict_blocked",
            attempt=claim.attempts,
            claim_blocked=blocked,
        )
        return True

    error = f"HTTP {status_code}"
    await _retry_claim(claim, error, delay_seconds=_retry_delay(claim.attempts))
    log.warning("call_ended_event_retryable_response", status=status_code, attempt=claim.attempts)
    return True


async def _worker_loop(*, interval_seconds: float) -> None:
    await run_worker_loop(
        dispatch_due_call_event,
        interval_seconds=interval_seconds,
        on_error=lambda: logger.exception("call_ended_event_worker_error"),
    )


async def start_call_event_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = start_worker_task(
        _worker_task,
        lambda: _worker_loop(interval_seconds=_WORKER_POLL_SECONDS),
        name="call-event-outbox",
    )
    logger.info("call_ended_event_worker_started")


async def stop_call_event_worker() -> None:
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is None:
        return
    await stop_worker_task(task)
    logger.info("call_ended_event_worker_stopped")
