"""The agent confirms the time, then the calendar catches up.

Sami, 2026-08-09, after hearing the agent say "our booking step is still
processing in the background, so give it a moment": *"Who said I want to give it
a moment? He should just trigger the tool to book and then just not wait for
it... immediately he says something along the lines of 'sounds good Sami,
Wednesday at midday it is' and then he immediately moves on."*

The measured gap was seven seconds, during which the prompt claimed every tool
answers instantly. It was not a phrasing problem — the model was correctly
describing real silence, and no prompt rule survives contact with a lie.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.tools import crm_tools
from app.services.tools.crm_tools import CRMTools

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


async def ready_to_book() -> CRMTools:
    tools = CRMTools(
        db=MagicMock(),
        user_id=1,
        variables={"leadName": "Sami", "leadEmail": "lead@example.com"},
    )
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=[SLOT])
    ):
        await tools.check_availability(time_zone="UTC")
    tools.observe_user_utterance("Monday at nine in the morning")
    await tools.select_slot("slot_1")
    return tools


@pytest.mark.asyncio
async def test_the_agent_is_told_it_is_booked_before_calcom_is_ever_called() -> None:
    """The whole ruling, in one assertion.

    If this ever goes back to waiting, the agent starts narrating the wait again
    — that is not a hypothetical, it is what the 2026-08-08 recording contains.
    """
    tools = await ready_to_book()
    calcom_was_called = asyncio.Event()

    async def slow_create(**_kwargs: object) -> dict[str, object]:
        calcom_was_called.set()
        await asyncio.sleep(30)  # never completes within this test
        raise AssertionError("unreachable")

    with (
        patch("app.services.calcom_client.create_booking", slow_create),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        result = await asyncio.wait_for(
            tools.book_appointment(SLOT["start"], icp=ICP), timeout=1
        )

        assert result["success"] is True
        assert "Booked" in result["message"]

        for task in list(crm_tools._CALENDAR_WRITES):  # noqa: SLF001
            task.cancel()


@pytest.mark.asyncio
async def test_the_answer_does_not_wait_on_a_hanging_calendar() -> None:
    """A calendar that never answers must not hold the conversation open.

    This is the same property from the caller's side: whatever Cal.com is doing,
    the sentence after "which time works" arrives immediately.
    """
    tools = await ready_to_book()
    started = asyncio.get_running_loop().time()

    async def never_answers(**_kwargs: object) -> dict[str, object]:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    with (
        patch("app.services.calcom_client.create_booking", never_answers),
        patch("app.services.calcom_client.find_existing_booking", never_answers),
    ):
        await tools.book_appointment(SLOT["start"], icp=ICP)
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 0.5

        for task in list(crm_tools._CALENDAR_WRITES):  # noqa: SLF001
            task.cancel()


@pytest.mark.asyncio
async def test_a_failed_write_names_the_prospect_and_the_time_they_were_promised(
    raised_alerts: list[dict[str, str]],
) -> None:
    """Requirement 5: a booking that fails after confirmation is never silent.

    "A booking failed" is not actionable at eight in the morning. The prospect's
    email and the exact words they heard are.
    """
    tools = await ready_to_book()

    with (
        patch(
            "app.services.calcom_client.create_booking",
            AsyncMock(return_value={"success": False, "category": "rejected", "status_code": 400}),
        ),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        await tools.book_appointment(SLOT["start"], icp=ICP)
        await crm_tools.wait_for_calendar_writes()

    assert len(raised_alerts) == 1
    message = raised_alerts[0]["message"]
    assert "lead@example.com" in message
    assert "Monday at nine in the morning" in message
    assert raised_alerts[0]["dedup_key"] == "voice-booking-unconfirmed:intent-1"


@pytest.mark.asyncio
async def test_a_successful_write_alerts_nobody(
    raised_alerts: list[dict[str, str]],
) -> None:
    """The alert has to be rare or it stops being read."""
    tools = await ready_to_book()

    with (
        patch(
            "app.services.calcom_client.create_booking",
            AsyncMock(
                return_value={"success": True, "category": "success", "uid": "booking-9"}
            ),
        ),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        await tools.book_appointment(SLOT["start"], icp=ICP)
        await crm_tools.wait_for_calendar_writes()

    assert raised_alerts == []
    assert any(
        attempt.get("uid") == "booking-9" for attempt in tools.get_booking_attempts()
    )


@pytest.mark.asyncio
async def test_the_write_survives_the_sentence_that_started_it() -> None:
    """asyncio holds only a weak reference to a running task.

    Without a strong reference at module scope the garbage collector is free to
    cancel a booking mid-flight, which would lose it silently — the exact failure
    mode this whole design is built to avoid.
    """
    tools = await ready_to_book()
    landed = asyncio.Event()

    async def slow_but_real(**_kwargs: object) -> dict[str, object]:
        await asyncio.sleep(0.05)
        landed.set()
        return {"success": True, "category": "success", "uid": "late-booking"}

    with (
        patch("app.services.calcom_client.create_booking", slow_but_real),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        await tools.book_appointment(SLOT["start"], icp=ICP)
        assert not landed.is_set()  # genuinely still in flight

        assert await crm_tools.wait_for_calendar_writes(timeout=2) == 1
        assert landed.is_set()


@pytest.mark.asyncio
async def test_asking_twice_books_once() -> None:
    """A model that calls the tool again must not produce a second appointment."""
    tools = await ready_to_book()
    create = AsyncMock(
        return_value={"success": True, "category": "success", "uid": "booking-1"}
    )

    with (
        patch("app.services.calcom_client.create_booking", create),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        first = await tools.book_appointment(SLOT["start"], icp=ICP)
        second = await tools.book_appointment(SLOT["start"], icp=ICP)
        await crm_tools.wait_for_calendar_writes()

    assert first == second
    assert create.await_count == 1
