"""Durable, deduplicated operator alerts - the one Slack-bound notification lane.

Two other durable outboxes (call_events.py, fulfilment_webhook.py) can each
reach a state where a human has to act: a finished call that can never reach
the reply system, or a promised list handover stuck for good. Both need the
same thing - a notification that survives a crash, never fires twice for the
same incident, and never spams. This module is that one mechanism; both
callers stage into it instead of inventing their own.

Lane split: `message` here is ALWAYS the operator lane - short, plain
English, naming the prospect and the by-hand action required, no ids,
hashes, HTTP codes, or internal field names. The technical lane is
unchanged and lives where it always has: structlog plus each outbox row's
own `last_error`.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.operator_alert import OperatorAlert
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
_MISSING_URL_ERROR = "OPERATOR_ALERTS_SLACK_WEBHOOK_URL not configured"

_worker_task: asyncio.Task[None] | None = None
_warned_missing_url = False


def _alert_insert(db: AsyncSession, *, dedup_key: str, message: str, now: datetime) -> object:
    values = {
        "dedup_key": dedup_key,
        "message": message,
        "state": "pending",
        "next_attempt_at": now,
        "attempts": 0,
    }
    if db.get_bind().dialect.name == "sqlite":
        return sqlite_insert(OperatorAlert).values(**values).on_conflict_do_nothing(
            index_elements=[OperatorAlert.dedup_key]
        )
    return postgresql_insert(OperatorAlert).values(**values).on_conflict_do_nothing(
        index_elements=[OperatorAlert.dedup_key]
    )


async def stage_operator_alert(
    db: AsyncSession,
    *,
    dedup_key: str,
    message: str,
    now: datetime | None = None,
) -> None:
    """Stage one deduplicated operator alert inside the CALLER's transaction.

    Callers pass their own in-flight `db` session so the alert commits or
    rolls back atomically with the state transition it belongs to - a
    permanent block is never recorded without its alert, and vice versa.
    `dedup_key` is the outbox primary key, so staging the same incident
    twice is a silent no-op.
    """
    await db.execute(
        _alert_insert(db, dedup_key=dedup_key, message=message, now=now or datetime.now(UTC))
    )


class _Claim:
    __slots__ = ("attempts", "dedup_key", "token")

    def __init__(self, dedup_key: str, token: uuid.UUID, attempts: int) -> None:
        self.dedup_key = dedup_key
        self.token = token
        self.attempts = attempts


async def _claim_due_alert(*, now: datetime | None = None) -> _Claim | None:
    now = now or datetime.now(UTC)
    expired_before = now - timedelta(seconds=_LEASE_SECONDS)
    statement = (
        select(OperatorAlert)
        .where(
            or_(
                and_(
                    OperatorAlert.state == "pending",
                    OperatorAlert.next_attempt_at <= now,
                ),
                and_(
                    OperatorAlert.state == "sending",
                    or_(
                        OperatorAlert.claimed_at.is_(None),
                        OperatorAlert.claimed_at <= expired_before,
                    ),
                ),
            )
        )
        .order_by(OperatorAlert.next_attempt_at, OperatorAlert.created_at)
        .limit(1)
        .with_for_update(of=OperatorAlert, skip_locked=True)
    )
    return await lease_one(
        AsyncSessionLocal,
        statement,
        now=now,
        claimed_state="sending",
        claim_factory=lambda row, token, attempts: _Claim(row.dedup_key, token, attempts),
    )


async def _ack_claim(claim: _Claim) -> bool:
    return await transition_claim(
        AsyncSessionLocal,
        OperatorAlert,
        identity_conditions=(OperatorAlert.dedup_key == claim.dedup_key,),
        token=claim.token,
        expected_state="sending",
        target_state="sent",
        mark_sent=True,
    )


async def _retry_claim(claim: _Claim, error: str, *, delay_seconds: int) -> bool:
    return await transition_claim(
        AsyncSessionLocal,
        OperatorAlert,
        identity_conditions=(OperatorAlert.dedup_key == claim.dedup_key,),
        token=claim.token,
        expected_state="sending",
        target_state="pending",
        error=error,
        delay_seconds=delay_seconds,
    )


async def _mark_missing_url() -> None:
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(OperatorAlert)
            .where(
                OperatorAlert.state.in_(("pending", "sending")),
                or_(
                    OperatorAlert.last_error.is_(None),
                    OperatorAlert.last_error != _MISSING_URL_ERROR,
                ),
            )
            .values(last_error=_MISSING_URL_ERROR, updated_at=datetime.now(UTC))
        )


def _retry_delay(attempts: int) -> int:
    return exponential_backoff(attempts)


async def _post_once(url: str, body: bytes) -> int:
    return await post_once(url, body, {"Content-Type": "application/json"}, timeout_seconds=_TIMEOUT_SECONDS)


async def dispatch_due_operator_alert() -> bool:
    """Attempt one due alert; return whether a row was claimed."""
    global _warned_missing_url
    webhook_url = settings.OPERATOR_ALERTS_SLACK_WEBHOOK_URL
    if not webhook_url:
        if not _warned_missing_url:
            _warned_missing_url = True
            logger.error("operator_alert_worker_unconfigured", reason=_MISSING_URL_ERROR)
        await _mark_missing_url()
        return False
    _warned_missing_url = False

    claim = await _claim_due_alert()
    if claim is None:
        return False

    log = logger.bind(component="operator_alerts", dedup_key=claim.dedup_key)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(OperatorAlert.message).where(
                OperatorAlert.dedup_key == claim.dedup_key,
                OperatorAlert.state == "sending",
                OperatorAlert.claim_token == claim.token,
            )
        )
        message = result.scalar_one_or_none()
    if message is None:
        log.warning("operator_alert_claim_lost")
        return True

    body = json.dumps({"text": message}, separators=(",", ":")).encode("utf-8")
    try:
        status_code = await _post_once(webhook_url, body)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        await _retry_claim(claim, error, delay_seconds=_retry_delay(claim.attempts))
        log.warning("operator_alert_delivery_error", error=error, attempt=claim.attempts)
        return True

    disposition = classify_delivery_status(status_code, permanent_client_errors=False)
    if disposition is DeliveryDisposition.ACK:
        acknowledged = await _ack_claim(claim)
        log.info("operator_alert_delivered", attempt=claim.attempts, claim_acknowledged=acknowledged)
        return True

    error = f"HTTP {status_code}"
    await _retry_claim(claim, error, delay_seconds=_retry_delay(claim.attempts))
    log.warning("operator_alert_retryable_response", status=status_code, attempt=claim.attempts)
    return True


async def _worker_loop(*, interval_seconds: float) -> None:
    await run_worker_loop(
        dispatch_due_operator_alert,
        interval_seconds=interval_seconds,
        on_error=lambda: logger.exception("operator_alert_worker_error"),
    )


async def start_operator_alert_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = start_worker_task(
        _worker_task,
        lambda: _worker_loop(interval_seconds=_WORKER_POLL_SECONDS),
        name="operator-alert-outbox",
    )
    logger.info("operator_alert_worker_started")


async def stop_operator_alert_worker() -> None:
    global _worker_task
    await stop_worker_task(_worker_task)
    _worker_task = None
