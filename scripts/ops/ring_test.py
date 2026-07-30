# ruff: noqa: T201 - CLI ops tool: the printed lines ARE the interface.
"""Ring one of OUR OWN phones with the live agent, to judge the call by ear.

Some things about a phone call cannot be tested any other way. The unit tests prove
the wiring and the conversational eval proves the reasoning, but only an ear can tell
you whether the "Hello?" lands quickly enough to feel human, or whether the opener
survives a real interruption over a real carrier.

    python ring_test.py --confirm                 # Sami's Swedish number
    python ring_test.py --to +13466317855 --confirm   # his US number

What to listen for, in order:
  1. a bare "Hello?" almost immediately after you pick up
  2. then SILENCE — it waits for you, however long you take
  3. the opener in one unbroken run, all the way to "caught you at an okay time?",
     even if you talk over it
  4. real times offered with no pause to "check the calendar"
  5. no "what timezone are you in?" — it already knows

Two safety properties, both deliberate:
  - `--to` is restricted to OUR OWN handsets. This script can never dial a prospect,
    so a fat-fingered number fails loudly instead of cold-calling a stranger.
  - the conversation id is `ringtest-<timestamp>`, which the reply-router's factory
    guard refuses to build a real lead list for. A seeded test must never spend
    fulfilment budget (learned the expensive way on 2026-07-30).
"""

from __future__ import annotations

import argparse
import sys
import time

from ops_common import (
    AGENT_ID,
    BACKEND,
    OpsError,
    admin_token,
    masked_phone,
    request_json,
)
from seeded_call import DIRECT_FROM_NUMBER

# Our own handsets, and nothing else. Keep in step with the backend's
# RECORDING_CONSENT_NUMBERS so a test call is also a recorded one.
OWN_NUMBERS = {
    "+46700171894": "Sami (Swedish, primary)",
    "+13466317855": "Sami (US, Tello eSIM)",
    "+963998183191": "Sami (Syrian eSIM — usually unreachable)",
}
DEFAULT_TO = "+46700171894"

RING_VARIABLES = {
    "agentName": "Dave",
    "leadName": "Sami",
    "company": "Pulsift",
    "leadEmail": "sami@pulsift.com",
    "brief": "Ring test of the rebuilt opening, pre-loaded calendar and booking flow.",
    "offer_name": "the free list of a hundred solar leads",
    "offer_value_line": (
        "it's a hundred solar businesses matched to who you actually sell to"
    ),
    "bonus_line": (
        "you're also set for an expert's audit of how you're currently getting clients"
    ),
    "book_reason_audit_no": (
        "either way, to build your hundred so they're genuinely qualified for what "
        "you do, the team needs a few details about your ideal customer"
    ),
}


def place(to_number: str) -> dict[str, object]:
    token = admin_token()
    conversation_id = f"ringtest-{int(time.time())}"
    variables = dict(RING_VARIABLES)
    variables.update(
        {
            "leadPhone": to_number,
            "phone": to_number,
            "conversation_id": conversation_id,
            "conversationId": conversation_id,
        }
    )
    status, payload = request_json(
        f"{BACKEND}/api/v1/telephony/calls",
        method="POST",
        body={
            "to_number": to_number,
            "from_number": DIRECT_FROM_NUMBER,
            "agent_id": AGENT_ID,
            "variables": variables,
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    if status not in {200, 201} or not isinstance(payload, dict):
        raise OpsError(f"call request returned HTTP {status}")
    if not payload.get("call_id"):
        raise OpsError("call request returned no call_id")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--to", default=DEFAULT_TO, help="one of our own numbers")
    parser.add_argument("--confirm", action="store_true", help="required: it rings")
    args = parser.parse_args()

    if args.to not in OWN_NUMBERS:
        allowed = ", ".join(f"{n} ({who})" for n, who in OWN_NUMBERS.items())
        raise OpsError(f"--to must be one of our own handsets. Allowed: {allowed}")
    if not args.confirm:
        raise OpsError(
            f"this places a REAL call to {masked_phone(args.to)} "
            f"({OWN_NUMBERS[args.to]}) — pass --confirm"
        )

    payload = place(args.to)
    print(
        f"ringing={masked_phone(args.to)} who={OWN_NUMBERS[args.to]} "
        f"call_id={payload.get('call_id')} provider={payload.get('provider')}"
    )
    print("dashboard: https://frontend-production-3c62.up.railway.app/dashboard/calls")
    print("listen for: bare 'Hello?' -> silence -> the opener runs to 'okay time?'")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OpsError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
