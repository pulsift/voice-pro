"""Work out the lead's local timezone BEFORE the call, so nobody has to ask.

Sami: "you should give him the timezone based on the leads state so he doesnt have
to ask for it, maybe dont give it to him via prompting and actually do the
calculations for him programmatically."

Asking "what timezone are you in?" was never a question a human needed answered — we
already know the number we dialled. This module derives the timezone from the data we
already hold, in order of how much it can be trusted:

  1. An explicit IANA timezone on the call variables (came from the lead's own record).
  2. A US state on the call variables ("CA", "California", "TX").
  3. The destination phone number: area code -> state -> timezone, reusing the same
     NANPA table the recording-consent policy is built on.
  4. The team's timezone, as a last resort.

It is an INFERENCE, not a fact: a mobile keeps its area code across a move, and a few
states straddle two zones. That is fine, because it is only used to pick which times
to offer — and if the lead says they are somewhere else, one refresh_availability call
rebuilds the menu in their real timezone. What it removes is the default case: a whole
question, on every call, that we already knew the answer to.

Before this existed, no timezone was passed at all and every US lead was offered slots
computed in Europe/Stockholm.
"""

from __future__ import annotations

from typing import Any, Final
from zoneinfo import ZoneInfo

import structlog

from app.core.config import settings
from app.services.telephony.recording_policy import AREA_CODE_TO_STATE, area_code

logger = structlog.get_logger()

EASTERN: Final = "America/New_York"
CENTRAL: Final = "America/Chicago"
MOUNTAIN: Final = "America/Denver"
PACIFIC: Final = "America/Los_Angeles"

# Each state's DOMINANT timezone (where the majority of its population lives).
STATE_TO_TIMEZONE: Final[dict[str, str]] = {
    "AL": CENTRAL, "AK": "America/Anchorage", "AZ": "America/Phoenix", "AR": CENTRAL,
    "CA": PACIFIC, "CO": MOUNTAIN, "CT": EASTERN, "DE": EASTERN, "DC": EASTERN,
    "FL": EASTERN, "GA": EASTERN, "HI": "Pacific/Honolulu", "ID": "America/Boise",
    "IL": CENTRAL, "IN": "America/Indiana/Indianapolis", "IA": CENTRAL, "KS": CENTRAL,
    "KY": EASTERN, "LA": CENTRAL, "ME": EASTERN, "MD": EASTERN, "MA": EASTERN,
    "MI": "America/Detroit", "MN": CENTRAL, "MS": CENTRAL, "MO": CENTRAL,
    "MT": MOUNTAIN, "NE": CENTRAL, "NV": PACIFIC, "NH": EASTERN, "NJ": EASTERN,
    "NM": MOUNTAIN, "NY": EASTERN, "NC": EASTERN, "ND": CENTRAL, "OH": EASTERN,
    "OK": CENTRAL, "OR": PACIFIC, "PA": EASTERN, "RI": EASTERN, "SC": EASTERN,
    "SD": CENTRAL, "TN": CENTRAL, "TX": CENTRAL, "UT": MOUNTAIN, "VT": EASTERN,
    "VA": EASTERN, "WA": PACIFIC, "WV": EASTERN, "WI": CENTRAL, "WY": MOUNTAIN,
}

# Area codes that sit in a DIFFERENT zone from their state's dominant one. Only the
# clear-cut cases: an area code wholly (or overwhelmingly) in the minority zone.
AREA_CODE_TIMEZONE_OVERRIDES: Final[dict[str, str]] = {
    "915": MOUNTAIN,  # El Paso, TX
    "423": EASTERN,   # Chattanooga / Tri-Cities, TN
    "865": EASTERN,   # Knoxville, TN
    "270": CENTRAL,   # western KY (Paducah, Bowling Green)
    "364": CENTRAL,   # western KY overlay
    "219": CENTRAL,   # northwest IN (Gary/Hammond)
}

_STATE_NAMES: Final[dict[str, str]] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "washington dc": "DC", "florida": "FL",
    "georgia": "GA", "hawaii": "HI", "idaho": "ID", "illinois": "IL",
    "indiana": "IN", "iowa": "IA", "kansas": "KS", "kentucky": "KY",
    "louisiana": "LA", "maine": "ME", "maryland": "MD", "massachusetts": "MA",
    "michigan": "MI", "minnesota": "MN", "mississippi": "MS", "missouri": "MO",
    "montana": "MT", "nebraska": "NE", "nevada": "NV", "new hampshire": "NH",
    "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}

_STATE_CODE_LENGTH: Final = 2

