"""The agent's own lightweight calendar, computed BEFORE the call starts.

Sami's design, in his words: "Railway carries the heavylifting of knowing when
there are free gaps on my calendar, then it gives the VA its own internal
lightweight calendar so the VA doesn't have to query anything, it can just check
it... that way the VA doesnt waste time doing random crap."

So availability stops being a mid-call tool round-trip and becomes DATA the agent
already has. At dial time the backend asks Cal.com once, thins the raw slot list
into a small human menu (a handful of days, a few times each), and ships it inside
the per-call variables. The prompt renders it; the booking tools seed their offered
set from it. The agent never waits on the calendar, and — the failure Sami actually
heard on the call — it can ANSWER "what about Friday?" from the menu instead of
volleying the question back.

Two things stay true regardless:
  - Cal.com remains the source of truth. The menu is a snapshot; booking still goes
    through Cal.com, and a slot taken in the meantime comes back as a conflict that
    refreshes the menu (crm_tools.book_appointment).
  - An empty menu is not a failure. The prompt then tells the agent to call
    refresh_availability once, which is exactly the old behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final, Literal, TypedDict
from zoneinfo import ZoneInfo

import structlog

from app.core.config import settings

logger = structlog.get_logger()

# Menu shape. The agent HOLDS everything and SAYS two — the opposite of the
# original settings, which held sixteen of a real 107 and said two.
#
# The window is one week, and that is load-bearing rather than a taste. Inside
# seven days each weekday name maps to exactly one date, so "Wednesday at midday"
# names exactly one opening. Widen it and it stops being true: measured against the
# live calendar on 2026-08-09, a twelve-day window held two of every weekday and
# produced 51 pairs of slots a caller cannot tell apart — and select_slot refuses
# anything the transcript cannot reduce to ONE slot, so every one of those pairs
# was a booking the agent would have declined to make. `_drop_indistinguishable`
# enforces the invariant directly, so a future widening degrades instead of breaking.
LOOKAHEAD_DAYS: Final = 7
# Not curation any more — safety rails on how much calendar can land in a prompt.
# MAX_PER_DAY does not bind on a normal business day (48 = every half hour, 8am-8pm,
# twice over); MAX_SLOTS caps the rendered block at roughly 5,000 characters.
MAX_DAYS: Final = 7
MAX_PER_DAY: Final = 48
MAX_SLOTS: Final = 120

# Server-owned call context. The public outbound telephony endpoint stamps this
# onto its persisted variables so a call that crosses a restart/deploy can never
# fall through to the fork's unrelated internal appointment calendar.
CALENDAR_BACKEND_VARIABLE: Final = "_calendar_backend"
CALCOM_REQUIRED_BACKEND: Final = "calcom_required"

AvailabilityStatus = Literal["available", "empty", "unavailable"]


class AvailabilityResult(TypedDict):
    """A calendar read whose business-empty and dependency-down states differ."""

    status: AvailabilityStatus
    timezone: str
    generated_at: str
    slots: list[dict[str, str]]
    block: str
    offer_slots: list[dict[str, str]]

_NOON: Final = 12
_EVENING_HOUR: Final = 17
_MINUTES_PER_HOUR: Final = 60
# The midday/early-afternoon band's upper edge: 14:30 as (hour, minute).
_MIDDAY_BAND_END: Final = (14, 30)

_HOUR_WORDS: Final = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
    7: "seven", 8: "eight", 9: "nine", 10: "ten", 11: "eleven", 12: "twelve",
}
_MINUTE_WORDS: Final = {
    5: "five past", 10: "ten past", 15: "quarter past", 20: "twenty past",
    25: "twenty five past", 30: "half past", 35: "twenty five to",
    40: "twenty to", 45: "quarter to", 50: "ten to", 55: "five to",
}


def spoken_time(moment: datetime) -> str:
    """Render a local time the way a person says it on the phone.

    The agent is forbidden from speaking digits, so the menu hands it words:
    09:00 -> "nine in the morning", 16:30 -> "half past four in the afternoon".
    Anything not on a five-minute boundary degrades to "nine oh seven" style,
    which is still speakable.
    """
    hour24 = moment.hour
    minute = moment.minute
    if hour24 == _NOON and minute == 0:
        return "midday"

    part = (
        "in the morning"
        if hour24 < _NOON
        else "in the afternoon"
        if hour24 < _EVENING_HOUR
        else "in the evening"
    )
    hour12 = hour24 % _NOON or _NOON

    if minute == 0:
        return f"{_HOUR_WORDS[hour12]} {part}"
    phrase = _MINUTE_WORDS.get(minute)
    if phrase is None:
        return f"{_HOUR_WORDS[hour12]} {minute:02d} {part}"
    if phrase.endswith(" to"):
        # "twenty to five" counts up to the NEXT hour.
        next_hour12 = (hour24 + 1) % _NOON or _NOON
        return f"{phrase} {_HOUR_WORDS[next_hour12]} {part}"
    return f"{phrase} {_HOUR_WORDS[hour12]} {part}"


def _thin(day_slots: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep at most `limit` slots, spread across the day rather than clustered.

    Cal.com returns every 15/30-minute opening; offering "nine, nine fifteen,
    nine thirty" is useless to a human. Even spacing gives a real choice of
    morning / midday / afternoon.
    """
    if len(day_slots) <= limit:
        return day_slots
    if limit <= 1:
        # One slot from a whole day: take the MIDDLE one, not the first. [Codex]
        # found that the old `step = 0` path always kept index zero, so a day
        # thinned to one became the earliest opening on it — eight in the morning
        # across the board, and a caller asking for the afternoon was told there
        # was none while every afternoon was free.
        return [day_slots[len(day_slots) // 2]]
    step = (len(day_slots) - 1) / (limit - 1)
    picked_indexes = sorted({round(index * step) for index in range(limit)})
    return [day_slots[index] for index in picked_indexes]


def _apply_ceiling(
    by_day: dict[str, list[dict[str, Any]]],
    max_days: int,
    max_per_day: int,
    max_slots: int,
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    """Fit the calendar under the prompt-size rails without losing whole days.

    The rails are safety, not curation — at the shipped settings none of them
    bind on a normal week. What matters is HOW they degrade when they do:

      - The original code filled up day by day and stopped at the ceiling, which
        deleted Friday off the end of a Monday-to-Friday calendar. The agent then
        told callers we hold nothing on Friday while Friday was wide open.
      - Sharing the ceiling out evenly fixes that, but `max(1, ...)` quietly
        chose to break the ceiling instead when it was smaller than the number of
        days: five days under a ceiling of two returned five slots ([Codex]).
        Below one-per-day the two goals genuinely conflict, so the nearest days
        are kept whole and the far ones dropped — a caller is far likelier to
        want this week than the end of next.
    """
    day_keys = sorted(by_day)[:max_days]
    kept: dict[str, list[dict[str, Any]]] = {
        key: _thin(sorted(by_day[key], key=lambda item: item["local"]), max_per_day)
        for key in day_keys
    }
    if not day_keys or sum(len(day) for day in kept.values()) <= max_slots:
        return day_keys, kept

    if max_slots < len(day_keys):
        day_keys = day_keys[:max_slots]
        kept = {key: kept[key] for key in day_keys}
    share = max(1, max_slots // len(day_keys))
    return day_keys, {key: _thin(day, share) for key, day in kept.items()}


def _is_morning(local: datetime) -> bool:
    """Before 12:00 local to the lead."""
    return local.hour < _NOON


def _is_midday_band(local: datetime) -> bool:
    """12:00 through about 14:30 local — midday to early afternoon."""
    return (_NOON, 0) <= (local.hour, local.minute) <= _MIDDAY_BAND_END


def _day_key(local: datetime) -> str:
    return local.strftime("%Y-%m-%d")


def _choose_offer_slots(
    ordered: list[tuple[dict[str, str], datetime]],
) -> list[dict[str, str]]:
    """Pick exactly two slots to lead with, so the agent reads a finished choice
    instead of computing a spread live on the call.

    The rule is about DAYS first, times second: lead with the two soonest days the
    calendar actually offers, never today. On a Wednesday that is Thursday and
    Friday; on a Friday it is Monday and Tuesday.

    Weekends are not computed here. Cal.com already knows the working week from
    Sami's own schedule, so a weekend never reaches this list - re-deriving it would
    be a second opinion about something the calendar has already decided, and the
    two would eventually disagree.

    Today never reaches this function - build_menu drops same-day slots before the
    menu exists, because the list has to be BUILT before that call. Do not re-check
    it here; one owner for that rule is the reason it cannot drift.

    Within the two chosen days the old preference survives: a morning slot for the
    first, a midday/early-afternoon one for the second, so the pair sounds like a
    real spread. But the DAY always wins - a sooner day with only afternoons still
    beats a later day that happens to have a morning (an earlier version searched
    for the earliest morning ANYWHERE, which is how today kept becoming option one).
    """
    if not ordered:
        return []
    if len(ordered) == 1:
        return [ordered[0][0]]

    day_order: list[str] = []
    by_day: dict[str, list[tuple[dict[str, str], datetime]]] = {}
    for pair in ordered:
        key = _day_key(pair[1])
        if key not in by_day:
            by_day[key] = []
            day_order.append(key)
        by_day[key].append(pair)

    if len(day_order) == 1:
        only = by_day[day_order[0]]
        morning = next((pair for pair in only if _is_morning(pair[1])), None)
        midday = next((pair for pair in only if _is_midday_band(pair[1])), None)
        if morning is not None and midday is not None:
            chosen = sorted((morning, midday), key=lambda pair: pair[1])
            return [chosen[0][0], chosen[1][0]]
        return [pair[0] for pair in only[:2]]

    first, second = by_day[day_order[0]], by_day[day_order[1]]
    lead = next((pair for pair in first if _is_morning(pair[1])), first[0])
    follow = next((pair for pair in second if _is_midday_band(pair[1])), second[0])
    return [lead[0], follow[0]]


def _render_offer_first_line(offer_slots: list[dict[str, str]]) -> str | None:
    """The finished sentence the agent reads verbatim as its first offer.

    Built from the same `label` the full menu uses ("<Day> at <spoken time>"),
    so wording stays in house style automatically — words not digits, day name
    only, never a date.
    """
    if not offer_slots:
        return None
    if len(offer_slots) == 1:
        return f"OFFER FIRST: {offer_slots[0]['label']}."
    return f"OFFER FIRST: {offer_slots[0]['label']}, or {offer_slots[1]['label']}."


def _render_span_lines(
    ordered: list[tuple[dict[str, str], datetime]],
) -> list[str]:
    """One spoken SPAN per day, so "what have you got?" is answered in a sentence.

    A span says where a day STARTS and where it ENDS. It never claims everything
    between the two is free: the calendar has gaps and `_thin` narrows the menu
    further, so a coverage claim would be a small lie the caller discovers on
    their very next sentence. Anything inside the span we do not actually hold is
    caught by select_slot, which walks it back honestly.

    The full list stays in the block underneath. Available and spoken aloud are
    different things - the agent keeps every slot, it just stops reciting them.
    """
    if not ordered:
        return []
    by_day: dict[str, list[datetime]] = {}
    day_names: dict[str, str] = {}
    for entry, local in ordered:
        key = _day_key(local)
        by_day.setdefault(key, []).append(local)
        day_names.setdefault(key, entry["day"])

    spans: list[str] = []
    for key, moments in by_day.items():
        first, last = min(moments), max(moments)
        name = day_names[key]
        if first == last:
            spans.append(f"{name}, {spoken_time(first)} only")
        else:
            spans.append(
                f"{name}, from {spoken_time(first)} through to {spoken_time(last)}"
            )
    return [
        "SPANS - say one of these, never a list of times: " + "; ".join(spans) + ".",
        "When they ask what you have - a single day or your availability in general "
        "- answer with the span in ONE sentence and stop. Never read individual "
        "times out loud. The calendar below is what you MATCH their words against; "
        "it is not a script to read from.",
    ]


def build_menu(
    raw_slots: list[dict[str, str]],
    lead_tz: str,
    *,
    max_days: int = MAX_DAYS,
    max_per_day: int = MAX_PER_DAY,
    max_slots: int = MAX_SLOTS,
) -> AvailabilityResult:
    """Turn a flat Cal.com slot list into the agent's menu (pure, testable).

    `raw_slots` items need only a "start" (ISO 8601). Grouping, ordering and
    slot ids are decided here so the ids in the prompt are the same ids the
    booking tools accept.
    """
    try:
        zone = ZoneInfo(lead_tz)
    except Exception:
        logger.warning("availability_menu_bad_timezone", lead_tz=lead_tz)
        return empty_menu(lead_tz)

    today_local = datetime.now(zone).date()
    by_day: dict[str, list[dict[str, Any]]] = {}
    seen_starts: set[datetime] = set()
    for slot in raw_slots:
        iso = slot.get("start") if isinstance(slot, dict) else slot
        if not iso:
            continue
        try:
            start = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        # One real appointment must never become two menu entries. If the
        # provider repeats a start - twice, or the same instant written two
        # ways - the offer picker could name the SAME time as both of its two
        # choices, and the caller would be asked to choose between a slot and
        # itself. Compare the parsed instant, never the raw string.
        if start in seen_starts:
            continue
        seen_starts.add(start)
        local = start.astimezone(zone)
        # Same-day is unbookable, not merely unoffered. The lead list has to be
        # BUILT before that call, so a time today is a promise we cannot keep -
        # and a menu that holds one can always be talked into it (Sami,
        # 2026-08-26: "they should not be able to take in a call on the same
        # day"). Dropping it here is the only place that makes it true
        # everywhere; filtering later would leave the tools still holding it.
        if local.date() == today_local:
            continue
        by_day.setdefault(local.strftime("%Y-%m-%d"), []).append(
            {"start": str(iso), "local": local}
        )

    day_keys, kept = _apply_ceiling(by_day, max_days, max_per_day, max_slots)

    slots: list[dict[str, str]] = []
    slot_locals: list[tuple[dict[str, str], datetime]] = []
    days: list[dict[str, Any]] = []
    spoken_seen: set[tuple[str, str]] = set()
    indistinguishable = 0
    for key in day_keys:
        entries = []
        for item in kept[key]:
            local: datetime = item["local"]
            spoken = (local.strftime("%A"), spoken_time(local))
            # Two slots the caller cannot tell apart are worse than one slot fewer:
            # select_slot only books when the transcript reduces to exactly ONE
            # slot, so a second "Wednesday at midday" does not add a choice, it
            # removes one. Keep the earlier — "Wednesday" means the next Wednesday.
            if spoken in spoken_seen:
                indistinguishable += 1
                continue
            spoken_seen.add(spoken)
            entry = {
                "slot_id": f"slot_{len(slots) + 1}",
                "start": item["start"],
                # `label` is what the agent says; `time` keeps the digits for logs
                # and the dashboard.
                "label": f"{spoken[0]} at {spoken[1]}",
                "day": spoken[0],
                "time": local.strftime("%I:%M %p").lstrip("0"),
                "timezone": lead_tz,
            }
            slots.append(entry)
            slot_locals.append((entry, local))
            entries.append(entry)
        if entries:
            days.append({"day": entries[0]["day"], "slots": entries})
    if indistinguishable:
        # Only reachable if the lookahead window ever grows past a week. Loud,
        # because it means the menu is quietly narrower than the calendar.
        logger.warning(
            "availability_menu_dropped_indistinguishable_slots",
            dropped=indistinguishable,
            kept=len(slots),
        )

    offer_slots = _choose_offer_slots(slot_locals)
    status: AvailabilityStatus = "available" if slots else "empty"
    return {
        "status": status,
        "timezone": lead_tz,
        "generated_at": datetime.now(UTC).isoformat(),
        "slots": slots,
        "block": render_block(
            days, lead_tz, offer_slots=offer_slots, spans=_render_span_lines(slot_locals)
        ),
        "offer_slots": offer_slots,
    }


def empty_menu(
    lead_tz: str,
    *,
    status: Literal["empty", "unavailable"] = "unavailable",
) -> AvailabilityResult:
    """Represent a healthy empty calendar separately from a failed calendar read."""
    if status == "empty":
        block = (
            "The calendar has no open business-hours times in the current window. "
            "Do not invent or promise a time."
        )
    else:
        block = (
            "The calendar could not be pre-loaded for this call. Once you know "
            "their timezone, call refresh_availability once and offer what it returns."
        )
    return {
        "status": status,
        "timezone": lead_tz,
        "generated_at": datetime.now(UTC).isoformat(),
        "slots": [],
        "block": block,
        "offer_slots": [],
    }


def render_block(
    days: list[dict[str, Any]],
    lead_tz: str,
    *,
    offer_slots: list[dict[str, str]] | None = None,
    spans: list[str] | None = None,
) -> str:
    """Render the menu as the prompt block the agent reads its times from.

    When `offer_slots` is given, a pre-worded "OFFER FIRST" line is placed ahead
    of the full menu so the agent reads a finished choice instead of picking a
    spread live on the call — see `_choose_offer_slots`. The full menu stays
    exactly as before it, unchanged, so the agent can still answer "what about
    Friday?" from it.
    """
    if not days:
        return empty_menu(lead_tz, status="empty")["block"]
    # Spoken form, never the IANA path: an agent that can read "Asia/Damascus" is an
    # agent that will eventually say "Asia slash Damascus" down a phone line.
    from app.services.lead_timezone import spoken_zone_name

    lines = []
    offer_line = _render_offer_first_line(offer_slots or [])
    if offer_line:
        lines.append(offer_line)
    lines.extend(spans or [])
    lines.append(
        f"This is the WHOLE calendar for the coming week - every time we hold, in the "
        f"lead's own clock ({spoken_zone_name(lead_tz)}). If a day or a time is on this "
        f"list, we have it; if it is not here, we genuinely do not. Answer any question "
        f"about our availability from this list and never guess. Say the words, never "
        f"the id:"
    )
    for day in days:
        times = " / ".join(f"{slot['label'].split(' at ', 1)[-1]} [{slot['slot_id']}]"
                           for slot in day["slots"])
        lines.append(f"- {day['day']}: {times}")
    return "\n".join(lines)


def missing_calcom_settings() -> tuple[str, ...]:
    """Return setting names only; never return or log credential values."""
    missing: list[str] = []
    if not (settings.CALCOM_API_KEY or "").strip():
        missing.append("CALCOM_API_KEY")
    if not settings.CALCOM_EVENT_TYPE_ID:
        missing.append("CALCOM_EVENT_TYPE_ID")
    return tuple(missing)


async def fetch_menu(lead_tz: str) -> AvailabilityResult:
    """Ask Cal.com for the real free gaps and return a discriminated result.

    Called on the dial path, so a slow or broken calendar must degrade to an
    unavailable result rather than raise. Callers decide whether that dependency
    failure is soft (preload) or must stop an interactive booking operation.
    """
    missing = missing_calcom_settings()
    if missing:
        logger.error("availability_menu_calcom_unconfigured", missing_settings=missing)
        return empty_menu(lead_tz)
    try:
        from app.services.calcom_client import get_open_slots

        raw = await get_open_slots(lead_tz=lead_tz, days=LOOKAHEAD_DAYS)
    except Exception as exc:
        logger.error(  # noqa: TRY400 - do not attach provider exception text to logs
            "availability_menu_fetch_failed", error_type=type(exc).__name__
        )
        return empty_menu(lead_tz)

    menu = build_menu(raw, lead_tz)
    logger.info(
        "availability_menu_built",
        status=menu["status"],
        lead_tz=lead_tz,
        raw_count=len(raw),
        slot_count=len(menu["slots"]),
    )
    return menu
