"""Finding #20: caller timezone corrections own one monotonic calendar state."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.core.config import settings
from app.services.gpt_realtime import GPTRealtimeSession
from app.services.tools.crm_tools import CRMTools


@pytest.fixture(autouse=True)
def configured_calcom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 42)


def menu(timezone: str, start: str, label: str) -> dict[str, object]:
    return {
        "status": "available",
        "timezone": timezone,
        "generated_at": "2026-08-01T00:00:00+00:00",
        "slots": [
            {
                "slot_id": "slot_1",
                "start": start,
                "label": label,
                "timezone": timezone,
            }
        ],
        "block": f"Authoritative calendar for {timezone}: {label}",
    }


def live_session(
    variables: dict[str, object] | None = None,
) -> tuple[GPTRealtimeSession, CRMTools, AsyncMock]:
    call_variables = variables or {
        "leadName": "Lead",
        "leadEmail": "lead@example.com",
    }
    session = GPTRealtimeSession(
        db=MagicMock(),
        user_id=1,
        agent_config={
            "system_prompt": "Timezone state: {{tz_spoken}}\nCalendar state:\n{{availability_block}}",
            "enabled_tools": [],
        },
        variables=call_variables,
    )
    crm = CRMTools(db=MagicMock(), user_id=1, variables=session.variables)
    registry = MagicMock()
    registry.crm_tools = crm
    registry.get_all_tool_definitions.return_value = [
        {"type": "function", "name": "refresh_availability"}
    ]
    session.tool_registry = registry
    update = AsyncMock()
    session.connection = MagicMock(session=SimpleNamespace(update=update))
    crm.set_live_availability_loader(session._refresh_live_availability)  # noqa: SLF001
    crm.set_live_availability_invalidator(
        session._invalidate_live_availability  # noqa: SLF001
    )
    return session, crm, update


@pytest.mark.asyncio
async def test_unresolved_correction_clears_old_prompt_and_booking_slots() -> None:
    session, crm, update = live_session()
    old = menu(
        "America/New_York",
        "2026-08-03T13:00:00Z",
        "Monday at nine in the morning",
    )
    crm.apply_availability_menu(old, origin="preloaded")
    session.variables["availability_block"] = old["block"]
    session._instructions_timezone = "America/New_York"  # noqa: SLF001

    result = await crm.check_availability(time_zone="somewhere near Atlantis")

    assert result["error"] == "timezone_unresolved"
    assert crm._offered_slots == []  # noqa: SLF001
    assert crm._normalized_timezone is None  # noqa: SLF001
    assert "could not be resolved" in session.variables["availability_block"]
    assert old["block"] not in session.variables["availability_block"]
    assert session.variables["tzName"] == "unresolved"
    assert session.variables["tz_spoken"] == "unresolved"
    sent = update.await_args.kwargs["session"]
    assert "could not be resolved" in sent["instructions"]
    assert "TIMEZONE CORRECTION OVERRIDE" in sent["instructions"]
    assert "Ask exactly one clarification" in sent["instructions"]
    assert "Timezone: unresolved" in sent["instructions"]
    assert sent["tools"] == [{"type": "function", "name": "refresh_availability"}]
    assert session._availability_revision == 1  # noqa: SLF001

@pytest.mark.asyncio
async def test_unavailable_preload_preserves_resolved_timezone_for_refresh() -> None:
    session, crm, update = live_session(
        {
            "leadName": "Lead",
            "leadEmail": "lead@example.com",
            "state": "CA",
        }
    )
    unavailable = {
        "status": "unavailable",
        "timezone": "America/Los_Angeles",
        "generated_at": "2026-08-01T00:00:00+00:00",
        "slots": [],
        "block": "Calendar unavailable",
    }
    pacific = menu(
        "America/Los_Angeles",
        "2026-08-03T16:00:00Z",
        "Monday at nine in the morning",
    )
    fetch = AsyncMock(side_effect=[unavailable, pacific])

    with patch("app.services.availability.fetch_menu", fetch):
        assert await session.load_availability() is False
        refreshed = await crm.check_availability()

    assert refreshed["timezone"] == "America/Los_Angeles"
    assert session.variables["tzName"] == "America/Los_Angeles"
    assert fetch.await_args_list == [
        call("America/Los_Angeles"),
        call("America/Los_Angeles"),
    ]
    update.assert_awaited_once()


@pytest.mark.asyncio
async def test_late_preload_cannot_overwrite_caller_timezone_refresh() -> None:
    session, crm, update = live_session(
        {
            "leadName": "Lead",
            "leadEmail": "lead@example.com",
            "state": "CA",
        }
    )
    preload_started = asyncio.Event()
    release_preload = asyncio.Event()
    pacific = menu(
        "America/Los_Angeles",
        "2026-08-03T16:00:00Z",
        "Monday at nine in the morning",
    )
    arizona = menu(
        "America/Phoenix",
        "2026-08-03T17:00:00Z",
        "Monday at ten in the morning",
    )

    async def fetch(lead_tz: str) -> dict[str, object]:
        if lead_tz == "America/Los_Angeles":
            preload_started.set()
            await release_preload.wait()
            return pacific
        assert lead_tz == "America/Phoenix"
        return arizona

    with patch("app.services.availability.fetch_menu", side_effect=fetch):
        preload = asyncio.create_task(session.load_availability())
        await asyncio.wait_for(preload_started.wait(), timeout=1)
        refreshed = await crm.check_availability(time_zone="Arizona")
        release_preload.set()
        assert await asyncio.wait_for(preload, timeout=1) is False

    assert refreshed["timezone"] == "America/Phoenix"
    assert session.variables["tzName"] == "America/Phoenix"
    assert session.variables["availability_block"] == arizona["block"]
    assert [slot["start"] for slot in crm._offered_slots] == [  # noqa: SLF001
        "2026-08-03T17:00:00Z"
    ]
    assert [attempt["category"] for attempt in crm.get_booking_attempts()] == [
        "offered"
    ]
    update.assert_awaited_once()


@pytest.mark.asyncio
async def test_interactive_refreshes_are_serialized_and_newest_wins() -> None:
    session, crm, update = live_session()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    active = 0
    max_active = 0

    async def fetch(lead_tz: str) -> dict[str, object]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            if lead_tz == "America/New_York":
                first_started.set()
                await release_first.wait()
                return menu(
                    lead_tz,
                    "2026-08-03T13:00:00Z",
                    "Monday at nine in the morning",
                )
            return menu(
                lead_tz,
                "2026-08-03T16:00:00Z",
                "Monday at nine in the morning",
            )
        finally:
            active -= 1

    with patch("app.services.availability.fetch_menu", side_effect=fetch):
        first = asyncio.create_task(
            session._refresh_live_availability("America/New_York", "offered")  # noqa: SLF001
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)
        second = asyncio.create_task(
            session._refresh_live_availability(  # noqa: SLF001
                "America/Los_Angeles", "offered"
            )
        )
        await asyncio.sleep(0)
        assert not second.done()
        assert active == 1
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)

    assert max_active == 1
    assert session._availability_revision == 2  # noqa: SLF001
    assert session.variables["tzName"] == "America/Los_Angeles"
    assert crm._normalized_timezone == "America/Los_Angeles"  # noqa: SLF001
    assert update.await_count == 2


@pytest.mark.asyncio
async def test_booking_conflict_refresh_republishes_same_menu_to_prompt_and_gate() -> None:
    session, crm, update = live_session()
    initial = menu(
        "UTC",
        "2026-08-03T09:00:00Z",
        "Monday at nine in the morning",
    )
    fresh = menu(
        "UTC",
        "2026-08-04T14:00:00Z",
        "Tuesday at two in the afternoon",
    )
    create_booking = AsyncMock(
        return_value={"success": False, "category": "conflict", "status_code": 409}
    )
    reconcile = AsyncMock(
        return_value={"success": False, "category": "not_found", "status_code": 200}
    )

    with (
        patch(
            "app.services.availability.fetch_menu",
            new=AsyncMock(side_effect=[initial, fresh]),
        ),
        patch("app.services.calcom_client.create_booking", create_booking),
        patch("app.services.calcom_client.find_existing_booking", reconcile),
    ):
        await crm.check_availability(time_zone="UTC")
        crm.observe_user_utterance("Monday at nine in the morning works")
        assert (await crm.select_slot("slot_1"))["success"] is True
        result = await crm.book_appointment(
            "2026-08-03T09:00:00Z",
            icp={"offer_types": ["commercial solar"], "min_kw": 50},
        )

    assert result["error"] == "slot_conflict"
    assert result["menu"] == fresh["block"]
    assert session.variables["availability_block"] == fresh["block"]
    assert crm._offered_slots[0]["start"] == "2026-08-04T14:00:00Z"  # noqa: SLF001
    assert update.await_count == 2
    create_booking.assert_awaited_once()