_EXPLICIT_ZONE_ALIASES: Final[dict[str, str]] = {
    "et": EASTERN,
    "est": EASTERN,
    "edt": EASTERN,
    "eastern": EASTERN,
    "eastern time": EASTERN,
    "eastern time zone": EASTERN,
    "eastern standard time": EASTERN,
    "eastern daylight time": EASTERN,
    "ct": CENTRAL,
    "cst": CENTRAL,
    "cdt": CENTRAL,
    "central": CENTRAL,
    "central time": CENTRAL,
    "central time zone": CENTRAL,
    "central standard time": CENTRAL,
    "central daylight time": CENTRAL,
    "mt": MOUNTAIN,
    "mst": MOUNTAIN,
    "mdt": MOUNTAIN,
    "mountain": MOUNTAIN,
    "mountain time": MOUNTAIN,
    "mountain time zone": MOUNTAIN,
    "mountain standard time": MOUNTAIN,
    "mountain daylight time": MOUNTAIN,
    "pt": PACIFIC,
    "pst": PACIFIC,
    "pdt": PACIFIC,
    "pacific": PACIFIC,
    "pacific time": PACIFIC,
    "pacific time zone": PACIFIC,
    "pacific standard time": PACIFIC,
    "pacific daylight time": PACIFIC,
    "arizona time": "America/Phoenix",
    "alaska time": "America/Anchorage",
    "hawaii time": "Pacific/Honolulu",
    "new york city": EASTERN,
    "new york": EASTERN,
    "chicago": CENTRAL,
    "denver": MOUNTAIN,
    "los angeles": PACIFIC,
    "phoenix": "America/Phoenix",
    "stockholm": "Europe/Stockholm",
    "london": "Europe/London",
    "damascus": "Asia/Damascus",
    "syria": "Asia/Damascus",
    "syrian time": "Asia/Damascus",
    "syrian time zone": "Asia/Damascus",
    "syrian timezone": "Asia/Damascus",
}

# Variable keys that may carry a state, in the order we trust them.
_STATE_KEYS: Final = ("state", "leadState", "region", "companyState")
_TZ_KEYS: Final = ("tzName", "timezone", "leadTimezone", "timeZone")


def _valid_zone(value: Any) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    try:
        ZoneInfo(candidate)
    except Exception:
        return None
    return candidate


def _spoken_key(value: Any) -> str:
    return " ".join(
        str(value or "").lower().replace("-", " ").replace("_", " ").split()
    ).strip(" .,!?:;")


def state_code(value: Any) -> str | None:
    """Normalize "california" / "CA" / " Ca. " to a two-letter state code."""
    text = " ".join(str(value or "").lower().replace(".", " ").split())
    if not text:
        return None
    if len(text) == _STATE_CODE_LENGTH and text.upper() in STATE_TO_TIMEZONE:
        return text.upper()
    return _STATE_NAMES.get(text)


def timezone_for_state(value: Any) -> str | None:
    code = state_code(value)
    return STATE_TO_TIMEZONE.get(code) if code else None


def resolve_explicit(value: Any) -> str | None:
    """Resolve caller-supplied timezone text without consulting a fallback."""
    state_zone = timezone_for_state(value)
    if state_zone:
        return state_zone
    alias = _EXPLICIT_ZONE_ALIASES.get(_spoken_key(value))
    if alias:
        return alias
    return _valid_zone(value)


def timezone_for_number(number: str | None) -> str | None:
    """Derive a timezone from a NANP number: area-code override, else its state."""
    npa = area_code(number)
    if npa is None:
        return None
    override = AREA_CODE_TIMEZONE_OVERRIDES.get(npa)
    if override:
        return override
    state = AREA_CODE_TO_STATE.get(npa)
    return STATE_TO_TIMEZONE.get(state) if state else None


_SPOKEN_ZONE_NAMES: Final[dict[str, str]] = {
    EASTERN: "Eastern time",
    CENTRAL: "Central time",
    MOUNTAIN: "Mountain time",
    PACIFIC: "Pacific time",
    "America/Phoenix": "Arizona time",
    "America/Detroit": "Eastern time",
    "America/Indiana/Indianapolis": "Eastern time",
    "America/Boise": "Mountain time",
    "America/Anchorage": "Alaska time",
    "Pacific/Honolulu": "Hawaii time",
}


def spoken_zone_name(zone: str) -> str:
    """A timezone the way a person says it out loud, never "America slash Denver"."""
    known = _SPOKEN_ZONE_NAMES.get(zone)
    if known:
        return known
    city = zone.rsplit("/", 1)[-1].replace("_", " ")
    return f"{city} time"


def resolve(variables: dict[str, Any] | None) -> tuple[str, str]:
    """Return ``(iana_timezone, source)`` for this call's lead.

    `source` is one of "variable" | "state" | "area_code" | "team_default" and exists
    so a wrong timezone can be diagnosed from the logs instead of guessed at.
    """
    data = variables or {}

    for key in _TZ_KEYS:
        zone = resolve_explicit(data.get(key))
        if zone:
            return zone, "variable"

    for key in _STATE_KEYS:
        zone = timezone_for_state(data.get(key))
        if zone:
            return zone, "state"

    for key in ("leadPhone", "phone", "to_number"):
        zone = timezone_for_number(data.get(key))
        if zone:
            return zone, "area_code"

    return settings.BOOKING_TEAM_TIMEZONE, "team_default"
