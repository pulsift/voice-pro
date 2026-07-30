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
    "refresh_availability",
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

# "The agent has just offered times." Deliberately broad: every scenario keys off
# this, so it must survive rewording of the offer itself.
OFFER_PATTERN = (
    r"which (suits|works|one|should)|work better|would you like|half past|quarter"
    r"|in the (morning|afternoon|evening)|o'clock|midday|prefer"
)

BOOKED_CLAIMS = ("booked", "you're set", "you are set", "locked in")

# The opener must reach its closing time-check inside the SAME turn.
OPENER_END_MARKERS = ("okay time", "ok time", "good time", "bad time", "caught you")

# Questions the agent can always answer from its own pre-loaded calendar. Asking
# them back is the exact behaviour Sami heard and called out.
BOUNCED_QUESTIONS = (
    "what day works",
    "which day works",
    "what day would work",
    "what timezone are you",
    "which timezone",
    "what time zone are you",
)


def fake_slots() -> list[dict[str, str]]:
    """Openings on the next Tuesday and Friday, in the lead's own timezone.

    Deliberately NOT every weekday: the agent must be able to say "nothing
    Wednesday, but I have Thursday" from data rather than lob the question back.
    Times are 10:00 and 13:00 Damascus (+03) on Tuesday, 16:30 and 18:00 on Friday.
    """
    now = datetime.now(UTC)

    def next_weekday(target: int) -> datetime:
        ahead = (target - now.weekday()) % 7 or 7
        return now + timedelta(days=ahead)

    def fmt(moment: datetime) -> str:
        return moment.isoformat().replace("+00:00", "Z")

    tue = next_weekday(1)
    fri = next_weekday(4)
    return [
        {"start": fmt(tue.replace(hour=7, minute=0, second=0, microsecond=0))},
        {"start": fmt(tue.replace(hour=10, minute=0, second=0, microsecond=0))},
        {"start": fmt(fri.replace(hour=13, minute=30, second=0, microsecond=0))},
        {"start": fmt(fri.replace(hour=15, minute=0, second=0, microsecond=0))},
    ]


def eval_menu() -> dict[str, Any]:
    """The pre-loaded menu the production bridge builds before the call starts."""
    from app.services.availability import build_menu

    return build_menu(fake_slots(), VARS["tzName"])


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
        if hasattr(module, "get_open_slots"):
            module.get_open_slots = AsyncMock(return_value=slots)
        if hasattr(module, "create_booking"):
            module.create_booking = AsyncMock(return_value=booked)
        if hasattr(module, "find_existing_booking"):
            module.find_existing_booking = AsyncMock(
                return_value={"success": False, "category": "not_found", "status_code": 200}
            )
        if hasattr(module, "schedule_fulfilment_webhook"):
            module.schedule_fulfilment_webhook = lambda _payload: None


def load_instructions(prompt_file: Path, menu: dict[str, Any]) -> str:
    """Render the prompt exactly as the live session does, menu included."""
    from app.services.lead_timezone import spoken_zone_name

    variables = dict(VARS)
    variables["availability_block"] = menu["block"]
    variables["tz_spoken"] = spoken_zone_name(str(VARS["tzName"]))
    raw = prompt_file.read_text(encoding="utf-8")
    rendered = render_template(raw, variables)
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


def normalize_spoken(text: str) -> str:
    """Lowercase and flatten smart punctuation.

    The model writes typographic apostrophes, so every substring check against a
    hand-written ASCII phrase silently missed. Found on 2026-07-30, when a perfectly
    correct "I don't have anything on Wednesday" was reported as a violation because
    its apostrophe was U+2019.
    """
    lowered = text.lower()
    for fancy, plain in (
        ("’", "'"), ("‘", "'"), ("“", '"'),  # noqa: RUF001
        ("”", '"'), ("—", "-"), ("–", "-"),  # noqa: RUF001
    ):
        lowered = lowered.replace(fancy, plain)
    return lowered


INTENT_MARKERS = ("i'll ", "i will ", "let me ", "i'm going to ", "i am going to ",
                  "going to ", "shall i", "want me to")


def strip_intent(text: str) -> str:
    """Drop sentences that state an INTENT rather than a completed fact.

    "I'll get that locked in" is a promise; "you're locked in" is a claim. Only the
    second one can lie about a booking that never happened, so only the second is a
    violation. Without this the eval failed honest turns.
    """
    kept = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text):
        if not any(marker in sentence for marker in INTENT_MARKERS):
            kept.append(sentence)
    return " ".join(kept)


