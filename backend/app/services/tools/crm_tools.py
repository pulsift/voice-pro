"""CRM tools for voice agents - bookings, contacts, appointments."""

import asyncio
import contextlib
import json
import math
import re
import uuid
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.cache import cache_invalidate
from app.core.config import settings
from app.models.appointment import Appointment
from app.models.contact import Contact
from app.services.availability import (
    CALCOM_REQUIRED_BACKEND,
    CALENDAR_BACKEND_VARIABLE,
    AvailabilityResult,
)
from app.services.fulfilment_webhook import (
    ExtraBookingConflictError,
    authorize_fulfilment_booking,
    claim_fulfilment_booking,
    finalize_fulfilment_intent,
    is_test_conversation,
    stage_fulfilment_intent,
)
from app.services.operator_alerts import raise_operator_alert

logger = structlog.get_logger()
MAX_BOOKING_ATTEMPTS = 2
MIN_SLOTS_FOR_SECOND_SELECTION = 2
MAX_12_HOUR = 12
MAX_MINUTE = 59
_FULFILMENT_ICP_LIST_FIELDS = (
    "offer_types", "states", "industries", "cities",
)
_FULFILMENT_ICP_FIELDS = {*_FULFILMENT_ICP_LIST_FIELDS, "min_kw"}

AvailabilityLoader = Callable[[str, str], Awaitable[AvailabilityResult | None]]
AvailabilityInvalidator = Callable[[str], Awaitable[None]]

# Calendar writes that outlive the sentence that promised them. asyncio keeps only
# a weak reference to a running task, so a set at module scope is what stops the
# garbage collector cancelling a booking mid-flight.
_CALENDAR_WRITES: set[asyncio.Task[None]] = set()


async def wait_for_calendar_writes(
    timeout: float = 10.0,  # noqa: ASYNC109
    tasks: set[asyncio.Task[None]] | None = None,
) -> int:
    """Let in-flight calendar writes finish. Returns how many were still running.

    `tasks` scopes the wait to one call's own writes. Waiting on the module-global
    set instead makes a call that is trying to hang up sit through some OTHER
    prospect's slow Cal.com request ([Codex]).

    At call teardown this is not correctness — the writes are durable and alert on
    failure either way — it is the difference between the booking id landing ON the
    call record and landing just after it. At SHUTDOWN it is correctness, because a
    cancelled write is a booking the caller was promised and did not get.
    """
    pending = {task for task in (tasks or _CALENDAR_WRITES) if not task.done()}
    if pending:
        logger.info("waiting_for_calendar_writes", count=len(pending))
        await asyncio.wait(pending, timeout=timeout)
    return len(pending)


async def drain_calendar_writes_for_shutdown(timeout: float = 25.0) -> int:  # noqa: ASYNC109
    """Finish every outstanding calendar write before the process exits.

    Railway sends SIGTERM on redeploy. Without this, a write that had already
    dispatched its booking lease got cancelled part-way: the caller had been told
    they were booked, the durable record said a write was in flight, and the
    booking did not exist. Found by [Codex] on 2026-08-09; the window is small but
    it is open on every single deploy, and deploys happen far more often than
    Cal.com fails.
    """
    outstanding = len({task for task in _CALENDAR_WRITES if not task.done()})
    if outstanding:
        logger.warning("draining_calendar_writes_before_shutdown", count=outstanding)
        await wait_for_calendar_writes(timeout=timeout)
    return outstanding


def _normalize_fulfilment_icp(value: dict[str, Any] | str) -> dict[str, Any]:
    """Return the exact ICP shape accepted by the paid fulfilment receiver."""
    if isinstance(value, str):
        parsed = json.loads(value)
        if not isinstance(parsed, dict):
            raise TypeError("ICP JSON must be an object")
        value = parsed
    if not isinstance(value, dict):
        raise TypeError("ICP must be an object")
    unknown = set(value) - _FULFILMENT_ICP_FIELDS
    if unknown:
        raise ValueError("ICP contains unsupported fields")

    normalized: dict[str, Any] = {}
    for field in _FULFILMENT_ICP_LIST_FIELDS:
        if field not in value:
            continue
        items = value[field]
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item.strip() for item in items
        ):
            raise TypeError(f"ICP {field} must be a list of non-empty strings")
        normalized[field] = [item.strip() for item in items]

    if "min_kw" in value:
        min_kw = value["min_kw"]
        if min_kw is not None and (
            isinstance(min_kw, bool)
            or not isinstance(min_kw, (int, float))
            or not math.isfinite(float(min_kw))
        ):
            raise TypeError("ICP min_kw must be a finite number or null")
        normalized["min_kw"] = min_kw
    return normalized


def _fulfilment_promise_key(conversation_id: Any, generation: Any) -> str | None:
    """f"{conversation_id}:g{generation}" - the true launch boundary shared with
    pulsift-reply-router's other fulfilment sender (see
    reply_router/factory.py's fulfilment_aggregate_id, reimplemented here since
    that repo is not a dependency). One promised list has two senders: this
    booking tool sends the Cal.com UID as booking_id, the reply router sends a
    synthetic magnet-<conversation_id>[-gN] - keying the paid build on
    booking_id let one promise launch two paid builds. `promise_key` is the
    receiver's real dedupe key now; booking_id is kept only for display.

    Returns None - never a guessed or fabricated key - when either half isn't
    genuinely available; the receiver is backward compatible and falls back
    to booking_id in that case.
    """
    conv_id = str(conversation_id or "")
    if not conv_id.strip() or type(generation) is not int or generation < 1:
        return None
    # Deliberately NOT stripped before formatting: the router does not strip
    # either, and the only thing that matters is that both sides derive the
    # IDENTICAL string. Stripping on one side alone would give one promise two
    # keys for an id carrying stray whitespace - the exact double-paid-build
    # bug this key exists to close. Emptiness is still rejected above.
    return f"{conv_id}:g{generation}"


