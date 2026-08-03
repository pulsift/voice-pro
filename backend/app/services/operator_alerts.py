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
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import structlog
from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import AsyncSessionLocal
from app.models.operator_alert import OperatorAlert
from app.services.durable_events import (
    DEFAULT_LEASE_SECONDS,
    DeliveryDisposition,
    classify_delivery_status,
    exponential_backoff,
    lease_one,
    run_worker_loop,
    start_worker_task,
    stop_worker_task,
    transition_claim,
)

logger = structlog.get_logger()

_TIMEOUT_SECONDS = 10.0
_WORKER_POLL_SECONDS = 1.0
_LEASE_SECONDS = DEFAULT_LEASE_SECONDS
_MISSING_DESTINATION_ERROR = (
    "no Slack destination configured "
    "(SLACK_BOT_TOKEN+SLACK_CHANNEL, or SLACK_WEBHOOK_URL)"
)
_SLACK_POST_MESSAGE_URL = "https://slack.com/api/chat.postMessage"


# --------------------------------------------------------------------------
# Destination resolution — the house Slack contract, reimplemented from
# pulsift-reply-router's reply_router/slack.py (not imported, that repo is
# not a dependency; kept in step by eye instead). A bot token is preferred
# (real Web API delivery); a legacy incoming-webhook URL is the fallback for
# a workspace that hasn't been handed a bot token yet. Operator alerts only
# ever stage into the OPS lane, but the LOGS fallback is reimplemented too
# so the resolution order matches the router's exactly, not just the part
# this caller happens to exercise today.
# --------------------------------------------------------------------------

OPS = "ops"
LOGS = "logs"


def _first_env(*names: str) -> str:
    """First non-empty env var among aliases (Sami's local `SLACK_[PULSIFT]_*`
    names, or Railway's canonical `SLACK_*`)."""
    for name in names:
        val = os.environ.get(name)
        if val:
            return val
    return ""


def _channel_for(lane: str) -> str:
    """The logs lane falls back to the ops channel when unset, so a
    half-configured workspace never silently drops a message."""
    ops = _first_env("SLACK_CHANNEL", "SLACK_[PULSIFT]_CHANNEL")
    if lane == LOGS:
        return _first_env("SLACK_LOGS_CHANNEL", "SLACK_[PULSIFT]_LOGS_CHANNEL") or ops
    return ops


def _webhook_for(lane: str) -> str:
    if lane == LOGS:
        hook = _first_env("SLACK_LOGS_WEBHOOK_URL", "SLACK_[PULSIFT]_LOGS_WEBHOOK_URL")
        if hook:
            return hook
    return _first_env("SLACK_WEBHOOK_URL", "SLACK_[PULSIFT]_WEBHOOK_URL")


@dataclass(frozen=True)
class _Destination:
    mode: str  # "bot" | "webhook"
    token: str = ""
    channel: str = ""
    url: str = ""


def _resolve_destination(lane: str = OPS) -> _Destination | None:
    token = _first_env("SLACK_BOT_TOKEN", "SLACK_[PULSIFT]_BOT_TOKEN")
    channel = _channel_for(lane)
    if token and channel:
        return _Destination(mode="bot", token=token, channel=channel)
    url = _webhook_for(lane)
    if url:
        return _Destination(mode="webhook", url=url)
    return None


_worker_task: asyncio.Task[None] | None = None
_warned_missing_destination = False


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


async def _mark_missing_destination() -> None:
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(OperatorAlert)
            .where(
                OperatorAlert.state.in_(("pending", "sending")),
                or_(
                    OperatorAlert.last_error.is_(None),
                    OperatorAlert.last_error != _MISSING_DESTINATION_ERROR,
                ),
            )
            .values(last_error=_MISSING_DESTINATION_ERROR, updated_at=datetime.now(UTC))
        )


def _retry_delay(attempts: int) -> int:
    return exponential_backoff(attempts)


async def _post_once(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST immutable bytes once; returns the effective status.

    Slack's `chat.postMessage` replies HTTP 200 even when it silently
    declined the message (`{"ok": false, "error": "..."}`) - fold that into
    a synthetic non-2xx so `classify_delivery_status` retries it exactly
    like any other failed delivery instead of falsely acking. Bypasses the
    shared `durable_events.post_once` (status-only) because that detection
    needs the response body.
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS) as client:
        response = await client.post(url, content=body, headers=headers)
    if url == _SLACK_POST_MESSAGE_URL:
        try:
            ok = response.json().get("ok") is True
        except ValueError:
            ok = False
        if not ok:
            return 599
    return response.status_code


async def dispatch_due_operator_alert() -> bool:
    """Attempt one due alert; return whether a row was claimed."""
    global _warned_missing_destination
    destination = _resolve_destination()
    if destination is None:
        if not _warned_missing_destination:
            _warned_missing_destination = True
            logger.error("operator_alert_worker_unconfigured", reason=_MISSING_DESTINATION_ERROR)
        await _mark_missing_destination()
        return False
    _warned_missing_destination = False

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

    if destination.mode == "bot":
        url = _SLACK_POST_MESSAGE_URL
        payload: dict[str, str] = {"channel": destination.channel, "text": message}
        headers = {
            "Authorization": f"Bearer {destination.token}",
            "Content-Type": "application/json; charset=utf-8",
        }
    else:
        url = destination.url
        payload = {"text": message}
        headers = {"Content-Type": "application/json"}
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    try:
        status_code = await _post_once(url, body, headers)
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
