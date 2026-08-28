"""A test call must never spend fulfilment money.

Found the expensive way on 2026-08-27. Sami ran `ring_test.py`, the agent booked,
and a REAL lead-list build started - `status: backfilling`, live, spending. It was
stopped by hand.

The guard existed. It was in the wrong repo. `reply_router/factory.py` has
TEST_SEED_PREFIXES and refuses to build for a `ringtest-` conversation, but the ring
test does not go through the router: it drives voice-pro directly, and voice-pro has
its OWN fulfilment sender, which was written by copying the router's key logic and
NOT its safety check. Two senders, one guard.

So the check now lives in the module that owns the paid work, where any future
sender inherits it rather than having to remember it.

The booking itself must still complete. A ring test is worth having precisely
because it exercises the real path end to end; it just must not buy anything.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.fulfilment_webhook import is_test_conversation
from app.services.tools import crm_tools
from app.services.tools.crm_tools import CRMTools

SLOT = {"start": "2026-09-01T09:00:00Z"}
ICP = {"offer_types": ["commercial solar"], "min_kw": 200, "states": ["Kern"]}


@pytest.fixture(autouse=True)
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 123)
    monkeypatch.setattr(settings, "BOOKING_TEAM_TIMEZONE", "Europe/Stockholm")
    monkeypatch.setattr(
        "app.services.tools.crm_tools.finalize_fulfilment_intent",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.claim_fulfilment_booking",
        AsyncMock(return_value=uuid.UUID(int=1)),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.authorize_fulfilment_booking",
        AsyncMock(return_value=True),
    )


def make_tools(conversation_id: str) -> CRMTools:
    return CRMTools(
        db=MagicMock(),
        user_id=1,
        variables={
            "leadName": "Sami",
            "leadEmail": "lead@example.com",
            "conversationId": conversation_id,
        },
    )


async def book(tools: CRMTools) -> dict:
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=[SLOT])
    ):
        await tools.check_availability(time_zone="UTC")
    tools.observe_user_utterance("the first one")
    assert (await tools.select_slot("slot_1"))["success"] is True
    with patch(
        "app.services.calcom_client.create_booking",
        AsyncMock(return_value={"success": True, "uid": "uid-1"}),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=ICP)
    await crm_tools.wait_for_calendar_writes()
    return result


@pytest.mark.parametrize(
    "conversation_id",
    ["ringtest-1756282800", "e2e-abc", "seed-99", "smoke-1", "verify-7", "proof-2"],
)
def test_every_test_prefix_is_recognised(conversation_id: str) -> None:
    assert is_test_conversation(conversation_id) is True


@pytest.mark.parametrize("conversation_id", ["conv_abc123", "01HXYZ", "", None])
def test_a_real_conversation_is_not_mistaken_for_a_test(conversation_id: object) -> None:
    assert is_test_conversation(conversation_id) is False


@pytest.mark.asyncio
async def test_a_ring_test_books_but_stages_no_paid_work() -> None:
    stage = AsyncMock(return_value="intent-key")
    with patch("app.services.tools.crm_tools.stage_fulfilment_intent", stage):
        result = await book(make_tools("ringtest-1756282800"))

    assert result["success"] is True, "the ring test must still exercise booking"
    stage.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_real_call_still_stages_the_paid_work() -> None:
    """The guard must not quietly break the thing the product is for."""
    stage = AsyncMock(return_value="intent-key")
    with patch("app.services.tools.crm_tools.stage_fulfilment_intent", stage):
        result = await book(make_tools("conv_real_123"))

    assert result["success"] is True
    stage.assert_awaited_once()
