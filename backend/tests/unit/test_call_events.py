"""Contracts for the signed call-ended event sender (B4)."""

# ruff: noqa: SLF001 - these tests intentionally verify module-private dispatch state.

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.api.telephony import telnyx_status_callback, twilio_status_callback
from app.api.telephony_ws import save_transcript_to_call_record
from app.core.config import settings
from app.models.call_event_outbox import CallEventOutbox
from app.models.call_record import CallRecord
from app.models.operator_alert import OperatorAlert
from app.services import call_events

BOOKED_ATTEMPTS = [
    {"operation": "availability", "category": "offered"},
    {"operation": "create", "category": "transient", "uid": None},
    {"operation": "create", "category": "success", "uid": "calcom-uid-1"},
]
UNBOOKED_ATTEMPTS = [
    {"operation": "availability", "category": "offered"},
    {"operation": "create", "category": "rejected", "uid": None},
]


def make_record(**overrides: Any) -> CallRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "provider": "twilio",
        "provider_call_id": "CA-test-1",
        "direction": "outbound",
        "status": "completed",
        "from_number": "+15550000001",
        "to_number": "+15550000002",
        "duration_seconds": 42,
        "answered_at": datetime.now(UTC),
        "booking_attempts": BOOKED_ATTEMPTS,
        "variables": {"leadName": "Ada", "tzName": "America/Los_Angeles"},
    }
    defaults.update(overrides)
    return CallRecord(**defaults)


@pytest_asyncio.fixture(autouse=True)
async def reset_dispatch_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    await call_events.stop_call_event_worker()
    monkeypatch.setattr(settings, "CALL_EVENTS_URL", "https://router.test")
    monkeypatch.setattr(settings, "CALL_EVENTS_SECRET", "events-secret")
    monkeypatch.setattr(call_events, "_warned_unsigned", False)
    monkeypatch.setattr(call_events, "_warned_missing_url", False)
    yield
    await call_events.stop_call_event_worker()


@pytest_asyncio.fixture
async def outbox_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'call-events.db').as_posix()}")

    def create_tables(connection: Connection) -> None:
        CallRecord.metadata.create_all(
            connection,
            tables=[CallRecord.__table__, CallEventOutbox.__table__, OperatorAlert.__table__],
        )

    async with engine.begin() as connection:
        await connection.run_sync(create_tables)
    yield engine
    await engine.dispose()


