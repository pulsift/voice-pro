"""Outbound webhook fired on a successful booking, to trigger lead-magnet fulfilment.

Fire-and-forget by design: `schedule_fulfilment_webhook()` never awaits the HTTP
call inline so it can never delay or fail the voice call / booking it's attached
to. If `FULFIL_WEBHOOK_URL` is unset (no fulfilment service deployed yet, or not
wanted for this environment) this is a silent no-op.

Idempotency is the RECEIVER's responsibility, keyed on `booking_id` (the Cal.com
booking uid, globally unique) which is always present in the payload — a retried
or duplicated delivery carries the same booking_id so the receiver can dedupe.
"""

import asyncio
from typing import Any

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger()

_MAX_ATTEMPTS = 3
_BACKOFF_BASE_SECONDS = 1.0

# Keep references to in-flight background tasks so they aren't garbage-collected
# mid-flight (asyncio only holds a weak reference to a bare create_task() result).
_background_tasks: set[asyncio.Task[None]] = set()


async def _post_with_retries(url: str, payload: dict[str, Any]) -> None:
    """POST payload to url, retrying 3x with exponential backoff on 5xx/timeout."""
    log = logger.bind(component="fulfilment_webhook", booking_id=payload.get("booking_id"))

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
            if resp.status_code < 500:
                if resp.status_code >= 400:
                    log.warning(
                        "fulfil_webhook_client_error",
                        status=resp.status_code,
                        body=resp.text[:300],
                        attempt=attempt,
                    )
                else:
                    log.info("fulfil_webhook_delivered", status=resp.status_code, attempt=attempt)
                return
            log.warning("fulfil_webhook_server_error", status=resp.status_code, attempt=attempt)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            log.warning("fulfil_webhook_transport_error", error=str(e), attempt=attempt)
        except Exception:
            # Unexpected/programming error - don't retry, just log and give up quietly.
            log.exception("fulfil_webhook_unexpected_error", attempt=attempt)
            return

        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1)))

    log.error("fulfil_webhook_failed_all_attempts", attempts=_MAX_ATTEMPTS)


def schedule_fulfilment_webhook(payload: dict[str, Any]) -> None:
    """Schedule the fulfilment webhook as a background task; returns immediately.

    Safe to call unconditionally after a successful booking - if FULFIL_WEBHOOK_URL
    isn't configured this just logs and returns without scheduling anything.
    """
    url = settings.FULFIL_WEBHOOK_URL
    if not url:
        logger.debug("fulfil_webhook_skipped_not_configured")
        return

    target = url.rstrip("/") + "/fulfil"
    task = asyncio.create_task(_post_with_retries(target, payload))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
