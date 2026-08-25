"""A rough answer is an answer. "Evening" books an evening.

Sami's ruling, 2026-08-25: "the lead should be able to say exactly what they want,
which time they want, or they can use vague terms such as midday or evening and the
AI will just assume on their behalf without asking questions."

Before this, select_slot demanded that the transcript reduce to EXACTLY one slot.
"Six in the evening" when we hold five evening times was therefore a refusal, and
the agent went back and asked them to narrow it down - the single most irritating
thing on the 2026-08-18 ring test.

Now the model picks, and the transcript CONSTRAINS rather than dictates: the chosen
slot must be one the caller's own words could have meant, read against what the
agent actually just offered. So when the agent has read out five evening times and
the caller says "evening", any of those five books - but a morning one never does,
and a refusal still books nothing at all.
"""

from unittest.mock import MagicMock

import pytest

from app.services.tools.crm_tools import CRMTools

TZ = "Europe/Stockholm"
# 15:00-17:30 UTC == 17:00-19:30 local: four evening times, plus one morning.
MENU = [
    {"slot_id": "morning", "start": "2026-08-27T07:00:00+00:00", "label": "nine in the morning"},
    {"slot_id": "five", "start": "2026-08-27T15:00:00+00:00", "label": "five in the evening"},
    {"slot_id": "half_five", "start": "2026-08-27T15:30:00+00:00", "label": "half past five in the evening"},
    {"slot_id": "six", "start": "2026-08-27T16:00:00+00:00", "label": "six in the evening"},
    {"slot_id": "half_six", "start": "2026-08-27T16:30:00+00:00", "label": "half past six in the evening"},
]
OFFER = (
    "Thursday we hold nine in the morning, five in the evening, half past five, "
    "six, or half past six. Which suits?"
)


def make_tools() -> CRMTools:
    tools = CRMTools(db=MagicMock(), user_id=1, variables={"leadName": "Sami"})
    tools.seed_offered_slots(MENU, TZ, origin="preloaded")
    return tools


@pytest.mark.asyncio
@pytest.mark.parametrize("chosen", ["five", "half_five", "six", "half_six"])
async def test_evening_books_any_evening_time_we_hold(chosen: str) -> None:
    tools = make_tools()
    tools.observe_assistant_utterance(OFFER)
    tools.observe_user_utterance("Evening works better for me.")

    assert (await tools.select_slot(chosen))["success"] is True


@pytest.mark.asyncio
async def test_evening_can_never_book_the_morning() -> None:
    """The words still constrain. Assuming on their behalf is not guessing."""
    tools = make_tools()
    tools.observe_assistant_utterance(OFFER)
    tools.observe_user_utterance("Evening works better for me.")

    assert (await tools.select_slot("morning"))["success"] is False


@pytest.mark.asyncio
async def test_a_refusal_still_books_nothing() -> None:
    """The guard that earned its place: never book a time they turned down."""
    tools = make_tools()
    tools.observe_assistant_utterance(OFFER)
    tools.observe_user_utterance("No, the evening doesn't work for me.")

    assert (await tools.select_slot("six"))["success"] is False


@pytest.mark.asyncio
async def test_saying_nothing_about_time_still_books_nothing() -> None:
    tools = make_tools()
    tools.observe_assistant_utterance(OFFER)
    tools.observe_user_utterance("Sorry, who is this again?")

    assert (await tools.select_slot("six"))["success"] is False
