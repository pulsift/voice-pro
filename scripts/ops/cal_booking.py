"""Read or explicitly cancel one Cal.com booking without printing the API key or PII."""

from __future__ import annotations

import argparse
import sys

from ops_common import CALCOM, OpsError, request_json, user_env


def headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {user_env('CALCOM_API_KEY')}",
        "cal-api-version": "2024-08-13",
        "Content-Type": "application/json",
    }


def booking_status(payload: object) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    return str(data.get("status") or data.get("bookingStatus") or "unknown").lower()


def get(uid: str) -> tuple[int, object, str]:
    status, payload = request_json(f"{CALCOM}/bookings/{uid}", headers=headers())
    return status, payload, booking_status(payload)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    get_parser = sub.add_parser("get")
    get_parser.add_argument("--uid", required=True)
    cancel = sub.add_parser("cancel")
    cancel.add_argument("--uid", required=True)
    cancel.add_argument("--reason", default="Voice Pro controlled test cleanup")
    cancel.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    status, _, state = get(args.uid)
    if status == 404:
        print(f"uid={args.uid} retrievable=false")
        return 1 if args.command == "get" else 2
    if status != 200:
        raise OpsError(f"Cal.com booking GET returned HTTP {status}")
    print(f"uid={args.uid} retrievable=true status={state}")
    if args.command == "get":
        return 0
    if state in {"cancelled", "canceled"}:
        print("already_cancelled=true")
        return 0
    if not args.confirm:
        raise OpsError("booking cancellation requires --confirm")
    cancel_status, _ = request_json(
        f"{CALCOM}/bookings/{args.uid}/cancel",
        method="POST",
        headers=headers(),
        body={"cancellationReason": args.reason},
    )
    if cancel_status != 200:
        raise OpsError(f"Cal.com booking cancel returned HTTP {cancel_status}")
    verify_status, _, verify_state = get(args.uid)
    if verify_status != 200 or verify_state not in {"cancelled", "canceled"}:
        raise OpsError(
            f"booking cancellation could not be verified (HTTP {verify_status}, status={verify_state})"
        )
    print(f"cancelled=true verified=true status={verify_state}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpsError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
