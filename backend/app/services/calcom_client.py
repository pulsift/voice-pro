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
from app.services import lead_timezone

logger = structlog.get_logger()

CALCOM_BASE = "https://api.cal.com/v2"
SLOTS_API_VERSION = "2024-09-04"
BOOKINGS_API_VERSION = "2024-08-13"
HTTP_CONFLICT = 409
HTTP_RATE_LIMITED = 429
HTTP_SERVER_ERROR_MIN = 500
SATURDAY_INDEX = 5

class CalendarAvailabilityError(RuntimeError):
    """Cal.com did not return a trustworthy availability document."""


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
    """Resolve a caller correction strictly, or use seeded defaults when absent.

    A non-empty spoken value is authoritative. If it cannot be resolved, return
    ``None`` so the agent asks one clarification instead of silently keeping the
    inferred pre-call timezone.
    """
    if str(spoken or "").strip():
        return lead_timezone.resolve_explicit(spoken)
    return _valid_timezone(fallback) or _valid_timezone(team_default)


def _parse_iso(value: str) -> datetime:
    """Parse a Cal.com ISO timestamp; assume UTC if it carries no timezone."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


async def _fetch_raw_slots(lead_tz: str, days: int) -> dict[str, Any]:
    """Ask Cal.com for this event type's openings, keyed by date."""
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
        payload = resp.json()
        data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        message = "Cal.com slots payload has no object data field"
        raise CalendarAvailabilityError(message)
    return data


def _resolve_lead_zone(lead_tz: str) -> tuple[str, ZoneInfo]:
    """Validate the lead timezone, falling back to the team's so a bad tzName
    can never 400 the slots request."""
    try:
        return lead_tz, ZoneInfo(lead_tz)
    except Exception:
        logger.warning("invalid_lead_tz_falling_back", lead_tz=lead_tz)
        fallback = settings.BOOKING_TEAM_TIMEZONE
        return fallback, ZoneInfo(fallback)


async def get_open_slots(lead_tz: str, days: int = 12) -> list[dict[str, str]]:
    """Every weekday business-hours opening, in order — the raw material for the
    pre-call slot menu (services/availability.py).

    Filters to weekdays inside [BOOKING_HOUR_START, BOOKING_HOUR_END),
    evaluated in the LEAD's local time. The menu layer decides how many
    times a human should actually be offered.
    """
    lead_tz, lead_zone = _resolve_lead_zone(lead_tz)
    data = await _fetch_raw_slots(lead_tz, days)

    picked: list[dict[str, str]] = []
    for date_key in sorted(data.keys()):
        slots = data[date_key]
        if not isinstance(slots, list):
            message = "Cal.com slots payload contains a non-list day"
            raise CalendarAvailabilityError(message)
        for slot in slots:
            iso = slot.get("start") if isinstance(slot, dict) else None
            if not isinstance(iso, str) or not iso.strip():
                message = "Cal.com slots payload contains a slot without a start"
                raise CalendarAvailabilityError(message)
            try:
                local_dt = _parse_iso(iso).astimezone(lead_zone)
            except (TypeError, ValueError) as exc:
                message = "Cal.com slots payload contains an invalid start"
                raise CalendarAvailabilityError(message) from exc
            if local_dt.weekday() >= SATURDAY_INDEX:
                continue
            if not (settings.BOOKING_HOUR_START <= local_dt.hour < settings.BOOKING_HOUR_END):
                continue
            picked.append({"start": iso})
    logger.info("open_slots_fetched", lead_tz=lead_tz, count=len(picked))
    return picked


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
        try:
            payload = resp.json()
            d = payload.get("data", {}) if isinstance(payload, dict) else {}
            uid = str(d.get("uid") or "").strip() if isinstance(d, dict) else ""
            returned_start = d.get("start") if isinstance(d, dict) else None
        except Exception as exc:
            # Cal.com may have committed the booking even when its success body is
            # unreadable. Treat the outcome as unknown so the caller reconciles it
            # before retrying or telling the prospect that booking failed.
            log.warning(
                "booking_response_unreadable",
                status=resp.status_code,
                error_type=type(exc).__name__,
            )
            return {
                "success": False,
                "category": "transient",
                "status_code": resp.status_code,
                "raw_body": raw_body,
            }
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