class CRMTools:
    """Internal CRM tools for voice agents.

    Provides tools for:
    - Looking up customers by phone/email/name
    - Creating new contacts
    - Checking appointment availability
    - Booking appointments
    - Viewing upcoming appointments
    - Canceling appointments
    """

    def __init__(
        self,
        db: AsyncSession,
        user_id: int,
        workspace_id: uuid.UUID | None = None,
        variables: dict[str, Any] | None = None,
    ) -> None:
        """Initialize CRM tools.

        Args:
            db: Database session
            user_id: User ID (agent owner) - integer matching Contact.user_id
            workspace_id: Workspace UUID for scoping contacts
            variables: Per-call lead data (leadName, leadEmail, tzName, company, ...) used
                       to fill the Cal.com attendee so the agent never has to ask for it.
        """
        self.db = db
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.variables = variables or {}
        self._requires_calcom = (
            self.variables.get(CALENDAR_BACKEND_VARIABLE) == CALCOM_REQUIRED_BACKEND
        )
        self.logger = logger.bind(
            component="crm_tools", user_id=user_id, workspace_id=str(workspace_id)
        )
        self._offered_slots: list[dict[str, str]] = []
        self._selected_slot_id: str | None = None
        self._selected_start: str | None = None
        self._normalized_timezone: str | None = None
        self._user_turn = 0
        self._offer_user_turn = 0
        self._latest_user_utterance = ""
        self._latest_assistant_utterance = ""
        self._selection_user_turn = 0
        self._booking_attempts: list[dict[str, Any]] = []
        self._booking_completed: dict[str, Any] | None = None
        # Kept OUTSIDE _booking_completed, which is deepcopied straight back to the
        # model: these two are for the operator alert, not for the agent to read.
        self._booked_when: str = ""
        self._booked_intent_key: str = ""
        # Calendar writes THIS session started. Draining the module-global set at
        # teardown made one call wait on another prospect's slow Cal.com request
        # ([Codex]); a call only ever waits for its own.
        self._calendar_writes: set[asyncio.Task[None]] = set()
        # Fit answers (what they install, areas they cover), captured the moment
        # they're given - independent of booking, so a call that ends before
        # book_appointment ever runs still hands the team something real. See
        # record_fit_answers / get_fit_answers.
        self._fit_answers: dict[str, Any] = {}
        # True once times have actually been said out loud (any tool-driven offer).
        # A pre-loaded menu starts False — see seed_offered_slots.
        self._menu_announced = True
        self._live_availability_loader: AvailabilityLoader | None = None
        self._live_availability_invalidator: AvailabilityInvalidator | None = None
        self._timezone_clarification_required = False

    @property
    def calendar_writes(self) -> set[asyncio.Task[None]]:
        """Detached calendar writes THIS call started."""
        return self._calendar_writes

    def set_live_availability_loader(self, loader: AvailabilityLoader) -> None:
        """Delegate live calendar publication to the owning Realtime session."""
        self._live_availability_loader = loader

    def set_live_availability_invalidator(
        self, invalidator: AvailabilityInvalidator
    ) -> None:
        """Clear stale prompt availability when a correction cannot be resolved."""
        self._live_availability_invalidator = invalidator

    async def _load_availability_menu(
        self, lead_tz: str, *, origin: str
    ) -> AvailabilityResult | None:
        if self._live_availability_loader:
            return await self._live_availability_loader(lead_tz, origin)

        from app.services.availability import fetch_menu

        menu = await fetch_menu(lead_tz)
        self.apply_availability_menu(menu, origin=origin)
        return menu

    def apply_availability_menu(
        self, menu: AvailabilityResult, *, origin: str
    ) -> int:
        """Adopt one typed calendar result into the transcript-bound slot gate."""
        status = menu["status"]
        timezone = str(menu.get("timezone") or "UTC")
        if status == "available":
            adopted = self.seed_offered_slots(
                menu.get("slots") or [], timezone, origin=origin
            )
            if adopted or origin == "preloaded":
                return adopted
            status = "unavailable"

        self._replace_offered_slots([], timezone)
        self._booking_attempts[-1]["category"] = status
        return 0

    async def _invalidate_unresolved_timezone(self) -> None:
        if self._live_availability_invalidator:
            try:
                await self._live_availability_invalidator("timezone_unresolved")
                return
            except Exception as exc:
                self.logger.warning(
                    "availability_invalidation_failed",
                    error_type=type(exc).__name__,
                )
        self._replace_offered_slots([], self._normalized_timezone or "UTC")
        self._booking_attempts[-1]["category"] = "timezone_unresolved"

    def seed_offered_slots(
        self,
        slots: list[dict[str, Any]],
        timezone: str,
        *,
        origin: str = "preloaded",
    ) -> int:
        """Adopt a slot menu as the offered set (services/availability.py).

        This is what lets the agent skip asking the calendar anything: the times it
        can see in its prompt are already the times select_slot will accept. The
        transcript-bound selection guard is untouched — a menu the agent can read is
        still not permission to pick on the caller's behalf.

        Whatever the origin, a selection must come from an utterance made AFTER this
        menu existed — `_offer_user_turn` is stamped with the current turn, which is
        naturally 0 when the pre-load wins the race against the caller's first word
        and correctly non-zero when it does not. (An earlier version special-cased
        "preloaded" to 0 unconditionally; because the pre-load is genuinely mid-call
        — it awaits Cal.com while the line is already open — that permanently
        disabled the guard whenever the calendar was slow, and a time the caller had
        merely *asked about* before the menu landed could then be booked.)

        `origin` still matters for `_menu_announced`: a pre-loaded menu has never been
        read out, so selecting from it needs an unambiguous time of the caller's own
        (see `_strong_time_signal`).

        Returns the number of slots adopted (0 leaves the tool path in charge).
        """
        if origin == "preloaded" and self._selected_slot_id:
            # The caller already picked something. A background pre-load is an
            # optimisation and must never damage in-flight state: resetting the
            # offered set here would drop their selection and make the agent
            # re-ask for a time it had already been given.
            self.logger.info("preloaded_menu_skipped_selection_in_flight")
            return 0
        usable = [
            {"slot_id": str(s["slot_id"]), "start": str(s["start"]),
             "label": str(s.get("label") or ""), "timezone": timezone}
            for s in slots
            if isinstance(s, dict) and s.get("slot_id") and s.get("start")
        ]
        if not usable:
            return 0
        self._offered_slots = usable
        self._normalized_timezone = timezone
        self._selected_slot_id = None
        self._selected_start = None
        self._selection_user_turn = 0
        self._offer_user_turn = self._user_turn
        # A pre-loaded menu was never READ OUT to the caller — it only exists in the
        # agent's head. "The caller clearly chose one of the times I offered" is
        # therefore not yet true of it, so selecting from it demands an unambiguous
        # time of their own (see _strong_time_signal).
        self._menu_announced = origin != "preloaded"
        self._booking_attempts.append(
            {
                "operation": "availability",
                "attempt": len(self._booking_attempts) + 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "category": origin,
                "timezone": timezone,
                "turn": self._offer_user_turn,
                "slot_ids": [slot["slot_id"] for slot in usable],
            }
        )
        self.logger.info(
            "offered_slots_seeded", origin=origin, count=len(usable), timezone=timezone
        )
        return len(usable)

    def _calcom_enabled(self) -> bool:
        """True when Cal.com is configured to back booking (else internal calendar)."""
        return bool(settings.CALCOM_API_KEY and settings.CALCOM_EVENT_TYPE_ID)

    def observe_user_utterance(self, text: str) -> None:
        """Observe one completed user transcript for transcript-bound slot selection."""
        self._user_turn += 1
        self._latest_user_utterance = text.strip()

    def observe_assistant_utterance(self, text: str) -> None:
        """Observe what the agent just SAID, so the caller's reply can be read in
        context — "yes" and "midday" only mean something next to the question."""
        spoken = text.strip()
        if spoken:
            self._latest_assistant_utterance = spoken

    def get_booking_attempts(self) -> list[dict[str, Any]]:
        """Return a safe copy for later CallRecord persistence."""
        return deepcopy(self._booking_attempts)

    def get_fit_answers(self) -> dict[str, Any]:
        """Return a safe copy of the fit answers captured so far this call.

        Populated by record_fit_answers as soon as the lead answers, independent
        of whether the call ever reaches book_appointment. Empty when nothing has
        been recorded - never a dict with empty/placeholder values.
        """
        return deepcopy(self._fit_answers)

    async def record_fit_answers(
        self,
        offer_types: list[str] | None = None,
        min_kw: float | None = None,
        states: list[str] | None = None,
    ) -> dict[str, Any]:
        """Save whichever fit answers the lead has given so far.

        Independent of select_slot/book_appointment on purpose: under the
        current call order the two fit questions are asked before any time is
        offered, so this is what keeps their answers for the team even if the
        call ends before booking. Merges into whatever was already saved - only
        the fields actually passed are touched, so calling it again with just
        the second answer never wipes out the first, and a field never given is
        never fabricated.
        """
        given: dict[str, Any] = {}
        if offer_types is not None:
            given["offer_types"] = offer_types
        if min_kw is not None:
            given["min_kw"] = min_kw
        if states is not None:
            given["states"] = states
        if not given:
            return {"success": True, "recorded": False}
        try:
            normalized = _normalize_fulfilment_icp(given)
        except (TypeError, ValueError):
            return {
                "success": False,
                "error": "invalid_fit_answers",
                "message": (
                    "Pass offer_types/states as lists of words, min_kw as a number."
                ),
            }
        self._fit_answers.update(normalized)
        return {"success": True, "recorded": True}

    def _replace_offered_slots(self, slots: list[dict[str, str]], timezone: str) -> None:
        self._offered_slots = [
            {
                "slot_id": f"slot_{index}",
                "start": slot["start"],
                "label": slot["label"],
                "timezone": timezone,
            }
            for index, slot in enumerate(slots, start=1)
        ]
        self._normalized_timezone = timezone
        self._selected_slot_id = None
        self._selected_start = None
        self._selection_user_turn = 0
        self._offer_user_turn = self._user_turn
        self._menu_announced = True
        self._booking_attempts.append(
            {
                "operation": "availability",
                "attempt": len(self._booking_attempts) + 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "category": "offered" if slots else "empty",
                "timezone": timezone,
                "turn": self._user_turn,
                "slot_ids": [slot["slot_id"] for slot in self._offered_slots],
            }
        )

    @staticmethod
    def _canonical_start(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC)

    @staticmethod
    def _spoken_minute_matches(
        text: str, word_hours: dict[str, int]
    ) -> tuple[set[tuple[int, int]], str]:
        """Parse spoken part-hours: "half past four", "quarter to five", "four thirty".

        Returns `(time_matches, residual)`. Both halves of the day are produced for
        each phrase, exactly like bare spoken hours: the caller says no am/pm, and
        the offered-slot set is what must reduce it to one. A "to" phrase counts back
        from the named hour ("quarter to five" is 4:45).

        `residual` is the text with every matched phrase blanked out, and it is the
        ONLY thing the bare-hour pass may consult. "half past five" is blanked, so a
        sentence containing only that never yields five o'clock — while "five in the
        evening, half past five" still yields both, because the standalone "five"
        survives in the residual.

        An earlier version ALSO suppressed by hour value: any hour seen in a
        "half past" was banned from the bare pass for the whole sentence. That is how
        the 2026-08-18 ring test failed to book. Reading its own menu aloud —
        "five, half past five, six, half past six" — the agent concluded it had
        offered only the half-past times, so the caller answering "six in the
        evening" matched nothing and was re-asked until he gave up.
        """
        matches: set[tuple[int, int]] = set()
        hour_word = "|".join(word_hours)
        past_minutes = {
            "five past": 5, "ten past": 10, "quarter past": 15, "twenty past": 20,
            "twenty five past": 25, "half past": 30,
        }
        to_minutes = {
            "twenty five to": 35, "twenty to": 40, "quarter to": 45,
            "ten to": 50, "five to": 55,
        }
        trailing_minutes = {
            "fifteen": 15, "thirty": 30, "forty five": 45, "forty-five": 45,
        }

        def add(hour12: int, minute: int) -> None:
            matches.add((hour12 % MAX_12_HOUR, minute))
            matches.add((hour12 % MAX_12_HOUR + MAX_12_HOUR, minute))

        def named_hour(token: str) -> int | None:
            return word_hours.get(token) or (
                int(token) if token.isdigit() and 1 <= int(token) <= MAX_12_HOUR else None
            )

        residual = text

        def consume(span: tuple[int, int]) -> None:
            nonlocal residual
            start, end = span
            residual = residual[:start] + " " * (end - start) + residual[end:]

        # Longest phrases first, blanking each match as it is taken, so "twenty five
        # past nine" is not ALSO read as "five past nine" or as a bare "five".
        for phrase, minute in sorted(
            {**past_minutes, **to_minutes}.items(), key=lambda kv: -len(kv[0])
        ):
            for match in list(
                re.finditer(rf"\b{phrase}\s+({hour_word}|\d{{1,2}})\b", residual)
            ):
                named = named_hour(match.group(1))
                if named is None:
                    continue
                # "quarter to five" = 4:45 — the hour BEFORE the one named.
                hour = (named - 1 or MAX_12_HOUR) if phrase in to_minutes else named
                add(hour, minute)
                consume(match.span())

        for phrase, minute in trailing_minutes.items():
            for match in list(
                re.finditer(rf"\b({hour_word}|\d{{1,2}})\s+{phrase}\b", residual)
            ):
                named = named_hour(match.group(1))
                if named is not None:
                    add(named, minute)
                    consume(match.span())
        return matches, residual

    @staticmethod
    def _extract_time_matches(text: str) -> set[tuple[int, int]]:
        """Parse every (hour, minute) the utterance could be naming."""
        time_matches: set[tuple[int, int]] = set()
        for match in re.finditer(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text):
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            if 1 <= hour <= MAX_12_HOUR and minute <= MAX_MINUTE:
                hour = hour % MAX_12_HOUR + (MAX_12_HOUR if match.group(3) == "pm" else 0)
                time_matches.add((hour, minute))
        for match in re.finditer(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text):
            time_matches.add((int(match.group(1)), int(match.group(2))))
        word_hours = {
            "one": 1,
            "two": 2,
            "three": 3,
            "four": 4,
            "five": 5,
            "six": 6,
            "seven": 7,
            "eight": 8,
            "nine": 9,
            "ten": 10,
            "eleven": 11,
            "twelve": 12,
        }
        # SPOKEN minutes. The agent offers times the way a person says them ("half
        # past four", "quarter to five"), so the caller repeats them back that way -
        # and a parser that only understood bare hours refused the very phrasing we
        # had just used. Found by the conversational eval, 2026-07-30.
        spoken, residual = CRMTools._spoken_minute_matches(text, word_hours)
        time_matches.update(spoken)
        # "one" doubles as a pronoun ("the morning one") - only count it as an
        # hour in a time context. Other number words are safe bare.
        one_as_hour = (
            r"\b(?:at|around|about|for)\s+one\b"
            r"|\bone\s+o'?clock\b"
            r"|\bone\s+(?:in|at|pm|am)\b"
        )
        for word, hour in word_hours.items():
            if word == "one":
                if not re.search(one_as_hour, residual):
                    continue
            elif not re.search(rf"\b{word}\b", residual):
                continue
            # Spoken bare hours have no AM/PM. Match both halves of the day;
            # the offered-slot set must still reduce this to exactly one slot.
            time_matches.add((hour % MAX_12_HOUR, 0))
            time_matches.add((hour % MAX_12_HOUR + MAX_12_HOUR, 0))
        # Bare DIGIT hours ("at 1", "around 10") - transcription often writes
        # digits, not words. Same both-halves treatment as spoken word hours.
        for match in re.finditer(
            r"\b(?:at|around|about|for)\s+(\d{1,2})\b(?!\s*(?::|am|pm))", residual
        ):
            hour = int(match.group(1))
            if 1 <= hour <= MAX_12_HOUR:
                time_matches.add((hour % MAX_12_HOUR, 0))
                time_matches.add((hour % MAX_12_HOUR + MAX_12_HOUR, 0))
        if re.search(r"\bnoon\b|\bmidday\b", text):
            time_matches.add((12, 0))
        return time_matches

    _DAY_NAMES = (
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    )

    def _time_signals(
        self, text: str
    ) -> tuple[set[tuple[int, int]], set[str], list[tuple[int, int]]]:
        """Everything a sentence says about WHEN, with one owner.

        Split out of `_slots_named_in` because two callers now need it: matching a
        sentence to slots, and answering the different question "did they name a
        time at all?". A second copy of this parse would drift, and the drift shows
        up on a call as the agent telling somebody a time is gone when they never
        named one.
        """
        time_matches = self._extract_time_matches(text)
        day_names = {
            name for name in self._DAY_NAMES if re.search(rf"\b{name}\b", text)
        }
        # Day-part references ("the morning one", "the afternoon slot") are how
        # people naturally answer when the two times are re-offered by name.
        periods: list[tuple[int, int]] = []
        if re.search(r"\bmorning\b", text):
            periods.append((0, 12))
        if re.search(r"\bafternoon\b", text):
            periods.append((12, 18))
        if re.search(r"\bevening\b|\btonight\b", text):
            periods.append((17, 24))
        return time_matches, day_names, periods

    def _named_a_time(self, utterance: str) -> tuple[bool, set[str]]:
        """Did they name a WHEN, and which day did they name?

        Used only to tell two very different refusals apart: "they have not chosen
        yet" and "they chose something we do not hold". Getting that wrong in
        either direction puts a lie in the agent's mouth, so it reuses the same
        parse as matching rather than a second opinion about it.
        """
        text = " ".join((utterance or "").lower().split())
        text = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", text)
        time_matches, day_names, periods = self._time_signals(text)
        return bool(time_matches or day_names or periods), day_names

    def _two_alternatives(self, day_names: set[str]) -> str:
        """Two real times to offer instead, same day first.

        Never invented: every one comes from the slots already offered, so a
        walk-back can only ever name something we actually hold.
        """
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(self._normalized_timezone or "UTC")
        ordered = sorted(self._offered_slots, key=lambda slot: str(slot["start"]))
        same_day = []
        if day_names:
            for slot in ordered:
                start = self._canonical_start(slot["start"])
                if start is None:
                    continue
                if start.astimezone(zone).strftime("%A").lower() in day_names:
                    same_day.append(slot)
        pool = same_day or ordered
        labels = [str(slot.get("label") or "") for slot in pool[:2] if slot.get("label")]
        if len(labels) >= MIN_SLOTS_FOR_SECOND_SELECTION:
            return f"{labels[0]}, or {labels[1]}"
        return labels[0] if labels else ""

    def _slots_named_in(self, utterance: str, *, shortlist: list[str] | None = None) -> set[str]:
        """Which offered slots this sentence could be naming.

        `shortlist` narrows the search to specific slot ids — used to read the
        CALLER's words against only what the agent actually just offered, which is
        how a person hears them too.
        """
        from zoneinfo import ZoneInfo

        text = " ".join(utterance.lower().split())
        # Whisper writes dotted meridiems ("1 p.m.") - normalize to "1 pm".
        text = re.sub(r"\b([ap])\.\s?m\.?", r"\1m", text)
        pool = [
            slot
            for slot in self._offered_slots
            if shortlist is None or slot["slot_id"] in shortlist
        ]

        # Ordinals are relative to WHAT WAS JUST OFFERED, never to the whole menu.
        # With a 16-slot menu, "the first one" meaning menu-position-one would pick a
        # time nobody had mentioned.
        ordered = sorted(pool, key=lambda slot: str(slot["start"]))
        ordinal_candidates: set[str] = set()
        # "earlier"/"later" only count inside a bounded phrase that points AT one of the
        # offered times — "the later one", "the later slot". Bare "later" is usually
        # about the call, not the calendar: "can we talk later?" must never book the
        # second option (Codex review #6).
        first = r"\bfirst\b|\bearlier (one|option|slot|time)\b"
        second = r"\bsecond\b|\blater (one|option|slot|time)\b"
        if re.search(first, text) and ordered:
            ordinal_candidates.add(ordered[0]["slot_id"])
        if re.search(second, text) and len(ordered) >= MIN_SLOTS_FOR_SECOND_SELECTION:
            ordinal_candidates.add(ordered[1]["slot_id"])
        if ordinal_candidates:
            return ordinal_candidates

        time_matches, day_names, periods = self._time_signals(text)
        if not time_matches and not day_names and not periods:
            return set()

        zone = ZoneInfo(self._normalized_timezone or "UTC")
        candidates: set[str] = set()
        for slot in pool:
            start = self._canonical_start(slot["start"])
            if start is None:
                continue
            local_start = start.astimezone(zone)
            time_ok = not time_matches or (local_start.hour, local_start.minute) in time_matches
            day_ok = not day_names or local_start.strftime("%A").lower() in day_names
            period_ok = not periods or any(
                lo <= local_start.hour < hi for lo, hi in periods
            )
            if time_ok and day_ok and period_ok:
                candidates.add(slot["slot_id"])
        return candidates

    def _utterance_slot_candidates(self) -> set[str]:
        """The slot the caller just chose, read in the context of what was offered.

        A live call on 2026-07-31 showed why context is not optional. The agent asked
        "would Monday at midday work?", Sami said "yes, it would", and the booking was
        refused because he had not NAMED a time. He then said "midday" — refused
        again, because a 16-slot menu has a midday on four different days, so the time
        alone was ambiguous. Both readings are wrong; a person in that conversation
        would have understood both answers perfectly.

        So the caller's words are read against the agent's last turn:
          - a plain "yes" selects the time IF the agent had just proposed exactly one
          - a time that matches several days narrows to the day the agent just named

        Everything else is unchanged: silence, a vague "yeah" with nothing proposed,
        or words matching several offered times still refuse and re-ask.
        """
        # A refusal or a "let me get back to you" is never a selection, however many
        # times it happens to name (Codex review #6: "No, Tuesday at ten doesn't work"
        # named a slot, so the matcher returned it and Cal.com booked the time the
        # caller had just rejected). The gate is deliberately strict — when the words
        # carry a no, a doubt or a delay, we re-ask rather than guess.
        if self._refuses_or_defers(self._latest_user_utterance):
            return set()

        offered_now = self.slots_offered_aloud()
        caller_anywhere = self._slots_named_in(self._latest_user_utterance)
        if not offered_now:
            return caller_anywhere

        # Read them against the offer FIRST — that is what disambiguates "midday"
        # when four days have one, and what makes "the first one" mean the first of
        # the two just named rather than the first in a sixteen-slot menu.
        in_context = self._slots_named_in(
            self._latest_user_utterance, shortlist=sorted(offered_now)
        )
        if in_context:
            return in_context
        if caller_anywhere:
            return caller_anywhere  # they named something else ("Tuesday instead")
        if len(offered_now) == 1 and self._is_agreement(self._latest_user_utterance):
            return offered_now  # "yes, it would" to a single proposed time
        return set()

    def slots_offered_aloud(self) -> set[str]:
        """Offered slots the agent named in its most recent turn."""
        return self._slots_named_in(self._latest_assistant_utterance)

    # A no, in any of the shapes a person actually says one.
    _NEGATION = re.compile(
        r"\b(no|nope|nah|not|never|cant|cannot|wont|dont|doesnt|isnt|aint)\b"
        r"|\bn'?t\b|\b(can|do|does|is|was|will|would)n'?t\b",
        re.IGNORECASE,
    )
    # Not a no, but not a yes either: they want to think, check, or be called back.
    _DEFERRAL = re.compile(
        r"\b(maybe|perhaps|possibly|probably|might|unsure|not sure|dunno|"
        r"let me check|i'?ll check|check with|get back to you|call me back|"
        r"another time|some other time|later on|next week|think about it|"
        r"run it by|confirm with|too early|too late)\b",
        re.IGNORECASE,
    )

    def _refuses_or_defers(self, utterance: str) -> bool:
        """Whether the reply carries a refusal, a doubt or a delay.

        Kept separate from time matching on purpose: a caller can name a time inside a
        sentence that rejects it ("no, Tuesday at ten doesn't work"), and naming is not
        choosing. "Tuesday instead" carries no negation, so it still selects — swapping
        to a different time IS a choice.
        """
        text = " ".join((utterance or "").lower().split())
        if not text:
            return False
        return bool(self._NEGATION.search(text) or self._DEFERRAL.search(text))

    _AGREEMENT_WORD = (
        r"(yes|yeah|yep|yup|sure|ok|okay|perfect|great|good|fine|lovely|brilliant|"
        r"sounds good|sounds great|sounds perfect|works|that works|this works|"
        r"that'?s? fine|it works|it would|that would|i can|we can|please|do that|"
        r"book it|go ahead|let'?s do that|lets do that|absolutely|definitely|deal)"
    )
    # Politeness that may TRAIL an agreement but never stands as one on its own.
    _AGREEMENT_TAIL = r"(thanks|thank you|cheers|for me|then|too|cool|nice|mate|man)"
    _AGREEMENT = re.compile(
        rf"^\W*{_AGREEMENT_WORD}"
        rf"(\s*[,.!-]?\s*({_AGREEMENT_WORD}|{_AGREEMENT_TAIL}))*"
        r"\s*[.!]*\W*$",
        re.IGNORECASE,
    )

    def _is_agreement(self, utterance: str) -> bool:
        """Whether the reply is a plain yes to whatever was just asked, and NOTHING else.

        Matches the WHOLE utterance, not just its opening (Codex review #6). A prefix
        match read "sure, let me check and get back to you" as consent, which is the
        opposite of what the caller said. "Yes, it would" and "perfect, thanks" still
        agree; anything carrying extra business does not, and is re-asked instead.
        """
        text = " ".join((utterance or "").lower().split())
        if not text or self._refuses_or_defers(text):
            return False
        return bool(self._AGREEMENT.match(text))

    _STRONG_TIME_SIGNAL = re.compile(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow)\b"
        r"|\b\d{1,2}\s*(am|pm)\b|\b[ap]\.?\s?m\.?\b|\bo'?clock\b"
        r"|\b(noon|midday|morning|afternoon|evening)\b"
        r"|\b(half|quarter)\s+(past|to)\b|\b\d{1,2}:\d{2}\b",
        re.IGNORECASE,
    )

    def _strong_time_signal(self) -> bool:
        """Whether the caller named a time in a way that cannot be something else.

        Only required for a menu the caller was never read (see seed_offered_slots).
        A bare number is not enough there: "give me two minutes" would otherwise
        match a two o'clock opening and look like a choice.
        """
        return bool(self._STRONG_TIME_SIGNAL.search(self._latest_user_utterance))

    async def select_slot(self, slot_id: str) -> dict[str, Any]:
        """Pin one offered slot only when the latest post-offer transcript agrees."""
        if not self._offered_slots:
            return {
                "success": False,
                "error": "slots_not_offered",
                "message": (
                    "You have no calendar to pick from. Do not invent a time. Say "
                    "you will email them today to get them set, then end_call."
                ),
            }
        if self._booking_completed is not None:
            # The booking is already written (or in flight) and the caller has
            # already HEARD the time. Affirmative-by-default would answer "sure,
            # Wednesday" here, and Cal.com would still say Tuesday - the caller
            # walks away believing something untrue and nothing alerts. So this is
            # the one place the agent must not agree, and a human has to move it.
            await self._alert_operator_about_booking(
                intent_key=self._booked_intent_key,
                prospect=str(self.variables.get("leadName") or "the lead"),
                when=self._booked_when,
                reason="they asked to change the time after it was already booked",
                detail="Reschedule by hand: the call ended with the ORIGINAL time booked.",
            )
            return {
                "success": False,
                "error": "already_booked",
                "message": (
                    "That time is already through and cannot be changed from here. "
                    "Never agree to a swap. Say it plainly and warmly: 'that one's "
                    "already gone through - I'll get the team to move it and email "
                    "you the new time today.' Then one goodbye and end_call."
                ),
            }
        # A menu the caller was never read needs an unambiguous time of their own —
        # UNLESS the agent has now actually said some of those times out loud, which
        # is the moment it stops being a private list and becomes a real offer.
        if self.slots_offered_aloud():
            self._menu_announced = True
        if not self._menu_announced and not self._strong_time_signal():
            return {
                "success": False,
                "error": "ambiguous_slot_selection",
                "message": (
                    "They have not named a time clearly enough yet. Offer two of your "
                    "times out loud, naturally, and wait for them to pick one."
                ),
            }
        if self._user_turn <= max(self._offer_user_turn, self._selection_user_turn):
            return {
                "success": False,
                "error": "selection_not_heard",
                "message": (
                    "Their answer has not reached you yet. This is OUR timing, not "
                    "anything they did - so never apologise and never suggest they "
                    "were unclear. Say nothing about hearing them. Either wait, or "
                    "put the time you think they said back to them in a few words "
                    "('Tuesday at midday?'). Never say 'first or second', never "
                    "mention formats, systems, or tools."
                ),
            }
        offered = {slot["slot_id"]: slot for slot in self._offered_slots}
        candidates = self._utterance_slot_candidates()
        # The transcript CONSTRAINS the choice; it no longer dictates it. Demanding
        # that the words reduce to exactly one slot meant a caller who said
        # "evening" when we held five evening times was refused and asked to narrow
        # it down - over and over, on the 2026-08-18 ring test, until he gave up.
        # A rough answer is still an answer, so the model picks and this gate only
        # checks that the pick is something their words could have meant. Saying
        # nothing about a time still names nothing, and _refuses_or_defers above
        # still empties the set outright when the words carry a no.
        if slot_id not in offered or slot_id not in candidates:
            named, named_days = self._named_a_time(self._latest_user_utterance)
            # Three refusals wear one error today, and they need three different
            # sentences. This is the "they named something real and we do not hold
            # it" one - and it is the ONLY one where the agent may say a time is
            # gone. It sits after _refuses_or_defers (inside _utterance_slot_
            # candidates) and after selection_not_heard on purpose: "no, Tuesday at
            # ten doesn't work" parses a time and matches nothing, and rendering
            # THAT as "that one's gone" would be a lie about a time they had just
            # turned down. Do not reorder these.
            alternatives = self._two_alternatives(named_days)
            refused = self._refuses_or_defers(self._latest_user_utterance)
            if named and not refused and not candidates and alternatives:
                return {
                    "success": False,
                    "error": "slot_unavailable",
                    "message": (
                        "They named a time we do not hold. Say so plainly and give "
                        f"them what we DO have, in one line: 'that one's actually "
                        f"gone - I've got {alternatives}.' Never apologise for it, "
                        "never blame them, and never mention systems or calendars. "
                        "From here on you ARE looking things up, so 'let me check' "
                        "is finally honest if you need it."
                    ),
                }
            return {
                "success": False,
                "error": "ambiguous_slot_selection",
                "message": (
                    "They have not named a time yet. Never apologise and never say "
                    "you did not catch it - just name two of your times again the "
                    "way a person would ('was that the Tuesday at ten, or the one in "
                    "the afternoon?'). Never say 'first or second', never mention "
                    "formats, systems, or tools."
                ),
            }
        selected = offered[slot_id]
        self._selected_slot_id = slot_id
        self._selected_start = selected["start"]
        self._selection_user_turn = self._user_turn
        self._booking_attempts.append(
            {
                "operation": "select",
                "attempt": len(self._booking_attempts) + 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "category": "selected",
                "timezone": self._normalized_timezone,
                "turn": self._user_turn,
                "slot_id": slot_id,
                "selected_start": selected["start"],
            }
        )
        result: dict[str, Any] = {
            "success": True,
            "slot_id": slot_id,
            "start": selected["start"],
            "when": selected["label"],
        }
        if self._fit_answers:
            # Everything book_appointment needs is already held here, so the
            # session forces it as the very next thing and the model is never
            # handed a turn to narrate into. Three attempts to ASK for this
            # silence failed - two prompt bans and a plea in this very return -
            # because a model always finds a new phrase for a gap. The gap is
            # gone now instead. `next_tool` is stripped before the model sees
            # the result: it is an instruction to the session, not to the agent.
            result["next_tool"] = "book_appointment"
        else:
            # The caller jumped straight to a time before the fit questions. Here
            # speaking IS the right move - say the time back and bridge into the
            # question in one turn - so nothing is forced.
            result["message"] = (
                "Pinned, not booked, and you still owe them a fit question. Say "
                "the time back and go straight into the question you have not "
                "asked, in ONE turn. Nothing about booking yet."
            )
        return result

    @staticmethod
    def get_tool_definitions() -> list[dict[str, Any]]:
        """Get OpenAI function calling tool definitions.

        Returns:
            List of tool definitions for GPT Realtime API (uses nested function format)
        """
        return [
            {
                "type": "function",
                "name": "search_customer",
                "description": "Search for a customer by phone number, email, or name",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Phone number, email, or name to search for",
                        },
                    },
                    "required": ["query"],
                },
            },
            {
                "type": "function",
                "name": "create_contact",
                "description": "Create a new contact/customer in the CRM. REQUIRED: first_name and phone_number. OPTIONAL: last_name, email, company_name. Do NOT ask for optional fields unless the customer volunteers the information.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "first_name": {
                            "type": "string",
                            "description": "REQUIRED. Customer's first name. Cannot be empty.",
                        },
                        "phone_number": {
                            "type": "string",
                            "description": "REQUIRED. Customer's phone number (7-20 digits). Format: digits only or E.164 format.",
                        },
                        "last_name": {
                            "type": "string",
                            "description": "OPTIONAL. Customer's last name. Only collect if volunteered.",
                        },
                        "email": {
                            "type": "string",
                            "description": "OPTIONAL. Customer's email address. Only collect if volunteered.",
                        },
                        "company_name": {
                            "type": "string",
                            "description": "OPTIONAL. Company or organization name. Only collect if volunteered.",
                        },
                    },
                    "required": ["first_name", "phone_number"],
                },
            },
            {
                "type": "function",
                "name": "refresh_availability",
                "description": "Re-read the calendar. You normally do NOT need this: the open times are already listed in your instructions, and they are the whole week. Call it ONLY if the lead's timezone turns out to be different from the one your listed times are in. A time you asked for and could not have is NOT a reason to call this - you were already told what to offer instead.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "time_zone": {
                            "type": "string",
                            "description": "The lead's IANA timezone as they stated it (e.g. Europe/Stockholm, America/New_York). The refreshed times come back in this timezone.",
                        },
                        "date": {
                            "type": "string",
                            "description": "Optional preferred date (YYYY-MM-DD) if the lead asked for one.",
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "description": "Desired appointment duration in minutes (default 30)",
                        },
                    },
                    "required": [],
                },
            },
            {
                "type": "function",
                "name": "select_slot",
                "description": (
                    "Lock in one of your listed times, after the lead clearly names it. Pass "
                    "the id shown next to that time. Call it as the FIRST thing you do on that "
                    "turn, with nothing spoken before it: it answers instantly and you speak "
                    "the moment it does, so the lead hears no gap and needs no warning."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "slot_id": {
                            "type": "string",
                            "description": "The id shown beside the time in your listed availability, such as slot_1 or slot_4.",
                        },
                    },
                    "required": ["slot_id"],
                },
            },
            {
                "type": "function",
                "name": "record_fit_answers",
                "description": (
                    "Save what the lead has told you about their business so far - the kind of "
                    "installs they take on and/or the areas they cover - the moment they answer, "
                    "before you ever offer a time. This is what keeps their answers for the team "
                    "even if the call ends before booking. Call it again for anything they add or "
                    "correct later; it adds to what is already saved, never wipes it out. Only pass "
                    "a field they actually answered - never guess or fill in one they did not "
                    "address. Call it as the FIRST thing you do on that turn, with nothing spoken "
                    "before it: it answers instantly and you speak the moment it does, so the "
                    "lead hears no gap and needs no warning."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "offer_types": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "What they install/sell, in their own words, e.g. ['rooftop', 'ground-mount', 'carports'].",
                        },
                        "min_kw": {
                            "type": "number",
                            "description": "The commercial system size they take on, in kW. Asked every call.",
                        },
                        "states": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "The exact counties they sell into, in their own words. Counties, not states, whenever they give them.",
                        },
                    },
                    "required": [],
                },
            },
            {
                "type": "function",
                "name": "book_appointment",
                "description": (
                    "Book the time select_slot pinned. Takes no arguments: the time, the fit "
                    "answers, the attendee name and the email on file are all filled in for you "
                    "- pass email only if the lead volunteers a correction. Nothing about the "
                    "booking is true until this returns."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "email": {
                            "type": "string",
                            "description": "Optional corrected email volunteered by the lead. Otherwise the email on file is used silently.",
                        },
                        "time_zone": {
                            "type": "string",
                            "description": "The lead's IANA timezone (e.g. Europe/Stockholm). Optional.",
                        },
                        "notes": {
                            "type": "string",
                            "description": "Notes for the team: what they said about their business - the installs they take on, the areas they cover, and anything else they volunteered.",
                        },
                        "contact_phone": {
                            "type": "string",
                            "description": "Optional - only used by the internal calendar fallback.",
                        },
                        "duration_minutes": {
                            "type": "integer",
                            "description": "Duration in minutes (default 30)",
                        },
                        "service_type": {
                            "type": "string",
                            "description": "Type of service/appointment",
                        },
                    },
                    "required": [],
                },
            },
            {
                "type": "function",
                "name": "list_appointments",
                "description": "List upcoming appointments, optionally filtered by date or contact",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "contact_phone": {
                            "type": "string",
                            "description": "Filter by customer phone number",
                        },
                        "start_date": {
                            "type": "string",
                            "description": "Start date in YYYY-MM-DD format",
                        },
                        "end_date": {
                            "type": "string",
                            "description": "End date in YYYY-MM-DD format",
                        },
                        "status": {
                            "type": "string",
                            "description": "Filter by status (scheduled, completed, cancelled, no_show)",
                        },
                    },
                    "required": [],
                },
            },
            {
                "type": "function",
                "name": "cancel_appointment",
                "description": "Cancel an existing appointment",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {
                            "type": "integer",
                            "description": "Appointment ID to cancel",
                        },
                        "reason": {"type": "string", "description": "Cancellation reason"},
                    },
                    "required": ["appointment_id"],
                },
            },
            {
                "type": "function",
                "name": "reschedule_appointment",
                "description": "Reschedule an existing appointment to a new time",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "appointment_id": {
                            "type": "integer",
                            "description": "Appointment ID to reschedule",
                        },
                        "new_scheduled_at": {
                            "type": "string",
                            "description": "New appointment time in ISO 8601 format",
                        },
                    },
                    "required": ["appointment_id", "new_scheduled_at"],
                },
            },
        ]

    async def search_customer(self, query: str) -> dict[str, Any]:
        """Search for a customer by phone, email, or name.

        Args:
            query: Search query

        Returns:
            Customer information or error
        """
        try:
            # Search by phone, email, or name - filtered by workspace_id for proper scoping
            # Falls back to user_id if workspace_id not available (backward compatibility)
            # Also search full name (first + last) for queries like "John Smith"
            full_name = func.concat(Contact.first_name, " ", func.coalesce(Contact.last_name, ""))

            # Build base query with search conditions
            search_conditions = (
                (Contact.phone_number.ilike(f"%{query}%"))
                | (Contact.email.ilike(f"%{query}%"))
                | (Contact.first_name.ilike(f"%{query}%"))
                | (Contact.last_name.ilike(f"%{query}%"))
                | (full_name.ilike(f"%{query}%"))
            )

            # Scope by workspace if available, otherwise by user
            if self.workspace_id:
                stmt = select(Contact).where(
                    Contact.workspace_id == self.workspace_id,
                    search_conditions,
                )
            else:
                stmt = select(Contact).where(
                    Contact.user_id == self.user_id,
                    search_conditions,
                )

            result = await self.db.execute(stmt)
            contacts = list(result.scalars().all())

            if not contacts:
                return {
                    "success": True,
                    "found": False,
                    "message": f"No customer found matching '{query}'",
                }

            # Return first match (or all if multiple)
            customer_data = [
                {
                    "id": c.id,
                    "name": f"{c.first_name} {c.last_name or ''}".strip(),
                    "phone": c.phone_number,
                    "email": c.email,
                    "company": c.company_name,
                    "status": c.status,
                }
                for c in contacts[:3]  # Limit to 3 results
            ]

            return {
                "success": True,
                "found": True,
                "count": len(customer_data),
                "customers": customer_data,
            }

        except Exception as e:
            self.logger.exception("search_customer_failed", query=query, error=str(e))
            return {"success": False, "error": str(e)}

    async def create_contact(
        self,
        first_name: str,
        phone_number: str,
        last_name: str | None = None,
        email: str | None = None,
        company_name: str | None = None,
    ) -> dict[str, Any]:
        """Create a new contact.

        Args:
            first_name: First name
            phone_number: Phone number
            last_name: Last name
            email: Email
            company_name: Company

        Returns:
            Created contact info
        """
        try:
            contact = Contact(
                user_id=self.user_id,
                workspace_id=self.workspace_id,
                first_name=first_name,
                last_name=last_name,
                phone_number=phone_number,
                email=email,
                company_name=company_name,
                status="new",
            )

            self.db.add(contact)
            await self.db.commit()
            await self.db.refresh(contact)

            # Invalidate CRM caches so new contacts appear immediately in the UI
            try:
                await cache_invalidate(f"crm:contacts:list:{self.user_id}:*")
                await cache_invalidate("crm:stats:*")
                self.logger.debug("invalidated_crm_cache_after_create_contact")
            except Exception:
                self.logger.exception("failed_to_invalidate_cache_after_create_contact")

            return {
                "success": True,
                "contact_id": contact.id,
                "message": f"Created contact for {first_name} {last_name or ''}",
            }

        except Exception as e:
            self.logger.exception("create_contact_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def check_availability(  # noqa: PLR0911, PLR0912
        self,
        date: str | None = None,
        duration_minutes: int = 30,  # noqa: ARG002
        time_zone: str | None = None,
    ) -> dict[str, Any]:
        """Check available time slots.

        When Cal.com is configured, returns the next business-hours openings on
        upcoming weekdays in the lead's timezone (single source of truth, no
        double-book). Otherwise falls back to the internal calendar for `date`.

        Args:
            date: Optional preferred date (YYYY-MM-DD) - internal fallback only
            duration_minutes: Desired duration (reserved for future use)
            time_zone: The lead's IANA timezone for returned slots

        Returns:
            Available time slots
        """
        # --- Cal.com path (preferred) ---
        if self._calcom_enabled():
            from app.services.calcom_client import normalize_timezone

            spoken_timezone = str(time_zone or "").strip()
            if self._timezone_clarification_required and not spoken_timezone:
                return {
                    "success": False,
                    "error": "timezone_unresolved",
                    "message": "Use the caller's clarified US state or standard time zone; do not fall back.",
                }
            lead_tz = normalize_timezone(
                spoken=time_zone,
                fallback=self.variables.get("tzName"),
                team_default=settings.BOOKING_TEAM_TIMEZONE,
            )
            if lead_tz is None:
                self._timezone_clarification_required = True
                await self._invalidate_unresolved_timezone()
                self._normalized_timezone = None
                return {
                    "success": False,
                    "error": "timezone_unresolved",
                    "message": "Ask once for their US state or standard time zone, such as Eastern or Pacific.",
                }
            try:
                # Same menu shape the pre-call load produces, so a refresh mid-call
                # (their timezone differed, or a slot was taken) replaces the menu
                # like-for-like instead of shrinking it to two times.
                menu = await self._load_availability_menu(lead_tz, origin="offered")
                if menu is None:
                    return {
                        "success": False,
                        "error": "availability_superseded",
                        "message": "Use the newer calendar refresh result.",
                    }
                if spoken_timezone:
                    self.variables["tzName"] = lead_tz
                    self._timezone_clarification_required = False
                if menu["status"] == "unavailable":
                    return {"success": False, "error": "calendar_unavailable"}
                if menu["status"] == "empty":
                    return {
                        "success": True,
                        "timezone": menu["timezone"],
                        "slots": [],
                        "menu": menu["block"],
                        "message": "No open business-hours slots in the next two weeks - ask the lead for a preferred day.",
                    }
                if not self._offered_slots:
                    return {"success": False, "error": "calendar_unavailable"}
                return {
                    "success": True,
                    "timezone": menu["timezone"],
                    "slots": [
                        {"slot_id": s["slot_id"], "when": s["label"], "start": s["start"]}
                        for s in self._offered_slots
                    ],
                    "menu": menu["block"],
                    "message": "This is the calendar now. Answer whatever they asked for from it, hear a clear choice, then call select_slot with its slot_id.",
                }
            except Exception as e:
                self._offered_slots = []
                self._selected_slot_id = None
                self._selected_start = None
                self.logger.exception(
                    "calcom_check_availability_failed", error_type=type(e).__name__
                )
                return {"success": False, "error": "calendar_unavailable"}

        if self._requires_calcom:
            self._offered_slots = []
            self._selected_slot_id = None
            self._selected_start = None
            return {"success": False, "error": "calendar_unavailable"}

        # --- Internal calendar fallback ---
        try:
            # Default to tomorrow if no date given
            if date:
                target_date = datetime.strptime(date, "%Y-%m-%d").date()
            else:
                target_date = (datetime.now() + timedelta(days=1)).date()

            # Get existing appointments for that day - filtered by workspace or user
            base_stmt = (
                select(Appointment)
                .join(Contact)
                .where(
                    Appointment.scheduled_at >= datetime.combine(target_date, datetime.min.time()),
                    Appointment.scheduled_at < datetime.combine(target_date, datetime.max.time()),
                    Appointment.status == "scheduled",
                )
            )

            if self.workspace_id:
                stmt = base_stmt.where(Contact.workspace_id == self.workspace_id)
            else:
                stmt = base_stmt.where(Contact.user_id == self.user_id)

            result = await self.db.execute(stmt)
            booked_appointments = list(result.scalars().all())

            # Simple availability: 9 AM to 5 PM, hourly slots
            available_slots = []
            for hour in range(9, 17):  # 9 AM to 5 PM
                slot_time = datetime.combine(target_date, datetime.min.time()).replace(hour=hour)

                # Check if slot conflicts with existing appointments
                is_available = True
                for apt in booked_appointments:
                    if apt.scheduled_at.hour == hour:
                        is_available = False
                        break

                if is_available:
                    available_slots.append(slot_time.isoformat())

            return {
                "success": True,
                "date": date,
                "available_slots": available_slots,
                "total_available": len(available_slots),
            }

        except Exception as e:
            self.logger.exception("check_availability_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def _alert_operator_about_booking(
        self,
        *,
        intent_key: str,
        prospect: str,
        when: str,
        reason: str,
        detail: str = "",
    ) -> None:
        """Tell a human, because the agent already promised this time out loud.

        The old code answered every calendar failure by handing the model a line
        to say. That option is gone by design - the caller has heard "you're
        booked" and the call has usually ended - so the recovery moves from the
        conversation to Sami. The alert names the prospect and the exact time,
        because "a booking failed" is not actionable and this is the only signal
        anyone gets.

        `prospect` and `when` are PASSED IN, never read off the session. [Codex]
        found the earlier version reading `self._pending_booking_*`, which two
        overlapping bookings on one session would overwrite - so the alert could
        name the wrong person and the wrong time, sending the operator to fix a
        booking that was never broken. The detached write already receives both
        values as arguments; there was never a reason to consult shared state.
        """
        message = (
            f"{prospect or 'a prospect'} was told on the phone that "
            f"{when or 'the time they chose'} was booked, and the calendar write did "
            f"not go through ({reason}). They are expecting an invite that does not "
            f"exist - this needs booking by hand and an email to them."
        )
        if detail:
            message = f"{message} {detail}"
        self.logger.error(
            "booking_promised_but_not_written", intent_key=intent_key, reason=reason
        )
        await raise_operator_alert(
            dedup_key=f"voice-booking-unconfirmed:{intent_key}", message=message
        )

    async def _write_booking_to_calendar(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        *,
        intent_key: str | None,
        booking_claim_token: uuid.UUID | None,
        fulfilment_skipped: bool = False,
        selected_start: str,
        when_spoken: str,
        name: str,
        attendee_email: str,
        lead_tz: str,
        full_notes: str,
    ) -> None:
        """Write the booking to Cal.com after the agent has already confirmed it.

        Detached from the conversation, so nothing here can return a message to the
        model: by the time it runs the caller has been told the time is theirs and
        the agent has moved on. Every path that used to hand the agent a recovery
        line therefore ends in an operator alert instead.

        The idempotency machinery is unchanged and still carries its full weight -
        reconcile before the first POST, at most one create, reconcile again after
        a transient. A repeat call after a reconnect or a redeploy must never
        produce a second booking for the same attendee and start.

        Everything it needs is an ARGUMENT. Nothing is read off the session, so a
        second booking on the same session cannot make this one alert about the
        wrong prospect.
        """
        booking_dispatched = False
        log = self.logger.bind(intent_key=intent_key, selected_start=selected_start)
        try:
            from app.services.calcom_client import create_booking, find_existing_booking

            booking_result: dict[str, Any] = {}
            selected_attempts = [
                attempt
                for attempt in self._booking_attempts
                if attempt.get("operation") == "create"
                and attempt.get("selected_start") == selected_start
            ]
            prior_count = len(selected_attempts)
            if selected_attempts:
                last_category = selected_attempts[-1].get("category")
                if last_category == "rejected":
                    await self._alert_operator_about_booking(
                        intent_key=intent_key,
                        prospect=attendee_email,
                        when=when_spoken,
                        reason="the calendar rejected it earlier",
                    )
                    return
                if last_category == "transient":
                    booking_result = await find_existing_booking(
                        start_iso=selected_start, email=attendee_email
                    )
                    self._record_booking_attempt(
                        "reconcile", selected_start, lead_tz, booking_result
                    )
                    if booking_result.get("success"):
                        prior_count = MAX_BOOKING_ATTEMPTS
                    elif booking_result.get("category") == "reconcile_unavailable":
                        await self._alert_operator_about_booking(
                            intent_key=intent_key,
                            prospect=attendee_email,
                            when=when_spoken,
                            reason="the calendar could not be checked safely",
                        )
                        return
                    elif prior_count >= MAX_BOOKING_ATTEMPTS:
                        await self._alert_operator_about_booking(
                            intent_key=intent_key,
                            prospect=attendee_email,
                            when=when_spoken,
                            reason="the calendar kept failing",
                        )
                        return

            # Reconcile before the first POST as well as after an unknown one. This
            # closes the process/session boundary: a repeated call after reconnect
            # or redeploy cannot create a second booking for the same attendee and
            # exact start.
            if prior_count == 0 and not booking_result:
                booking_result = await find_existing_booking(
                    start_iso=selected_start, email=attendee_email
                )
                self._record_booking_attempt(
                    "reconcile", selected_start, lead_tz, booking_result
                )
                if booking_result.get("success"):
                    prior_count = MAX_BOOKING_ATTEMPTS
                elif booking_result.get("category") == "reconcile_unavailable":
                    await self._alert_operator_about_booking(
                        intent_key=intent_key,
                        prospect=attendee_email,
                        when=when_spoken,
                        reason="the calendar could not be checked safely",
                    )
                    return

            if not booking_result.get("success"):
                # A null claim token used to mean one thing: we lost the race for
                # the booking lease, so stop. Since 2026-08-27 it can also mean we
                # deliberately took no lease because this is our own test call —
                # and reading the second as the first made the agent promise Sami
                # "Thursday at six is set" while the write silently gave up. Two
                # meanings, one null. They have separate names now.
                if booking_claim_token is None and not fulfilment_skipped:
                    await self._alert_operator_about_booking(
                        intent_key=intent_key,
                        prospect=attendee_email,
                        when=when_spoken,
                        reason="another attempt already held the booking lease",
                    )
                    return
                booking_dispatched = fulfilment_skipped or (
                    await authorize_fulfilment_booking(intent_key, booking_claim_token)
                )
                if not booking_dispatched:
                    await self._alert_operator_about_booking(
                        intent_key=intent_key,
                        prospect=attendee_email,
                        when=when_spoken,
                        reason="the booking lease changed mid-write",
                    )
                    return

            # A Cal.com create is non-idempotent. Once its durable dispatch marker
            # is set, every later attempt is reconciliation-only.
            remaining_attempts = min(1, MAX_BOOKING_ATTEMPTS - prior_count)
            for local_attempt in range(remaining_attempts):
                booking_result = await create_booking(
                    start_iso=selected_start,
                    name=name,
                    email=attendee_email,
                    lead_tz=lead_tz,
                    notes=full_notes or None,
                )
                self._record_booking_attempt(
                    "create", selected_start, lead_tz, booking_result, default="rejected"
                )
                if booking_result.get("category") != "transient":
                    break
                reconciliation = await find_existing_booking(
                    start_iso=selected_start, email=attendee_email
                )
                self._record_booking_attempt(
                    "reconcile", selected_start, lead_tz, reconciliation
                )
                if reconciliation.get("success") or (
                    reconciliation.get("category") == "reconcile_unavailable"
                ):
                    booking_result = reconciliation
                    break
                if local_attempt < remaining_attempts - 1:
                    await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            # A Railway redeploy sends SIGTERM, which cancels this task. [Codex]
            # found that CancelledError is a BaseException, so `except Exception`
            # below let it straight through: the caller had been told they were
            # booked, the lease said a write was dispatched, and NOTHING alerted.
            # The prospect would have found out by not receiving an invite.
            log.warning("calendar_write_cancelled_mid_flight")
            with contextlib.suppress(Exception):
                await asyncio.shield(
                    self._alert_operator_about_booking(
                        intent_key=intent_key,
                        prospect=attendee_email,
                        when=when_spoken,
                        reason="the server restarted part-way through the write",
                    )
                )
            raise
        except Exception as exc:
            log.exception("calcom_book_failed", error_type=type(exc).__name__)
            await self._alert_operator_about_booking(
                intent_key=intent_key,
                prospect=attendee_email,
                when=when_spoken,
                reason="the calendar call raised an error",
            )
            return

        if booking_result.get("success"):
            booking_id = booking_result.get("uid")
            try:
                if not await finalize_fulfilment_intent(intent_key, booking_id):
                    log.error("fulfilment_intent_missing_after_booking")
            except ExtraBookingConflictError as exc:
                log.exception(
                    "calcom_extra_booking_requires_manual_reconciliation",
                    booking_id=booking_id,
                    error=str(exc),
                )
            except Exception:
                # The pre-booking intent is already durable. Its worker can
                # reconcile this exact attendee/slot after a commit-unknown
                # finalization, so never treat this as a lost booking.
                log.exception("fulfilment_intent_finalize_failed")
            log.info("calendar_write_landed_after_the_agent_said_so", uid=booking_id)
            return

        # Everything below here means the caller was told a time that is not on the
        # calendar. Conflict included: the old code re-offered fresh times, which is
        # no longer possible or wanted - the agent has already committed.
        reasons = {
            "conflict": "that time was taken between the menu being built and the write",
            "rejected": "the calendar refused it",
            "reconcile_unavailable": "the calendar response was uncertain",
            "transient": "the calendar response was uncertain",
        }
        category = str(booking_result.get("category") or "")
        await self._alert_operator_about_booking(
            intent_key=intent_key,
            prospect=attendee_email,
            when=when_spoken,
            reason=reasons.get(category, "the calendar write did not complete"),
        )

    def _record_booking_attempt(
        self,
        operation: str,
        selected_start: str,
        lead_tz: str,
        result: dict[str, Any],
        *,
        default: str | None = None,
    ) -> None:
        """Append one calendar attempt to the record the call is persisted with."""
        self._booking_attempts.append(
            {
                "operation": operation,
                "attempt": len(self._booking_attempts) + 1,
                "timestamp": datetime.now(UTC).isoformat(),
                "selected_start": selected_start,
                "timezone": lead_tz,
                "category": result.get("category", default)
                if default
                else result.get("category"),
                "status_code": result.get("status_code"),
                "uid": result.get("uid") if result.get("success") else None,
                "raw_body": str(result.get("raw_body") or "")[:1000],
            }
        )

    async def book_appointment(  # noqa: PLR0911, PLR0912, PLR0915
        self,
        scheduled_at: str | None = None,
        email: str | None = None,
        icp: dict[str, Any] | str | None = None,
        contact_phone: str | None = None,
        duration_minutes: int = 30,
        service_type: str | None = None,
        notes: str | None = None,
        time_zone: str | None = None,
    ) -> dict[str, Any]:
        """Book an appointment.

        When Cal.com is configured, a current transcript-bound selected slot is
        mandatory. ICP comes from this call; email uses a volunteered correction or
        silently falls back to the seeded address on file.
        Otherwise falls back to the internal calendar (phone-based).

        Args:
            scheduled_at: Optional. Defaults to the slot select_slot pinned, which
                is the only time this may ever book. Older callers still pass it.
            email: Optional corrected email volunteered on the call
            icp: Optional. Defaults to whatever record_fit_answers already saved.
            contact_phone: Customer phone (internal fallback only)
            duration_minutes: Duration
            service_type: Service type
            notes: Notes for the team - what they said about their business
            time_zone: Lead's IANA timezone

        Returns:
            Booking confirmation
        """
        # --- Cal.com path (preferred) ---
        if self._calcom_enabled():
            del (
                time_zone
            )  # Deliberately ignored: booking must reuse the stored normalized timezone.
            if self._booking_completed is not None:
                # Reaching here AT ALL means the booking already succeeded and the
                # agent has already been told to say the time once. Replaying that
                # same message was our own tool instructing the repeat the prompt
                # spends a line forbidding ("never read a time back twice") — and a
                # rule the code argues with is not a rule.
                return {
                    "success": True,
                    "message": (
                        "Already booked, and you have already told them. Do NOT say "
                        "the time again. One short goodbye, then end_call."
                    ),
                }
            if not self._offered_slots:
                return {"success": False, "error": "slots_not_offered"}
            if not self._selected_start or not self._selected_slot_id:
                return {
                    "success": False,
                    "error": "slot_not_selected",
                    "message": (
                        "Nothing is pinned yet. Offer two of your times out loud and "
                        "wait for them to pick. Say nothing about booking."
                    ),
                }
            # Both arguments are now derived, not asked for. A tool the model
            # cannot fill in wrongly is a tool that can be FORCED, and forcing it
            # is what finally removes the gap it used to narrate into.
            if not icp:
                icp = deepcopy(self._fit_answers)
            supplied_start = self._canonical_start(scheduled_at or self._selected_start)
            selected_start = self._canonical_start(self._selected_start)
            if supplied_start is None or selected_start is None or supplied_start != selected_start:
                return {
                    "success": False,
                    "error": "slot_mismatch",
                    "message": (
                        "That is not the time that was pinned. Call select_slot for "
                        "the time they actually said, then book. Say nothing yet."
                    ),
                }

            name = (self.variables.get("leadName") or "").strip() or "Guest"
            attendee_email = (email or "").strip() or str(
                self.variables.get("leadEmail") or ""
            ).strip()
            lead_tz = self._normalized_timezone or "UTC"

            if (
                not attendee_email
                or "{{" in attendee_email
                or "}}" in attendee_email
                or attendee_email.lower() in {"none", "null", "n/a", "unknown"}
            ):
                self.logger.warning("calcom_book_missing_email")
                return {
                    "success": False,
                    "error": "missing_email",
                    "message": "Ask for an email once, then call book_appointment again with it.",
                }
            if not icp:
                self.logger.warning("calcom_book_missing_icp")
                return {
                    "success": False,
                    "error": "missing_icp",
                    "message": "Ask the lead the two fit questions (what kind of installs they take on, and which areas they cover) before booking, then call book_appointment again with icp filled in.",
                }

            try:
                fulfilment_icp = _normalize_fulfilment_icp(icp)
            except (TypeError, ValueError) as exc:
                self.logger.warning(
                    "calcom_book_invalid_icp", error_type=type(exc).__name__
                )
                return {
                    "success": False,
                    "error": "invalid_icp",
                    "message": (
                        "Ask the two fit questions again, then call book_appointment with "
                        "offer_types and states as lists, plus min_kw as a number."
                    ),
                }

            # Captured before the handover nulls the live selection: the background
            # write must book the time the caller actually chose, not whatever the
            # session state has become by the time it runs.
            selected_start_iso = self._selected_start
            when_spoken = next(
                (
                    str(slot.get("label") or "")
                    for slot in self._offered_slots
                    if slot.get("slot_id") == self._selected_slot_id
                ),
                "",
            )

            icp_str = json.dumps(fulfilment_icp, ensure_ascii=False)
            full_notes = notes or ""
            if service_type:
                full_notes = f"{service_type}. {full_notes}".strip()
            full_notes = f"{full_notes}\nICP: {icp_str}".strip()

            conversation_id = self.variables.get("conversation_id") or self.variables.get(
                "conversationId"
            )
            conversation_generation = self.variables.get(
                "conversation_generation"
            ) or self.variables.get("conversationGeneration")

            fulfilment_payload = {
                "name": name,
                "company": str(self.variables.get("company") or ""),
                "email": attendee_email,
                "phone": self.variables.get("leadPhone") or self.variables.get("phone"),
                "icp": fulfilment_icp,
                "campaign_id": self.variables.get("campaign_id")
                or self.variables.get("campaignId"),
                "conversation_id": conversation_id,
            }
            promise_key = _fulfilment_promise_key(conversation_id, conversation_generation)
            if promise_key is not None:
                fulfilment_payload["promise_key"] = promise_key

            fulfilment_skipped = False
            if is_test_conversation(conversation_id):
                # Our own test call. Book it - that is the whole point of a ring
                # test - but never stage paid work. A null intent key is already
                # understood downstream as "there is nothing to ship".
                self.logger.warning(
                    "fulfilment_skipped_test_conversation",
                    conversation_id=str(conversation_id),
                )
                intent_key = None
                booking_claim_token = None
                fulfilment_skipped = True
            else:
                try:
                    intent_key = await stage_fulfilment_intent(
                        start_iso=selected_start_iso,
                        email=attendee_email,
                        payload=fulfilment_payload,
                        workspace_id=self.workspace_id,
                        user_id=self.user_id,
                    )
                    booking_claim_token = await claim_fulfilment_booking(intent_key)
                except Exception as exc:
                    self.logger.exception(
                        "fulfilment_intent_stage_failed",
                        error_type=type(exc).__name__,
                    )
                    return {
                        "success": False,
                        "error": "fulfilment_unavailable",
                        "message": (
                            "The booking system is unavailable. Do not retry this "
                            "time; tell them the team will confirm by email."
                        ),
                    }

            # THE HANDOVER. Everything above this line was local: the slot was
            # already validated against the loaded menu by select_slot, and
            # stage_fulfilment_intent has just made the promise durable. All that
            # is left is Cal.com's own write, which measured SEVEN SECONDS on the
            # 2026-08-08 call - seven seconds in which the prompt claimed "every
            # tool answers instantly", the model correctly sensed the real silence,
            # and filled it with "our booking step is still processing in the
            # background, so give it a moment". Sami: "Who said I want to give it a
            # moment?"
            #
            # His ruling, 2026-08-09: "he should just trigger the tool to book and
            # then just not wait for it... immediately he says something along the
            # lines of 'sounds good Sami, Wednesday at midday it is' and then he
            # immediately moves on." So the tool answers now and the calendar
            # catches up. A failure after this point cannot lose the lead - the
            # intent is on disk and the alert below names the prospect and the
            # exact time promised.
            self._selected_slot_id = None
            self._selected_start = None
            self._booked_when = when_spoken
            self._booked_intent_key = intent_key or ""
            self._booking_completed = {
                "success": True,
                "message": (
                    "Booked. Say the day and time back to them ONCE in a short line, "
                    "give ONE warm goodbye, then call end_call."
                ),
            }
            task = asyncio.create_task(
                self._write_booking_to_calendar(
                    intent_key=intent_key,
                    booking_claim_token=booking_claim_token,
                    fulfilment_skipped=fulfilment_skipped,
                    selected_start=selected_start_iso,
                    when_spoken=when_spoken,
                    name=name,
                    attendee_email=attendee_email,
                    lead_tz=lead_tz,
                    full_notes=full_notes,
                )
            )
            # A task with no strong reference can be garbage-collected mid-flight.
            # The module-global set is what keeps it alive and lets shutdown find
            # it; the per-session set is what teardown waits on, so one call never
            # blocks on another prospect's write.
            for registry in (_CALENDAR_WRITES, self._calendar_writes):
                registry.add(task)
            task.add_done_callback(_CALENDAR_WRITES.discard)
            task.add_done_callback(self._calendar_writes.discard)
            return deepcopy(self._booking_completed)
        if self._requires_calcom:
            return {"success": False, "error": "calendar_unavailable"}


        # --- Internal calendar fallback (phone-based) ---
        if not contact_phone:
            return {"success": False, "error": "contact_phone required for internal booking"}
        try:
            # Find contact - filtered by workspace or user for security
            if self.workspace_id:
                stmt = select(Contact).where(
                    Contact.workspace_id == self.workspace_id,
                    Contact.phone_number == contact_phone,
                )
            else:
                stmt = select(Contact).where(
                    Contact.user_id == self.user_id,
                    Contact.phone_number == contact_phone,
                )
            result = await self.db.execute(stmt)
            contact = result.scalar_one_or_none()

            if not contact:
                return {
                    "success": False,
                    "error": f"No contact found with phone {contact_phone}. Please create contact first.",
                }

            # Parse datetime and handle timezone
            appointment_time = datetime.fromisoformat(scheduled_at.replace("Z", "+00:00"))

            # If datetime is naive (no timezone), interpret it in workspace timezone
            if appointment_time.tzinfo is None and self.workspace_id:
                from zoneinfo import ZoneInfo

                from app.models.workspace import Workspace

                # Get workspace timezone
                ws_result = await self.db.execute(
                    select(Workspace).where(Workspace.id == self.workspace_id)
                )
                workspace = ws_result.scalar_one_or_none()
                if workspace and workspace.settings:
                    tz_name = workspace.settings.get("timezone", "UTC")
                    try:
                        tz = ZoneInfo(tz_name)
                        # Interpret the naive datetime as being in workspace timezone
                        appointment_time = appointment_time.replace(tzinfo=tz)
                        self.logger.info(
                            "interpreted_naive_datetime",
                            original=scheduled_at,
                            timezone=tz_name,
                            result=appointment_time.isoformat(),
                        )
                    except Exception as tz_error:
                        self.logger.warning(
                            "timezone_conversion_failed",
                            timezone=tz_name,
                            error=str(tz_error),
                        )

            # Create appointment (inherit workspace_id from contact)
            appointment = Appointment(
                contact_id=contact.id,
                workspace_id=contact.workspace_id,
                scheduled_at=appointment_time,
                duration_minutes=duration_minutes,
                service_type=service_type,
                notes=notes,
                status="scheduled",
            )

            self.db.add(appointment)
            await self.db.commit()
            await self.db.refresh(appointment)

            # Invalidate CRM stats cache after booking
            try:
                await cache_invalidate("crm:stats:*")
                self.logger.debug("invalidated_crm_cache_after_book_appointment")
            except Exception:
                self.logger.exception("failed_to_invalidate_cache_after_book_appointment")

            return {
                "success": True,
                "appointment_id": appointment.id,
                "customer_name": f"{contact.first_name} {contact.last_name or ''}",
                "scheduled_at": appointment.scheduled_at.isoformat(),
                "duration_minutes": appointment.duration_minutes,
                "message": f"Appointment booked for {contact.first_name} on {appointment.scheduled_at.strftime('%B %d at %I:%M %p')}",
            }

        except Exception as e:
            self.logger.exception("book_appointment_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def list_appointments(
        self,
        contact_phone: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """List appointments with optional filters.

        Args:
            contact_phone: Filter by phone
            start_date: Start date filter
            end_date: End date filter
            status: Status filter

        Returns:
            List of appointments
        """
        try:
            # Use selectinload to eagerly load contacts in a single query (fixes N+1)
            # Filter by workspace or user for security
            base_stmt = select(Appointment).join(Contact).options(selectinload(Appointment.contact))

            if self.workspace_id:
                stmt = base_stmt.where(Contact.workspace_id == self.workspace_id)
            else:
                stmt = base_stmt.where(Contact.user_id == self.user_id)

            # Apply filters
            if contact_phone:
                stmt = stmt.where(Contact.phone_number == contact_phone)

            if start_date:
                start_dt = datetime.strptime(start_date, "%Y-%m-%d")
                stmt = stmt.where(Appointment.scheduled_at >= start_dt)

            if end_date:
                end_dt = datetime.strptime(end_date, "%Y-%m-%d")
                stmt = stmt.where(Appointment.scheduled_at <= end_dt)

            if status:
                stmt = stmt.where(Appointment.status == status)
            else:
                stmt = stmt.where(Appointment.status == "scheduled")

            stmt = stmt.order_by(Appointment.scheduled_at)

            result = await self.db.execute(stmt)
            appointments = list(result.scalars().all())

            # Contact is already loaded via selectinload - no additional queries needed
            appointment_list = [
                {
                    "id": apt.id,
                    "customer_name": f"{apt.contact.first_name} {apt.contact.last_name or ''}",
                    "phone": apt.contact.phone_number,
                    "scheduled_at": apt.scheduled_at.isoformat(),
                    "duration_minutes": apt.duration_minutes,
                    "service_type": apt.service_type,
                    "status": apt.status,
                }
                for apt in appointments
            ]

            return {
                "success": True,
                "total": len(appointment_list),
                "appointments": appointment_list,
            }

        except Exception as e:
            self.logger.exception("list_appointments_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def cancel_appointment(
        self, appointment_id: int, reason: str | None = None
    ) -> dict[str, Any]:
        """Cancel an appointment.

        Args:
            appointment_id: Appointment ID
            reason: Cancellation reason

        Returns:
            Cancellation confirmation
        """
        try:
            # Verify appointment belongs to user's workspace/contact
            base_stmt = select(Appointment).join(Contact).where(Appointment.id == appointment_id)

            if self.workspace_id:
                stmt = base_stmt.where(Contact.workspace_id == self.workspace_id)
            else:
                stmt = base_stmt.where(Contact.user_id == self.user_id)

            result = await self.db.execute(stmt)
            appointment = result.scalar_one_or_none()

            if not appointment:
                return {
                    "success": False,
                    "error": f"Appointment {appointment_id} not found",
                }

            # Update status
            appointment.status = "cancelled"
            if reason:
                appointment.notes = (
                    f"{appointment.notes}\n\nCancellation reason: {reason}"
                    if appointment.notes
                    else f"Cancellation reason: {reason}"
                )

            await self.db.commit()

            return {
                "success": True,
                "appointment_id": appointment_id,
                "message": f"Appointment on {appointment.scheduled_at.strftime('%B %d at %I:%M %p')} has been cancelled",
            }

        except Exception as e:
            self.logger.exception("cancel_appointment_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def reschedule_appointment(
        self, appointment_id: int, new_scheduled_at: str
    ) -> dict[str, Any]:
        """Reschedule an appointment.

        Args:
            appointment_id: Appointment ID
            new_scheduled_at: New datetime in ISO 8601 format

        Returns:
            Reschedule confirmation
        """
        try:
            # Verify appointment belongs to user's workspace/contact
            base_stmt = select(Appointment).join(Contact).where(Appointment.id == appointment_id)

            if self.workspace_id:
                stmt = base_stmt.where(Contact.workspace_id == self.workspace_id)
            else:
                stmt = base_stmt.where(Contact.user_id == self.user_id)

            result = await self.db.execute(stmt)
            appointment = result.scalar_one_or_none()

            if not appointment:
                return {
                    "success": False,
                    "error": f"Appointment {appointment_id} not found",
                }

            # Parse new datetime
            new_time = datetime.fromisoformat(new_scheduled_at.replace("Z", "+00:00"))

            old_time = appointment.scheduled_at
            appointment.scheduled_at = new_time

            await self.db.commit()

            return {
                "success": True,
                "appointment_id": appointment_id,
                "old_time": old_time.strftime("%B %d at %I:%M %p"),
                "new_time": new_time.strftime("%B %d at %I:%M %p"),
                "message": f"Appointment rescheduled from {old_time.strftime('%B %d at %I:%M %p')} to {new_time.strftime('%B %d at %I:%M %p')}",
            }

        except Exception as e:
            self.logger.exception("reschedule_appointment_failed", error=str(e))
            return {"success": False, "error": str(e)}

    async def execute_tool(  # noqa: PLR0911
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """Execute a CRM tool by name.

        Args:
            tool_name: Tool name
            arguments: Tool arguments

        Returns:
            Tool result
        """
        if tool_name == "search_customer":
            return await self.search_customer(**arguments)
        if tool_name == "create_contact":
            return await self.create_contact(**arguments)
        # Two names, one handler: "refresh_availability" is what the agent is now
        # offered (the menu is pre-loaded, so re-reading the calendar is the
        # exception), while "check_availability" stays routed for older agent
        # configs and saved transcripts.
        if tool_name in ("refresh_availability", "check_availability"):
            return await self.check_availability(**arguments)
        if tool_name == "select_slot":
            return await self.select_slot(**arguments)
        if tool_name == "record_fit_answers":
            return await self.record_fit_answers(**arguments)
        if tool_name == "book_appointment":
            return await self.book_appointment(**arguments)
        if tool_name == "list_appointments":
            return await self.list_appointments(**arguments)
        if tool_name == "cancel_appointment":
            return await self.cancel_appointment(**arguments)
        if tool_name == "reschedule_appointment":
            return await self.reschedule_appointment(**arguments)
        return {"success": False, "error": f"Unknown tool: {tool_name}"}
