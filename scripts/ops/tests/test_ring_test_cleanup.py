"""A test call must not leave a real appointment on the real calendar.

A ring test dials a real handset, through the real agent, into the real Cal.com
account. When it works, it books — and on 2026-08-08 it did exactly that:
Tuesday 11 August at midday, uid wn67EzPpffBHu2ZS4WwhNX, a genuine appointment
nobody was going to attend. Sami's standing instruction is that a test call
cleans up after itself.

The failure mode worth guarding is not "cancel breaks loudly" — it is "cleanup
quietly does nothing", which looks identical to success from the console.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS))

import ring_test  # noqa: E402


def call_record(*attempts: dict[str, object]) -> dict[str, object]:
    return {"id": "call-1", "status": "completed", "booking_attempts": list(attempts)}


CREATED = {"operation": "create", "category": "success", "uid": "bk_real_1"}


def test_a_successful_booking_is_found_for_cancellation() -> None:
    assert ring_test.booked_uids(call_record(CREATED)) == ["bk_real_1"]


def test_a_call_that_booked_nothing_offers_nothing_to_cancel() -> None:
    assert ring_test.booked_uids(call_record()) == []


def test_a_failed_booking_attempt_is_not_mistaken_for_a_real_one() -> None:
    """`create` that came back an error puts nothing on the calendar. Cancelling
    a uid that was never created would fail and read as a broken cleanup."""
    failed = {"operation": "create", "category": "error", "uid": "bk_never_made"}
    assert ring_test.booked_uids(call_record(failed)) == []


def test_the_availability_and_select_steps_are_not_mistaken_for_bookings() -> None:
    """Every call logs `availability` and most log `select`. Neither creates an
    appointment; treating them as one would fire a cancel at a nonexistent uid
    on every single test call."""
    noise = [
        {"operation": "availability", "category": "preloaded", "slot_ids": ["slot_1"]},
        {"operation": "select", "category": "selected", "slot_id": "slot_6"},
        {"operation": "reconcile", "category": "not_found", "uid": None},
    ]
    assert ring_test.booked_uids(call_record(*noise)) == []


def test_a_reconcile_that_found_an_existing_booking_is_not_ours_to_cancel() -> None:
    """`reconcile`/`found` means the appointment already existed before this
    call. Cancelling it would delete a REAL prospect's meeting."""
    found = {"operation": "reconcile", "category": "found", "uid": "bk_someone_elses"}
    assert ring_test.booked_uids(call_record(found)) == []


def test_several_bookings_in_one_call_are_all_returned() -> None:
    second = {"operation": "create", "category": "success", "uid": "bk_real_2"}
    assert ring_test.booked_uids(call_record(CREATED, second)) == ["bk_real_1", "bk_real_2"]


def test_a_malformed_attempts_list_does_not_crash_the_cleanup() -> None:
    """Cleanup runs after a live call; raising here would leave the booking in
    place AND lose the console output that says so."""
    record = {"booking_attempts": ["not a dict", None, 7, CREATED]}
    assert ring_test.booked_uids(record) == ["bk_real_1"]
    assert ring_test.booked_uids({}) == []


def test_cleanup_is_the_default_and_keeping_the_booking_must_be_asked_for() -> None:
    """If cleanup were opt-IN it would be forgotten, which is how the 11 August
    appointment survived in the first place."""
    parser_flags = Path(ring_test.__file__).read_text(encoding="utf-8")
    assert '"--keep-booking"' in parser_flags
    assert "if args.keep_booking:" in parser_flags


def test_cancel_failure_is_reported_as_a_nonzero_exit(monkeypatch) -> None:
    """A cleanup that cannot verify the cancellation must NOT exit 0. Silent
    failure here is the whole risk: the operator sees a clean run and a real
    appointment stays on the calendar."""
    monkeypatch.setattr(ring_test, "find_call", lambda _id: call_record(CREATED))
    monkeypatch.setattr(ring_test.cal_booking, "get", lambda _uid: (200, {}, "accepted"))
    monkeypatch.setattr(ring_test, "request_json", lambda *_a, **_k: (500, {}))
    assert ring_test.cleanup_bookings("CA123") == 1


def test_a_verified_cancellation_exits_clean(monkeypatch) -> None:
    states = iter(["accepted", "cancelled"])
    monkeypatch.setattr(ring_test, "find_call", lambda _id: call_record(CREATED))
    monkeypatch.setattr(
        ring_test.cal_booking, "get", lambda _uid: (200, {}, next(states))
    )
    monkeypatch.setattr(ring_test, "request_json", lambda *_a, **_k: (200, {}))
    assert ring_test.cleanup_bookings("CA123") == 0


def test_a_cancel_that_returns_200_but_does_not_stick_is_still_a_failure(
    monkeypatch,
) -> None:
    """Cal.com answering 200 is not evidence the meeting is gone. Only the
    re-read is."""
    monkeypatch.setattr(ring_test, "find_call", lambda _id: call_record(CREATED))
    monkeypatch.setattr(ring_test.cal_booking, "get", lambda _uid: (200, {}, "accepted"))
    monkeypatch.setattr(ring_test, "request_json", lambda *_a, **_k: (200, {}))
    assert ring_test.cleanup_bookings("CA123") == 1


def test_a_call_record_that_never_appears_is_reported_not_swallowed(monkeypatch) -> None:
    monkeypatch.setattr(ring_test, "find_call", lambda _id: None)
    monkeypatch.setattr(ring_test, "CALL_WAIT_CEILING_SECONDS", 0)
    assert ring_test.cleanup_bookings("CA123") == 1


@pytest.mark.parametrize("number", ["+15551234567", "+441234567890", ""])
def test_the_dialler_still_refuses_any_number_that_is_not_ours(number: str) -> None:
    """Unchanged by this work, and re-asserted because the cleanup edit touched
    main(): this script must never be able to ring a prospect."""
    assert number not in ring_test.OWN_NUMBERS