def check_common(convo: Conversation, violations: list[str]) -> None:
    texts = [normalize_spoken(t) for t in convo.assistant_texts]
    if not texts:
        violations.append("agent never spoke")
        return
    if not texts[0].startswith("heyy sami"):
        violations.append(f"first line is not the greeting: {texts[0][:80]!r}")
    # Sami's ask: the opener keeps going past the name, all the way to the
    # time-check. Splitting it across turns is the failure this pins.
    if not any(marker in texts[0] for marker in OPENER_END_MARKERS):
        violations.append(f"opener stopped before the time check: {texts[0][:160]!r}")
    # The calendar is already in hand, so bouncing "what day works for you?" back
    # at the caller is never the right move.
    for text in texts:
        if any(phrase in text for phrase in BOUNCED_QUESTIONS):
            violations.append(f"asked for a day it could have offered: {text[:120]!r}")
    for text in texts:
        low = text
        for phrase in FORBIDDEN_SPOKEN:
            if phrase in low:
                violations.append(f"tech leakage {phrase!r} in: {text[:100]!r}")
    # "booked"-style claims must come only after a successful create tool event.
    create_seen = False
    for e in convo.events:
        if e[0] == "tool" and e[1] == "book_appointment" and e[2]:
            create_seen = True
        if e[0] == "assistant" and not create_seen:
            low = strip_intent(normalize_spoken(e[1]))
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


def check_booked_no_wednesday(convo: Conversation, violations: list[str]) -> None:
    check_booked(convo, violations)
    for text in convo.assistant_texts:
        if re.search(r"\bwednesday at\b", text.lower()):
            violations.append(f"invented a Wednesday time: {text[:100]!r}")


def check_day_probe(convo: Conversation, violations: list[str]) -> None:
    """A named day must be ANSWERED from the calendar, both ways round."""
    check_booked(convo, violations)
    texts = [normalize_spoken(t) for t in convo.assistant_texts]
    joined = " ".join(texts)
    # Asked about Friday, which the calendar has: real Friday times must appear.
    friday_reply = next((t for t in texts if "friday" in t), "")
    if not friday_reply:
        violations.append("never answered the Friday question with Friday times")
    elif not any(word in friday_reply for word in ("four", "three", "half past", "quarter")):
        violations.append(f"named Friday but no actual time: {friday_reply[:120]!r}")
    # Asked about Wednesday, which it does not have: it must say so rather than
    # invent one or go quiet about it.
    if "wednesday at" in joined:
        violations.append("invented a Wednesday time that is not on the calendar")
    if "wednesday" in joined and not any(
        marker in joined
        for marker in ("nothing", "not got", "haven't got", "don't have", "no wednesday",
                       "afraid", "nothing on wednesday", "isn't", "not free", "no openings")
    ):
        violations.append("did not say plainly that Wednesday is unavailable")


