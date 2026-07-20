"""Contracts for the signed call-ended event sender (B4)."""

# ruff: noqa: SLF001 - these tests intentionally verify module-private dispatch state.

import asyncio
import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.api.telephony import telnyx_status_callback, twilio_status_callback
from app.core.config import settings
from app.models.call_record import CallRecord
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


def make_http_context(*statuses: int) -> tuple[MagicMock, MagicMock]:
    responses = [MagicMock(status_code=status) for status in statuses]
    client = MagicMock(post=AsyncMock(side_effect=responses))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    return context, client


async def drain_tasks() -> None:
    tasks = tuple(call_events._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


@pytest.fixture(autouse=True)
def reset_dispatch_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CALL_EVENTS_URL", "https://router.test")
    monkeypatch.setattr(settings, "CALL_EVENTS_SECRET", "events-secret")
    monkeypatch.setattr(call_events, "_warned_unsigned", False)
    call_events._sent_call_ids.clear()
    call_events._background_tasks.clear()


# ---------------------------------------------------------------------------
# Payload construction + booked extraction
# ---------------------------------------------------------------------------


def test_payload_reports_booking_from_successful_create_attempt() -> None:
    record = make_record()
    payload = call_events.build_call_ended_payload(record)

    assert payload == {
        "call_id": str(record.id),
        "provider_call_id": "CA-test-1",
        "to_number": "+15550000002",
        "status": "completed",
        "answered": True,
        "duration_seconds": 42,
        "booked": True,
        "booking_uid": "calcom-uid-1",
        "variables": {"leadName": "Ada", "tzName": "America/Los_Angeles"},
    }


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
# Single-shot guard + scheduling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exactly_one_event_per_call_across_repeated_terminal_signals() -> None:
    record = make_record()
    post = AsyncMock(return_value=(True, False))

    with patch.object(call_events, "_post_once", post):
        call_events.schedule_call_ended_event(record)
        call_events.schedule_call_ended_event(record)
        await drain_tasks()
        # A late signal after delivery must also be a no-op.
        call_events.schedule_call_ended_event(record)
        await drain_tasks()

    post.assert_awaited_once()
    url = post.await_args.args[0]
    assert url == "https://router.test/webhooks/call-ended"


@pytest.mark.asyncio
async def test_delayed_ws_fallback_yields_to_the_status_callback_send() -> None:
    record = make_record()
    post = AsyncMock(return_value=(True, False))

    with (
        patch.object(call_events, "_post_once", post),
        patch("app.services.call_events.asyncio.sleep", AsyncMock()),
    ):
        # Status callback (primary) and media-WS teardown (delayed fallback)
        # both fire for the same call.
        call_events.schedule_call_ended_event(record)
        call_events.schedule_call_ended_event(record, delay_seconds=20.0)
        await drain_tasks()

    post.assert_awaited_once()


@pytest.mark.asyncio
async def test_unset_url_disables_the_sender() -> None:
    monkey_record = make_record()
    with patch.object(settings, "CALL_EVENTS_URL", None):
        call_events.schedule_call_ended_event(monkey_record)

    assert not call_events._background_tasks
    assert not call_events._sent_call_ids


# ---------------------------------------------------------------------------
# Wiring: telephony status callbacks emit on terminal status only
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


async def run_twilio_status_callback(record: MagicMock | None, call_status: str) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = record
    db = MagicMock(execute=AsyncMock(return_value=result), commit=AsyncMock())
    with (
        patch("app.api.telephony.verify_twilio_webhook", AsyncMock()),
        patch("app.api.telephony.schedule_call_ended_event") as schedule,
    ):
        await twilio_status_callback(
            request=MagicMock(),
            db=db,
            call_sid="CA-wiring-1",
            call_status=call_status,
            call_duration="17",
            from_number="+15550000001",
            to_number="+15550000002",
        )
    return schedule


@pytest.mark.asyncio
@pytest.mark.parametrize("call_status", ["completed", "no-answer", "busy", "failed", "canceled"])
async def test_twilio_terminal_status_emits_call_ended(call_status: str) -> None:
    record = make_callback_record()
    schedule = await run_twilio_status_callback(record, call_status)
    schedule.assert_called_once_with(record)


@pytest.mark.asyncio
async def test_twilio_non_terminal_status_does_not_emit() -> None:
    record = make_callback_record()
    schedule = await run_twilio_status_callback(record, "ringing")
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_twilio_inbound_terminal_status_does_not_emit() -> None:
    record = make_callback_record(direction="inbound")
    schedule = await run_twilio_status_callback(record, "completed")
    schedule.assert_not_called()


@pytest.mark.asyncio
async def test_telnyx_hangup_callback_emits_call_ended() -> None:
    record = make_callback_record(provider="telnyx", provider_call_id="call-sid-9")
    result = MagicMock()
    result.scalars.return_value.all.return_value = [record]
    db = MagicMock(execute=AsyncMock(return_value=result), commit=AsyncMock())
    request = MagicMock()
    request.json = AsyncMock(side_effect=ValueError("not json"))
    request.form = AsyncMock(
        return_value={
            "CallSid": "call-sid-9",
            "CallStatus": "completed",
            "CallDuration": "33",
        }
    )

    with (
        patch("app.api.telephony.verify_telnyx_webhook", AsyncMock()),
        patch("app.api.telephony.schedule_call_ended_event") as schedule,
    ):
        await telnyx_status_callback(request=request, db=db)

    schedule.assert_called_once_with(record)
    assert record.status == "completed"
    assert record.duration_seconds == 33


# ---------------------------------------------------------------------------
# Delivery retry semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "expected_attempts"),
    [
        ((204,), 1),  # success first try
        ((500, 200), 2),  # one retry on 5xx, then success
        ((400,), 1),  # non-5xx rejection is terminal, no retry
        ((500, 500), 2),  # retry budget is exactly one extra attempt
    ],
)
async def test_http_retry_semantics(statuses: tuple[int, ...], expected_attempts: int) -> None:
    context, client = make_http_context(*statuses)

    with (
        patch("app.services.call_events.httpx.AsyncClient", return_value=context),
        patch("app.services.call_events.asyncio.sleep", AsyncMock()),
    ):
        await call_events._deliver(
            "https://router.test/webhooks/call-ended",
            {"call_id": "retry-test"},
            "retry-test",
            delay_seconds=0.0,
        )

    assert client.post.await_count == expected_attempts


@pytest.mark.asyncio
async def test_timeout_retries_once_and_never_raises() -> None:
    client = MagicMock(post=AsyncMock(side_effect=httpx.TimeoutException("boom")))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)

    with (
        patch("app.services.call_events.httpx.AsyncClient", return_value=context),
        patch("app.services.call_events.asyncio.sleep", AsyncMock()),
    ):
        await call_events._deliver(
            "https://router.test/webhooks/call-ended",
            {"call_id": "timeout-test"},
            "timeout-test",
            delay_seconds=0.0,
        )

    assert client.post.await_count == 2
