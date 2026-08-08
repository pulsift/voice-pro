"""A refused tool must never make the caller feel misheard.

2026-08-08 ring test. Sami said "Let's do Tuesday at midday" — perfectly clear,
first time. `select_slot` refused because his transcript had not reached the
booking state yet (our timing, entirely). The agent turned that refusal into:

    "Sorry, I didn't catch that clearly. Was that Tuesday at midday, or a
     different time you had in mind?"

He repeated the identical sentence and it worked. His verdict on the agent as a
whole was "rough on the edges", and that one line is a large part of why: it
apologises for our lag and puts the fault on him.

The Retell receptionist he rates never apologises once in an entire booking. It
answers "got it Rose", and when the caller CHANGES the time mid-booking it says
"of course Rose". These tests hold our refusal text to that bar.

The guards themselves are right and stay strict — booking a time nobody clearly
named is fabrication. What is under test here is the RECOVERY.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.services import availability
from app.services.tools.crm_tools import CRMTools

# Monday 8am and Tuesday midday, the pair the live agent actually offered.
RAW_SLOTS = [{"start": "2026-08-10T06:00:00Z"}, {"start": "2026-08-11T10:00:00Z"}]


def crm() -> CRMTools:
    tools = CRMTools(db=MagicMock(), user_id=1, variables={"leadName": "Sami"})
    menu = availability.build_menu(RAW_SLOTS, "Europe/Stockholm")
    assert tools.seed_offered_slots(menu["slots"], "Europe/Stockholm") == 2
    return tools


async def refusal(utterance: str) -> dict[str, object]:
    tools = crm()
    if utterance:
        tools.observe_user_utterance(utterance)
    result = await tools.select_slot("slot_2")
    assert result["success"] is False, "these cases must refuse; the TONE is the test"
    return result


@pytest.mark.asyncio
async def test_the_race_that_made_it_apologise_no_longer_asks_it_to() -> None:
    """THE regression. The times were re-offered AFTER the caller last spoke, so
    their answer to this offer has not reached us yet. That is our timing, and
    the message must say so instead of inviting an apology at them."""
    tools = crm()
    tools.observe_user_utterance("Let's do Tuesday at midday")
    menu = availability.build_menu(RAW_SLOTS, "Europe/Stockholm")
    tools.seed_offered_slots(menu["slots"], "Europe/Stockholm", origin="offered")

    result = await tools.select_slot("slot_2")
    assert result["error"] == "selection_not_heard"
    message = str(result["message"]).lower()
    assert "our timing" in message
    assert "never apologise" in message
    assert "never suggest they" in message


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ["", "hmm, whichever really", "yeah either is fine"])
async def test_no_refusal_message_ever_invites_an_apology(utterance: str) -> None:
    """Both refusal paths, across the ways a caller lands on neither time."""
    message = str((await refusal(utterance))["message"]).lower()
    for word in ("sorry", "apolog", "did not catch", "didn't catch"):
        assert f"never {word}" in message or word not in message.replace("never apologise", ""), (
            f"refusal text contains {word!r} without forbidding it: {message}"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("utterance", ["", "whichever", "either works I guess"])
async def test_no_refusal_message_leaks_the_machinery(utterance: str) -> None:
    """Unchanged contract, re-asserted because the wording changed underneath it.
    'first or second' and slot ids are how a call stops sounding like a call."""
    message = str((await refusal(utterance))["message"]).lower()
    for leak in ("slot_", "json", "first or second"):
        assert leak not in message.replace("never say 'first or second'", "")


@pytest.mark.asyncio
async def test_a_clearly_named_time_is_still_accepted_without_a_second_ask() -> None:
    """The point of all this. The gate must stay strict about fabrication and
    still let an obvious answer straight through on the FIRST attempt — Sami had
    to say the same sentence twice, and that is the behaviour being removed."""
    tools = crm()
    tools.observe_assistant_utterance(
        "Monday at eight in the morning, or Tuesday at midday — would either work?"
    )
    tools.observe_user_utterance("Let's do Tuesday at midday")
    result = await tools.select_slot("slot_2")
    assert result["success"] is True, result


@pytest.mark.asyncio
async def test_a_time_nobody_named_is_still_refused() -> None:
    """The guard that stopped a fabricated booking must not be softened by any
    of this. Tone is the only thing changing."""
    tools = crm()
    tools.observe_assistant_utterance("Monday at eight, or Tuesday at midday?")
    tools.observe_user_utterance("what does the call actually cover?")
    result = await tools.select_slot("slot_2")
    assert result["success"] is False
