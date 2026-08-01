"""Shared primitives for Postgres-backed, leased durable event delivery."""

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

import httpx
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

HTTP_SUCCESS_MIN = 200
HTTP_SUCCESS_MAX = 300
HTTP_CLIENT_ERROR = 400
HTTP_SERVER_ERROR = 500
HTTP_CONFLICT = 409
DEFAULT_LEASE_SECONDS = 60
DEFAULT_INITIAL_BACKOFF_SECONDS = 5
DEFAULT_MAX_BACKOFF_SECONDS = 30 * 60


class DeliveryDisposition(str, Enum):
    """A receiver response's durable state transition."""

    ACK = "ack"
    BLOCK = "block"
    RETRY = "retry"


def classify_delivery_status(
    status_code: int,
    *,
    permanent_client_errors: bool,
    retryable_client_statuses: frozenset[int] = frozenset(),
) -> DeliveryDisposition:
    """Classify one HTTP result without coupling it to a specific event type."""
    if HTTP_SUCCESS_MIN <= status_code < HTTP_SUCCESS_MAX:
        return DeliveryDisposition.ACK
    if status_code == HTTP_CONFLICT:
        return DeliveryDisposition.BLOCK
    if (
        permanent_client_errors
        and HTTP_CLIENT_ERROR <= status_code < HTTP_SERVER_ERROR
        and status_code not in retryable_client_statuses
    ):
        return DeliveryDisposition.BLOCK
    return DeliveryDisposition.RETRY


def exponential_backoff(
    attempts: int,
    *,
    initial_seconds: int = DEFAULT_INITIAL_BACKOFF_SECONDS,
    maximum_seconds: int = DEFAULT_MAX_BACKOFF_SECONDS,
) -> int:
    """Return the bounded backoff shared by every durable event producer."""
    exponent = min(max(attempts - 1, 0), 8)
    return int(min(initial_seconds * (2**exponent), maximum_seconds))


async def lease_one[ClaimT](
    session_factory: Callable[[], AsyncSession],
    statement: Any,
    *,
    now: datetime,
    claimed_state: str | Callable[[Any], str],
    claim_factory: Callable[[Any, uuid.UUID, int], ClaimT],
) -> ClaimT | None:
    """Atomically lease one row selected by a caller-supplied locking query."""
    async with session_factory() as db, db.begin():
        result = await db.execute(statement)
        row = result.scalar_one_or_none()
        if row is None:
            return None
        token = uuid.uuid4()
        row.state = claimed_state(row) if callable(claimed_state) else claimed_state
        row.claimed_at = now
        row.claim_token = token
        row.attempts += 1
        row.last_error = None
        row.updated_at = now
        return claim_factory(row, token, row.attempts)


async def transition_claim(
    session_factory: Callable[[], AsyncSession],
    model: Any,
    *,
    identity_conditions: Sequence[Any],
    token: uuid.UUID,
    expected_state: str,
    target_state: str,
    error: str | None = None,
    delay_seconds: int | None = None,
    mark_sent: bool = False,
) -> bool:
    """Mutate only the exact live lease; stale workers cannot overwrite a successor."""
    now = datetime.now(UTC)
    values: dict[str, Any] = {
        "state": target_state,
        "claimed_at": None,
        "claim_token": None,
        "last_error": error[:1000] if error is not None else None,
        "updated_at": now,
    }
    if delay_seconds is not None:
        values["next_attempt_at"] = now + timedelta(seconds=delay_seconds)
    if mark_sent:
        values["sent_at"] = now
    async with session_factory() as db, db.begin():
        result = await db.execute(
            update(model)
            .where(
                *identity_conditions,
                model.state == expected_state,
                model.claim_token == token,
            )
            .values(**values)
        )
        return bool(getattr(result, "rowcount", 0))


async def post_once(
    url: str,
    body: bytes,
    headers: dict[str, str],
    *,
    timeout_seconds: float,
) -> int:
    """POST immutable bytes once; durable retry decisions stay with the caller."""
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(url, content=body, headers=headers)
    return response.status_code


async def run_worker_loop(
    dispatch: Callable[[], Awaitable[bool]],
    *,
    interval_seconds: float,
    on_error: Callable[[], None],
) -> None:
    """Run one durable dispatcher until cancellation."""
    while True:
        try:
            processed = await dispatch()
        except asyncio.CancelledError:
            raise
        except Exception:
            on_error()
            processed = False
        if not processed:
            await asyncio.sleep(interval_seconds)


def start_worker_task(
    current_task: asyncio.Task[None] | None,
    task_factory: Callable[[], Awaitable[None]],
    *,
    name: str,
) -> asyncio.Task[None]:
    """Start a named worker once per process."""
    if current_task is not None and not current_task.done():
        return current_task
    return asyncio.create_task(task_factory(), name=name)


async def stop_worker_task(task: asyncio.Task[None] | None) -> None:
    """Cancel and join one process-local worker task."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
