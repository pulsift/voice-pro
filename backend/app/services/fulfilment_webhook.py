"""Outbound webhook fired on a successful booking, to trigger lead-magnet fulfilment.

Fire-and-forget by design: `schedule_fulfilment_webhook()` never awaits the HTTP
call inline so it can never delay or fail the voice call / booking it's attached
to. If `FULFIL_WEBHOOK_URL` is unset (no fulfilment service deployed yet, or not
wanted for this environment) this is a silent no-op.

Idempotency is the RECEIVER's responsibility, keyed on `booking_id` (the Cal.com
booking uid, globally unique) which is always present in the payload — a retried
or duplicated delivery carries the same booking_id so the receiver can dedupe.

Authenticity: when FULFIL_WEBHOOK_SECRET is configured, every POST carries
`X-Fulfil-Signature: sha256=<hex>` — an HMAC-SHA256 over the raw JSON body bytes —
so the fulfilment service can reject forged requests (S1). Unset secret = send
unsigned as before, with a one-time warning.
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

logger = structlog.get_logger()

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0
_UID_GUARD_TTL_SECONDS = 24 * 60 * 60
_HTTP_SUCCESS_MIN = 200
_HTTP_SUCCESS_MAX = 300
_HTTP_SERVER_ERROR = 500
_HTTP_REQUEST_TIMEOUT = 408
_HTTP_TOO_MANY_REQUESTS = 429
_RETRYABLE_CLIENT_STATUSES = {_HTTP_REQUEST_TIMEOUT, _HTTP_TOO_MANY_REQUESTS}

# Keep references to in-flight background tasks so they aren't garbage-collected
# mid-flight (asyncio only holds a weak reference to a bare create_task() result).
_background_tasks: set[asyncio.Task[bool]] = set()

# A media-stream reconnect creates a fresh CRMTools instance. Keep a bounded,
# process-local record of booking UIDs already dispatched so the new instance does
# not re-fire the same fulfilment webhook. The receiver remains the cross-process
# idempotency authority.
_scheduled_booking_ids: dict[str, float] = {}

# Warn only once per process when the outbound webhook goes out unsigned.
_warned_unsigned = False


def _signed_request_parts(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """Serialize the payload once and sign those exact bytes (raw-bytes HMAC).

    The signature must cover the bytes actually sent, so the body is serialized
    here rather than delegated to httpx's `json=` re-serialization.
    """
    global _warned_unsigned
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    secret = settings.FULFIL_WEBHOOK_SECRET
    if secret:
        digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        headers["X-Fulfil-Signature"] = f"sha256={digest}"
    elif not _warned_unsigned:
        _warned_unsigned = True
        logger.warning(
            "fulfil_webhook_unsigned",
            reason="FULFIL_WEBHOOK_SECRET unset - sending without X-Fulfil-Signature",
        )
    return body, headers


async def _post_with_retries(url: str, payload: dict[str, Any]) -> bool:
    """POST payload, returning whether delivery reached a terminal outcome.

    Successful 2xx and non-retryable responses are terminal. HTTP 408/429,
    5xx, and transport failures are retried and return False when exhausted.
    """
    log = logger.bind(component="fulfilment_webhook", booking_id=payload.get("booking_id"))
    body, headers = _signed_request_parts(payload)

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, content=body, headers=headers)
            if _HTTP_SUCCESS_MIN <= resp.status_code < _HTTP_SUCCESS_MAX:
                log.info("fulfil_webhook_delivered", status=resp.status_code, attempt=attempt)
                return True
            if (
                resp.status_code in _RETRYABLE_CLIENT_STATUSES
                or resp.status_code >= _HTTP_SERVER_ERROR
            ):
                log.warning(
                    "fulfil_webhook_retryable_response",
                    status=resp.status_code,
                    attempt=attempt,
                )
            else:
                log.warning(
                    "fulfil_webhook_terminal_response",
                    status=resp.status_code,
                    body=resp.text[:300],
                    attempt=attempt,
                )
                return True
        except (httpx.TimeoutException, httpx.TransportError) as e:
            log.warning("fulfil_webhook_transport_error", error=str(e), attempt=attempt)
        except Exception:
            # Unexpected/programming error - don't retry, just log and give up quietly.
            log.exception("fulfil_webhook_unexpected_error", attempt=attempt)
            return False

        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    log.error("fulfil_webhook_failed_all_attempts", attempts=_MAX_ATTEMPTS)
    return False


def _finish_background_task(task: asyncio.Task[bool], booking_id: str | None) -> None:
    """Release task state and permit a later dispatch when all delivery attempts failed."""
    _background_tasks.discard(task)
    terminal = not task.cancelled() and task.exception() is None and task.result()
    if booking_id and not terminal:
        _scheduled_booking_ids.pop(booking_id, None)


def schedule_fulfilment_webhook(payload: dict[str, Any]) -> None:
    """Schedule the fulfilment webhook as a background task; returns immediately.

    Safe to call unconditionally after a successful booking - if FULFIL_WEBHOOK_URL
    isn't configured this just logs and returns without scheduling anything.
    """
    url = settings.FULFIL_WEBHOOK_URL
    if not url:
        logger.debug("fulfil_webhook_skipped_not_configured")
        return

    booking_id_value = payload.get("booking_id")
    booking_id = (
        booking_id_value if isinstance(booking_id_value, str) and booking_id_value else None
    )
    now = time.monotonic()
    expired_before = now - _UID_GUARD_TTL_SECONDS
    for seen_id, scheduled_at in list(_scheduled_booking_ids.items()):
        if scheduled_at < expired_before:
            _scheduled_booking_ids.pop(seen_id, None)
    if booking_id and booking_id in _scheduled_booking_ids:
        logger.info("fulfil_webhook_skipped_duplicate", booking_id=booking_id)
        return

    target = url.rstrip("/") + "/fulfil"
    task = asyncio.create_task(_post_with_retries(target, payload))
    if booking_id:
        _scheduled_booking_ids[booking_id] = now
    _background_tasks.add(task)
    task.add_done_callback(lambda completed: _finish_background_task(completed, booking_id))