@pytest.fixture
def outbox_session_factory(
    monkeypatch: pytest.MonkeyPatch,
    outbox_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    factory = async_sessionmaker(
        outbox_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    monkeypatch.setattr(call_events, "AsyncSessionLocal", factory)
    return factory


async def persist_record(factory: async_sessionmaker[AsyncSession], record: CallRecord) -> None:
    async with factory() as db:
        db.add(record)
        await db.commit()


async def load_outbox(
    factory: async_sessionmaker[AsyncSession], call_id: uuid.UUID
) -> CallEventOutbox | None:
    async with factory() as db:
        return await db.get(CallEventOutbox, call_id)


async def load_operator_alert(
    factory: async_sessionmaker[AsyncSession], dedup_key: str
) -> OperatorAlert | None:
    async with factory() as db:
        return await db.get(OperatorAlert, dedup_key)


# ---------------------------------------------------------------------------
# Payload construction + booked extraction
# ---------------------------------------------------------------------------


def test_payload_reports_booking_from_successful_create_attempt() -> None:
    record = make_record()
    payload = call_events.build_call_ended_payload(record)

    assert payload == {
        "call_id": str(record.id),
        "provider_call_id": "CA-test-1",
        "dial_attempt_id": None,
        "to_number": "+15550000002",
        "status": "completed",
        "answered": True,
        "duration_seconds": 42,
        "booked": True,
        "booking_uid": "calcom-uid-1",
        "variables": {"leadName": "Ada", "tzName": "America/Los_Angeles"},
        # Transparency stack (B2/C2): a public transcript link when one has been
        # minted, plus the AMD voicemail verdict. The router treats both as
        # optional, so an older receiver is unaffected.
        "transcript_url": None,
        "voicemail": False,
    }


def test_payload_preserves_conversation_generation_aliases() -> None:
    record = make_record(variables={
        "leadName": "Ada",
        "conversation_generation": 2,
        "conversationGeneration": 2,
    })

    payload = call_events.build_call_ended_payload(record)

    assert payload["variables"]["conversation_generation"] == 2
    assert payload["variables"]["conversationGeneration"] == 2


def test_payload_counts_reconciled_booking_as_booked() -> None:
    record = make_record(
        booking_attempts=[
            {"operation": "create", "category": "transient", "uid": None},
            {"operation": "reconcile", "category": "reconciled_success", "uid": "calcom-uid-2"},
        ]
    )
    payload = call_events.build_call_ended_payload(record)

    assert payload["booked"] is True
    assert payload["booking_uid"] == "calcom-uid-2"


def test_payload_without_booking_or_answer_defaults_cleanly() -> None:
    record = make_record(
        status="no_answer",
        answered_at=None,
        duration_seconds=0,
        booking_attempts=UNBOOKED_ATTEMPTS,
        variables=None,
    )
    payload = call_events.build_call_ended_payload(record)

    assert payload["answered"] is False
    assert payload["booked"] is False
    assert payload["booking_uid"] is None
    assert payload["variables"] == {}


# ---------------------------------------------------------------------------
# Signature
# ---------------------------------------------------------------------------


def test_signature_header_covers_the_exact_bytes_sent() -> None:
    payload = {"call_id": "abc", "booked": True}
    body, headers = call_events._signed_request_parts(payload)

    expected = hmac.new(b"events-secret", body, hashlib.sha256).hexdigest()
    assert headers["X-VoicePro-Signature"] == f"sha256={expected}"
    assert headers["Content-Type"] == "application/json"
    assert json.loads(body) == payload


def test_unset_secret_sends_unsigned_and_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CALL_EVENTS_SECRET", None)

    with patch.object(call_events.logger, "warning") as warning:
        _, first_headers = call_events._signed_request_parts({"call_id": "a"})
        _, second_headers = call_events._signed_request_parts({"call_id": "b"})

    assert "X-VoicePro-Signature" not in first_headers
    assert "X-VoicePro-Signature" not in second_headers
    unsigned_warnings = [
        call for call in warning.call_args_list if call.args[0] == "call_ended_event_unsigned"
    ]
    assert len(unsigned_warnings) == 1


# ---------------------------------------------------------------------------
# Durable signal staging and carrier-first eligibility
# ---------------------------------------------------------------------------


async def stage_signals(
    factory: async_sessionmaker[AsyncSession],
    record: CallRecord,
    *,
    observed_at: datetime,
    terminal: bool,
    media: bool,
) -> None:
    await persist_record(factory, record)
    async with factory() as db:
        stored = await db.get(CallRecord, record.id)
        assert stored is not None
        if terminal:
            await call_events.stage_terminal_call_event(db, stored, observed_at=observed_at)
        if media:
            await call_events.stage_media_finalized_call_event(db, stored, observed_at=observed_at)
        await db.commit()


def test_outbox_schema_enforces_one_row_per_call() -> None:
    assert [column.name for column in CallEventOutbox.__table__.primary_key.columns] == ["call_id"]
    assert "ix_call_event_outbox_due" in {index.name for index in CallEventOutbox.__table__.indexes}


@pytest.mark.asyncio
async def test_repeated_signals_upsert_one_row_and_preserve_both_facts(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC) - timedelta(seconds=1)
    record = make_record()
    await persist_record(outbox_session_factory, record)

    async with outbox_session_factory() as db:
        stored = await db.get(CallRecord, record.id)
        assert stored is not None
        await call_events.stage_terminal_call_event(db, stored, observed_at=observed_at)
        await call_events.stage_terminal_call_event(
            db, stored, observed_at=observed_at + timedelta(milliseconds=1)
        )
        await call_events.stage_media_finalized_call_event(db, stored, observed_at=observed_at)
        await db.commit()

    async with outbox_session_factory() as db:
        rows = (await db.execute(select(CallEventOutbox))).scalars().all()
        stored = await db.get(CallRecord, record.id)

    assert len(rows) == 1
    assert rows[0].carrier_terminal_at is not None
    assert stored is not None
    assert stored.media_finalized_at is not None


@pytest.mark.asyncio
async def test_media_only_dispatches_exactly_once_once_grace_elapses(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A lost or delayed carrier terminal callback must not strand a
    media-only row forever - the router still needs to learn the call ended.
    Before the fallback grace elapses the row stays pending; once it elapses
    the row becomes due and is delivered exactly once.
    """
    observed_at = datetime.now(UTC) - timedelta(seconds=call_events.FALLBACK_DELAY_SECONDS + 1)
    record = make_record()
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=False,
        media=True,
    )

    before_grace = await call_events._claim_due_event(
        now=observed_at + timedelta(seconds=call_events.FALLBACK_DELAY_SECONDS - 1)
    )
    assert before_grace is None
    row = await load_outbox(outbox_session_factory, record.id)
    assert row is not None
    assert row.state == "pending"
    assert row.carrier_terminal_at is None

    with patch.object(call_events, "_post_once", AsyncMock(return_value=204)) as post:
        assert await call_events.dispatch_due_call_event() is True
        # No second row is due - the media-only event was delivered exactly once.
        assert await call_events.dispatch_due_call_event() is False

    post.assert_awaited_once()
    row = await load_outbox(outbox_session_factory, record.id)
    assert row is not None
    assert row.state == "sent"


@pytest.mark.asyncio
async def test_terminal_only_waits_for_bounded_grace(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC)
    record = make_record()
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=True,
        media=False,
    )

    before_grace = await call_events._claim_due_event(
        now=observed_at + timedelta(seconds=call_events.FALLBACK_DELAY_SECONDS - 1)
    )
    after_grace = await call_events._claim_due_event(
        now=observed_at + timedelta(seconds=call_events.FALLBACK_DELAY_SECONDS + 1)
    )

    assert before_grace is None
    assert after_grace is not None
    assert after_grace.call_id == record.id


@pytest.mark.asyncio
async def test_terminal_plus_media_is_immediately_eligible(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC)
    record = make_record()
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=True,
        media=True,
    )

    claim = await call_events._claim_due_event(now=observed_at + timedelta(milliseconds=1))

    assert claim is not None
    assert claim.call_id == record.id


@pytest.mark.asyncio
async def test_inbound_media_is_marked_without_creating_outbox_work(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC)
    record = make_record(direction="inbound")
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=False,
        media=True,
    )

    async with outbox_session_factory() as db:
        stored = await db.get(CallRecord, record.id)
        outbox = await db.get(CallEventOutbox, record.id)

    assert stored is not None
    assert stored.media_finalized_at is not None
    assert outbox is None


# ---------------------------------------------------------------------------
# Immutable delivery, durable retries, leases, and worker lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_reuses_exact_persisted_bytes_after_record_changes(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC) - timedelta(seconds=1)
    record = make_record(status="completed", transcript="initial transcript")
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=True,
        media=True,
    )
    post = AsyncMock(side_effect=[503, 204])

    with patch.object(call_events, "_post_once", post):
        assert await call_events.dispatch_due_call_event() is True
        first_row = await load_outbox(outbox_session_factory, record.id)
        assert first_row is not None
        assert first_row.state == "pending"
        assert first_row.payload_body is not None
        first_body = post.await_args_list[0].args[1]
        assert first_body == first_row.payload_body.encode("utf-8")
        assert first_row.payload_sha256 == hashlib.sha256(first_body).hexdigest()

        async with outbox_session_factory() as db:
            stored = await db.get(CallRecord, record.id)
            outbox = await db.get(CallEventOutbox, record.id)
            assert stored is not None
            assert outbox is not None
            stored.status = "failed"
            stored.transcript = "late transcript must not alter the event"
            stored.booking_attempts = []
            stored.variables = {"leadName": "Changed"}
            stored.share_token = "tr_changed"
            outbox.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
            await db.commit()

        assert await call_events.dispatch_due_call_event() is True

    second_row = await load_outbox(outbox_session_factory, record.id)
    second_body = post.await_args_list[1].args[1]
    assert post.await_count == 2
    assert second_body == first_body
    assert second_row is not None
    assert second_row.state == "sent"
    assert second_row.payload_body == first_body.decode("utf-8")
    assert second_row.payload_sha256 == hashlib.sha256(first_body).hexdigest()


@pytest.mark.asyncio
async def test_http_409_blocks_and_retains_payload_for_operator_action(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC) - timedelta(seconds=1)
    record = make_record()
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=True,
        media=True,
    )

    with patch.object(call_events, "_post_once", AsyncMock(return_value=409)):
        assert await call_events.dispatch_due_call_event() is True

    row = await load_outbox(outbox_session_factory, record.id)
    assert row is not None
    assert row.state == "blocked"
    assert row.payload_body is not None
    assert row.payload_sha256 is not None
    assert row.last_error == "HTTP 409: immutable call-event payload conflict"
    assert await call_events._claim_due_event(now=datetime.now(UTC) + timedelta(days=1)) is None


# ---------------------------------------------------------------------------
# Operator alert on a permanent block (finding #2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_permanent_block_stages_exactly_one_plain_english_operator_alert(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC) - timedelta(seconds=1)
    record = make_record()
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=True,
        media=True,
    )

    with patch.object(call_events, "_post_once", AsyncMock(return_value=409)):
        assert await call_events.dispatch_due_call_event() is True

    alert = await load_operator_alert(
        outbox_session_factory, f"call-handoff-blocked:{record.id}"
    )
    assert alert is not None
    assert alert.state == "pending"
    assert alert.message == (
        "A finished call with Ada could not be handed to the reply system. Check it by hand."
    )
    # Operator lane: no ids, hashes, or HTTP codes - those stay in the logs lane.
    assert str(record.id) not in alert.message
    assert "409" not in alert.message


@pytest.mark.asyncio
async def test_permanent_block_is_not_committed_unless_its_alert_commits(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC) - timedelta(seconds=1)
    record = make_record()
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=True,
        media=True,
    )

    with (
        patch.object(call_events, "_post_once", AsyncMock(return_value=409)),
        patch.object(
            call_events,
            "stage_operator_alert",
            AsyncMock(side_effect=RuntimeError("slack outage")),
        ),
        pytest.raises(RuntimeError, match="slack outage"),
    ):
        await call_events.dispatch_due_call_event()

    row = await load_outbox(outbox_session_factory, record.id)
    assert row is not None
    assert row.state == "sending"  # the block itself never committed either
    assert (
        await load_operator_alert(outbox_session_factory, f"call-handoff-blocked:{record.id}")
        is None
    )


@pytest.mark.asyncio
async def test_repeat_block_of_the_same_call_stages_no_second_alert(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    record = make_record()
    dedup_key = f"call-handoff-blocked:{record.id}"
    await persist_record(outbox_session_factory, record)

    async with outbox_session_factory() as db:
        await call_events._stage_call_handoff_blocked_alert(db, record.id)
        await db.commit()
    async with outbox_session_factory() as db:
        await call_events._stage_call_handoff_blocked_alert(db, record.id)
        await db.commit()

    async with outbox_session_factory() as db:
        result = await db.execute(select(OperatorAlert).where(OperatorAlert.dedup_key == dedup_key))
        rows = result.scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_non_conflict_http_rejection_remains_durably_retryable(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC) - timedelta(seconds=1)
    record = make_record()
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=True,
        media=True,
    )

    with patch.object(call_events, "_post_once", AsyncMock(return_value=400)):
        assert await call_events.dispatch_due_call_event() is True

    row = await load_outbox(outbox_session_factory, record.id)
    assert row is not None
    assert row.state == "pending"
    assert row.attempts == 1
    assert row.last_error == "HTTP 400"
    assert row.payload_body is not None


@pytest.mark.asyncio
async def test_missing_url_leaves_pending_visible_work_without_claiming(
    monkeypatch: pytest.MonkeyPatch,
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC) - timedelta(seconds=1)
    record = make_record()
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=True,
        media=True,
    )
    monkeypatch.setattr(settings, "CALL_EVENTS_URL", None)
    post = AsyncMock()

    with (
        patch.object(call_events, "_post_once", post),
        patch.object(call_events.logger, "error") as error_log,
    ):
        assert await call_events.dispatch_due_call_event() is False
        assert await call_events.dispatch_due_call_event() is False

    row = await load_outbox(outbox_session_factory, record.id)
    assert row is not None
    assert row.state == "pending"
    assert row.attempts == 0
    assert row.last_error == "CALL_EVENTS_URL not configured"
    post.assert_not_awaited()
    error_log.assert_called_once_with(
        "call_ended_event_worker_unconfigured",
        reason="CALL_EVENTS_URL not configured",
    )


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_all_mutations_require_exact_token(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    observed_at = datetime.now(UTC) - timedelta(minutes=5)
    record = make_record()
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=True,
        media=True,
    )
    stale_token = uuid.uuid4()
    now = datetime.now(UTC)
    async with outbox_session_factory() as db:
        row = await db.get(CallEventOutbox, record.id)
        assert row is not None
        row.state = "sending"
        row.claim_token = stale_token
        row.claimed_at = now - timedelta(seconds=call_events._LEASE_SECONDS + 1)
        row.attempts = 1
        await db.commit()

    claim = await call_events._claim_due_event(now=now)
    assert claim is not None
    assert claim.call_id == record.id
    assert claim.token != stale_token
    assert claim.attempts == 2

    stale_claim = call_events._Claim(record.id, stale_token, 1)
    assert await call_events._ack_claim(stale_claim) is False
    assert await call_events._retry_claim(stale_claim, "stale", delay_seconds=1) is False
    assert await call_events._block_claim(stale_claim, "stale") is False
    assert await call_events._ack_claim(claim) is True

    row = await load_outbox(outbox_session_factory, record.id)
    assert row is not None
    assert row.state == "sent"
    assert row.claim_token is None


@pytest.mark.asyncio
async def test_worker_start_is_idempotent_and_stop_cancels_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fake_worker(*, interval_seconds: float) -> None:
        assert interval_seconds == call_events._WORKER_POLL_SECONDS
        entered.set()
        await release.wait()

    monkeypatch.setattr(call_events, "_worker_loop", fake_worker)
    await call_events.start_call_event_worker()
    first_task = call_events._worker_task
    await entered.wait()
    await call_events.start_call_event_worker()

    assert first_task is not None
    assert call_events._worker_task is first_task

    await call_events.stop_call_event_worker()
    assert call_events._worker_task is None
    assert first_task.cancelled()


# ---------------------------------------------------------------------------
# Callback and media-teardown transaction wiring
# ---------------------------------------------------------------------------


def make_callback_record(**overrides: Any) -> MagicMock:
    record = MagicMock(
        id=uuid.uuid4(),
        direction="outbound",
        contact_id=None,
        status="in_progress",
        answered_at=None,
        ended_at=None,
        duration_seconds=0,
        provider="twilio",
        provider_call_id="CA-wiring-1",
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


async def run_twilio_status_callback(
    record: MagicMock | None, call_status: str
) -> tuple[AsyncMock, MagicMock, list[str]]:
    events: list[str] = []
    result = MagicMock()
    result.scalars.return_value.all.return_value = [record] if record else []

    async def commit() -> None:
        events.append("commit")

    async def stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("stage")

    db = MagicMock(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(side_effect=commit),
        rollback=AsyncMock(),
    )
    stage_mock = AsyncMock(side_effect=stage)
    with (
        patch("app.api.telephony.verify_twilio_webhook", AsyncMock()),
        patch("app.api.telephony.stage_terminal_call_event", stage_mock),
    ):
        await twilio_status_callback(
            request=MagicMock(),
            db=db,
            call_record_id="",
            call_sid="CA-wiring-1",
            call_status=call_status,
            call_duration="17",
            from_number="+15550000001",
            to_number="+15550000002",
        )
    return stage_mock, db, events


@pytest.mark.asyncio
@pytest.mark.parametrize("call_status", ["completed", "no-answer", "busy", "failed", "canceled"])
async def test_twilio_terminal_stage_precedes_same_transaction_commit(
    call_status: str,
) -> None:
    record = make_callback_record()
    stage, db, events = await run_twilio_status_callback(record, call_status)

    stage.assert_awaited_once()
    assert stage.await_args.args == (db, record)
    assert isinstance(stage.await_args.kwargs["observed_at"], datetime)
    assert events == ["stage", "commit"]


@pytest.mark.asyncio
async def test_twilio_non_terminal_status_does_not_stage() -> None:
    record = make_callback_record()
    stage, _db, events = await run_twilio_status_callback(record, "ringing")

    stage.assert_not_awaited()
    assert events == ["commit"]


@pytest.mark.asyncio
async def test_twilio_inbound_terminal_status_does_not_stage() -> None:
    record = make_callback_record(direction="inbound")
    stage, _db, events = await run_twilio_status_callback(record, "completed")

    stage.assert_not_awaited()
    assert events == ["commit"]


@pytest.mark.asyncio
async def test_twilio_stage_failure_prevents_state_commit() -> None:
    record = make_callback_record()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [record]
    db = MagicMock(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    with (
        patch("app.api.telephony.verify_twilio_webhook", AsyncMock()),
        patch(
            "app.api.telephony.stage_terminal_call_event",
            AsyncMock(side_effect=RuntimeError("outbox write failed")),
        ),
        pytest.raises(RuntimeError, match="outbox write failed"),
    ):
        await twilio_status_callback(
            request=MagicMock(),
            db=db,
            call_record_id="",
            call_sid="CA-wiring-1",
            call_status="completed",
            call_duration="17",
            from_number="+15550000001",
            to_number="+15550000002",
        )

    db.commit.assert_not_awaited()


def make_telnyx_request() -> MagicMock:
    request = MagicMock()
    request.json = AsyncMock(side_effect=ValueError("not json"))
    request.form = AsyncMock(
        return_value={
            "CallSid": "call-sid-9",
            "CallStatus": "completed",
            "CallDuration": "33",
        }
    )
    return request


@pytest.mark.asyncio
async def test_telnyx_hangup_stage_precedes_same_transaction_commit() -> None:
    events: list[str] = []
    record = make_callback_record(provider="telnyx", provider_call_id="call-sid-9")
    result = MagicMock()
    result.scalars.return_value.all.return_value = [record]

    async def commit() -> None:
        events.append("commit")

    async def stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("stage")

    db = MagicMock(execute=AsyncMock(return_value=result), commit=AsyncMock(side_effect=commit))
    stage_mock = AsyncMock(side_effect=stage)
    with (
        patch("app.api.telephony.verify_telnyx_webhook", AsyncMock()),
        patch("app.api.telephony.stage_terminal_call_event", stage_mock),
    ):
        await telnyx_status_callback(request=make_telnyx_request(), db=db)

    stage_mock.assert_awaited_once()
    assert stage_mock.await_args.args == (db, record)
    assert isinstance(stage_mock.await_args.kwargs["observed_at"], datetime)
    assert events == ["stage", "commit"]
    assert record.status == "completed"
    assert record.duration_seconds == 33


@pytest.mark.asyncio
async def test_telnyx_stage_failure_prevents_state_commit() -> None:
    record = make_callback_record(provider="telnyx", provider_call_id="call-sid-9")
    result = MagicMock()
    result.scalars.return_value.all.return_value = [record]
    db = MagicMock(execute=AsyncMock(return_value=result), commit=AsyncMock())
    with (
        patch("app.api.telephony.verify_telnyx_webhook", AsyncMock()),
        patch(
            "app.api.telephony.stage_terminal_call_event",
            AsyncMock(side_effect=RuntimeError("outbox write failed")),
        ),
        pytest.raises(RuntimeError, match="outbox write failed"),
    ):
        await telnyx_status_callback(request=make_telnyx_request(), db=db)

    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_media_finalization_stages_before_commit_even_when_artifacts_unchanged() -> None:
    events: list[str] = []
    owner_user_id = uuid.uuid4()
    record = MagicMock(
        id=uuid.uuid4(),
        transcript="already saved",
        booking_attempts=None,
        variables={},
        share_token="tr_existing",  # noqa: S106 - inert public-token-shaped fixture
        direction="outbound",
        media_finalized_at=None,
    )
    result = MagicMock()
    result.scalars.return_value.all.return_value = [record]

    async def commit() -> None:
        events.append("commit")

    async def stage(*_args: Any, **_kwargs: Any) -> None:
        events.append("stage")

    db = MagicMock(execute=AsyncMock(return_value=result), commit=AsyncMock(side_effect=commit))
    stage_mock = AsyncMock(side_effect=stage)
    with patch("app.api.telephony_ws.stage_media_finalized_call_event", stage_mock):
        saved = await save_transcript_to_call_record(
            "CA-media-final",
            "already saved",
            db,
            MagicMock(),
            owner_user_id=owner_user_id,
            workspace_id=None,
            provider="twilio",
            media_finalized=True,
        )

    assert saved is record
    stage_mock.assert_awaited_once()
    assert stage_mock.await_args.args == (db, record)
    assert isinstance(stage_mock.await_args.kwargs["observed_at"], datetime)
    assert events == ["stage", "commit"]


@pytest.mark.asyncio
async def test_persisted_payload_hash_mismatch_blocks_instead_of_retrying_forever(
    outbox_session_factory: async_sessionmaker[AsyncSession],
    capsys: pytest.CaptureFixture[str],
) -> None:
    observed_at = datetime.now(UTC) - timedelta(seconds=1)
    record = make_record()
    await stage_signals(
        outbox_session_factory,
        record,
        observed_at=observed_at,
        terminal=True,
        media=True,
    )

    with patch.object(call_events, "_post_once", AsyncMock(return_value=503)):
        assert await call_events.dispatch_due_call_event() is True

    async with outbox_session_factory() as db:
        row = await db.get(CallEventOutbox, record.id)
        assert row is not None
        assert row.payload_body is not None
        original_body = row.payload_body
        row.payload_sha256 = "0" * 64
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()

    post = AsyncMock()
    with patch.object(call_events, "_post_once", post):
        assert await call_events.dispatch_due_call_event() is True

    row = await load_outbox(outbox_session_factory, record.id)
    assert row is not None
    assert row.state == "blocked"
    assert row.payload_body == original_body
    assert row.payload_sha256 == "0" * 64
    assert row.last_error == ("_PayloadIntegrityError: persisted call-event body hash mismatch")
    post.assert_not_awaited()
    assert "call_ended_event_payload_integrity_blocked" in capsys.readouterr().out
