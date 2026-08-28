"""The gap between select_slot and book_appointment is where the filler lives.

Two prompt rules have now failed to stop it. On 2026-08-21 the agent said "let me
lock that in"; the ban was rewritten as a rule about SHAPE rather than a word list,
and on 2026-08-27 it said "Sure, let me line up that Friday time and then I'll
confirm what's next." A model that wants to fill a silence will always find a new
phrase, so the prompt is the wrong lever.

The real cause is that select_slot returned no `message` at all. Every other tool
tells the agent what to do next; this one handed back a bare success and left a turn
shaped like a gap. It now says: do not speak, book it.

The ending is the same problem from the other side. The agent confirmed the booking
and hung up on itself in the same breath, which lands abruptly on a real call. The
booking result now tells it to stop after the confirmation and let the caller answer.
"""

from unittest.mock import MagicMock

import pytest

from app.services.tools.crm_tools import CRMTools

TZ = "Europe/Stockholm"
MENU = [
    {"slot_id": "slot_1", "start": "2026-08-28T08:30:00+00:00", "label": "half past ten in the morning"},
    {"slot_id": "slot_2", "start": "2026-08-28T12:00:00+00:00", "label": "two in the afternoon"},
]


def make_tools() -> CRMTools:
    tools = CRMTools(db=MagicMock(), user_id=1, variables={"leadName": "Sami"})
    tools.seed_offered_slots(MENU, TZ, origin="preloaded")
    tools.observe_assistant_utterance(
        "Are you free Friday at half past ten in the morning, or two in the afternoon?"
    )
    tools.observe_user_utterance("Could we do Friday at two?")
    return tools


@pytest.mark.asyncio
async def test_select_slot_tells_the_agent_not_to_speak_yet() -> None:
    """A success with no instruction is a turn-shaped hole. Fill it."""
    result = await make_tools().select_slot("slot_2")

    assert result["success"] is True
    message = result["message"].lower()
    assert "do not speak" in message
    assert "book_appointment" in message


@pytest.mark.asyncio
async def test_select_slot_still_says_which_time_it_pinned() -> None:
    """The new message must not cost the agent the fact it actually needs."""
    result = await make_tools().select_slot("slot_2")

    assert result["when"] == "two in the afternoon"
    assert result["slot_id"] == "slot_2"
