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
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import structlog
from openai import AsyncOpenAI

from app.core.config import settings
from app.services import calcom_client
from app.services.gpt_realtime import (
    GPTRealtimeSession,
    build_instructions_with_language,
    render_template,
)
from app.services.tools import crm_tools as crm_module
from app.services.tools.call_control_tools import CallControlTools
from app.services.tools.crm_tools import CRMTools
from app.services.tools.registry import ToolRegistry

MODEL = os.environ.get("EVAL_REALTIME_MODEL", "gpt-realtime-2.1")
MAX_RESPONSES_PER_SCENARIO = 30
MAX_RATE_LIMIT_RETRIES = 12
INTER_TURN_SLEEP_SECONDS = 3.0
# early_time_jump needs the greeting, the jumped-time turn, and the reply that
# follows it before there is anything to check.
MIN_CALLER_TURNS_FOR_EARLY_JUMP_CHECK = 3

VARS = {
    "agentName": "Adam",
    "leadName": "Sami",
    "company": "Pulsift",
    "leadEmail": "seeded@example.com",
    "leadPhone": "+963998183191",
    "phone": "+963998183191",
    "tzName": "Asia/Damascus",
    "brief": "Voice Pro booking test for Pulsift's solar lead-list offer.",
    # Said out loud in the opener, so it has to be words a person uses. The
    # catalogue name ("the free list of a hundred solar leads") read as a product
    # SKU, and "free" out of a stranger's mouth is the strongest telemarketer
    # marker there is - the email already established it costs nothing.
    "offer_name": "that list of solar leads",
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
    # Process narration while making the offer (live call, 2026-08-02): the agent
    # reads the pre-chosen OFFER FIRST line, it does not describe searching or
    # matching for one live on the call.
    "line that up",
    "moment while i match",
    "closest slot we have",
)

# "The agent has just offered times." Deliberately broad: every scenario keys off
# this, so it must survive rewording of the offer itself.
OFFER_PATTERN = (
    r"which (suits|works|one|should)|work better|would you like|half past|quarter"
    r"|in the (morning|afternoon|evening)|o'clock|midday|prefer"
)

BOOKED_CLAIMS = ("booked", "you're set", "you are set", "locked in")

# The opener must reach its closing time-check inside the SAME turn.
# The opener must reach its closing time-check inside the SAME turn. Kept as a
# list rather than one phrase because the closing beat is the part most likely
# to be reworded by ear, and a rig that only knows the current wording fails on
# the next good change rather than on a real regression.
OPENER_END_MARKERS = (
    "got a sec", "got a second", "got a minute", "catch you at a bad time",
    "okay time", "ok time", "good time", "bad time", "caught you",
    # The agenda opener ends on this beat, and prompt_publish.py REQUIRES it.
    "that alright", "sound good", "that okay", "that ok",
)
# The same beat, as a regex, for the scenario rules that answer it.
OPENER_PATTERN = "|".join(re.escape(marker) for marker in OPENER_END_MARKERS)

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


def four_day_slots() -> list[dict[str, str]]:
    """Monday to Thursday, four openings a day — the shape of a REAL calendar.

    The shared `fake_slots` holds Tuesday and Friday only, so every day it has is
    also a day it offers, and the 2026-08-08 live failure cannot happen in it.
    On that call the agent held sixteen slots including four on Wednesday, was
    asked for Wednesday, and said "I don't have Wednesday at midday in the pair I
    offered" — treating its two opening times as its whole calendar. Reproducing
    that needs a day the calendar HOLDS but did not open with.
    """
    now = datetime.now(UTC)

    def fmt(moment: datetime) -> str:
        return moment.isoformat().replace("+00:00", "Z")

    def next_weekday(target: int) -> datetime:
        ahead = (target - now.weekday()) % 7 or 7
        return now + timedelta(days=ahead)

    slots: list[dict[str, str]] = []
    for weekday in (0, 1, 2, 3):  # Monday .. Thursday
        day = next_weekday(weekday)
        for hour in (6, 9, 12, 16):  # 09:00 / 12:00 / 15:00 / 19:00 Damascus
            slots.append(
                {"start": fmt(day.replace(hour=hour, minute=0, second=0, microsecond=0))}
            )
    return slots


