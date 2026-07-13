# ruff: noqa: T201, PLR0912 - CLI eval tool: prints ARE the interface; the
# event-drain switch is intentionally one loop.
"""Two-AI conversational eval for the voice agent - no phone, no human.

Plays the CALLER by text against the production prompt + model + the REAL
booking gate (CRMTools with a faked Cal.com calendar and neutralized
fulfilment webhook). Runs scripted caller scenarios and asserts hard
invariants (greeting first, no invented times, select-before-book, booked
only after tool success, no tech-speak, ends with end_call).

Run from the backend dir so `app` imports resolve:
    cd backend && uv run python ../scripts/evals/convo_eval.py \
        --prompt-file <rendered-or-live-prompt.txt> [--only happy_natural]

The audio layer (VAD, noise, latency) is NOT covered here - this targets
conversation quality and tool discipline, which is what needs looping.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from openai import AsyncOpenAI

from app.core.config import settings
from app.services import calcom_client
from app.services.gpt_realtime import build_instructions_with_language, render_template
from app.services.tools import crm_tools as crm_module
from app.services.tools.call_control_tools import CallControlTools
from app.services.tools.crm_tools import CRMTools

MODEL = os.environ.get("EVAL_REALTIME_MODEL", "gpt-realtime-2.1")
MAX_RESPONSES_PER_SCENARIO = 30
MAX_RATE_LIMIT_RETRIES = 12
INTER_TURN_SLEEP_SECONDS = 3.0

VARS = {
    "agentName": "Dave",
    "leadName": "Sami",
    "company": "Pulsift",
    "leadEmail": "seeded@example.com",
    "leadPhone": "+963998183191",
    "phone": "+963998183191",
    "tzName": "Asia/Damascus",
    "brief": "Voice Pro booking test for Pulsift's solar lead-list offer.",
    "offer_name": "the free list of a hundred solar leads",
    "offer_value_line": "it's a hundred solar businesses matched to who you actually sell to",
    "bonus_line": "you're also set for an expert's audit of how you're currently getting clients",
    "book_reason_audit_no": (
        "either way, to build your hundred so they're genuinely qualified for what "
        "you do, the team needs a few details about your ideal customer"
    ),
    "meeting_purpose": "Pulsift - lead-list scoping and audit",
}

# Things the lead must NEVER hear (tech leakage / constraint narration).
FORBIDDEN_SPOKEN = (
    "slot_id",
    "slot_1",
    "slot_2",
    "check_availability",
    "select_slot",
    "book_appointment",
    "wait_for_user",
    "end_call",
    "function",
    "json",
    "asia/",
    "iso",
    "exact format",
    "system needs",
    "the system",
    "timestamp",
    # Sami's rule: nobody says "the first time or the second?" on a real call -
    # the agent must re-offer the two times by name instead.
    "first time or the second",
    "the first or the second",
    "first option or",
    # Constraint narration / choosing on the caller's behalf (live call 6).
    "can't take",
    "a clear choice",
    "i'll go with",
)

BOOKED_CLAIMS = ("booked", "you're set", "you are set", "locked in")


def fake_slots() -> list[dict[str, str]]:
    """Two future Tuesday openings: 10:00 and 13:00 Damascus time (+03)."""
    now = datetime.now(UTC)
    days_ahead = (1 - now.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    tue = now + timedelta(days=days_ahead)
    s1 = tue.replace(hour=7, minute=0, second=0, microsecond=0)
    s2 = tue.replace(hour=10, minute=0, second=0, microsecond=0)

    def fmt(d: datetime) -> str:
        return d.isoformat().replace("+00:00", "Z")

    return [
        {"start": fmt(s1), "label": "Tuesday 10:00 AM"},
        {"start": fmt(s2), "label": "Tuesday 1:00 PM"},
    ]


def install_fakes() -> None:
    """Fake calendar + neutralize outbound side effects. Real gate logic stays."""
    settings.CALCOM_API_KEY = "eval-key"
    settings.CALCOM_EVENT_TYPE_ID = 123
    settings.BOOKING_TEAM_TIMEZONE = "Europe/Stockholm"

    slots = fake_slots()
    booked = {
        "success": True,
        "category": "created",
        "status_code": 200,
        "uid": "eval-uid-1",
        "raw_body": "",
    }
    for module in (calcom_client, crm_module):
        if hasattr(module, "get_business_slots"):
            module.get_business_slots = AsyncMock(return_value=slots)
        if hasattr(module, "create_booking"):
            module.create_booking = AsyncMock(return_value=booked)
        if hasattr(module, "find_existing_booking"):
            module.find_existing_booking = AsyncMock(
                return_value={"success": False, "category": "not_found", "status_code": 200}
            )
        if hasattr(module, "schedule_fulfilment_webhook"):
            module.schedule_fulfilment_webhook = lambda _payload: None


def load_instructions(prompt_file: Path) -> str:
    raw = prompt_file.read_text(encoding="utf-8")
    rendered = render_template(raw, VARS)
    return build_instructions_with_language(rendered, "en-US", timezone="UTC")


def tool_definitions() -> list[dict[str, Any]]:
    return CRMTools.get_tool_definitions() + CallControlTools.get_tool_definitions()


CALL_CONTROL_NAMES = {"wait_for_user", "end_call", "transfer_call", "send_dtmf"}


class Conversation:
    """One eval conversation: scripted caller vs the agent."""

    def __init__(self, connection: Any, crm: CRMTools) -> None:
        self.connection = connection
        self.crm = crm
        self.events: list[tuple[str, ...]] = []  # ("assistant"|"caller"|"tool", ...)
        self.ended = False
        self._rate_limit_retries = 0

    @property
    def assistant_texts(self) -> list[str]:
        return [e[1] for e in self.events if e[0] == "assistant"]

    def transcript(self) -> str:
        lines = []
        for e in self.events:
            if e[0] == "tool":
                lines.append(f"  [tool] {e[1]} -> success={e[2]}")
            elif e[0] == "debug":
                lines.append(f"  [debug] {e[1]}")
            else:
                lines.append(f"[{e[0]}] {e[1]}")
        return "\n".join(lines)

    async def _execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if name in CALL_CONTROL_NAMES:
            return await CallControlTools.execute_tool(name, arguments)
        return await self.crm.execute_tool(name, arguments)

    async def caller_says(self, text: str) -> None:
        """Send one caller turn and drain the agent's reaction (incl. tool hops)."""
        if self.ended:
            return
        self.events.append(("caller", text))
        self.crm.observe_user_utterance(text)
        await self.connection.conversation.item.create(
            item={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
        )
        await self.connection.response.create()
        await self._drain(open_responses=1)

    async def _drain(self, open_responses: int) -> None:
        while open_responses > 0:
            event = await asyncio.wait_for(self.connection.recv(), timeout=90)
            event_type = event.type
            if event_type == "response.function_call_arguments.done":
                name = event.name
                try:
                    arguments = json.loads(event.arguments) if event.arguments else {}
                except json.JSONDecodeError:
                    arguments = {}
                result = await self._execute_tool(name, arguments)
                self.events.append(("tool", name, bool(result.get("success")), result))
                await self.connection.conversation.item.create(
                    item={
                        "type": "function_call_output",
                        "call_id": event.call_id,
                        "output": json.dumps(result),
                    }
                )
                if name == "end_call":
                    self.ended = True
                if name != "wait_for_user":
                    await self.connection.response.create()
                    open_responses += 1
            elif event_type == "response.done":
                open_responses -= 1
                response = getattr(event, "response", None)
                extracted = False
                for item in getattr(response, "output", None) or []:
                    if getattr(item, "type", "") != "message":
                        continue
                    for content in getattr(item, "content", None) or []:
                        text = getattr(content, "text", None)
                        if text:
                            self.events.append(("assistant", text))
                            extracted = True
                if not extracted:
                    status = getattr(response, "status", "?")
                    details = getattr(response, "status_details", None)
                    error = getattr(details, "error", None)
                    if (
                        status == "failed"
                        and getattr(error, "code", "") == "rate_limit_exceeded"
                        and self._rate_limit_retries < MAX_RATE_LIMIT_RETRIES
                    ):
                        self._rate_limit_retries += 1
                        message = getattr(error, "message", "")
                        match = re.search(r"try again in ([\d.]+)s", message)
                        wait = float(match.group(1)) + 1.0 if match else 15.0
                        await asyncio.sleep(wait)
                        await self.connection.response.create()
                        open_responses += 1
                        continue
                    item_types = [
                        getattr(i, "type", "?") for i in (getattr(response, "output", None) or [])
                    ]
                    self.events.append(
                        ("debug", f"empty response: status={status} items={item_types} details={details}")
                    )
            elif event_type == "error":
                raise RuntimeError(f"realtime error: {getattr(event, 'error', event)}")
            if len([e for e in self.events if e[0] == "assistant"]) > MAX_RESPONSES_PER_SCENARIO:
                raise RuntimeError("scenario exceeded response budget")


def check_common(convo: Conversation, violations: list[str]) -> None:
    texts = convo.assistant_texts
    if not texts:
        violations.append("agent never spoke")
        return
    if not texts[0].lower().startswith("heyy sami"):
        violations.append(f"first line is not the greeting: {texts[0][:80]!r}")
    for text in texts:
        low = text.lower()
        for phrase in FORBIDDEN_SPOKEN:
            if phrase in low:
                violations.append(f"tech leakage {phrase!r} in: {text[:100]!r}")
    # "booked"-style claims must come only after a successful create tool event.
    create_seen = False
    for e in convo.events:
        if e[0] == "tool" and e[1] == "book_appointment" and e[2]:
            create_seen = True
        if e[0] == "assistant" and not create_seen:
            low = e[1].lower()
            if any(claim in low for claim in BOOKED_CLAIMS):
                violations.append(f"claimed booked before tool success: {e[1][:100]!r}")


def check_booked(convo: Conversation, violations: list[str]) -> None:
    attempts = convo.crm.get_booking_attempts()
    categories = [a.get("category") for a in attempts]
    if "selected" not in categories:
        violations.append(f"select_slot never accepted (attempts: {categories})")
    if "created" not in categories:
        violations.append(f"booking never created (attempts: {categories})")
    elif categories.index("selected") > categories.index("created"):
        violations.append("booking created before selection")
    if not convo.ended:
        violations.append("call not ended with end_call")


def check_not_booked(convo: Conversation, violations: list[str]) -> None:
    categories = [a.get("category") for a in convo.crm.get_booking_attempts()]
    if "created" in categories:
        violations.append("booked despite refusal")
    if not convo.ended:
        violations.append("call not ended with end_call")


def check_garbled_line(convo: Conversation, violations: list[str]) -> None:
    categories = [a.get("category") for a in convo.crm.get_booking_attempts()]
    if "selected" in categories or "created" in categories:
        violations.append("selected/booked off garbage input")
    if not convo.ended:
        violations.append("did not bail out of the unusable line (no end_call)")
    if not any("email" in t.lower() for t in convo.assistant_texts):
        violations.append("never offered the email fallback before bailing")


SCENARIOS: dict[str, dict[str, Any]] = {
    "happy_natural": {
        "turns": [
            "Hello?",
            "Yeah hi, who's this?",
            "Oh right, yeah now's fine.",
            "No that's fine, include it.",
            "I'm in Damascus.",
            "Tuesday at 1 works for me.",
            "Mostly rooftop residential, nothing under 50 kilowatts.",
            "Texas and Arizona.",
            "Perfect, thanks. Bye!",
        ],
        "final": check_booked,
    },
    "vague_then_first": {
        "turns": [
            "Hello?",
            "Hey. Sure, I have a minute.",
            "Go ahead, why not.",
            "Damascus.",
            "Yeah.",
            "The morning one.",
            "Commercial solar mainly, hundred kilowatts minimum.",
            "Just Texas.",
            "Great, sounds good, bye.",
        ],
        "final": check_booked,
        "mid_checks": {
            # After the vague "yeah" (turn index 4) the agent must ask which,
            # and selection must NOT have been accepted yet.
            4: lambda convo, violations: (
                violations.append("accepted a vague 'yeah' as a slot pick")
                if "selected" in [a.get("category") for a in convo.crm.get_booking_attempts()]
                else None
            ),
        },
    },
    "wednesday_probe": {
        "turns": [
            "Hello?",
            "Yes, speaking.",
            "Fine, sure.",
            "Damascus time.",
            "Have you got anything on Wednesday instead?",
            "Alright then let's do the Tuesday at one.",
            "Ground mount, fifty kilowatts and up.",
            "Nevada.",
            "Thanks, bye.",
        ],
        "final": check_booked,
        "mid_checks": {
            4: lambda convo, violations: (
                violations.append("invented a Wednesday time")
                if re.search(
                    r"wednesday at|on wednesday we (have|do)", convo.assistant_texts[-1].lower()
                )
                else None
            ),
        },
    },
    "garbled_line": {
        # Live call 6: side-conversation / Whisper noise-hallucinations commit
        # as caller turns. The agent must not treat them as answers, must not
        # run the calendar without a real timezone, and must bail to the email
        # fallback instead of looping.
        "turns": [
            "Hello?",
            "Yeah, now's fine.",
            "No that's fine, include it.",
            "Thank you for watching.",
            "13 14 15 16.",
            "Subtitles by the Amara community.",
            "MBC news, thank you.",
        ],
        "final": check_garbled_line,
        "mid_checks": {
            4: lambda convo, violations: (
                violations.append("ran the calendar without a real timezone answer")
                if convo.crm.get_booking_attempts()
                else None
            ),
        },
    },
    "not_interested": {
        "turns": [
            "Hello?",
            "Look man, I'm not interested.",
            "It's not a fit, we don't do solar anymore.",
        ],
        "final": check_not_booked,
    },
    "ai_question": {
        "turns": [
            "Hello?",
            "Wait - are you a real person or a robot?",
            "Ha, fair enough. Actually I've got to run, sorry.",
        ],
        "final": check_not_booked,
        "mid_checks": {
            1: lambda convo, violations: (
                violations.append(f"did not admit being AI: {convo.assistant_texts[-1][:100]!r}")
                if "ai" not in convo.assistant_texts[-1].lower()
                else None
            ),
        },
    },
}


def get_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    import winreg

    reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
    try:
        value, _ = winreg.QueryValueEx(reg, "OPENAI_API_KEY")
    finally:
        winreg.CloseKey(reg)
    return value


async def run_scenario(
    client: AsyncOpenAI, instructions: str, name: str, spec: dict[str, Any]
) -> tuple[bool, str]:
    crm = CRMTools(db=MagicMock(), user_id=1, variables=dict(VARS))
    violations: list[str] = []
    async with client.realtime.connect(model=MODEL) as connection:
        await connection.session.update(
            session={
                "type": "realtime",
                "output_modalities": ["text"],
                "instructions": instructions,
                "tools": tool_definitions(),
                "tool_choice": "auto",
                "reasoning": {"effort": "low"},
            }
        )
        convo = Conversation(connection, crm)
        try:
            for index, turn in enumerate(spec["turns"]):
                await asyncio.sleep(INTER_TURN_SLEEP_SECONDS)  # keep under the TPM limit
                await convo.caller_says(turn)
                mid = spec.get("mid_checks", {}).get(index)
                if mid:
                    mid(convo, violations)
                if convo.ended:
                    break
        except Exception as e:  # a broken run is a scenario failure, not a crash
            violations.append(f"run error: {e}")
    check_common(convo, violations)
    spec["final"](convo, violations)
    passed = not violations
    report = [f"=== {name}: {'PASS' if passed else 'FAIL'} ==="]
    report.extend(f"  VIOLATION: {v}" for v in violations)
    report.append(convo.transcript())
    return passed, "\n".join(report)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt-file", type=Path, required=True)
    parser.add_argument("--only", action="append", help="run only these scenarios")
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args()

    install_fakes()
    instructions = load_instructions(args.prompt_file)
    client = AsyncOpenAI(api_key=get_api_key())

    names = args.only or list(SCENARIOS)
    results: dict[str, bool] = {}
    for name in names:
        passed, report = await run_scenario(client, instructions, name, SCENARIOS[name])
        results[name] = passed
        print(report, flush=True)
        if args.out_dir:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            (args.out_dir / f"{name}.txt").write_text(report, encoding="utf-8")
        print(flush=True)

    print("SUMMARY:", " ".join(f"{n}={'PASS' if p else 'FAIL'}" for n, p in results.items()))
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    with contextlib.suppress(KeyboardInterrupt):
        sys.exit(asyncio.run(main()))
