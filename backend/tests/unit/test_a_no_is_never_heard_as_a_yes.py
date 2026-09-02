"""Findings from the GPT-5.6 judge pass, 2026-09-02, each reproduced first.

Saying yes by default made two old holes expensive. Before this change a
mis-read "no" only meant an awkward turn; now it pins a slot, forces
book_appointment, and the caller is told a time they refused is theirs. The
booking write is fire-and-forget, so nobody finds out.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services.tools.crm_tools import CRMTools

MONDAY_9 = {"start": "2026-07-13T09:00:00Z", "label": "Monday at nine in the morning"}
MONDAY_3 = {"start": "2026-07-13T15:00:00Z", "label": "Monday at three in the afternoon"}
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


async def offered(slots: list[dict[str, str]] | None = None) -> CRMTools:
    tools = CRMTools(
        db=MagicMock(),
        user_id=1,
        variables={"leadName": "Sami", "leadEmail": "seeded@example.com"},
    )
    with patch(
        "app.services.calcom_client.get_open_slots",
        AsyncMock(return_value=slots or [MONDAY_9, MONDAY_3]),
    ):
        await tools.check_availability(time_zone="UTC")
    await tools.record_fit_answers(
        offer_types=["rooftop"], min_kw=50, states=["Texas"]
    )
    return tools


@pytest.mark.parametrize(
    "refusal",
    [
        "I can't do Monday at nine",
        "I won't do Monday at nine",
        "no, Monday at nine doesn't work",
        "Monday at nine is no good",
        "sorry, Monday at nine wouldn't suit me",
    ],
)
@pytest.mark.asyncio
async def test_a_refusal_never_books_the_time_it_refused(refusal: str) -> None:
    """The contraction, not the word list, is what makes this hold.

    The old pattern spelled contractions as stem + "n't", so it was looking for
    "cann't" and "willn't". "I can't do Monday at nine" therefore carried no
    refusal, matched the slot, pinned it and forced the booking. [Codex]
    """
    tools = await offered()
    tools.observe_user_utterance(refusal)

    result = await tools.select_slot("slot_1")

    assert result["success"] is False
    assert "next_tool" not in result


@pytest.mark.parametrize(
    "wanted",
    [
        "I want the Monday at nine",
        "let's just go for Monday at nine",
        "Monday at nine works",
        "what about Monday at nine",
    ],
)
@pytest.mark.asyncio
async def test_wanting_a_time_is_still_taking_it(wanted: str) -> None:
    """The other half of the same regex.

    A pattern loose enough to catch every "n't" also catches every word ending
    in "nt" - "I WANT the Tuesday at one" read as a refusal. The apostrophe is
    what separates them, and this fails the moment it is dropped again.
    """
    tools = await offered()
    tools.observe_user_utterance(wanted)

    result = await tools.select_slot("slot_1")

    assert result["success"] is True


@pytest.mark.parametrize(
    "question",
    [
        "do you have Monday at nine?",
        "is Monday at nine available?",
        "have you got anything Monday?",
    ],
)
@pytest.mark.asyncio
async def test_asking_what_we_hold_is_not_asking_for_it(question: str) -> None:
    """Asking is not choosing, and a booking can no longer be undone on the call.

    With the answers already in, an availability question used to pin the slot
    and force the write - so someone checking whether Monday was open had Monday
    booked. [Codex]
    """
    tools = await offered()
    tools.observe_user_utterance(question)

    result = await tools.select_slot("slot_1")

    assert result["error"] == "question_not_selection"
    assert "confirm" in result["message"]


@pytest.mark.asyncio
async def test_nothing_to_offer_still_says_the_time_is_gone() -> None:
    """A walk-back with no alternatives must not become a different sentence.

    When the menu had no label to hand back, this fell through to "they have not
    named a time yet" - which is untrue and reads as not having listened. [Codex]
    """
    tools = CRMTools(
        db=MagicMock(), user_id=1, variables={"leadName": "Sami"}
    )
    tools.seed_offered_slots(
        [{"slot_id": "slot_1", "start": "2026-07-13T09:00:00Z", "label": ""}],
        "UTC",
        origin="preloaded",
    )
    tools.observe_assistant_utterance("Are you free Monday at nine in the morning?")
    tools.observe_user_utterance("could you do Monday at seven in the evening?")

    result = await tools.select_slot("slot_1")

    assert result["error"] == "slot_unavailable"
    assert "have not named a time" not in result["message"]


@pytest.mark.asyncio
async def test_a_thank_you_after_booking_does_not_wake_a_human(
    raised_alerts: list[dict[str, str]],
) -> None:
    """Only an actual change of time is a human's problem.

    Any select_slot after the write used to raise the reschedule alert, so a
    plain "yes, thanks" told an operator to go and fix a booking that was fine -
    and it did so under the dedup key the REAL write-failure alert uses, which
    would then be dropped as a duplicate. [Codex]
    """
    tools = await offered()
    tools.observe_user_utterance("Monday at nine please")
    await tools.select_slot("slot_1")
    with patch(
        "app.services.calcom_client.create_booking",
        AsyncMock(return_value={"success": True, "uid": "bk_1"}),
    ):
        assert (await tools.book_appointment(MONDAY_9["start"], icp=ICP))["success"]
    raised_alerts.clear()

    tools.observe_user_utterance("yes, thanks")
    late = await tools.select_slot("slot_1")

    assert late["error"] == "already_booked"
    assert raised_alerts == []