def full_week_slots() -> list[dict[str, str]]:
    """The REAL production calendar shape, measured 2026-08-09: Monday to Friday,
    every half hour across a working morning and early afternoon — 51 slots.

    The other two fixtures predate the caps being lifted, so both are smaller than
    anything the live agent now holds. A menu of four slots a day cannot exercise
    the thing that actually goes wrong at fifty-one: the model reading a long list
    and having to answer a specific question from the middle of it.
    """
    now = datetime.now(UTC)

    def fmt(moment: datetime) -> str:
        return moment.isoformat().replace("+00:00", "Z")

    def next_weekday(target: int) -> datetime:
        ahead = (target - now.weekday()) % 7 or 7
        return now + timedelta(days=ahead)

    slots: list[dict[str, str]] = []
    for weekday in range(5):  # Monday .. Friday
        day = next_weekday(weekday)
        for hour in range(6, 12):  # 09:00 .. 14:30 Damascus
            for minute in (0, 30):
                slots.append(
                    {
                        "start": fmt(
                            day.replace(hour=hour, minute=minute, second=0, microsecond=0)
                        )
                    }
                )
    return slots


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

    # The promised-list outbox is a REAL database write that happens before any
    # Cal.com call. Unfaked, every booking died as `fulfilment_unavailable` on a
    # refused connection to a database this rig has no business touching — so
    # `check_booked` failed on every scenario for reasons that had nothing to do
    # with the conversation, and the one invariant that matters most (never claim
    # booked before the tool succeeds) was never actually exercised.
    crm_module.stage_fulfilment_intent = AsyncMock(return_value="eval-intent-key")
    crm_module.claim_fulfilment_booking = AsyncMock(return_value=uuid.uuid4())
    crm_module.authorize_fulfilment_booking = AsyncMock(return_value=True)
    crm_module.finalize_fulfilment_intent = AsyncMock(return_value=True)


def load_instructions(prompt_file: Path, menu: dict[str, Any]) -> str:
    """Render the prompt exactly as the live session does, menu included."""
    from app.services.lead_timezone import spoken_zone_name

    variables = dict(VARS)
    variables["availability_block"] = menu["block"]
    variables["tz_spoken"] = spoken_zone_name(str(VARS["tzName"]))
    raw = prompt_file.read_text(encoding="utf-8")
    rendered = render_template(raw, variables)
    return build_instructions_with_language(rendered, "en-US", timezone="UTC")


# The five without which a booking call is theatre. If the live agent row stops
# producing any of them, this rig must go RED — not green with a quieter agent.
LOAD_BEARING_TOOLS = (
    "select_slot",
    "record_fit_answers",
    "book_appointment",
    "refresh_availability",
    "end_call",
)


HTTP_OK = 200


class ToolConfigError(RuntimeError):
    """The live agent is configured with a tool list that cannot book."""


def tool_names(tools: list[dict[str, Any]]) -> list[str]:
    return [t.get("name") or t.get("function", {}).get("name") or "?" for t in tools]


