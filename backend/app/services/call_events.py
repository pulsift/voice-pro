"""Signed call-ended events fired when an outbound call reaches a terminal state (B4).

Without this, nothing downstream ever learns what happened after a call: a
no-answer never becomes a retry, and an answered-but-unbooked lead is stranded.
When CALL_EVENTS_URL is configured, every call that reaches a terminal state
POSTs one JSON event to f"{CALL_EVENTS_URL}/webhooks/call-ended" so the
reply-router can advance the lead's post-call status machine. Unset = silent no-op.

Fired from the telephony status callbacks on terminal status (primary, carries
the authoritative carrier outcome) with the media-WS teardown as a delayed
fallback for calls whose status callback never arrives. An in-process per-call
guard keeps it to one event per call; a duplicate across a redeploy race is
acceptable — the receiver dedupes on call_id.

Authenticity: the body is signed with `X-VoicePro-Signature: sha256=<hex>` — an
HMAC-SHA256 over the raw JSON bytes — keyed by CALL_EVENTS_SECRET. Unset secret
= send unsigned, with a one-time warning.

Delivery is fire-and-forget: failures log loudly but never break call teardown.
One retry on 5xx/timeout/transport error; any other response is terminal.
"""

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any

import httpx
import structlog

from app.core.config import settings
from app.models.call_record import CallRecord

logger = structlog.get_logger()

_TIMEOUT_SECONDS = 10.0
_MAX_ATTEMPTS = 2  # one retry on 5xx/timeout
_RETRY_DELAY_SECONDS = 1.0
_HTTP_SUCCESS_MIN = 200
_HTTP_SUCCESS_MAX = 300
_HTTP_SERVER_ERROR = 500
_SENT_GUARD_TTL_SECONDS = 24 * 60 * 60
# The media-WS teardown fallback waits this long so the provider's own terminal
# status callback (the authoritative carrier outcome) can claim the send first.
FALLBACK_DELAY_SECONDS = 20.0

# Booking-attempt categories that prove a real Cal.com booking exists (a direct
# create success, or a transient POST later reconciled as landed).
_BOOKED_CATEGORIES = {"success", "reconciled_success"}

# Keep references to in-flight background tasks so they aren't garbage-collected
# mid-flight (asyncio only holds a weak reference to a bare create_task() result).
_background_tasks: set[asyncio.Task[None]] = set()

# Per-call single-shot guard (in-process). Never released on delivery failure:
# exactly one event per call from this process, by design.
_sent_call_ids: dict[str, float] = {}

# Warn only once per process when the event goes out unsigned.
_warned_unsigned = False


def extract_booking_outcome(
    booking_attempts: list[dict[str, Any]] | None,
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


def build_call_ended_payload(record: CallRecord) -> dict[str, Any]:
    """Build the call-ended event body from the record's in-memory state."""
    booked, booking_uid = extract_booking_outcome(record.booking_attempts)
    return {
        "call_id": str(record.id),
        "provider_call_id": record.provider_call_id,
        "to_number": record.to_number,
        "status": record.status,
        "answered": record.answered_at is not None,
        "duration_seconds": record.duration_seconds or 0,
        "booked": booked,
        "booking_uid": booking_uid,
        "variables": record.variables or {},
    }


def _signed_request_parts(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """Serialize the payload once and sign those exact bytes (raw-bytes HMAC)."""
    global _warned_unsigned
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
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
    return body, headers


async def _post_once(url: str, body: bytes, headers: dict[str, str]) -> tuple[bool, bool]:
    """POST the event once; return (delivered, retryable)."""
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        resp = await client.post(url, content=body, headers=headers)
    if _HTTP_SUCCESS_MIN <= resp.status_code < _HTTP_SUCCESS_MAX:
        return True, False
    return False, resp.status_code >= _HTTP_SERVER_ERROR


async def _deliver(
    url: str,
    payload: dict[str, Any],
    call_id: str,
    *,
    delay_seconds: float,
) -> None:
    """Claim the per-call guard (after any fallback delay) and deliver the event."""
    log = logger.bind(component="call_events", call_id=call_id)
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)
    # Claim after the delay so a primary (status-callback) send wins the guard.
    # Single-threaded event loop + no await between check and set = race-free.
    if call_id in _sent_call_ids:
        log.info("call_ended_event_skipped_already_sent")
        return
    _sent_call_ids[call_id] = time.monotonic()

    body, headers = _signed_request_parts(payload)
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            delivered, retryable = await _post_once(url, body, headers)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            log.warning("call_ended_event_transport_error", error=str(e), attempt=attempt)
            retryable = True
        except Exception:
            # Unexpected/programming error - never break call teardown over telemetry.
            log.exception("call_ended_event_unexpected_error", attempt=attempt)
            return
        else:
            if delivered:
                log.info("call_ended_event_delivered", attempt=attempt)
                return
            if not retryable:
                log.error("call_ended_event_rejected", attempt=attempt)
                return
            log.warning("call_ended_event_retryable_response", attempt=attempt)
        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_RETRY_DELAY_SECONDS)

    log.error("call_ended_event_failed_all_attempts", attempts=_MAX_ATTEMPTS)


def schedule_call_ended_event(record: CallRecord, *, delay_seconds: float = 0.0) -> None:
    """Schedule the call-ended event as a background task; returns immediately.

    Safe to call unconditionally at any terminal point - if CALL_EVENTS_URL isn't
    configured this just logs and returns, and the per-call guard makes repeated
    calls (status callback + media-WS fallback) send exactly one event. Call this
    BEFORE the session commits/expires the record: the payload is built from the
    record's in-memory state right here.
    """
    base_url = settings.CALL_EVENTS_URL
    if not base_url:
        logger.debug("call_ended_event_skipped_not_configured")
        return

    call_id = str(record.id)
    now = time.monotonic()
    expired_before = now - _SENT_GUARD_TTL_SECONDS
    for seen_id, sent_at in list(_sent_call_ids.items()):
        if sent_at < expired_before:
            _sent_call_ids.pop(seen_id, None)
    if call_id in _sent_call_ids:
        logger.info("call_ended_event_skipped_already_sent", call_id=call_id)
        return

    url = base_url.rstrip("/") + "/webhooks/call-ended"
    payload = build_call_ended_payload(record)
    task = asyncio.create_task(_deliver(url, payload, call_id, delay_seconds=delay_seconds))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
