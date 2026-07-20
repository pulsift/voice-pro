"""Thin async client for Cal.com v2 (slots + bookings).

Used by the voice agent's check_availability / book_appointment tools when
CALCOM_API_KEY + CALCOM_EVENT_TYPE_ID are configured. Cal.com is the single
source of truth: it already reflects the host's real Google Calendar free/busy
(so we can't double-book) and emails the attendee the invite on booking.

Per-endpoint API version headers DIFFER (this is the silent-break gotcha):
  - slots:    cal-api-version: 2024-09-04
  - bookings: cal-api-version: 2024-08-13
"""

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import structlog

from app.core.config import settings

logger = structlog.get_logger()

CALCOM_BASE = "https://api.cal.com/v2"
SLOTS_API_VERSION = "2024-09-04"
BOOKINGS_API_VERSION = "2024-08-13"
HTTP_CONFLICT = 409
HTTP_RATE_LIMITED = 429
HTTP_SERVER_ERROR_MIN = 500
SATURDAY_INDEX = 5
MORNING_START_HOUR = 9
NOON_HOUR = 12
AFTERNOON_END_HOUR = 16

_TIMEZONE_ALIASES = {
    "syria": "Asia/Damascus",
    "syrian time": "Asia/Damascus",
    "syrian time zone": "Asia/Damascus",
    "syrian timezone": "Asia/Damascus",
}
_SENSITIVE_RESPONSE_KEYS = {
    "apikey",
    "attendee",
    "attendees",
    "authorization",
    "email",
    "metadata",
    "name",
    "phone",
    "phonenumber",
    "secret",
    "token",
}


def _redact_text_leaf(value: str) -> str:
    """Redact PII/secrets even when embedded in an ordinary JSON string value."""
    redacted = re.sub(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
        "[email]",
        value,
    )
    redacted = re.sub(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)", "[phone]", redacted)
    return re.sub(
        r"""(?i)\b(authorization|token|secret|api[_-]?key)\b["']?(\s*[:=]\s*)[^\r\n,;}]+""",
        r"\1\2[redacted]",
        redacted,
    )


def sanitize_provider_text(value: str) -> str:
    """Preserve provider diagnostics while removing common secrets and PII."""

    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: "[redacted]"
                if re.sub(r"[^a-z0-9]", "", key.lower()) in _SENSITIVE_RESPONSE_KEYS
                else scrub(child)
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [scrub(child) for child in item]
        if isinstance(item, str):
            return _redact_text_leaf(item)
        return item

    raw = value[:4000]
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return _redact_text_leaf(raw)[:1000]
    return json.dumps(scrub(parsed), ensure_ascii=False, separators=(",", ":"))[:1000]


def _valid_timezone(value: str | None) -> str | None:
    """Return a stripped IANA timezone name when ZoneInfo can load it."""
    candidate = (value or "").strip()
    if not candidate:
        return None
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return candidate