def tool_definitions(
    enabled_tools: list[str],
    enabled_tool_ids: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Build the tool list the way the PRODUCTION session builds it.

    This used to be `CRMTools.get_tool_definitions() + CallControlTools...`
    handed straight to the model, which bypassed both the registry and the agent
    row — so the rig always held a full toolbox no matter what production held.
    On 2026-08-08 production held `enabled_tools = []`, meaning ZERO tools on
    every real call, and all ten scenarios here still passed green. The agent
    claimed a booking it had no `book_appointment` to make and argued about a
    calendar it had no `refresh_availability` to check; the rig saw none of it.

    So this now makes the identical call `gpt_realtime._configure_session` makes,
    and refuses to run at all when the result cannot book. A rig that passes
    while production cannot book is worse than no rig.
    """
    registry = ToolRegistry(db=MagicMock(), user_id=1, integrations={}, workspace_id=None)
    tools = registry.get_all_tool_definitions(enabled_tools, enabled_tool_ids)
    have = set(tool_names(tools))
    missing = [name for name in LOAD_BEARING_TOOLS if name not in have]
    if missing:
        raise ToolConfigError(
            f"the agent's enabled_tools ({enabled_tools!r}) produce {len(tools)} "
            f"tools and are MISSING {missing}. Production cannot book with this "
            f"configuration, so there is nothing here worth evaluating."
        )
    return tools


def live_enabled_tools() -> tuple[list[str], dict[str, list[str]] | None]:
    """Read `enabled_tools` off the LIVE agent row, through the deployed API.

    Reading the real row is the whole point: a rig that hardcodes its own list is
    testing a system we do not ship.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops"))
    from ops_common import AGENT_ID, BACKEND, admin_token, request_json

    status, payload = request_json(
        f"{BACKEND}/api/v1/agents/{AGENT_ID}",
        headers={"Authorization": f"Bearer {admin_token()}"},
    )
    if status != HTTP_OK or not isinstance(payload, dict):
        raise ToolConfigError(f"could not read the live agent row (HTTP {status})")
    return list(payload.get("enabled_tools") or []), payload.get("enabled_tool_ids")


CALL_CONTROL_NAMES = {"wait_for_user", "end_call", "transfer_call", "send_dtmf"}


class Conversation:
    """One eval conversation: scripted caller vs the agent."""

    def __init__(self, connection: Any, crm: CRMTools) -> None:
        self.connection = connection
        self.crm = crm
        # Borrowed by _execute_tool so the rig runs the live dead-air rule.
        # structlog, not stdlib: the borrowed method logs with keyword fields.
        self._consecutive_waits = 0
        self.session_id = "eval"
        self.logger = structlog.get_logger("convo_eval")
        self.events: list[tuple[str, ...]] = []  # ("assistant"|"caller"|"tool", ...)
        self.ended = False
        self._rate_limit_retries = 0
        # An instance, not the class: execute_tool holds per-call goodbye state,
        # so calling it on the class raised on every single end_call and killed
        # every scenario before its final check ran.
        self.call_control = CallControlTools()
        # Item order inside each model response. A response holding BOTH a
        # spoken message and a function call is where "I'll check that time and
        # then..." comes from: the model speaks, calls the tool, and we then ask
        # for a SECOND response that says the real thing. Two utterances, one of
        # them pure narration. The order tells us whether it can be cut before
        # the caller ever hears it.
        self.response_shapes: list[list[str]] = []

    @property
    def assistant_texts(self) -> list[str]:
        return [e[1] for e in self.events if e[0] == "assistant"]

    def transcript(self) -> str:
        lines = []
        for e in self.events:
            if e[0] == "tool":
                line = f"  [tool] {e[1]} -> success={e[2]}"
                if not e[2]:
                    # A refused tool is the most important line in the whole
                    # transcript and it used to print as a bare "False".
                    result = e[3] if len(e) > 3 else {}
                    detail = result.get("error") or result.get("message") or result
                    line += f"  ({str(detail)[:200]})"
                lines.append(line)
            elif e[0] == "debug":
                lines.append(f"  [debug] {e[1]}")
            else:
                lines.append(f"[{e[0]}] {e[1]}")
        return "\n".join(lines)

    async def _execute_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = (
            await self.call_control.execute_tool(name, arguments)
            if name in CALL_CONTROL_NAMES
            else await self.crm.execute_tool(name, arguments)
        )
        # The dead-air limit lives on the live SESSION, and this rig talks to the
        # model directly, so without this line the rig runs a system we do not
        # ship — the exact gap that let `enabled_tools = []` pass 10/10 on
        # 2026-08-08. Borrow the real method rather than reimplement it.
        return GPTRealtimeSession._apply_dead_air_limit(self, name, result)  # noqa: SLF001

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
                # Same contract as app/services/gpt_realtime.py: a tool may name
                # the tool that must run next, and the model never sees the key.
                forced_next = (
                    result.pop("next_tool", None) if isinstance(result, dict) else None
                )
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
                if forced_next:
                    print(f"  [debug] forcing {forced_next}", flush=True)
                    await self.connection.response.create(
                        response={
                            "tool_choice": {"type": "function", "name": forced_next}
                        }
                    )
                    open_responses += 1
                elif name != "wait_for_user":
                    await self.connection.response.create()
                    open_responses += 1
            elif event_type == "response.done":
                open_responses -= 1
                response = getattr(event, "response", None)
                shape = [
                    (str(getattr(i, "type", "?")), str(getattr(i, "name", "") or ""))
                    for i in (getattr(response, "output", None) or [])
                ]
                if shape:
                    self.response_shapes.append(shape)
                    if len(shape) > 1:
                        readable = [f"{kind}:{name}" if name else kind
                                    for kind, name in shape]
                        self.events.append(("debug", f"response items in order: {readable}"))
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


# Tools whose result the caller is waiting on. Speaking before one of these
# produces the turn Sami heard on his own call: a promise ("I'll check that time
# and then we'll do two quick fit details"), the tool, and then the real sentence.
# end_call and wait_for_user are exempt: "take care" followed by a hang-up is one
# turn, not two, and it is exactly what we want.
SILENT_TOOLS = {
    "select_slot",
    "record_fit_answers",
    "book_appointment",
    "refresh_availability",
}


MAX_AFFIRMATION_WORDS = 8


def check_no_narrated_tool_calls(convo: Conversation, violations: list[str]) -> None:
    """No speaking before a tool the caller is waiting on.

    Read off the model's own response items rather than off the words, because
    the tell is structural: one response holding a message AND a function call
    always becomes two spoken turns once we feed the result back.

    One exception, added 2026-09-02 on Sami's ruling: a named time is answered
    "Sure" immediately, because the tool does the judging and the agent never
    has to. That affirmation IS the wanted behaviour, so a short leading message
    with no intent marker in it is allowed ahead of select_slot only. Anything
    that promises what happens next ("I'll pick that time and finish setting
    things up") still carries an intent marker and still fails.
    """
    for index, shape in enumerate(convo.response_shapes):
        spoke_first = False
        for kind, name in shape:
            if kind == "message":
                spoke_first = True
            elif kind == "function_call" and spoke_first and name in SILENT_TOOLS:
                if name == "select_slot" and _is_bare_affirmation(convo, index):
                    continue
                violations.append(
                    f"narrated {name} before calling it (response items: "
                    f"{[f'{k}:{n}' if n else k for k, n in shape]})"
                )
                break


def _is_bare_affirmation(convo: Conversation, index: int) -> bool:
    """Short, and promising nothing about what happens next."""
    texts = convo.assistant_texts
    if index >= len(texts):
        return False
    spoken = normalize_spoken(texts[index])
    if any(marker in spoken for marker in INTENT_MARKERS):
        return False
    return len(spoken.split()) <= MAX_AFFIRMATION_WORDS


def check_booking_turn_is_silent(convo: Conversation, violations: list[str]) -> None:
    """book_appointment must arrive alone, with nothing spoken alongside it.

    This is the deterministic half of the 2026-09-02 filler fix: the response
    that books is scoped to one named function, so it can only ever hold that
    function call. A message item sitting in the same response means the forced
    turn did not happen and the agent is narrating into the gap again.
    """
    for shape in convo.response_shapes:
        names = [name for kind, name in shape if kind == "function_call"]
        if "book_appointment" not in names:
            continue
        if len(shape) > 1:
            readable = [f"{k}:{n}" if n else k for k, n in shape]
            violations.append(f"booking turn was not silent (response items: {readable})")


def check_common(convo: Conversation, violations: list[str]) -> None:
    texts = [normalize_spoken(t) for t in convo.assistant_texts]
    if not texts:
        violations.append("agent never spoke")
        return
    # What the opener must DO, not the words it happens to use today. The literal
    # "heyy sami" that used to be here failed the 2026-08-09 rewrite on all five
    # scenarios while every one of them was correct — a check that knows only one
    # phrasing reports the next good change as a regression, and a check that
    # cries wolf stops being read.
    opener = texts[0]
    if not re.match(r"^(hey|hi|hello|good (morning|afternoon|evening))\b", opener):
        violations.append(f"opener does not greet them: {opener[:80]!r}")
    # The agent's own name is deliberately NOT here. Since 2026-08-25 the opener
    # is "this is Pulsift's AI assistant", and the name is spent on "who's this?"
    # instead - the prompt has a rule for exactly that. Requiring it here was
    # marking the approved opener as broken on every run.
    for label, needle in (
        ("their name", str(VARS["leadName"]).lower()),
        ("who is calling", "pulsift"),
    ):
        if needle not in opener:
            violations.append(f"opener never says {label}: {opener[:80]!r}")
    # Sami's ask: the opener keeps going past the name, all the way to the
    # time-check. Splitting it across turns is the failure this pins.
    if not any(marker in opener for marker in OPENER_END_MARKERS):
        violations.append(f"opener stopped before the time check: {opener[:160]!r}")
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
    check_no_narrated_tool_calls(convo, violations)
    check_booking_turn_is_silent(convo, violations)
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


def check_offers_a_held_day(convo: Conversation, violations: list[str]) -> None:
    """A day the calendar HOLDS must be offered, even if it was not opened with.

    The failure being pinned: the agent treats its two opening times as its whole
    calendar and denies a day it can plainly see. Denying a day we hold is worse
    than useless — it costs the booking and reads as arguing.
    """
    texts = [normalize_spoken(t) for t in convo.assistant_texts]
    joined = " ".join(texts)
    wednesday_reply = next((t for t in texts if "wednesday" in t), "")

    if not wednesday_reply:
        violations.append("never answered the Wednesday question at all")
        return
    for denial in ("don't have", "do not have", "don't hold", "nothing on wednesday",
                   "no wednesday", "not available", "nothing wednesday"):
        if denial in joined:
            violations.append(
                f"denied a day the calendar HOLDS: {wednesday_reply[:140]!r}"
            )
            break
    if not any(word in wednesday_reply for word in
               ("nine", "midday", "three", "seven", "morning", "afternoon", "evening")):
        violations.append(
            f"named Wednesday but offered no actual Wednesday time: "
            f"{wednesday_reply[:140]!r}"
        )
    check_booked(convo, violations)


def check_takes_a_time_from_deep_in_the_menu(
    convo: Conversation, violations: list[str]
) -> None:
    """A named time in the MIDDLE of a fifty-slot list must be honoured.

    The caps came off on 2026-08-09, so the agent now holds a full week instead of
    sixteen curated times. That trades one failure for a different one: with four
    slots the model could hardly miss the right one, and with fifty-one it can skim
    a long block and answer from the two it opened with. This asks for a Thursday
    afternoon time that is genuinely on the calendar and nowhere near the opener.
    """
    texts = [normalize_spoken(t) for t in convo.assistant_texts]
    joined = " ".join(texts)
    thursday_reply = next((t for t in texts if "thursday" in t), "")

    if not thursday_reply:
        violations.append("never answered the Thursday question at all")
        return
    for denial in ("don't have", "do not have", "don't hold", "nothing on thursday",
                   "no thursday", "not available", "can't do thursday",
                   "nothing thursday", "afraid not"):
        if denial in joined:
            violations.append(
                f"denied a time the calendar HOLDS: {thursday_reply[:140]!r}"
            )
            break
    check_booked(convo, violations)


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
    # A phrase whitelist, and it is brittle by nature: "we don't hold Wednesday"
    # is a perfectly plain denial and failed this check on 2026-08-08 purely for
    # wording. The substantive guard is the "wednesday at" check above — that one
    # catches invention, which is the behaviour that actually costs us. Widen the
    # list when a real transcript denies availability in words a person would use;
    # do NOT bend the prompt to satisfy the phrasing.
    if "wednesday" in joined and not any(
        marker in joined
        for marker in ("nothing", "not got", "haven't got", "don't have", "don't hold",
                       "do not hold", "no wednesday", "afraid", "nothing on wednesday",
                       "isn't", "not free", "no openings", "can't do", "cannot do",
                       "nothing free", "not available", "unavailable")
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


def check_questions_before_booking(convo: Conversation, violations: list[str]) -> None:
    """The two fit questions must be asked - and answered - before any time is
    offered. This is what changed on 2026-08-02: the old flow offered a time
    first and asked after; this pins the reorder so it can never quietly slide
    back."""
    check_booked(convo, violations)
    offer_index = next(
        (
            i
            for i, e in enumerate(convo.events)
            if e[0] == "assistant" and re.search(OFFER_PATTERN, normalize_spoken(e[1]))
        ),
        None,
    )
    if offer_index is None:
        violations.append("never offered a time at all")
        return
    caller_said_before_offer = " ".join(
        e[1].lower() for e in convo.events[:offer_index] if e[0] == "caller"
    )
    if "rooftop" not in caller_said_before_offer:
        violations.append("offered a time before the install-type question was answered")
    if not any(word in caller_said_before_offer for word in ("texas", "arizona")):
        violations.append("offered a time before the coverage-area question was answered")


def check_early_time_jump(convo: Conversation, violations: list[str]) -> None:
    """A time named before the questions must be served, not brushed off - and
    the two fit questions must still get asked afterward (live prompt rewrite,
    2026-08-02 - this is the order this scenario pins)."""
    check_booked(convo, violations)
    caller_positions = [i for i, e in enumerate(convo.events) if e[0] == "caller"]
    if len(caller_positions) < MIN_CALLER_TURNS_FOR_EARLY_JUMP_CHECK:
        violations.append("scenario ended before the early time could be served")
        return
    # Between the caller naming "Tuesday at ten" and their next turn, the agent's
    # reply must actually engage with it - not ignore it and open with a
    # question of its own instead.
    span = convo.events[caller_positions[1] : caller_positions[2]]
    reply_to_jump = " ".join(normalize_spoken(e[1]) for e in span if e[0] == "assistant")
    if not any(word in reply_to_jump for word in ("tuesday", "ten")):
        violations.append(f"ignored the early time instead of serving it: {reply_to_jump[:160]!r}")

    select_turn = next(
        (
            i
            for i, e in enumerate(convo.events)
            if e[0] == "tool" and e[1] == "select_slot" and e[2]
        ),
        None,
    )
    if select_turn is None:
        violations.append("never locked in the early time at all")
        return
    asked_after = " ".join(
        normalize_spoken(e[1]) for e in convo.events[select_turn:] if e[0] == "assistant"
    )
    if not any(
        word in asked_after for word in ("install", "rooftop", "ground", "carport", "kind of work")
    ):
        violations.append("never came back to ask about the kind of installs after locking the time")
    if not any(word in asked_after for word in ("area", "cover", "state")):
        violations.append("never came back to ask about the areas covered after locking the time")


SCENARIOS: dict[str, dict[str, Any]] = {
    "happy_natural": {
        # 2026-08-02 reorder: the two fit questions are answered before any time
        # is even offered, and the old separate audit question no longer exists.
        "turns": [
            "Hello?",
            "Yeah hi, who's this?",
            "Oh right, yeah now's fine.",
            "Mostly rooftop residential, nothing under 50 kilowatts.",
            "Texas and Arizona.",
            "Tuesday at 1 works for me.",
            "Perfect, thanks. Bye!",
        ],
        "final": check_booked,
    },
    "vague_then_first": {
        "rules": [
            (OPENER_PATTERN, ["Hey. Sure, I have a minute."]),
            (r"solar work|smallest|mainly|kind of work|installs", [
                "Commercial solar, hundred kilowatts minimum."
            ]),
            (r"states|areas|cover|counties|county|regions", ["Just Texas."]),
            (r"system size|sizes|how big|what size|kw|kilowatt",
             ["Anything from a hundred kilowatts up."]),
            (OFFER_PATTERN, [
                "Yeah.",  # a vague yes is NOT a pick - the agent must ask which
                "The later one on Friday then.",
                "Friday at six in the evening.",
            ]),
            (r"you're set|invite|anything else|booked", ["Great, sounds good, bye."]),
            (r".", ["Okay."]),
        ],
        "final": check_vague_then_pick,
    },
    "day_we_hold_but_did_not_offer": {
        # SAMI'S LIVE FAILURE, 2026-08-08. The agent held four Wednesday slots,
        # was asked for Wednesday, and answered "I don't have Wednesday at midday
        # in the pair I offered" — its two opening times had become, in its head,
        # the whole calendar. He then had to be talked back to a time he had not
        # asked for. This is the scenario that would have caught it.
        "slots": four_day_slots,
        "rules": [
            (OPENER_PATTERN, ["Yeah, go ahead."]),
            (r"solar work|smallest|mainly|kind of work|installs|rooftop", [
                "Mostly rooftop."
            ]),
            (r"states|areas|cover|counties|county|regions", ["Northern California."]),
            (r"system size|sizes|how big|what size|kw|kilowatt",
             ["Anything from a hundred kilowatts up."]),
            (OFFER_PATTERN, [
                "What about Wednesday?",
                "Wednesday at three then.",
                "Yeah, Wednesday at three in the afternoon.",
            ]),
            (r"you're set|invite|anything else|booked", ["Great, thanks, bye."]),
            (r".", ["Okay."]),
        ],
        "final": check_offers_a_held_day,
    },
    "time_deep_in_a_full_week": {
        # The new failure mode the caps coming off creates. Fifty-one slots is a
        # long block; the risk stops being "we thinned it away" and becomes "the
        # model skimmed it". The time asked for is real, on the calendar, and
        # nowhere near the two the agent opened with.
        "slots": full_week_slots,
        "rules": [
            (OPENER_PATTERN, ["Yeah, go on."]),
            (r"solar work|smallest|mainly|kind of work|installs|rooftop", [
                "Rooftop resi mostly."
            ]),
            (r"states|areas|cover|counties|county|regions", ["Arizona and Nevada."]),
            (r"system size|sizes|how big|what size|kw|kilowatt",
             ["Anything from a hundred kilowatts up."]),
            (OFFER_PATTERN, [
                "Can you do Thursday afternoon?",
                "Thursday at half past one in the afternoon.",
                "Yeah, Thursday at half past one.",
            ]),
            (r"you're set|invite|anything else|booked", ["Great, cheers, bye."]),
            (r".", ["Okay."]),
        ],
        "final": check_takes_a_time_from_deep_in_the_menu,
    },
    "wednesday_probe": {
        # Rules-mode: the caller answers whatever the agent actually asked, so
        # legitimate question re-ordering between prompt versions can't desync the
        # conversation. Rules are checked in order; each reply is used once.
        "rules": [
            (OPENER_PATTERN, ["Yes, fine."]),
            (r"solar work|smallest|mainly|kind of work|installs|ground.mount", [
                "Ground mount, fifty kilowatts and up."
            ]),
            (r"states|areas|cover|counties|county|regions", ["Nevada."]),
            (r"system size|sizes|how big|what size|kw|kilowatt",
             ["Anything from a hundred kilowatts up."]),
            (OFFER_PATTERN, [
                "Have you got anything on Wednesday instead?",
                "Alright then, Friday at half past four in the afternoon.",
                "The Friday at half past four.",
            ]),
            (r"you're set|invite|anything else|booked", ["Perfect, thanks, bye."]),
            (r".", ["Okay.", "Go ahead."]),
        ],
        "final": check_booked_no_wednesday,
    },
    "recovering_pick": {
        # Live call 7: a cut-off answer followed by a CLEAR one must select and
        # book - the bad-line bailout must not fire (clear answers reset it,
        # and the caller's Wednesday question is engagement, not failure). Fit
        # answers now come first, then the offer/pick.
        "turns": [
            "Hello?",
            "Yeah, now works.",
            "Rooftop residential, fifty kilowatts minimum.",
            "Texas.",
            "Do you have anything on Wednesday?",
            "Let's go for Tuesday on",
            "Tuesday at one.",
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
            "Thank you for watching.",
            "13 14 15 16.",
            "Subtitles by the Amara community.",
            "MBC news, thank you.",
        ],
        "final": check_garbled_line,
        "mid_checks": {
            2: lambda convo, violations: (
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
            (OPENER_PATTERN, ["Yeah, go ahead."]),
            (r"solar work|smallest|mainly|kind of work|installs|carports|rooftop", [
                "Carports, hundred kilowatts up."
            ]),
            (r"states|areas|cover|counties|county|regions", ["California."]),
            (r"system size|sizes|how big|what size|kw|kilowatt",
             ["Anything from a hundred kilowatts up."]),
            (r"tuesday|friday|which (suits|works|one)|got", [
                "What have you got on Friday?",
                "Hmm, what about Wednesday?",
                "Fine, Friday at half past four then.",
            ]),
            (r"you're set|invite|anything else", ["Great, thanks, bye."]),
            (r".", ["Okay."]),
        ],
        "final": check_day_probe,
    },
    "questions_before_booking": {
        # Pins the 2026-08-02 reorder itself: both fit questions must be
        # answered before the agent ever offers a time.
        "turns": [
            "Hello?",
            "Sure, go ahead.",
            "Rooftop residential, fifty kilowatts minimum.",
            "Texas and Arizona.",
            "Tuesday at one works.",
            "Perfect, thanks, bye!",
        ],
        "final": check_questions_before_booking,
    },
    "early_time_jump": {
        # The caller names a time on their SECOND turn, before either question.
        # The agent must serve it (lock it in) first, then come back for the
        # two questions - never ignore the time, never skip the questions.
        "turns": [
            "Hello?",
            "Can you do Tuesday at ten?",
            "Yeah, ten works, lock it in.",
            "Rooftop residential, fifty kilowatts minimum.",
            "Texas and Arizona.",
            "Perfect, thanks, bye!",
        ],
        "final": check_early_time_jump,
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
    prompt_file: Path,
    name: str,
    spec: dict[str, Any],
    menu: dict[str, Any],
    tools: list[dict[str, Any]],
) -> tuple[bool, str]:
    # A scenario may bring its OWN calendar. The shared one holds Tuesday and
    # Friday, so every day it has is also a day it offers — which makes the real
    # 2026-08-08 failure impossible to reproduce here: the agent held sixteen
    # slots including four on Wednesday, was asked for Wednesday, and answered
    # "I don't have Wednesday in the pair I offered". Reproducing that needs a
    # third day, so the menu and the rendered prompt are built per scenario.
    if spec.get("slots"):
        from app.services.availability import build_menu

        menu = build_menu(spec["slots"](), VARS["tzName"])
    instructions = load_instructions(prompt_file, menu)
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
                "tools": tools,
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
    parser.add_argument(
        "--enabled-tools",
        help="comma-separated override, for running with no network. Without it "
             "the LIVE agent row decides, which is the point.",
    )
    args = parser.parse_args()

    # Before anything else: what does production actually give the model? A run
    # against a toolbox production does not have is a run against a system we do
    # not ship, and that is precisely how ten green scenarios coexisted with an
    # agent that could not book.
    if args.enabled_tools:
        enabled, enabled_ids = [t.strip() for t in args.enabled_tools.split(",")], None
        source = "--enabled-tools override"
    else:
        enabled, enabled_ids = live_enabled_tools()
        source = "the LIVE agent row"
    try:
        tools = tool_definitions(enabled, enabled_ids)
    except ToolConfigError as error:
        print(f"REFUSING TO RUN: {error}")
        print(f"(read from {source})")
        return 2
    print(f"tools: {len(tools)} from {source} {enabled} -> {', '.join(tool_names(tools))}")

    install_fakes()
    menu = eval_menu()
    client = AsyncOpenAI(api_key=get_api_key())

    names = args.only or list(SCENARIOS)
    results: dict[str, bool] = {}
    for name in names:
        passed, report = await run_scenario(
            client, args.prompt_file, name, SCENARIOS[name], menu, tools
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
