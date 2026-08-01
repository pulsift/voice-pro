"""Contracts for locked, monotonic Twilio lifecycle callbacks."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.telephony import (
    _apply_twilio_lifecycle_status,
    _find_twilio_lifecycle_record,
    _parse_twilio_duration,
    twilio_status_callback,
)
from app.models.call_record import CallDirection, CallStatus


def make_record(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "provider": "twilio",
        "provider_call_id": "CA-lifecycle-1",
        "direction": CallDirection.OUTBOUND.value,
        "from_number": "+15550000001",
        "to_number": "+15550000002",
        "contact_id": None,
        "status": CallStatus.INITIATED.value,
        "answered_at": None,
        "ended_at": None,
        "duration_seconds": 0,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def candidate_result(*records: SimpleNamespace) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(records)
    return result


@pytest.mark.asyncio
async def test_signed_id_reconciles_pending_row_under_lock() -> None:
    record = make_record(provider_call_id="pending:nonce")
    db = MagicMock(execute=AsyncMock(return_value=candidate_result(record)))

    found, count = await _find_twilio_lifecycle_record(
        call_record_id=str(record.id),
        call_sid="CA-lifecycle-1",
        from_number=record.from_number,
        to_number=record.to_number,
        db=db,
    )

    assert found is record
    assert count == 1
    assert record.provider_call_id == "CA-lifecycle-1"
    statement = db.execute.await_args.args[0]
    sql = str(statement)
    params = list(statement.compile().params.values())
    assert "FOR UPDATE" in sql
    assert "call_records.id" in sql
    assert "call_records.provider" in sql
    assert "call_records.direction" in sql
    assert "call_records.from_number" in sql
    assert "call_records.to_number" in sql
    assert record.id in params
    assert "twilio" in params
    assert CallDirection.OUTBOUND.value in params
    assert "pending:%" in params


@pytest.mark.asyncio
async def test_invalid_signed_id_never_falls_back_to_sid_lookup() -> None:
    db = MagicMock(execute=AsyncMock())

    found, count = await _find_twilio_lifecycle_record(
        call_record_id="not-a-uuid",
        call_sid="CA-lifecycle-1",
        from_number="+15550000001",
        to_number="+15550000002",
        db=db,
    )

    assert found is None
    assert count == 0
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_exact_sid_refuses_ambiguous_rows() -> None:
    first = make_record()
    second = make_record()
    db = MagicMock(execute=AsyncMock(return_value=candidate_result(first, second)))

    found, count = await _find_twilio_lifecycle_record(
        call_record_id="",
        call_sid="CA-lifecycle-1",
        from_number="",
        to_number="",
        db=db,
    )

    assert found is None
    assert count == 2
    statement = db.execute.await_args.args[0]
    assert "FOR UPDATE" in str(statement)
    assert "call_records.provider" in str(statement)


def test_late_nonterminal_status_cannot_regress_live_call() -> None:
    answered_at = datetime.now(UTC)
    record = make_record(status=CallStatus.IN_PROGRESS.value, answered_at=answered_at)

    entered_terminal = _apply_twilio_lifecycle_status(
        record,
        CallStatus.RINGING.value,
        event_at=datetime.now(UTC),
        provider_duration=None,
    )

    assert entered_terminal is False
    assert record.status == CallStatus.IN_PROGRESS.value
    assert record.answered_at == answered_at


@pytest.mark.asyncio
async def test_only_first_terminal_callback_runs_side_effects() -> None:
    record = make_record(status=CallStatus.IN_PROGRESS.value, answered_at=datetime.now(UTC))
    result = candidate_result(record)
    db = MagicMock(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    with (
        patch("app.api.telephony.verify_twilio_webhook", AsyncMock()),
        patch("app.api.telephony.update_campaign_contact_from_call", AsyncMock()) as update,
        patch("app.api.telephony.schedule_call_ended_event") as schedule,
    ):
        await twilio_status_callback(
            request=MagicMock(),
            db=db,
            call_record_id=str(record.id),
            call_sid="CA-lifecycle-1",
            call_status="no-answer",
            call_duration="17",
            from_number=record.from_number,
            to_number=record.to_number,
        )
        first_ended_at = record.ended_at

        await twilio_status_callback(
            request=MagicMock(),
            db=db,
            call_record_id=str(record.id),
            call_sid="CA-lifecycle-1",
            call_status="completed",
            call_duration="99",
            from_number=record.from_number,
            to_number=record.to_number,
        )

    assert record.status == CallStatus.NO_ANSWER.value
    assert record.duration_seconds == 17
    assert record.ended_at == first_ended_at
    update.assert_awaited_once()
    schedule.assert_called_once_with(record)
    assert db.commit.await_count == 2
    db.rollback.assert_not_awaited()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", None), ("invalid", None), ("-4", 0), ("12", 12)],
)
def test_twilio_duration_is_bounded(raw: str, expected: int | None) -> None:
    assert _parse_twilio_duration(raw) == expected