def normalize_timezone(
    spoken: str | None,
    fallback: str | None,
    team_default: str | None,
) -> str | None:
    """Resolve spoken/seeded timezone input to one validated IANA timezone.

    Spoken aliases are deliberately narrow. Unknown spoken text falls back to the
    seeded timezone, then the configured team default. None means no safe timezone
    could be produced and callers must not contact Cal.com.
    """
    normalized_spoken = " ".join(
        (spoken or "").lower().replace("-", " ").replace("_", " ").split()
    ).strip(" .,!?:;")
    alias = _TIMEZONE_ALIASES.get(normalized_spoken)
    if alias:
        return alias
    return (
        _valid_timezone((spoken or "").strip())
        or _valid_timezone(fallback)
        or _valid_timezone(team_default)
    )


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
    LEAD's timezone (so a US lead is never offered middle-of-the-night times just
    because they fall inside the team's day), and returns one slot per day for
    variety. Only when no valid lead timezone is known does the window fall back
    to the team timezone.

    Each item: {"start": <original ISO for booking>, "label": <human label in lead tz>}.
    """
    log = logger.bind(component="calcom", op="get_business_slots", lead_tz=lead_tz)
    now = datetime.now(UTC)
    start = (now + timedelta(days=1)).strftime("%Y-%m-%d")
    end = (now + timedelta(days=days)).strftime("%Y-%m-%d")

    # Validate the lead timezone up front; fall back to the team tz so an invalid
    # tzName can't 400 the slots request (and the business-hours window below then
    # evaluates in the team timezone as the only remaining anchor).
    try:
        lead_zone = ZoneInfo(lead_tz)
    except Exception:
        log.warning("invalid_lead_tz_falling_back", lead_tz=lead_tz)
        lead_tz = settings.BOOKING_TEAM_TIMEZONE
        lead_zone = ZoneInfo(lead_tz)

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

    # Offer one MORNING (9-12) + one AFTERNOON (12-17) slot rather than two earliest
    # (which were both 8am). `extra` is a fallback if only one half-day has openings.
    morning: dict[str, str] | None = None
    afternoon: dict[str, str] | None = None
    extra: dict[str, str] | None = None

    for date_key in sorted(data.keys()):
        slots = data[date_key]
        if not isinstance(slots, list):
            continue
        for slot in slots:
            iso = slot.get("start") if isinstance(slot, dict) else slot
            if not iso:
                continue
            dt = _parse_iso(iso)
            # Evaluate the business-hours window in the LEAD's local time — the same
            # clock the slot is spoken in — never the team's (B3).
            local_dt = dt.astimezone(lead_zone)
            if local_dt.weekday() >= SATURDAY_INDEX:  # Sat/Sun
                continue
            h = local_dt.hour
            if not (settings.BOOKING_HOUR_START <= h < settings.BOOKING_HOUR_END):
                continue
            entry = {
                "start": iso,
                # Day + time only — no date (the caller found the spoken date pointless).
                "label": local_dt.strftime("%A %I:%M %p").replace(" 0", " "),
            }
            if MORNING_START_HOUR <= h < NOON_HOUR and morning is None:
                morning = entry
            elif NOON_HOUR <= h <= AFTERNOON_END_HOUR and afternoon is None:
                afternoon = entry
            elif extra is None:
                extra = entry

    picked = [s for s in (morning, afternoon) if s]
    if len(picked) < max_slots and extra:
        picked.append(extra)
    log.info(
        "slots_picked",
        count=len(picked),
        morning=morning is not None,
        afternoon=afternoon is not None,
    )
    return picked[:max_slots]


async def create_booking(
    start_iso: str,
    name: str,
    email: str,
    lead_tz: str,
    notes: str | None = None,
) -> dict[str, Any]:
    """Book the configured event and return a classified, sanitized outcome."""
    log = logger.bind(component="calcom", op="create_booking")
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
        raw_body = sanitize_provider_text(resp.text)
        if resp.status_code not in (200, 201):
            if resp.status_code == HTTP_CONFLICT:
                category = "conflict"
            elif resp.status_code == HTTP_RATE_LIMITED or resp.status_code >= HTTP_SERVER_ERROR_MIN:
                category = "transient"
            else:
                category = "rejected"
            log.warning("booking_failed", status=resp.status_code, category=category)
            return {
                "success": False,
                "category": category,
                "status_code": resp.status_code,
                "raw_body": raw_body,
            }
        payload = resp.json()
        d = payload.get("data", {}) if isinstance(payload, dict) else {}
        uid = str(d.get("uid") or "").strip() if isinstance(d, dict) else ""
        returned_start = d.get("start") if isinstance(d, dict) else None
        try:
            start_matches = bool(returned_start) and _parse_iso(str(returned_start)).astimezone(
                UTC
            ) == _parse_iso(start_utc).astimezone(UTC)
        except (TypeError, ValueError):
            start_matches = False
        if not uid or not start_matches:
            log.warning(
                "booking_response_unverifiable",
                status=resp.status_code,
                has_uid=bool(uid),
                start_matches=start_matches,
            )
            return {
                "success": False,
                "category": "transient",
                "status_code": resp.status_code,
                "raw_body": raw_body,
            }
        log.info("booking_created", uid=uid, start=returned_start)
        return {
            "success": True,
            "category": "success",
            "status_code": resp.status_code,
            # Success details are intentionally not persisted: the provider body can
            # include attendee PII and the UID/start below are sufficient evidence.
            "raw_body": "",
            "uid": uid,
            "start": returned_start,
        }
    except (httpx.TimeoutException, httpx.TransportError) as e:
        log.exception("booking_exception", error=str(e))
        return {
            "success": False,
            "category": "transient",
            "status_code": None,
            "raw_body": sanitize_provider_text(str(e)),
        }
    except Exception as e:
        log.exception("booking_response_invalid", error=str(e))
        return {
            "success": False,
            "category": "rejected",
            "status_code": None,
            "raw_body": sanitize_provider_text(str(e)),
        }


async def find_existing_booking(
    start_iso: str,
    email: str,
) -> dict[str, Any]:
    """Reconcile an unknown POST outcome before any retry can create a duplicate."""
    target_start = _parse_iso(start_iso).astimezone(UTC)
    window_start = (target_start - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end = (target_start + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {
        "Authorization": f"Bearer {settings.CALCOM_API_KEY}",
        "cal-api-version": BOOKINGS_API_VERSION,
    }
    params = {
        "attendeeEmail": email,
        "eventTypeId": settings.CALCOM_EVENT_TYPE_ID,
        "afterStart": window_start,
        "beforeEnd": window_end,
        "limit": 20,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{CALCOM_BASE}/bookings", headers=headers, params=params)
        if resp.status_code != httpx.codes.OK:
            return {
                "success": False,
                "category": "reconcile_unavailable",
                "status_code": resp.status_code,
                "raw_body": sanitize_provider_text(resp.text),
            }
        payload = resp.json()
        bookings = payload.get("data", [])
        if isinstance(bookings, dict):
            bookings = bookings.get("bookings", [])
        if not isinstance(bookings, list):
            bookings = []
        for booking in bookings:
            if not isinstance(booking, dict) or not booking.get("start"):
                continue
            status = str(booking.get("status") or "").lower()
            if status in {"cancelled", "canceled", "rejected"}:
                continue
            try:
                exact_start = _parse_iso(str(booking["start"])).astimezone(UTC) == target_start
            except (TypeError, ValueError):
                continue
            if exact_start:
                uid = str(booking.get("uid") or "").strip()
                if not uid:
                    return {
                        "success": False,
                        "category": "reconcile_unavailable",
                        "status_code": resp.status_code,
                        "raw_body": "matching booking missing uid",
                    }
                return {
                    "success": True,
                    "category": "reconciled_success",
                    "status_code": resp.status_code,
                    "raw_body": "",
                    "uid": uid,
                    "start": booking.get("start"),
                }
        return {
            "success": False,
            "category": "not_found",
            "status_code": resp.status_code,
            "raw_body": "",
        }
    except (httpx.TimeoutException, httpx.TransportError, ValueError, TypeError) as exc:
        logger.warning(
            "booking_reconciliation_unavailable",
            error_type=type(exc).__name__,
        )
        return {
            "success": False,
            "category": "reconcile_unavailable",
            "status_code": None,
            "raw_body": sanitize_provider_text(str(exc)),
        }
