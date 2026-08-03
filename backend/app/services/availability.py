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

# Menu shape. Small enough to read aloud from, wide enough to answer "got anything
# Friday?" without another calendar call.
LOOKAHEAD_DAYS: Final = 12
MAX_DAYS: Final = 5
MAX_PER_DAY: Final = 4
MAX_SLOTS: Final = 16

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
    step = (len(day_slots) - 1) / (limit - 1) if limit > 1 else 0
    picked_indexes = sorted({round(index * step) for index in range(limit)})
    return [day_slots[index] for index in picked_indexes]


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
    instead of computing a spread live on the call (see module docstring header
    on this file's fix commit for why that computation must never happen aloud).

    `ordered` is the flat slot list paired with its local datetime, already sorted
    day-major then time-minor (build_menu's construction order). Fallback order,
    most-preferred first:
      1. one morning slot + one midday/early-afternoon slot, on two different
         days, earliest suitable days first.
      2. the same two bands on the only available day, when there is only one.
      3. the two earliest slots that land on different days.
      4. the two earliest slots outright (may share a day).
      5. one slot.
      6. none.
    """
    if not ordered:
        return []
    if len(ordered) == 1:
        return [ordered[0][0]]

    distinct_days = {_day_key(local) for _, local in ordered}

    if len(distinct_days) > 1:
        morning = next((pair for pair in ordered if _is_morning(pair[1])), None)
        if morning is not None:
            midday = next(
                (
                    pair
                    for pair in ordered
                    if _is_midday_band(pair[1]) and _day_key(pair[1]) != _day_key(morning[1])
                ),
                None,
            )
            if midday is not None:
                chosen = sorted((morning, midday), key=lambda pair: pair[1])
                return [chosen[0][0], chosen[1][0]]
    else:
        morning = next((pair for pair in ordered if _is_morning(pair[1])), None)
        midday = next((pair for pair in ordered if _is_midday_band(pair[1])), None)
        if morning is not None and midday is not None:
            chosen = sorted((morning, midday), key=lambda pair: pair[1])
            return [chosen[0][0], chosen[1][0]]

    for index, (_entry, local) in enumerate(ordered):
        for later_entry, later_local in ordered[index + 1 :]:
            if _day_key(local) != _day_key(later_local):
                return [ordered[index][0], later_entry]

    return [ordered[0][0], ordered[1][0]]


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

    by_day: dict[str, list[dict[str, Any]]] = {}
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
        local = start.astimezone(zone)
        by_day.setdefault(local.strftime("%Y-%m-%d"), []).append(
            {"start": str(iso), "local": local}
        )

    slots: list[dict[str, str]] = []
    slot_locals: list[tuple[dict[str, str], datetime]] = []
    days: list[dict[str, Any]] = []
    for day_key in sorted(by_day)[:max_days]:
        day_slots = sorted(by_day[day_key], key=lambda item: item["local"])
        entries = []
        for item in _thin(day_slots, max_per_day):
            if len(slots) >= max_slots:
                break
            local: datetime = item["local"]
            entry = {
                "slot_id": f"slot_{len(slots) + 1}",
                "start": item["start"],
                # `label` is what the agent says; `time` keeps the digits for logs
                # and the dashboard.
                "label": f"{local.strftime('%A')} at {spoken_time(local)}",
                "day": local.strftime("%A"),
                "time": local.strftime("%I:%M %p").lstrip("0"),
                "timezone": lead_tz,
            }
            slots.append(entry)
            slot_locals.append((entry, local))
            entries.append(entry)
        if entries:
            days.append({"day": entries[0]["day"], "slots": entries})
        if len(slots) >= max_slots:
            break

    offer_slots = _choose_offer_slots(slot_locals)
    status: AvailabilityStatus = "available" if slots else "empty"
    return {
        "status": status,
        "timezone": lead_tz,
        "generated_at": datetime.now(UTC).isoformat(),
        "slots": slots,
        "block": render_block(days, lead_tz, offer_slots=offer_slots),
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
    lines.append(
        f"These are the open times on our calendar, already in the lead's own clock "
        f"({spoken_zone_name(lead_tz)}). Say the words, never the id:"
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
