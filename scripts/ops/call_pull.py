"""Pull one Voice Pro CallRecord with sanitized operational evidence."""

from __future__ import annotations

import argparse
import json
import sys

from ops_common import OpsError, admin_request, masked_phone, sanitize_booking_attempts


def select_call(args: argparse.Namespace) -> dict[str, object]:
    if args.call_id:
        payload = admin_request(f"/api/v1/calls/{args.call_id}")
        if not isinstance(payload, dict):
            raise OpsError("call GET returned an unexpected response")
        return payload
    payload = admin_request("/api/v1/calls?direction=outbound&page=1&page_size=100")
    if not isinstance(payload, dict):
        raise OpsError("call list returned an unexpected response")
    calls = [item for item in payload.get("calls", []) if isinstance(item, dict)]
    if args.provider_call_id:
        calls = [
            item
            for item in calls
            if item.get("provider_call_id") == args.provider_call_id
        ]
    if not calls:
        raise OpsError("no matching outbound CallRecord was found")
    return calls[0]


def tool_sequence(attempts: list[dict[str, object]]) -> list[str]:
    sequence = []
    for item in attempts:
        value = item.get("tool") or item.get("action") or item.get("category")
        if value:
            sequence.append(str(value))
    return sequence


def canonical_sequence(attempts: list[dict[str, object]]) -> list[str]:
    """Normalize persisted tool/event names when richer backend events are available."""
    aliases = {
        "availability": "availability",
        "check_availability": "availability",
        "offered_slots": "availability",
        "offer": "availability",
        "select": "select",
        "selection": "select",
        "select_slot": "select",
        "book": "book",
        "create": "book",
        "book_appointment": "book",
    }
    result = []
    for attempt in attempts:
        raw = attempt.get("tool") or attempt.get("event") or attempt.get("operation")
        canonical = aliases.get(str(raw or "").lower())
        if canonical:
            result.append(canonical)
    return result


def assert_booking_sequence(sequence: list[str]) -> None:
    """Require availability -> explicit select -> book, with no premature book."""
    if not all(item in sequence for item in ("availability", "select", "book")):
        raise OpsError("booking sequence evidence is incomplete")
    availability_index = sequence.index("availability")
    select_index = sequence.index("select")
    book_index = sequence.index("book")
    if not availability_index < select_index < book_index:
        raise OpsError("booking sequence was not availability -> select -> book")
    if "book" in sequence[: select_index + 1]:
        raise OpsError("booking occurred before explicit selection")


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--call-id")
    group.add_argument("--provider-call-id")
    parser.add_argument("--include-transcript", action="store_true")
    parser.add_argument("--assert-booking-sequence", action="store_true")
    args = parser.parse_args()
    call = select_call(args)
    attempts = sanitize_booking_attempts(call.get("booking_attempts"))
    print(
        f"call_id={call.get('id')} provider={call.get('provider')} "
        f"provider_call_id={call.get('provider_call_id')} status={call.get('status')} "
        f"destination={masked_phone(str(call.get('to_number') or ''))} duration_seconds={call.get('duration_seconds')}"
    )
    print(
        f"started_at={call.get('started_at')} answered_at={call.get('answered_at')} "
        f"ended_at={call.get('ended_at')}"
    )
    print(
        "booking_attempts=" + json.dumps(attempts, ensure_ascii=False, sort_keys=True)
    )
    print("booking_operation_sequence=" + json.dumps(tool_sequence(attempts)))
    sequence = canonical_sequence(attempts)
    print("canonical_booking_sequence=" + json.dumps(sequence))
    if args.assert_booking_sequence:
        assert_booking_sequence(sequence)
        print("booking_sequence_assertion=pass")
    else:
        evidence = (
            "available"
            if all(x in sequence for x in ("availability", "select", "book"))
            else "incomplete"
        )
        print(f"booking_sequence_evidence={evidence}")
    if args.include_transcript:
        print("--- transcript ---")
        print(str(call.get("transcript") or "<empty>"))
    else:
        transcript = str(call.get("transcript") or "")
        print(
            f"transcript_present={str(bool(transcript)).lower()} transcript_chars={len(transcript)}"
        )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpsError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
