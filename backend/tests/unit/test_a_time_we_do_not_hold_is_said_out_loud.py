"""Affirmative by default, and the one refusal that is allowed to name a time.

Sami's ruling, 2026-08-29: the tool does the judging, so the agent can answer a
named time with "sure" and never think in the moment. That is only safe if the
tool, when it refuses, hands back the TRUE sentence for the refusal it is making.

Three refusals used to wear one error and one apology. They are now told apart,
because only one of them may say a time is gone:

  * they have not chosen anything yet          -> re-offer, never "it's gone"
  * they named something inside a REFUSAL      -> re-offer, never "it's gone"
  * they named a real time we do not hold      -> "that one's actually gone"

Getting the third one's boundary wrong is how the agent ends up telling somebody
that a time they had just turned down is unavailable.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.tools.crm_tools import CRMTools

MONDAY_9 = {"start": "2026-07-13T09:00:00Z", "label": "Monday at nine in the morning"}
MONDAY_3 = {"start": "2026-07-13T15:00:00Z", "label": "Monday at three in the afternoon"}
TUESDAY_10 = {"start": "2026-07-14T10:00:00Z", "label": "Tuesday at ten in the morning"}
ICP = {"offer_types": ["commercial solar"], "min_kw": 50, "states": ["Texas"]}


@pytest.fixture(autouse=True)
def raised_alerts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    raised: list[dict[str, str]] = []

    async def capture(*, dedup_key: str, message: str) -> bool:
        raised.append({"dedup_key": dedup_key, "message": message})
        return True

    monkeypatch.setattr("app.services.tools.crm_tools.raise_operator_alert", capture)
    return raised


@pytest.fixture(autouse=True)
def configured_calcom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 123)
    monkeypatch.setattr(settings, "BOOKING_TEAM_TIMEZONE", "Europe/Stockholm")
    for name, value in (
        ("stage_fulfilment_intent", AsyncMock(return_value="intent-key")),
        ("finalize_fulfilment_intent", AsyncMock(return_value=True)),
        ("claim_fulfilment_booking", AsyncMock(return_value=uuid.UUID(int=1))),
        ("authorize_fulfilment_booking", AsyncMock(return_value=True)),
    ):
        monkeypatch.setattr(f"app.services.tools.crm_tools.{name}", value)


async def offered(slots: list[dict[str, str]]) -> CRMTools:
    tools = CRMTools(
        db=MagicMock(),
        user_id=1,
        variables={"leadName": "Sami", "leadEmail": "seeded@example.com"},
    )
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=slots)
    ):
        await tools.check_availability(time_zone="UTC")
    return tools


@pytest.mark.asyncio
async def test_a_time_we_do_not_hold_is_named_as_gone_with_two_real_swaps() -> None:
    tools = await offered([MONDAY_9, MONDAY_3])
    tools.observe_user_utterance("could we do Monday at seven in the evening?")

    result = await tools.select_slot("slot_1")

    assert result["error"] == "slot_unavailable"
    assert "actually" in result["message"] and "gone" in result["message"]
    assert "Monday at nine in the morning" in result["message"]
    assert "Monday at three in the afternoon" in result["message"]


@pytest.mark.asyncio
async def test_the_swaps_offered_are_on_the_day_they_asked_for() -> None:
    tools = await offered([MONDAY_9, MONDAY_3, TUESDAY_10])
    tools.observe_user_utterance("how about Tuesday at four in the afternoon?")

    message = (await tools.select_slot("slot_1"))["message"]

    assert "Tuesday at ten in the morning" in message
    assert "Monday" not in message


@pytest.mark.asyncio
async def test_the_swaps_are_only_ever_times_we_actually_hold() -> None:
    """A walk-back may never invent a time; that is the whole point of it."""
    tools = await offered([MONDAY_9, MONDAY_3])
    tools.observe_user_utterance("Monday at seven in the evening?")

    message = (await tools.select_slot("slot_1"))["message"]
    labels = {slot["label"] for slot in tools._offered_slots}

    named = [label for label in labels if label in message]
    assert len(named) == 2
    # Nothing that looks like a time appears in the message except those two.
    for label in labels:
        assert message.count(label) <= 1


@pytest.mark.asyncio
async def test_a_time_named_inside_a_refusal_is_never_called_gone() -> None:
    """The Codex #6 lie, in new clothes.

    "No, Monday at nine doesn't work" names a real time and matches nothing,
    because a refusal empties the candidate set. Rendering that as "that one's
    actually gone" would tell the caller a time they had just turned down was
    unavailable. The refusal check must run first, and this fails if anyone
    reorders them.
    """
    tools = await offered([MONDAY_9, MONDAY_3])
    tools.observe_user_utterance("no, Monday at nine doesn't work for me")

    result = await tools.select_slot("slot_1")

    assert result["error"] == "ambiguous_slot_selection"
    assert "gone" not in result["message"]


@pytest.mark.asyncio
async def test_saying_nothing_about_a_time_is_not_a_time_we_do_not_hold() -> None:
    tools = await offered([MONDAY_9, MONDAY_3])
    tools.observe_user_utterance("hmm, what do you reckon")

    result = await tools.select_slot("slot_1")

    assert result["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_after_it_is_booked_a_change_of_mind_is_refused_and_alerted(
    raised_alerts: list[dict[str, str]],
) -> None:
    """The lie this change could have created, pinned shut.

    The booking write is fire-and-forget: by the time anyone changes their mind,
    the caller has HEARD the original time. Affirmative-by-default would answer
    "sure, Monday at three" while Cal.com still held nine, and nothing would ever
    say otherwise. So this is the one moment the agent must not agree, and a
    human is told instead.
    """
    tools = await offered([MONDAY_9, MONDAY_3])
    tools.observe_user_utterance("Monday at nine please")
    await tools.select_slot("slot_1")
    with patch(
        "app.services.calcom_client.create_booking",
        AsyncMock(return_value={"success": True, "uid": "bk_1"}),
    ):
        assert (await tools.book_appointment(MONDAY_9["start"], icp=ICP))["success"]

    tools.observe_user_utterance("actually can we make it Monday at three instead")
    late = await tools.select_slot("slot_2")

    assert late["error"] == "already_booked"
    assert "already gone through" in late["message"]
    assert any(
        "change the time after it was already booked" in alert["message"]
        for alert in raised_alerts
    )
