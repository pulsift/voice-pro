"""The agent must hear the times it just read out loud.

The 2026-08-18 ring test never booked. The caller said "six in the evening" four
times and every one was refused. The cause was not the caller and not the model:
when the agent reads a day's times aloud it says both the hour and its half past
("five in the evening, half past five, six, half past six"), and the parser that
reads the agent's OWN sentence swallowed every bare hour that also appeared as a
"half past". It believed it had offered only the half-past times. The caller's
clear answer then matched nothing the agent thought it had said, so select_slot
refused and the agent re-asked - forever.
"""

from unittest.mock import MagicMock

import pytest

from app.services.tools.crm_tools import CRMTools

TZ = "Europe/Stockholm"
# 17:00-19:30 local == 15:00-17:30 UTC, the Friday menu from the ring test.
FRIDAY_EVENING = [
    {"slot_id": "slot_1", "start": "2026-08-21T15:00:00+00:00", "label": "five in the evening"},
    {"slot_id": "slot_2", "start": "2026-08-21T15:30:00+00:00", "label": "half past five in the evening"},
    {"slot_id": "slot_3", "start": "2026-08-21T16:00:00+00:00", "label": "six in the evening"},
    {"slot_id": "slot_4", "start": "2026-08-21T16:30:00+00:00", "label": "half past six in the evening"},
]


def make_tools() -> CRMTools:
    tools = CRMTools(db=MagicMock(), user_id=1, variables={"leadName": "Sami"})
    tools.seed_offered_slots(FRIDAY_EVENING, TZ, origin="preloaded")
    return tools


def test_an_hour_survives_being_listed_next_to_its_own_half_past() -> None:
    """The exact sentence from the ring test. All four times were offered."""
    tools = make_tools()
    tools.observe_assistant_utterance(
        "We hold Friday at five in the evening, half past five, six, or half past six. "
        "Which do you want?"
    )

    assert tools.slots_offered_aloud() == {"slot_1", "slot_2", "slot_3", "slot_4"}


def test_a_lone_half_past_still_does_not_offer_the_bare_hour() -> None:
    """The guard this protects: "half past six" alone must never mean six o'clock."""
    tools = make_tools()
    tools.observe_assistant_utterance("Would half past six in the evening work?")

    assert tools.slots_offered_aloud() == {"slot_4"}


@pytest.mark.asyncio
async def test_the_caller_choosing_the_hour_gets_booked() -> None:
    """What the ring test proved impossible: answering "six in the evening"."""
    tools = make_tools()
    tools.observe_assistant_utterance(
        "We hold Friday at five in the evening, half past five, six, or half past six."
    )
    tools.observe_user_utterance("Six in the evening.")

    assert (await tools.select_slot("slot_3"))["success"] is True


@pytest.mark.asyncio
async def test_a_time_they_did_not_name_is_still_refused() -> None:
    """The guard that matters: never book a time of our own choosing."""
    tools = make_tools()
    tools.observe_assistant_utterance(
        "We hold Friday at five in the evening, half past five, six, or half past six."
    )
    tools.observe_user_utterance("Six in the evening.")

    assert (await tools.select_slot("slot_1"))["success"] is False
