"""Attempt four at the filler, and the first one that is not a request.

Three attempts failed, all of them asking the model to stay quiet: a word ban
(2026-08-21), a ban on the SHAPE of the sentence (2026-08-25), and a plea in
select_slot's own return value (2026-08-28). A model that has a silence to fill
will always find a new phrase, so asking was never going to hold.

What changed: the gap itself is gone. select_slot names the tool that must run
next, and the SESSION forces it - a response scoped to one named function comes
back as that function call and nothing else, so there is no message item, no
audio, and nothing to narrate with.

The limit of this, stated plainly: only the turn AFTER a tool belongs to us. The
caller's own turns are created by server VAD inside the Realtime API, so the
filler that lands on the question-3 turn is not reachable this way.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.gpt_realtime import GPTRealtimeSession
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
async def test_a_pinned_slot_names_the_tool_that_must_run_next() -> None:
    tools = make_tools()
    await tools.record_fit_answers(offer_types=["rooftop"], min_kw=50, states=["Texas"])

    result = await tools.select_slot("slot_2")

    assert result["success"] is True
    assert result["next_tool"] == "book_appointment"
    # No line to say, because the agent is not getting a turn here at all.
    assert "message" not in result


@pytest.mark.asyncio
async def test_it_still_says_which_time_it_pinned() -> None:
    tools = make_tools()
    await tools.record_fit_answers(offer_types=["rooftop"], min_kw=50, states=["Texas"])

    result = await tools.select_slot("slot_2")

    assert result["when"] == "two in the afternoon"
    assert result["slot_id"] == "slot_2"


@pytest.mark.asyncio
async def test_a_time_named_before_the_questions_is_not_forced_into_booking() -> None:
    """The early-jump turn is the one place speaking IS the right move.

    They handed us a time before we asked anything, so the agent says it back and
    bridges into the question in one turn. Forcing a booking here would book a
    lead we know nothing about.
    """
    result = await make_tools().select_slot("slot_2")

    assert result["success"] is True
    assert "next_tool" not in result
    assert "fit question" in result["message"]


class Recorder:
    """A Realtime connection that remembers what it was asked to do."""

    def __init__(self) -> None:
        self.sent_outputs: list[dict[str, Any]] = []
        self.created: list[dict[str, Any] | None] = []
        outer = self

        class _Item:
            async def create(self, *, item: dict[str, Any]) -> None:
                outer.sent_outputs.append(item)

        class _Response:
            async def create(self, *, response: dict[str, Any] | None = None) -> None:
                outer.created.append(response)

        self.conversation = MagicMock(item=_Item())
        self.response = _Response()


def session_with(result: dict[str, Any]) -> tuple[GPTRealtimeSession, Recorder]:
    session = GPTRealtimeSession(
        db=MagicMock(), user_id=1, agent_config={"enable_transcript": False}
    )
    recorder = Recorder()
    session.connection = recorder
    session.tool_registry = MagicMock()
    session.handle_tool_call = AsyncMock(return_value=result)
    return session, recorder


def call_event(name: str) -> MagicMock:
    event = MagicMock()
    event.call_id = "call_1"
    event.name = name
    event.arguments = "{}"
    return event


@pytest.mark.asyncio
async def test_the_forced_tool_is_the_only_thing_that_response_may_contain() -> None:
    session, recorder = session_with(
        {"success": True, "when": "two in the afternoon", "next_tool": "book_appointment"}
    )

    await session.handle_function_call_event(call_event("select_slot"))

    assert recorder.created == [
        {"tool_choice": {"type": "function", "name": "book_appointment"}}
    ]


@pytest.mark.asyncio
async def test_the_model_never_reads_the_instruction_meant_for_the_session() -> None:
    """`next_tool` is an instruction to us, not to the agent.

    Left in the tool result it becomes one more sentence for the model to have an
    opinion about - which is the failure mode of all three earlier attempts.
    """
    session, recorder = session_with(
        {"success": True, "when": "two in the afternoon", "next_tool": "book_appointment"}
    )

    await session.handle_function_call_event(call_event("select_slot"))

    sent = json.loads(recorder.sent_outputs[0]["output"])
    assert "next_tool" not in sent
    assert sent["when"] == "two in the afternoon"


@pytest.mark.asyncio
async def test_an_ordinary_tool_result_still_gets_an_ordinary_turn() -> None:
    session, recorder = session_with({"success": True, "recorded": True})

    await session.handle_function_call_event(call_event("record_fit_answers"))

    assert recorder.created == [None]


@pytest.mark.asyncio
async def test_waiting_for_the_caller_is_still_silence_not_a_forced_turn() -> None:
    session, recorder = session_with({"success": True, "action": "wait"})

    await session.handle_function_call_event(call_event("wait_for_user"))

    assert recorder.created == []
