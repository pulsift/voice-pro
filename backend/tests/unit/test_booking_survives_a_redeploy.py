"""What happens to a promised booking when the process goes away.

Every test here exists because [Codex] found the failure on 2026-08-09 and the
existing suite could not have. The ported booking tests all drain the detached
write inside their patch block, with the event loop, the mocks and the session
guaranteed alive — which is exactly the context production does NOT provide. A
green suite there proves eventual completion under an artificially preserved
request, not detached behaviour.

The shape being pinned: the caller has ALREADY been told they are booked. From
that instant, the only unacceptable outcome is silence.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import availability
from app.services.tools import crm_tools
from app.services.tools.crm_tools import CRMTools
from app.services.tools.registry import ToolRegistry

SLOT = {"start": "2026-07-13T09:00:00Z", "label": "Monday at nine in the morning"}
ICP = {"offer_types": ["rooftop solar"], "min_kw": 50, "states": ["Texas"]}


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 123)
    for name, value in (
        ("stage_fulfilment_intent", AsyncMock(return_value="intent-1")),
        ("claim_fulfilment_booking", AsyncMock(return_value=uuid.UUID(int=1))),
        ("authorize_fulfilment_booking", AsyncMock(return_value=True)),
        ("finalize_fulfilment_intent", AsyncMock(return_value=True)),
    ):
        monkeypatch.setattr(f"app.services.tools.crm_tools.{name}", value)


@pytest.fixture(autouse=True)
def raised_alerts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    raised: list[dict[str, str]] = []

    async def capture(*, dedup_key: str, message: str) -> bool:
        raised.append({"dedup_key": dedup_key, "message": message})
        return True

    monkeypatch.setattr("app.services.tools.crm_tools.raise_operator_alert", capture)
    return raised


def _registry_over(tools: CRMTools) -> SimpleNamespace:
    """The only thing ToolRegistry.wait_for_calendar_writes touches.

    Constructing a real registry needs a database, an agent and a workspace; the
    method under test reads one attribute. Calling it unbound over this keeps the
    REAL production method in the assertion, which is the point — a stub of the
    method would pass no matter how teardown was wired.
    """
    return SimpleNamespace(crm_tools=tools)


async def ready_to_book(email: str = "lead@example.com") -> CRMTools:
    tools = CRMTools(
        db=MagicMock(), user_id=1, variables={"leadName": "Sami", "leadEmail": email}
    )
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=[SLOT])
    ):
        await tools.check_availability(time_zone="UTC")
    tools.observe_user_utterance("Monday at nine in the morning")
    await tools.select_slot("slot_1")
    return tools


@pytest.mark.asyncio
async def test_a_redeploy_mid_write_still_tells_somebody(
    raised_alerts: list[dict[str, str]],
) -> None:
    """SIGTERM between the booking lease and the Cal.com POST.

    CancelledError is a BaseException, so `except Exception` let it straight
    through: the caller had been told they were booked, the durable record said a
    write was dispatched, and NOTHING alerted. The prospect would have found out
    by not receiving an invite. Railway redeploys far more often than Cal.com
    fails, so this window was open on every single deploy.
    """
    tools = await ready_to_book()
    reached_calcom = asyncio.Event()

    async def hangs(**_kwargs: object) -> dict[str, object]:
        reached_calcom.set()
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    with (
        patch("app.services.calcom_client.create_booking", hangs),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        await tools.book_appointment(SLOT["start"], icp=ICP)
        await asyncio.wait_for(reached_calcom.wait(), timeout=2)

        task = next(iter(tools._calendar_writes))  # noqa: SLF001
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert len(raised_alerts) == 1
    assert "the server restarted part-way through the write" in raised_alerts[0]["message"]
    assert "lead@example.com" in raised_alerts[0]["message"]
    assert "Monday at nine in the morning" in raised_alerts[0]["message"]


@pytest.mark.asyncio
async def test_shutdown_waits_for_a_promised_booking_instead_of_killing_it() -> None:
    """The drain has to run BEFORE the process tears itself down.

    Cancelling here is not a lost request — it is a prospect who was told on the
    phone that they were booked and is not.
    """
    tools = await ready_to_book()
    landed = asyncio.Event()

    async def slow(**_kwargs: object) -> dict[str, object]:
        await asyncio.sleep(0.05)
        landed.set()
        return {"success": True, "category": "success", "uid": "survived-shutdown"}

    with (
        patch("app.services.calcom_client.create_booking", slow),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        await tools.book_appointment(SLOT["start"], icp=ICP)
        assert not landed.is_set()

        assert await crm_tools.drain_calendar_writes_for_shutdown(timeout=5) == 1
        assert landed.is_set()


@pytest.mark.asyncio
async def test_one_call_never_waits_on_another_prospects_calendar() -> None:
    """Teardown drains THIS call's writes, not the whole process's.

    Draining the module-global set made a call trying to hang up sit through some
    other prospect's slow Cal.com request — up to ten seconds of dead air on a
    line that had already said goodbye.
    """
    fast = await ready_to_book("fast@example.com")
    slow = await ready_to_book("slow@example.com")
    parked = asyncio.Event()

    async def create(**kwargs: object) -> dict[str, object]:
        # One mock for both calls, branching on the attendee, so swapping patches
        # mid-flight cannot let the slow write finish early by accident.
        if kwargs.get("email") == "slow@example.com":
            parked.set()
            await asyncio.sleep(30)
            raise AssertionError("unreachable")
        return {"success": True, "category": "success", "uid": "quick"}

    with (
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
        patch("app.services.calcom_client.create_booking", create),
    ):
        await slow.book_appointment(SLOT["start"], icp=ICP)
        await asyncio.wait_for(parked.wait(), timeout=2)

        await fast.book_appointment(SLOT["start"], icp=ICP)

        # Exactly what teardown does, through the real delegation chain the
        # WebSocket handler uses. A drain that reverted to the process-global
        # set would sit here for the full ten-second default waiting on the
        # OTHER prospect's write.
        started = asyncio.get_running_loop().time()
        await asyncio.wait_for(
            ToolRegistry.wait_for_calendar_writes(_registry_over(fast)), timeout=3
        )
        assert asyncio.get_running_loop().time() - started < 1

        assert slow.calendar_writes  # the other call's write is still outstanding
        for task in list(slow.calendar_writes):
            task.cancel()


@pytest.mark.asyncio
async def test_a_second_booking_cannot_make_the_first_alert_about_the_wrong_person(
    raised_alerts: list[dict[str, str]],
) -> None:
    """The alert reads its arguments, never the session.

    Reading `self._pending_booking_*` meant two overlapping bookings could send
    the operator to fix the wrong prospect's booking — worse than no alert,
    because it looks actionable and is wrong.
    """
    first = await ready_to_book("first@example.com")

    with (
        patch(
            "app.services.calcom_client.create_booking",
            AsyncMock(
                return_value={"success": False, "category": "rejected", "status_code": 400}
            ),
        ),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        await first.book_appointment(SLOT["start"], icp=ICP)
        # A second session books while the first write is still in flight.
        second = await ready_to_book("second@example.com")
        await second.book_appointment(SLOT["start"], icp=ICP)
        await crm_tools.wait_for_calendar_writes()

    assert len(raised_alerts) == 2
    # Each alert names its OWN prospect, and neither borrowed the other's.
    named = sorted(
        next(
            address
            for address in ("first@example.com", "second@example.com")
            if address in alert["message"]
        )
        for alert in raised_alerts
    )
    assert named == ["first@example.com", "second@example.com"]


# --- the calendar rails, which are not ceilings unless they actually cap -------


def iso(day: int, hour: int) -> str:
    return f"2026-07-{day:02d}T{hour:02d}:00:00Z"


def test_the_ceiling_is_a_ceiling_even_below_one_slot_per_day() -> None:
    """`max(1, max_slots // days)` silently chose to break the ceiling.

    Five days under a ceiling of two returned five slots. Anything sizing a
    prompt off that number was being lied to.
    """
    raw = [{"start": iso(day, hour)} for day in (13, 14, 15, 16, 17) for hour in (9, 14)]

    menu = availability.build_menu(raw, "UTC", max_slots=2)

    assert len(menu["slots"]) <= 2
    assert menu["slots"][0]["day"] == "Monday"  # the NEAREST days survive


def test_a_day_thinned_to_one_slot_is_not_always_eight_in_the_morning() -> None:
    """_thin with limit=1 took index zero, so every day collapsed to its earliest.

    A caller asking for the afternoon would be told there was none, on a calendar
    where every single afternoon was free.
    """
    raw = [{"start": iso(13, hour)} for hour in (8, 12, 16)]

    menu = availability.build_menu(raw, "UTC", max_per_day=1)

    assert len(menu["slots"]) == 1
    assert menu["slots"][0]["label"] != "Monday at eight in the morning"
