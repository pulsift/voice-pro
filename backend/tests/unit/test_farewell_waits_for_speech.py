"""The line must not drop until the caller has actually heard the goodbye.

2026-08-08, a real ring test: `book_appointment` succeeded at 15:28:13.9, the
agent called `end_call`, and the call closed at 15:28:14.9. In between, one
response completed in 180 milliseconds — far too fast to have carried any
speech. The hang-up guard was counting RESPONSES, so that empty one satisfied
it. Sami was booked into Tuesday at midday and never told; the line just died.

Counting responses is not the same as knowing they heard something. These tests
pin the difference.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api.telephony_ws import (
    MAX_SILENT_RESPONSES_BEFORE_HANGUP,
    _response_spoke,
)


def part(**kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(**{"transcript": None, "text": None, "type": None, **kwargs})


def response(*items: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(response=SimpleNamespace(output=list(items)))


def message(*parts: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(type="message", content=list(parts))


def function_call(name: str = "end_call") -> SimpleNamespace:
    return SimpleNamespace(type="function_call", name=name, content=None)


# --- the exact 2026-08-08 shape -------------------------------------------------


def test_the_empty_response_that_dropped_the_line_is_not_speech() -> None:
    """THE regression. An output-less response is what arrived 180ms after
    end_call, and the old guard accepted it as the goodbye."""
    assert _response_spoke(response()) is False


def test_a_response_carrying_only_a_function_call_is_not_speech() -> None:
    """Tool-first means the tool call and the words are separate responses. The
    one holding end_call has said nothing yet."""
    assert _response_spoke(response(function_call())) is False


def test_a_response_with_no_response_object_at_all_is_not_speech() -> None:
    assert _response_spoke(SimpleNamespace()) is False
    assert _response_spoke(SimpleNamespace(response=None)) is False


def test_a_message_with_empty_content_is_not_speech() -> None:
    """The model can open a message item and put nothing in it."""
    assert _response_spoke(response(message())) is False


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_transcript_of_only_whitespace_is_not_speech(blank: str) -> None:
    assert _response_spoke(response(message(part(transcript=blank)))) is False


# --- what does count ------------------------------------------------------------


def test_a_spoken_transcript_counts_as_speech() -> None:
    spoken = response(message(part(transcript="You're all set for Tuesday at midday.")))
    assert _response_spoke(spoken) is True


def test_a_text_only_message_counts_as_speech() -> None:
    """The eval rig runs in text mode; the same guard has to read it."""
    assert _response_spoke(response(message(part(text="Take care.")))) is True


@pytest.mark.parametrize("audio_type", ["audio", "output_audio"])
def test_an_audio_part_counts_even_before_its_transcript_lands(audio_type: str) -> None:
    """Audio reaches the caller's ear whether or not we have transcribed it. If
    the transcript is late, the words were still heard."""
    assert _response_spoke(response(message(part(type=audio_type)))) is True


def test_speech_alongside_a_function_call_still_counts() -> None:
    """One response can hold both. If it spoke, the caller heard it."""
    mixed = response(function_call(), message(part(transcript="All booked.")))
    assert _response_spoke(mixed) is True


def test_speech_in_a_later_content_part_is_still_found() -> None:
    later = response(message(part(), part(transcript="Speak now.")))
    assert _response_spoke(later) is True


# --- the ceiling ----------------------------------------------------------------


def test_the_wait_is_bounded_so_a_silent_model_cannot_hold_a_billed_line() -> None:
    """We wait for speech, but not forever — the leg costs money every second
    and the caller is sitting in silence. Small on purpose: this survives one
    empty response, it does not wait out a model that will never speak."""
    assert 1 <= MAX_SILENT_RESPONSES_BEFORE_HANGUP <= 3


def test_both_bridges_wait_for_speech_rather_than_counting_responses() -> None:
    """Twilio is production and Telnyx is Syria testing, and the guard was
    duplicated into both. A fix applied to one of them is not a fix."""
    from pathlib import Path

    import app.api.telephony_ws as module

    source = Path(module.__file__).read_text(encoding="utf-8")
    assert source.count("if farewell_pending and not _response_spoke(event):") == 2
    assert source.count("hanging_up_without_a_spoken_goodbye") == 2


# --- the cause, not just the floor ----------------------------------------------


def test_end_call_asks_for_the_closing_line_instead_of_excusing_silence() -> None:
    """The guard stops the line dropping. THIS stops the silence happening.

    The old result text — "Call will be ended after this response" — reads as
    permission to stop talking, and the model took it.
    """
    from app.services.tools.call_control_tools import CallControlTools

    result = CallControlTools()._execute_end_call({"reason": "conversation_complete"})  # noqa: SLF001
    message = result["message"].lower()
    assert result["action"] == "end_call"
    assert "closing line" in message
    assert "never end on silence" in message


def test_a_missing_reason_still_asks_for_a_goodbye() -> None:
    """`reason` is optional in the schema, so the default path is the common one."""
    from app.services.tools.call_control_tools import CallControlTools

    assert "closing line" in CallControlTools()._execute_end_call({})["message"].lower()  # noqa: SLF001


@pytest.mark.parametrize("reason", ["voicemail", "Voicemail", "answering_machine"])
def test_a_machine_is_still_left_without_a_message(reason: str) -> None:
    """Sami's rule: no message on voicemail. Demanding a closing line from every
    end_call would have the agent talking to an answering machine."""
    from app.services.tools.call_control_tools import CallControlTools

    message = CallControlTools()._execute_end_call({"reason": reason})["message"].lower()  # noqa: SLF001
    assert "say nothing further" in message
    assert "closing line" not in message
