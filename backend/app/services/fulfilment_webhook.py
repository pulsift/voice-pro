"""Durable handoff from a confirmed Cal.com booking to list fulfilment.

The booking tool persists a deterministic intent before it can POST to Cal.com.
Once Cal.com returns a booking UID, that same row is finalized with one immutable
JSON body and digest. A leased worker either reconciles an interrupted booking
attempt or retries delivery until the fulfilment receiver acknowledges it.
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
from app.models.fulfilment_outbox import FulfilmentOutbox
from app.services.durable_events import (
    DEFAULT_LEASE_SECONDS,
    HTTP_CONFLICT,
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
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_NOT_FOUND = 404
_HTTP_METHOD_NOT_ALLOWED = 405
_HTTP_REQUEST_TIMEOUT = 408
_HTTP_TOO_EARLY = 425
_HTTP_TOO_MANY_REQUESTS = 429
_WORKER_POLL_SECONDS = 1.0
_LEASE_SECONDS = DEFAULT_LEASE_SECONDS
_RECONCILE_GRACE_SECONDS = 30
_RECONCILE_WINDOW_HOURS = 24
_MISSING_URL_ERROR = "FULFIL_WEBHOOK_URL not configured"
_MISSING_SECRET_ERROR = "FULFIL_WEBHOOK_SECRET not configured"
_RETRYABLE_CLIENT_STATUSES = frozenset(
    {
        _HTTP_UNAUTHORIZED,
        _HTTP_FORBIDDEN,
        _HTTP_NOT_FOUND,
        _HTTP_METHOD_NOT_ALLOWED,
        _HTTP_REQUEST_TIMEOUT,
        _HTTP_TOO_EARLY,
        _HTTP_TOO_MANY_REQUESTS,
    }
)

_worker_task: asyncio.Task[None] | None = None
_warned_missing_url = False
_warned_missing_secret = False


class FulfilmentIntentConflictError(RuntimeError):
    """A deterministic booking intent was reused with different paid-work input."""


class ExtraBookingConflictError(FulfilmentIntentConflictError):
    """A second Cal.com UID conflicts with an already-frozen booking intent."""


class _PayloadIntegrityError(RuntimeError):
    """Persisted immutable JSON no longer matches its digest or required shape."""


class _Claim(NamedTuple):
    intent_key: str
    token: uuid.UUID
    attempts: int
    action: str


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _canonical_start(value: str) -> str:
    raw = value.strip()
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("booking start must include a timezone")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _intent_parts(
    *,
    start_iso: str,
    email: str,
    payload: dict[str, Any],
    workspace_id: uuid.UUID | None,
    user_id: int,
) -> tuple[str, str, str, int, bytes]:
    booking_start = _canonical_start(start_iso)
    booking_email = email.strip().lower()
    if not booking_email:
        raise ValueError("booking email is required")
    event_type_id = settings.CALCOM_EVENT_TYPE_ID
    if not isinstance(event_type_id, int):
        raise TypeError("Cal.com event type is required for fulfilment reconciliation")

    fulfilment_payload = dict(payload)
    fulfilment_payload.pop("booking_id", None)
    intent_body = _canonical_json(fulfilment_payload)
    identity = _canonical_json(
        {
            "booking_start": booking_start,
            "email": booking_email,
            "event_type_id": event_type_id,
            "user_id": user_id,
            "workspace_id": str(workspace_id) if workspace_id else None,
        }
    )
    return (
        hashlib.sha256(identity).hexdigest(),
        booking_start,
        booking_email,
        event_type_id,
        intent_body,
    )


def _intent_insert(db: AsyncSession, values: dict[str, Any]) -> Any:
    if db.get_bind().dialect.name == "sqlite":
        statement = sqlite_insert(FulfilmentOutbox).values(**values)
    else:
        statement = postgresql_insert(FulfilmentOutbox).values(**values)
    return statement.on_conflict_do_nothing(
        index_elements=[FulfilmentOutbox.intent_key]
    )


async def stage_fulfilment_intent(
    *,
    start_iso: str,
    email: str,
    payload: dict[str, Any],
    workspace_id: uuid.UUID | None,
    user_id: int,
) -> str:
    """Commit immutable paid-work input before any Cal.com booking attempt."""
    intent_key, booking_start, booking_email, event_type_id, intent_body = (
        _intent_parts(
            start_iso=start_iso,
            email=email,
            payload=payload,
            workspace_id=workspace_id,
            user_id=user_id,
        )
    )
    intent_sha256 = hashlib.sha256(intent_body).hexdigest()
    now = datetime.now(UTC)
    conflict = False
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            _intent_insert(
                db,
                {
                    "intent_key": intent_key,
                    "state": "awaiting_booking",
                    "booking_start": booking_start,
                    "booking_email": booking_email,
                    "cal_event_type_id": event_type_id,
                    "intent_body": intent_body.decode("utf-8"),
                    "intent_sha256": intent_sha256,
                    "next_attempt_at": now
                    + timedelta(seconds=_RECONCILE_GRACE_SECONDS),
                    "reconcile_until": now + timedelta(hours=_RECONCILE_WINDOW_HOURS),
                    "attempts": 0,
                },
            )
        )
        result = await db.execute(
            select(FulfilmentOutbox)
            .where(FulfilmentOutbox.intent_key == intent_key)
            .with_for_update(of=FulfilmentOutbox)
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one()
        if (
            row.booking_start != booking_start
            or row.booking_email != booking_email
            or row.cal_event_type_id != event_type_id
            or row.intent_sha256 != intent_sha256
            or row.intent_body != intent_body.decode("utf-8")
        ):
            # The first committed intent is authoritative. A drifted replay must
            # not poison an already-pending or sent paid-work handoff.
            conflict = True
        elif row.state == "blocked":
            conflict = True
        elif row.state == "cancelled" and row.booking_id is None:
            row.state = "awaiting_booking"
            row.booking_dispatched_at = None
            row.attempts = 0
            row.next_attempt_at = now + timedelta(seconds=_RECONCILE_GRACE_SECONDS)
            row.reconcile_until = now + timedelta(hours=_RECONCILE_WINDOW_HOURS)
            row.claimed_at = None
            row.claim_token = None
            row.last_error = None
            row.updated_at = now

    if conflict:
        raise FulfilmentIntentConflictError(
            "deterministic fulfilment intent conflicts with stored input"
        )
    return intent_key


async def claim_fulfilment_booking(
    intent_key: str,
    *,
    now: datetime | None = None,
) -> uuid.UUID | None:
    """Lease the sole right to preflight and authorize one Cal.com create."""
    now = now or datetime.now(UTC)
    expired_before = now - timedelta(seconds=_LEASE_SECONDS)
    token = uuid.uuid4()
    async with AsyncSessionLocal() as db, db.begin():
        result = await db.execute(
            update(FulfilmentOutbox)
            .where(
                FulfilmentOutbox.intent_key == intent_key,
                FulfilmentOutbox.booking_id.is_(None),
                FulfilmentOutbox.booking_dispatched_at.is_(None),
                or_(
                    FulfilmentOutbox.state == "awaiting_booking",
                    and_(
                        FulfilmentOutbox.state == "booking_claimed",
                        or_(
                            FulfilmentOutbox.claimed_at.is_(None),
                            FulfilmentOutbox.claimed_at <= expired_before,
                        ),
                    ),
                ),
            )
            .values(
                state="booking_claimed",
                claimed_at=now,
                claim_token=token,
                last_error=None,
                updated_at=now,
            )
        )
        if not bool(getattr(result, "rowcount", 0)):
            return None
    return token


async def authorize_fulfilment_booking(intent_key: str, token: uuid.UUID) -> bool:
    """Fence the irreversible Cal.com POST behind the exact live booking lease."""
    now = datetime.now(UTC)
    expired_before = now - timedelta(seconds=_LEASE_SECONDS)
    async with AsyncSessionLocal() as db, db.begin():
        result = await db.execute(
            update(FulfilmentOutbox)
            .where(
                FulfilmentOutbox.intent_key == intent_key,
                FulfilmentOutbox.state == "booking_claimed",
                FulfilmentOutbox.claim_token == token,
                FulfilmentOutbox.claimed_at > expired_before,
                FulfilmentOutbox.booking_id.is_(None),
                FulfilmentOutbox.booking_dispatched_at.is_(None),
            )
            .values(
                state="booking_dispatched",
                booking_dispatched_at=now,
                claimed_at=now,
                next_attempt_at=now + timedelta(seconds=_RECONCILE_GRACE_SECONDS),
                last_error=None,
                updated_at=now,
            )
        )
        return bool(getattr(result, "rowcount", 0))


def _finalized_body(row: FulfilmentOutbox, booking_id: str) -> bytes:
    intent_bytes = row.intent_body.encode("utf-8")
    if hashlib.sha256(intent_bytes).hexdigest() != row.intent_sha256:
        raise _PayloadIntegrityError("persisted fulfilment intent hash mismatch")
    value = json.loads(row.intent_body)
    if not isinstance(value, dict):
        raise _PayloadIntegrityError("persisted fulfilment intent is not an object")
    value["booking_id"] = booking_id
    return _canonical_json(value)


def _payload_matches(row: FulfilmentOutbox, booking_id: str, body: bytes) -> bool:
    digest = hashlib.sha256(body).hexdigest()
    return (
        row.booking_id == booking_id
        and row.payload_body == body.decode("utf-8")
        and row.payload_sha256 == digest
    )


async def _lock_booking_id(db: AsyncSession, booking_id: str) -> None:
    """Serialize ownership of one Cal.com UID before checking its unique row."""
    if db.get_bind().dialect.name != "postgresql":
        return
    lock_key = int.from_bytes(
        hashlib.sha256(booking_id.encode()).digest()[:8], signed=True
    )
    await db.execute(select(func.pg_advisory_xact_lock(lock_key)))


async def finalize_fulfilment_intent(  # noqa: PLR0912, PLR0915
    intent_key: str | None, booking_id: Any
) -> bool:
    """Attach the proven Cal.com UID and freeze the exact delivery body."""
    if intent_key is None:
        return False
    normalized_booking_id = str(booking_id or "").strip()
    if not normalized_booking_id:
        raise ValueError("successful booking is missing its UID")

    now = datetime.now(UTC)
    conflict = False
    extra_booking_conflict: tuple[str, str] | None = None
    found = False
    async with AsyncSessionLocal() as db, db.begin():
        result = await db.execute(
            select(FulfilmentOutbox)
            .where(FulfilmentOutbox.intent_key == intent_key)
            .with_for_update(of=FulfilmentOutbox)
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        found = True
        try:
            body = _finalized_body(row, normalized_booking_id)
        except (json.JSONDecodeError, _PayloadIntegrityError) as exc:
            row.state = "blocked"
            row.claimed_at = None
            row.claim_token = None
            row.last_error = f"{type(exc).__name__}: {exc}"[:1000]
            row.updated_at = now
            conflict = True
        else:
            if row.payload_body is not None:
                if not _payload_matches(row, normalized_booking_id, body):
                    extra_booking_conflict = (
                        str(row.booking_id or ""),
                        normalized_booking_id,
                    )
                elif row.state == "blocked":
                    conflict = True
            elif row.state == "blocked":
                conflict = True
            else:
                await _lock_booking_id(db, normalized_booking_id)
                duplicate = await db.execute(
                    select(FulfilmentOutbox.intent_key).where(
                        FulfilmentOutbox.booking_id == normalized_booking_id,
                        FulfilmentOutbox.intent_key != intent_key,
                    )
                )
                if duplicate.scalar_one_or_none() is not None:
                    row.state = "blocked"
                    row.claimed_at = None
                    row.claim_token = None
                    row.last_error = (
                        "Cal.com booking UID belongs to another fulfilment intent"
                    )
                    row.updated_at = now
                    conflict = True
                elif row.state != "sent":
                    row.booking_id = normalized_booking_id
                    row.payload_body = body.decode("utf-8")
                    row.payload_sha256 = hashlib.sha256(body).hexdigest()
                    row.state = "pending"
                    row.attempts = 0
                    row.next_attempt_at = now
                    row.claimed_at = None
                    row.claim_token = None
                    row.last_error = None
                    row.updated_at = now

    if extra_booking_conflict is not None:
        authoritative_booking_id, conflicting_booking_id = extra_booking_conflict
        logger.error(
            "fulfilment_extra_booking_requires_manual_reconciliation",
            intent_key=intent_key,
            authoritative_booking_id=authoritative_booking_id,
            conflicting_booking_id=conflicting_booking_id,
        )
        raise ExtraBookingConflictError(
            "second Cal.com booking UID conflicts with authoritative fulfilment intent"
        )
    if conflict:
        raise FulfilmentIntentConflictError(
            "booking UID or frozen fulfilment payload conflicts"
        )
    return found


async def _claim_due_event(*, now: datetime | None = None) -> _Claim | None:
    now = now or datetime.now(UTC)
    expired_before = now - timedelta(seconds=_LEASE_SECONDS)
    statement = (
        select(FulfilmentOutbox)
        .where(
            or_(
                and_(
                    FulfilmentOutbox.state.in_(
                        ("awaiting_booking", "booking_dispatched", "pending")
                    ),
                    FulfilmentOutbox.next_attempt_at <= now,
                ),
                and_(
                    FulfilmentOutbox.state.in_(
                        ("booking_claimed", "reconciling", "sending")
                    ),
                    or_(
                        FulfilmentOutbox.claimed_at.is_(None),
                        FulfilmentOutbox.claimed_at <= expired_before,
                    ),
                ),
            )
        )
        .order_by(FulfilmentOutbox.next_attempt_at, FulfilmentOutbox.created_at)
        .limit(1)
        .with_for_update(of=FulfilmentOutbox, skip_locked=True)
    )
    return await lease_one(
        AsyncSessionLocal,
        statement,
        now=now,
        claimed_state=lambda row: "reconciling"
        if row.booking_id is None
        else "sending",
        claim_factory=lambda row, token, attempts: _Claim(
            row.intent_key,
            token,
            attempts,
            "reconcile" if row.booking_id is None else "deliver",
        ),
    )


async def _retry_claim(claim: _Claim, error: str, *, delay_seconds: int) -> bool:
    sending_state = "reconciling" if claim.action == "reconcile" else "sending"
    pending_state = "awaiting_booking" if claim.action == "reconcile" else "pending"
    return await transition_claim(
        AsyncSessionLocal,
        FulfilmentOutbox,
        identity_conditions=(FulfilmentOutbox.intent_key == claim.intent_key,),
        token=claim.token,
        expected_state=sending_state,
        target_state=pending_state,
        error=error,
        delay_seconds=delay_seconds,
    )


async def _block_claim(claim: _Claim, error: str) -> bool:
    expected_state = "reconciling" if claim.action == "reconcile" else "sending"
    return await transition_claim(
        AsyncSessionLocal,
        FulfilmentOutbox,
        identity_conditions=(FulfilmentOutbox.intent_key == claim.intent_key,),
        token=claim.token,
        expected_state=expected_state,
        target_state="blocked",
        error=error,
    )


async def _cancel_reconcile_claim(claim: _Claim, reason: str) -> bool:
    return await transition_claim(
        AsyncSessionLocal,
        FulfilmentOutbox,
        identity_conditions=(FulfilmentOutbox.intent_key == claim.intent_key,),
        token=claim.token,
        expected_state="reconciling",
        target_state="cancelled",
        error=reason,
    )


async def _ack_claim(claim: _Claim) -> bool:
    return await transition_claim(
        AsyncSessionLocal,
        FulfilmentOutbox,
        identity_conditions=(FulfilmentOutbox.intent_key == claim.intent_key,),
        token=claim.token,
        expected_state="sending",
        target_state="sent",
        mark_sent=True,
    )


async def _load_reconcile_input(
    claim: _Claim,
) -> tuple[str, str, int, datetime] | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(FulfilmentOutbox).where(
                FulfilmentOutbox.intent_key == claim.intent_key,
                FulfilmentOutbox.state == "reconciling",
                FulfilmentOutbox.claim_token == claim.token,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        intent_bytes = row.intent_body.encode("utf-8")
        if hashlib.sha256(intent_bytes).hexdigest() != row.intent_sha256:
            raise _PayloadIntegrityError("persisted fulfilment intent hash mismatch")
        return (
            row.booking_start,
            row.booking_email,
            row.cal_event_type_id,
            row.reconcile_until,
        )


async def _finalize_reconcile_claim(claim: _Claim, booking_id: str) -> bool:
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db, db.begin():
        result = await db.execute(
            select(FulfilmentOutbox)
            .where(
                FulfilmentOutbox.intent_key == claim.intent_key,
                FulfilmentOutbox.state == "reconciling",
                FulfilmentOutbox.claim_token == claim.token,
            )
            .with_for_update(of=FulfilmentOutbox)
            .execution_options(populate_existing=True)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return False
        await _lock_booking_id(db, booking_id)
        duplicate = await db.execute(
            select(FulfilmentOutbox.intent_key).where(
                FulfilmentOutbox.booking_id == booking_id,
                FulfilmentOutbox.intent_key != claim.intent_key,
            )
        )
        if duplicate.scalar_one_or_none() is not None:
            raise FulfilmentIntentConflictError(
                "Cal.com booking UID belongs to another fulfilment intent"
            )
        body = _finalized_body(row, booking_id)
        row.booking_id = booking_id
        row.payload_body = body.decode("utf-8")
        row.payload_sha256 = hashlib.sha256(body).hexdigest()
        row.state = "pending"
        row.attempts = 0
        row.next_attempt_at = now
        row.claimed_at = None
        row.claim_token = None
        row.last_error = None
        row.updated_at = now
        return True


async def _materialize_delivery_body(claim: _Claim) -> bytes | None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(FulfilmentOutbox).where(
                FulfilmentOutbox.intent_key == claim.intent_key,
                FulfilmentOutbox.state == "sending",
                FulfilmentOutbox.claim_token == claim.token,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        if not row.booking_id or row.payload_body is None or row.payload_sha256 is None:
            raise _PayloadIntegrityError(
                "finalized fulfilment row has no immutable payload"
            )
        body = row.payload_body.encode("utf-8")
        if hashlib.sha256(body).hexdigest() != row.payload_sha256:
            raise _PayloadIntegrityError("persisted fulfilment payload hash mismatch")
        payload = json.loads(row.payload_body)
        if not isinstance(payload, dict) or payload.get("booking_id") != row.booking_id:
            raise _PayloadIntegrityError(
                "persisted fulfilment payload has the wrong booking UID"
            )
        return body


def _headers_for_body(body: bytes) -> dict[str, str]:
    secret = settings.FULFIL_WEBHOOK_SECRET
    if not secret:
        raise RuntimeError(_MISSING_SECRET_ERROR)
    headers = {"Content-Type": "application/json"}
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    headers["X-Fulfil-Signature"] = f"sha256={digest}"
    return headers


def _signed_request_parts(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    """Compatibility helper for raw-body signature verification tests."""
    body = _canonical_json(payload)
    return body, _headers_for_body(body)


def _retry_delay(attempts: int) -> int:
    return exponential_backoff(attempts)


async def _post_once(url: str, body: bytes, headers: dict[str, str]) -> int:
    return await post_once(url, body, headers, timeout_seconds=_TIMEOUT_SECONDS)


async def _mark_configuration_error(error: str) -> None:
    async with AsyncSessionLocal() as db, db.begin():
        await db.execute(
            update(FulfilmentOutbox)
            .where(
                FulfilmentOutbox.state.in_(
                    ("awaiting_booking", "reconciling", "pending", "sending")
                ),
                or_(
                    FulfilmentOutbox.last_error.is_(None),
                    FulfilmentOutbox.last_error != error,
                ),
            )
            .values(last_error=error, updated_at=datetime.now(UTC))
        )


async def dispatch_due_fulfilment() -> bool:  # noqa: PLR0911, PLR0912, PLR0915
    """Reconcile or deliver one due row; return whether work was claimed."""
    global _warned_missing_secret, _warned_missing_url
    base_url = settings.FULFIL_WEBHOOK_URL
    if not base_url:
        if not _warned_missing_url:
            _warned_missing_url = True
            logger.error(
                "fulfilment_outbox_worker_unconfigured", reason=_MISSING_URL_ERROR
            )
        await _mark_configuration_error(_MISSING_URL_ERROR)
        return False
    _warned_missing_url = False
    if not settings.FULFIL_WEBHOOK_SECRET:
        if not _warned_missing_secret:
            _warned_missing_secret = True
            logger.error(
                "fulfilment_outbox_worker_unsigned_blocked",
                reason=_MISSING_SECRET_ERROR,
            )
        await _mark_configuration_error(_MISSING_SECRET_ERROR)
        return False
    _warned_missing_secret = False

    claim = await _claim_due_event()
    if claim is None:
        return False
    log = logger.bind(component="fulfilment_outbox", intent_key=claim.intent_key)

    if claim.action == "reconcile":
        try:
            inputs = await _load_reconcile_input(claim)
            if inputs is None:
                log.warning("fulfilment_reconcile_claim_lost")
                return True
            booking_start, booking_email, event_type_id, reconcile_until = inputs
            if reconcile_until.tzinfo is None:
                reconcile_until = reconcile_until.replace(tzinfo=UTC)
            from app.services.calcom_client import find_existing_booking

            result = await find_existing_booking(
                start_iso=booking_start,
                email=booking_email,
                event_type_id=event_type_id,
            )
            if result.get("success"):
                booking_id = str(result.get("uid") or "").strip()
                if not booking_id:
                    raise _PayloadIntegrityError("reconciled booking has no UID")  # noqa: TRY301
                finalized = await _finalize_reconcile_claim(claim, booking_id)
                log.info("fulfilment_booking_reconciled", claim_finalized=finalized)
                return True
            category = str(result.get("category") or "reconcile_unavailable")
            if category == "not_found" and datetime.now(UTC) >= reconcile_until:
                cancelled = await _cancel_reconcile_claim(
                    claim, "Cal.com booking not found before reconciliation deadline"
                )
                log.warning(
                    "fulfilment_intent_expired_without_booking", cancelled=cancelled
                )
                return True
            await _retry_claim(
                claim,
                f"Cal.com reconciliation: {category}",
                delay_seconds=_retry_delay(claim.attempts),
            )
            return True
        except asyncio.CancelledError:
            raise
        except (
            FulfilmentIntentConflictError,
            _PayloadIntegrityError,
            json.JSONDecodeError,
        ) as exc:
            error = f"{type(exc).__name__}: {exc}"
            blocked = await _block_claim(claim, error)
            log.exception(
                "fulfilment_reconcile_payload_blocked", error=error, blocked=blocked
            )
            return True
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            await _retry_claim(claim, error, delay_seconds=_retry_delay(claim.attempts))
            log.warning(
                "fulfilment_reconcile_error", error=error, attempt=claim.attempts
            )
            return True

    try:
        body = await _materialize_delivery_body(claim)
        if body is None:
            log.warning("fulfilment_delivery_claim_lost")
            return True
        status_code = await _post_once(
            base_url.rstrip("/") + "/fulfil",
            body,
            _headers_for_body(body),
        )
    except asyncio.CancelledError:
        raise
    except (_PayloadIntegrityError, json.JSONDecodeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        blocked = await _block_claim(claim, error)
        log.exception(
            "fulfilment_payload_integrity_blocked", error=error, blocked=blocked
        )
        return True
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        await _retry_claim(claim, error, delay_seconds=_retry_delay(claim.attempts))
        log.warning("fulfilment_delivery_error", error=error, attempt=claim.attempts)
        return True

    disposition = classify_delivery_status(
        status_code,
        permanent_client_errors=True,
        retryable_client_statuses=_RETRYABLE_CLIENT_STATUSES,
    )
    if disposition is DeliveryDisposition.ACK:
        acknowledged = await _ack_claim(claim)
        log.info("fulfilment_webhook_delivered", claim_acknowledged=acknowledged)
        return True
    if disposition is DeliveryDisposition.BLOCK:
        if status_code == HTTP_CONFLICT:
            error = "HTTP 409: immutable fulfilment payload conflict"
            event = "fulfilment_payload_conflict_blocked"
        else:
            error = f"HTTP {status_code}: permanent fulfilment receiver rejection"
            event = "fulfilment_webhook_permanent_rejection_blocked"
        blocked = await _block_claim(claim, error)
        log.error(event, status=status_code, blocked=blocked)
        return True

    error = f"HTTP {status_code}"
    await _retry_claim(claim, error, delay_seconds=_retry_delay(claim.attempts))
    if status_code in {_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN}:
        log.error(
            "fulfilment_webhook_auth_failure_retrying",
            status=status_code,
            attempt=claim.attempts,
        )
    else:
        log.warning("fulfilment_webhook_retryable_response", status=status_code)
    return True


async def _worker_loop(*, interval_seconds: float) -> None:
    await run_worker_loop(
        dispatch_due_fulfilment,
        interval_seconds=interval_seconds,
        on_error=lambda: logger.exception("fulfilment_outbox_worker_error"),
    )


async def start_fulfilment_worker() -> None:
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    _worker_task = start_worker_task(
        _worker_task,
        lambda: _worker_loop(interval_seconds=_WORKER_POLL_SECONDS),
        name="fulfilment-outbox",
    )
    logger.info("fulfilment_outbox_worker_started")


async def stop_fulfilment_worker() -> None:
    global _worker_task
    task = _worker_task
    _worker_task = None
    if task is None:
        return
    await stop_worker_task(task)
    logger.info("fulfilment_outbox_worker_stopped")
