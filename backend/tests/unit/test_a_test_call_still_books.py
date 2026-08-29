"""A ring test must still reach the calendar, or it proves nothing.

Introduced and caught within two days. The 2026-08-27 guard stopped a test call
buying a lead list by skipping fulfilment staging and passing a null claim token.
But _write_booking_to_calendar reads a null token as "another attempt already holds
the booking lease" and returns without creating anything.

So on 2026-08-29 the agent told Sami "Thursday at six in the evening is set" and
nothing was booked. Because the calendar write is fire-and-forget, the agent had
already promised the time before the write quietly gave up. It lied, and only the
booking-attempt record showed it: ["preloaded", "selected", "not_found"] with no
create.

Two different things were being said with one null: "we deliberately took no lease"
and "we lost the race for the lease". They now have separate names.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.tools import crm_tools
from app.services.tools.crm_tools import CRMTools

SLOT = {"start": "2026-09-03T16:00:00Z"}
ICP = {"offer_types": ["rooftop"], "min_kw": 100, "states": ["Kern"]}


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 123)
    monkeypatch.setattr(settings, "BOOKING_TEAM_TIMEZONE", "Europe/Stockholm")
    monkeypatch.setattr(
        "app.services.tools.crm_tools.stage_fulfilment_intent",
        AsyncMock(return_value="intent-key"),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.claim_fulfilment_booking",
        AsyncMock(return_value=uuid.UUID(int=1)),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.authorize_fulfilment_booking",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.finalize_fulfilment_intent",
        AsyncMock(return_value=True),
    )


async def run_call(conversation_id: str, create: AsyncMock) -> dict:
    tools = CRMTools(
        db=MagicMock(),
        user_id=1,
        variables={
            "leadName": "Sami",
            "leadEmail": "lead@example.com",
            "conversationId": conversation_id,
        },
    )
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=[SLOT])
    ):
        await tools.check_availability(time_zone="UTC")
    tools.observe_user_utterance("the first one")
    assert (await tools.select_slot("slot_1"))["success"] is True

    with (
        patch("app.services.calcom_client.create_booking", create),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=ICP)
        await crm_tools.wait_for_calendar_writes()
    return result


@pytest.mark.asyncio
async def test_a_ring_test_actually_reaches_the_calendar() -> None:
    """Skipping the PAID work must not also skip the booking."""
    create = AsyncMock(return_value={"success": True, "uid": "uid-test"})

    result = await run_call("ringtest-1756000000", create)

    assert result["success"] is True
    create.assert_awaited_once(), "the agent said it was booked; it must be booked"


@pytest.mark.asyncio
async def test_a_real_call_still_reaches_the_calendar() -> None:
    create = AsyncMock(return_value={"success": True, "uid": "uid-real"})

    result = await run_call("conv_real_1", create)

    assert result["success"] is True
    create.assert_awaited_once()
