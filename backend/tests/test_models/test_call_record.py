"""Tests for persisted voice-call records."""

import uuid

from app.models.call_record import CallDirection, CallRecord, CallStatus


def test_booking_attempts_are_kept_on_the_model() -> None:
    """Structured Cal.com diagnostics are accepted by the mapped model."""
    attempts: list[dict[str, object]] = [
        {
            "attempt": 1,
            "timestamp": "2026-07-10T19:00:00Z",
            "selected_start": "2026-07-13T07:00:00Z",
            "timezone": "Asia/Damascus",
            "category": "booking_rejected",
            "status_code": 400,
            "body": '{"message":"invalid timezone"}',
        }
    ]
    record = CallRecord(
        user_id=uuid.uuid4(),
        provider="telnyx",
        provider_call_id="call-control-test",
        direction=CallDirection.OUTBOUND.value,
        status=CallStatus.COMPLETED.value,
        from_number="+14085550100",
        to_number="+963998000000",
        booking_attempts=attempts,
    )

    assert record.booking_attempts == attempts


def test_booking_attempts_are_nullable() -> None:
    """Existing call rows remain valid without booking diagnostics."""
    record = CallRecord(
        user_id=uuid.uuid4(),
        provider="telnyx",
        provider_call_id="call-control-no-booking",
        direction=CallDirection.OUTBOUND.value,
        status=CallStatus.NO_ANSWER.value,
        from_number="+14085550100",
        to_number="+963998000000",
    )

    assert record.booking_attempts is None
