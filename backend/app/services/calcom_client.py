"""Thin async client for Cal.com v2 (slots + bookings).

Used by the voice agent's check_availability / book_appointment tools when
CALCOM_API_KEY + CALCOM_EVENT_TYPE_ID are configured. Cal.com is the single
source of truth: it already reflects the host's real Google Calendar free/busy
(so we can't double-book) and emails the attendee the invite on booking.

Per-endpoint API version headers DIFFER (this is the silent-break gotcha):
  - slots:    cal-api-version: 2024-09-04
  - bookings: cal-api-version: 2024-08-13
"""

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger()

CALCOM_BASE = "https://api.cal.com/v2"
SLOTS_API_VERSION = "2024-09-04"
BOOKINGS_API_VERSION = "2024-08-13"


def _parse_iso(value: str) -> datetime:
    """Parse a Cal.com ISO timestamp; assume UTC if it carries no timezone."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def get_business_slots(
    lead_tz: str,
    days: int = 10,
    max_slots: int = 2,
) -> list[dict[str, str]]:
    """Return up to `max_slots` open slots within business hours, on distinct days.

    Pulls Cal.com availability for the configured event type starting tomorrow,
    then filters to weekdays inside [BOOKING_HOUR_START, BOOKING_HOUR_END) in the
    TEAM timezone (so we never offer out-of-hours slots even if the Cal.com schedule
    is permissive), and returns one slot per day for variety.

    Each item: {"start": <original ISO for booking>, "label": <human label in lead tz>}.
    """
    log = logger.bind(component="calcom", op="get_business_slots", lead_tz=lead_tz)
    now = datetime.now(UTC)
    start = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    end = (now + timedelta(days=days)).strftime("%Y-%m-%d")

    headers = {
        "Authorization": f"Bearer {settings.CALCOM_API_KEY}",
        "cal-api-version": SLOTS_API_VERSION,
    }
    params = {
        "eventTypeId": settings.CALCOM_EVENT_TYPE_ID,
        "start": start,
        "end": end,
        "timeZone": lead_tz,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(f"{CALCOM_BASE}/slots", headers=headers, params=params)
        resp.raise_for_status()
        data = resp.json().get("data", {})

    team_tz = ZoneInfo(settings.BOOKING_TEAM_TIMEZONE)
    try:
        lead_zone = ZoneInfo(lead_tz)
    except Exception:
        lead_zone = team_tz  # fall back to team tz if the lead tz is unrecognised

    picked: list[dict[str, str]] = []
    seen_days: set[Any] = set()

    for date_key in sorted(data.keys()):
        slots = data[date_key]
        if not isinstance(slots, list):
            continue
        for slot in slots:
            iso = slot.get("start") if isinstance(slot, dict) else slot
            if not iso:
                continue
            dt = _parse_iso(iso)
            team_dt = dt.astimezone(team_tz)
            if team_dt.weekday() >= 5:  # Sat/Sun
                continue
            if not (settings.BOOKING_HOUR_START <= team_dt.hour < settings.BOOKING_HOUR_END):
                continue
            day = team_dt.date()
            if day in seen_days:
                continue
            seen_days.add(day)
            lead_dt = dt.astimezone(lead_zone)
            picked.append(
                {
                    "start": iso,
                    "label": lead_dt.strftime("%A, %B %d at %I:%M %p").replace(" 0", " "),
                }
            )
            if len(picked) >= max_slots:
                log.info("slots_picked", count=len(picked))
                return picked

    log.info("slots_picked", count=len(picked))
    return picked


async def create_booking(
    start_iso: str,
    name: str,
    email: str,
    lead_tz: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Book the configured Cal.com event type for the attendee. Returns success/uid or error.

    `start` is normalised to UTC (Cal.com expects UTC). On a taken slot or any
    failure, returns {"success": False, ...} so the agent uses its hiccup line.
    """
    log = logger.bind(component="calcom", op="create_booking", email=email)
    start_utc = _parse_iso(start_iso).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    headers = {
        "Authorization": f"Bearer {settings.CALCOM_API_KEY}",
        "cal-api-version": BOOKINGS_API_VERSION,
        "Content-Type": "application/json",
    }
    body: dict[str, Any] = {
        "eventTypeId": settings.CALCOM_EVENT_TYPE_ID,
        "start": start_utc,
        "attendee": {"name": name, "email": email, "timeZone": lead_tz},
    }
    if notes:
        body["metadata"] = {"notes": notes[:480]}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(f"{CALCOM_BASE}/bookings", headers=headers, json=body)
        if resp.status_code not in (200, 201):
            log.warning("booking_failed", status=resp.status_code, body=resp.text[:300])
            return {"success": False, "error": resp.text[:300], "status_code": resp.status_code}
        d = resp.json().get("data", {})
        log.info("booking_created", uid=d.get("uid"), start=d.get("start"))
        return {
            "success": True,
            "uid": d.get("uid"),
            "start": d.get("start", start_utc),
        }
    except Exception as e:
        log.exception("booking_exception", error=str(e))
        return {"success": False, "error": str(e)}
