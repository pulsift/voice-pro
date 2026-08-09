"""Never say the time twice; never say goodbye twice.

Both rules were prose in the prompt, and both were being CONTRADICTED by the
tools the prompt was talking to — which is the whole reason they moved into code,
the same way the dead-air limit did on 2026-08-08.

  - book_appointment cached its success and replayed it verbatim, so a model that
    called it again was told, again, to "say the day and time back to them ONCE".
  - end_call replayed "say your one closing line NOW", so a second call asked for
    a second goodbye.

A rule the code argues with is not a rule. The prompt can only win an argument
with a tool result if the tool result agrees with it.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.tools import crm_tools
from app.services.tools.call_control_tools import CallControlTools
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
        ("raise_operator_alert", AsyncMock(return_value=True)),
    ):
        monkeypatch.setattr(f"app.services.tools.crm_tools.{name}", value)


# --- goodbyes ----------------------------------------------------------------


def test_the_first_end_call_asks_for_a_closing_line() -> None:
    tools = CallControlTools()

    message = tools._execute_end_call({"reason": "conversation_complete"})["message"]  # noqa: SLF001

    assert "closing line" in message.lower()


def test_the_second_end_call_asks_for_silence_not_another_goodbye() -> None:
    """The fix. Asking twice used to produce the instruction twice."""
    tools = CallControlTools()
    tools._execute_end_call({"reason": "conversation_complete"})  # noqa: SLF001

    second = tools._execute_end_call({"reason": "conversation_complete"})["message"]  # noqa: SLF001

    assert "already said goodbye" in second.lower()
    assert "closing line" not in second.lower()


def test_the_count_belongs_to_one_call_not_to_the_process() -> None:
    """A fresh call must get the full instruction, not the previous call's tail."""
    CallControlTools()._execute_end_call({"reason": "conversation_complete"})  # noqa: SLF001

    fresh = CallControlTools()._execute_end_call({"reason": "conversation_complete"})  # noqa: SLF001

    assert "closing line" in fresh["message"].lower()


def test_a_machine_still_gets_silence_on_the_first_request() -> None:
    """Voicemail wants no message at all, and that rule is unchanged."""
    message = CallControlTools()._execute_end_call({"reason": "voicemail"})["message"]  # noqa: SLF001

    assert "say nothing" in message.lower()


# --- the booked time ---------------------------------------------------------


async def booked_once() -> CRMTools:
    tools = CRMTools(
        db=MagicMock(), user_id=1,
        variables={"leadName": "Sami", "leadEmail": "lead@example.com"},
    )
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=[SLOT])
    ):
        await tools.check_availability(time_zone="UTC")
    tools.observe_user_utterance("Monday at nine in the morning")
    await tools.select_slot("slot_1")
    with (
        patch(
            "app.services.calcom_client.create_booking",
            AsyncMock(return_value={"success": True, "category": "success", "uid": "b1"}),
        ),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        await tools.book_appointment(SLOT["start"], icp=ICP)
        await crm_tools.wait_for_calendar_writes()
    return tools


@pytest.mark.asyncio
async def test_booking_again_does_not_ask_for_the_time_a_second_time() -> None:
    tools = await booked_once()

    second = await tools.book_appointment(SLOT["start"], icp=ICP)

    assert second["success"] is True
    assert "do not" in second["message"].lower()
    assert "say the day and time back" not in second["message"].lower()


@pytest.mark.asyncio
async def test_the_first_answer_still_asks_for_the_confirmation() -> None:
    """The caller must be told once. Suppressing BOTH would be the old bug back."""
    tools = CRMTools(
        db=MagicMock(), user_id=1,
        variables={"leadName": "Sami", "leadEmail": "lead@example.com"},
    )
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=[SLOT])
    ):
        await tools.check_availability(time_zone="UTC")
    tools.observe_user_utterance("Monday at nine in the morning")
    await tools.select_slot("slot_1")

    with (
        patch(
            "app.services.calcom_client.create_booking",
            AsyncMock(return_value={"success": True, "category": "success", "uid": "b1"}),
        ),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        first = await tools.book_appointment(SLOT["start"], icp=ICP)
        await crm_tools.wait_for_calendar_writes()

    assert "say the day and time back" in first["message"].lower()