def check_vague_then_pick(convo: Conversation, violations: list[str]) -> None:
    """A vague "yeah" must not select; a named time afterwards must."""
    check_booked(convo, violations)
    for index, event in enumerate(convo.events):
        if event[0] != "caller" or event[1] != "Yeah.":
            continue
        # Nothing may have been selected as of the turn right after the vague yes.
        selected_before = [
            e for e in convo.events[: index + 2]
            if e[0] == "tool" and e[1] == "select_slot" and e[2]
        ]
        if selected_before:
            violations.append("accepted a vague 'yeah' as a slot pick")


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
            "Tuesday at 1 works for me.",
            "Mostly rooftop residential, nothing under 50 kilowatts.",
            "Texas and Arizona.",
            "Perfect, thanks. Bye!",
        ],
        "final": check_booked,
    },
    "vague_then_first": {
        "rules": [
            (r"okay time|caught you|good time", ["Hey. Sure, I have a minute."]),
            (r"audit|includ|shouldn't", ["Go ahead, why not."]),
            (OFFER_PATTERN, [
                "Yeah.",  # a vague yes is NOT a pick - the agent must ask which
                "The later one on Friday then.",
                "Friday at six in the evening.",
            ]),
            (r"solar work|smallest|mainly|kind of work", ["Commercial solar, hundred kilowatts minimum."]),
            (r"states|areas|cover", ["Just Texas."]),
            (r"you're set|invite|anything else|booked", ["Great, sounds good, bye."]),
            (r".", ["Okay."]),
        ],
        "final": check_vague_then_pick,
    },
    "wednesday_probe": {
        # Rules-mode: the caller answers whatever the agent actually asked, so
        # legitimate question re-ordering between prompt versions can't desync the
        # conversation. Rules are checked in order; each reply is used once.
        "rules": [
            (r"okay time|caught you|good time", ["Yes, fine."]),
            (r"audit|includ|shouldn't", ["Sure, include it."]),
            (OFFER_PATTERN, [
                "Have you got anything on Wednesday instead?",
                "Alright then, Friday at half past four in the afternoon.",
                "The Friday at half past four.",
            ]),
            (r"solar work|smallest|mainly|kind of work", ["Ground mount, fifty kilowatts and up."]),
            (r"states|areas|cover", ["Nevada."]),
            (r"you're set|invite|anything else|booked", ["Perfect, thanks, bye."]),
            (r".", ["Okay.", "Go ahead."]),
        ],
        "final": check_booked_no_wednesday,
    },
    "recovering_pick": {
        # Live call 7: a cut-off answer followed by a CLEAR one must select and
        # book - the bad-line bailout must not fire (clear answers reset it,
        # and the caller's Wednesday question is engagement, not failure).
        "turns": [
            "Hello?",
            "Yeah, now works.",
            "Sure, include it.",
            "Do you have anything on Wednesday?",
            "Let's go for Tuesday on",
            "Tuesday at one.",
            "Rooftop residential, fifty kilowatts minimum.",
            "Texas.",
            "Perfect, bye!",
        ],
        "final": check_booked,
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
            3: lambda convo, violations: (
                violations.append("selected a time off Whisper noise")
                if "selected"
                in [a.get("category") for a in convo.crm.get_booking_attempts()]
                else None
            ),
        },
    },
    "day_probe": {
        # THE regression this whole change exists for. Sami named a day and the
        # agent asked "what day works for you?" back. The calendar holds Tuesday
        # and Friday only, so: a Friday ask must get Friday TIMES, and a
        # Wednesday ask must get an honest "nothing Wednesday" plus real
        # alternatives - never the question bounced back.
        "rules": [
            (r"okay time|caught you|good time", ["Yeah, go ahead."]),
            (r"audit|includ|shouldn't", ["Sure, include it."]),
            (r"tuesday|friday|which (suits|works|one)|got", [
                "What have you got on Friday?",
                "Hmm, what about Wednesday?",
                "Fine, Friday at half past four then.",
            ]),
            (r"solar work|smallest|mainly|kind of work", ["Carports, hundred kilowatts up."]),
            (r"states|areas|cover", ["California."]),
            (r"you're set|invite|anything else", ["Great, thanks, bye."]),
            (r".", ["Okay."]),
        ],
        "final": check_day_probe,
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


MAX_RULES_STEPS = 20


async def drive_rules_caller(convo: Conversation, spec: dict[str, Any]) -> None:
    """Adaptive caller: answer whatever the agent actually asked (rules-mode)."""
    await convo.caller_says(spec.get("opening", "Hello?"))
    rules = [
        {"pattern": re.compile(pattern, re.IGNORECASE), "replies": list(replies)}
        for pattern, replies in spec["rules"]
    ]
    for _ in range(MAX_RULES_STEPS):
        if convo.ended:
            return
        said_since_caller: list[str] = []
        for event in reversed(convo.events):
            if event[0] == "caller":
                break
            if event[0] == "assistant":
                said_since_caller.append(event[1])
        agent_said = " ".join(reversed(said_since_caller))
        reply = None
        for rule in rules:
            if rule["replies"] and rule["pattern"].search(agent_said):
                reply = rule["replies"].pop(0)
                break
        if reply is None:
            return
        await asyncio.sleep(INTER_TURN_SLEEP_SECONDS)
        await convo.caller_says(reply)


async def run_scenario(
    client: AsyncOpenAI,
    instructions: str,
    name: str,
    spec: dict[str, Any],
    menu: dict[str, Any],
) -> tuple[bool, str]:
    crm = CRMTools(db=MagicMock(), user_id=1, variables=dict(VARS))
    # The production bridge pre-loads the calendar before the caller speaks; the
    # eval must start from the same state or it tests a system we do not ship.
    crm.seed_offered_slots(menu["slots"], menu["timezone"])
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
            if "rules" in spec:
                await drive_rules_caller(convo, spec)
            else:
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
    menu = eval_menu()
    instructions = load_instructions(args.prompt_file, menu)
    client = AsyncOpenAI(api_key=get_api_key())

    names = args.only or list(SCENARIOS)
    results: dict[str, bool] = {}
    for name in names:
        passed, report = await run_scenario(
            client, instructions, name, SCENARIOS[name], menu
        )
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
