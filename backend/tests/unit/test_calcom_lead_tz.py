"""Contracts for lead-timezone business-hours slot filtering (B3).

The window [BOOKING_HOUR_START, BOOKING_HOUR_END) must be evaluated in the
LEAD's timezone — never the team's — so a US lead is not offered slots that are
business hours in Stockholm but the middle of the night locally. The team
timezone is only the fallback anchor when no valid lead timezone is known.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.calcom_client import get_business_slots

# 2026-07-21 is a Tuesday; 2026-07-25 is a Saturday.
# America/Los_Angeles is UTC-7 in July; Europe/Stockholm is UTC+2.
SLOT_LATE_NIGHT_LA = "2026-07-21T06:00:00Z"  # 08:00 Stockholm / 23:00 Mon LA
SLOT_MORNING_LA = "2026-07-21T16:00:00Z"  # 18:00 Stockholm / 09:00 LA
SLOT_AFTERNOON_LA = "2026-07-21T20:00:00Z"  # 22:00 Stockholm / 13:00 LA
SLOT_SATURDAY = "2026-07-25T16:00:00Z"  # Saturday in both timezones


def make_slots_context(data: dict[str, list[dict[str, str]]]) -> MagicMock:
    response = MagicMock(status_code=200)
    response.json.return_value = {"data": data}
    response.raise_for_status.return_value = None
    client = MagicMock(get=AsyncMock(return_value=response))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    return context


@pytest.fixture(autouse=True)
def booking_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 42)
    monkeypatch.setattr(settings, "BOOKING_TEAM_TIMEZONE", "Europe/Stockholm")
    monkeypatch.setattr(settings, "BOOKING_HOUR_START", 8)
    monkeypatch.setattr(settings, "BOOKING_HOUR_END", 20)


@pytest.mark.asyncio
async def test_window_is_evaluated_in_the_lead_timezone() -> None:
    context = make_slots_context(
        {
            "2026-07-21": [
                {"start": SLOT_LATE_NIGHT_LA},
                {"start": SLOT_MORNING_LA},
                {"start": SLOT_AFTERNOON_LA},
            ],
            "2026-07-25": [{"start": SLOT_SATURDAY}],
        }
    )

    with patch("app.services.calcom_client.httpx.AsyncClient", return_value=context):
        slots = await get_business_slots(lead_tz="America/Los_Angeles")

    starts = [slot["start"] for slot in slots]
    # Stockholm business hours ≠ LA business hours: 06:00Z (8am Stockholm) is
    # 11pm in LA and must be dropped; the LA-daytime slots must survive.
    assert SLOT_LATE_NIGHT_LA not in starts
    assert starts == [SLOT_MORNING_LA, SLOT_AFTERNOON_LA]
    assert [slot["label"] for slot in slots] == ["Tuesday 9:00 AM", "Tuesday 1:00 PM"]


@pytest.mark.asyncio
async def test_weekend_check_uses_lead_local_day() -> None:
    context = make_slots_context({"2026-07-25": [{"start": SLOT_SATURDAY}]})

    with patch("app.services.calcom_client.httpx.AsyncClient", return_value=context):
        slots = await get_business_slots(lead_tz="America/Los_Angeles")

    assert slots == []


@pytest.mark.asyncio
async def test_invalid_lead_timezone_falls_back_to_team_window() -> None:
    context = make_slots_context(
        {
            "2026-07-21": [
                {"start": SLOT_LATE_NIGHT_LA},
                {"start": SLOT_AFTERNOON_LA},
            ]
        }
    )

    with patch("app.services.calcom_client.httpx.AsyncClient", return_value=context):
        slots = await get_business_slots(lead_tz="Nowhere/Land")

    # With no usable lead timezone the window anchors on the team timezone:
    # 06:00Z (8am Stockholm) is kept, 20:00Z (10pm Stockholm) is dropped.
    assert [slot["start"] for slot in slots] == [SLOT_LATE_NIGHT_LA]
    assert slots[0]["label"] == "Tuesday 8:00 AM"
