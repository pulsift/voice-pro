"""The opening, driven through the REAL bridge with a scripted Realtime stream.

These tests exist because the opening is the part of a call we cannot rehearse
cheaply: it depends on the exact order of OpenAI Realtime events, and getting it
wrong is audible to a prospect on the first second of the call. So instead of
mocking our own logic back at us, each test feeds `_handle_twilio_stream` a real
event sequence — the same shapes the live API sends — and asserts what the caller
would experience.

Covered: the bare hello goes out first, the opener is protected by HOLDING caller
audio rather than dropping it, held audio is always flushed (response.done, an
error, or the ceiling timer), a hold never engages before the caller has spoken
(which would blind the dead-air watchdog), and dead air hangs up instead of
holding a paid line open.
"""

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.telephony_ws import OpeningSequence, _handle_twilio_stream
from app.core.config import settings
from app.services.gpt_realtime import GPTRealtimeSession


def event(event_type: str, **fields: Any) -> MagicMock:
    """One Realtime server event."""
    node = MagicMock()
    node.type = event_type
    for key, value in fields.items():
        setattr(node, key, value)
    return node


class ScriptedRealtime:
    """Yields a fixed event list, then blocks until cancelled (as the real one does)."""

    def __init__(self, events: list[MagicMock]) -> None:
        self._events = list(events)
        self.session = MagicMock(update=AsyncMock())
        self.response = MagicMock(create=AsyncMock())
        self.conversation = MagicMock(item=MagicMock(create=AsyncMock()))
        self.input_audio_buffer = MagicMock(append=AsyncMock(), clear=AsyncMock())

    def __aiter__(self) -> "ScriptedRealtime":
        return self

    async def __anext__(self) -> MagicMock:
        if self._events:
            await asyncio.sleep(0)
            return self._events.pop(0)
        await asyncio.Event().wait()  # keep the bridge alive until cancelled
        raise StopAsyncIteration


class SilentWebSocket:
    """A live line with nobody talking.

    Faithful on the point that matters: Twilio streams media frames continuously for
    the whole call — silence is still encoded audio — so the media loop keeps ticking
    and therefore keeps observing `should_end_call`. A fake that simply blocked would
    hide the fact that the hangup depends on those frames arriving.
    """

    SILENCE = json.dumps({"event": "media", "media": {"payload": "//////////8="}})

    def __init__(self) -> None:
        self.send_text = AsyncMock()
        self.close = AsyncMock()
        self.frames_sent = 0

    async def receive_text(self) -> str:
        await asyncio.sleep(0.01)  # ~one frame interval, compressed
        self.frames_sent += 1
        return self.SILENCE


def real_session(events: list[MagicMock]) -> GPTRealtimeSession:
    """A genuine session object (not a mock) wired to a scripted connection."""
    session = GPTRealtimeSession(
        db=MagicMock(), user_id=1, agent_config={"enable_transcript": False}
    )
    session.connection = ScriptedRealtime(events)
    session.tool_registry = MagicMock()
    session._hello_line = "Hello?"  # noqa: SLF001
    return session


async def run_bridge(session: GPTRealtimeSession, websocket: Any, *, settle: float = 1.5) -> None:
    """Drive the bridge, then tear it down the way a finished call does."""
    task = asyncio.create_task(
        _handle_twilio_stream(
            websocket=websocket,
            realtime_session=session,
            log=MagicMock(),
            stream_sid="stream-1",
            call_sid="call-1",
        )
    )
    await asyncio.sleep(settle)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Compress the watchdog so tests take milliseconds, not half a minute."""
    monkeypatch.setattr(settings, "REALTIME_POST_HELLO_NUDGE_SECONDS", 0.15)
    monkeypatch.setattr(settings, "REALTIME_POST_HELLO_GIVEUP_SECONDS", 0.15)
    monkeypatch.setattr(settings, "REALTIME_OPENER_HOLD_MAX_SECONDS", 0.4)
    monkeypatch.setattr(settings, "REALTIME_SESSION_READY_TIMEOUT_SECONDS", 0.1)


@pytest.mark.asyncio
async def test_the_first_thing_the_callee_hears_is_the_bare_hello() -> None:
    session = real_session([event("session.updated")])
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]

    await run_bridge(session, SilentWebSocket(), settle=0.05)

    sent = session.connection.response.create.await_args.kwargs["response"]
    assert '"Hello?"' in sent["instructions"]
    assert sent["tool_choice"] == "none"


@pytest.mark.asyncio
async def test_dead_air_gets_one_nudge_then_a_hangup() -> None:
    """A phone call has a case a chat does not: nobody there at all."""
    session = real_session([event("session.updated")])
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]
    websocket = SilentWebSocket()

    await run_bridge(session, websocket, settle=0.5)

    prompts = [
        call.kwargs["response"]["instructions"]
        for call in session.connection.response.create.await_args_list
    ]
    assert len(prompts) == 2  # the hello, then exactly ONE nudge
    assert "wait for them" in prompts[0].lower()  # turn 1 was the bare hello
    assert "Can you hear me?" in prompts[1]
    websocket.close.assert_awaited()  # the line was let go, not held open


@pytest.mark.asyncio
async def test_a_caller_who_answers_is_never_hung_up_on() -> None:
    session = real_session(
        [
            event("session.updated"),
            event("response.created"),
            event("response.done", response=None),
            event("input_audio_buffer.speech_started"),  # they said "hello?"
        ]
    )
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]
    websocket = SilentWebSocket()

    await run_bridge(session, websocket, settle=0.6)

    assert session.caller_has_spoken is True
    websocket.close.assert_not_awaited()  # watchdog disarmed by their voice


@pytest.mark.asyncio
async def test_caller_audio_during_the_opener_is_held_then_delivered() -> None:
    """The opener must reach its closing question — but nothing the caller said
    during it may be lost. Held, then flushed."""
    session = real_session(
        [
            event("session.updated"),
            event("response.created"),  # the hello
            event("response.done", response=None),
            event("input_audio_buffer.speech_started"),  # caller speaks
            event("response.created"),  # the opener -> hold engages
        ]
    )
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]
    append = session.connection.input_audio_buffer.append

    task = asyncio.create_task(
        _handle_twilio_stream(
            websocket=SilentWebSocket(),
            realtime_session=session,
            log=MagicMock(),
            stream_sid="stream-1",
            call_sid="call-1",
        )
    )
    await asyncio.sleep(0.05)
    assert session.input_held is True
    # Ignore the frames that flowed normally before the opener began.
    append.reset_mock()

    await session.send_audio(b"\xff" * 320)  # a "yeah?" on top of the opener
    await asyncio.sleep(0.03)  # and the line's own frames meanwhile
    assert append.await_count == 0  # nothing reaches OpenAI: the opener is safe

    await session.release_input_hold(reason="opener finished")
    assert append.await_count >= 1  # and nothing was lost either
    assert session.input_held is False

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_response_that_never_completes_cannot_leave_the_caller_unheard() -> None:
    """If response.done never arrives, the ceiling timer must release the hold."""
    session = real_session(
        [
            event("session.updated"),
            event("response.created"),
            event("response.done", response=None),
            event("input_audio_buffer.speech_started"),
            event("response.created"),  # opener starts and never finishes
        ]
    )
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]

    task = asyncio.create_task(
        _handle_twilio_stream(
            websocket=SilentWebSocket(),
            realtime_session=session,
            log=MagicMock(),
            stream_sid="stream-1",
            call_sid="call-1",
        )
    )
    await asyncio.sleep(0.05)
    assert session.input_held is True
    await asyncio.sleep(settings.REALTIME_OPENER_HOLD_MAX_SECONDS + 0.2)
    assert session.input_held is False  # ceiling fired

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_realtime_error_also_releases_the_hold() -> None:
    session = real_session(
        [
            event("session.updated"),
            event("response.created"),
            event("response.done", response=None),
            event("input_audio_buffer.speech_started"),
            event("response.created"),
            event("error", error="server blew up mid-opener"),
        ]
    )
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]

    await run_bridge(session, SilentWebSocket(), settle=0.1)

    assert session.input_held is False  # fail-open: never leave them unheard


@pytest.mark.asyncio
async def test_the_barge_in_flush_is_sent_to_twilio_on_caller_speech() -> None:
    """Audio already in Twilio's playout buffer keeps playing after OpenAI cancels,
    so the agent talks over the caller unless we clear it."""
    session = real_session(
        [event("session.updated"), event("input_audio_buffer.speech_started")]
    )
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]
    websocket = SilentWebSocket()

    await run_bridge(session, websocket, settle=0.05)

    sent = [json.loads(call.args[0]) for call in websocket.send_text.await_args_list]
    assert {"event": "clear", "streamSid": "stream-1"} in sent


@pytest.mark.asyncio
async def test_the_availability_preload_starts_without_blocking_the_hello() -> None:
    """The calendar must never be on the critical path of the first word."""
    started = asyncio.Event()

    async def slow_load() -> bool:
        started.set()
        await asyncio.sleep(5)  # Cal.com being slow
        return True

    session = real_session([event("session.updated")])
    session.load_availability = slow_load  # type: ignore[method-assign]

    await run_bridge(session, SilentWebSocket(), settle=0.05)

    assert started.is_set()  # it was kicked off
    session.connection.response.create.assert_awaited()  # and the hello still went out


@pytest.mark.asyncio
async def test_a_second_session_updated_cannot_restart_the_opening() -> None:
    """The availability preload pushes new instructions, which emits another
    session.updated. That must not re-greet or re-arm anything."""
    session = real_session(
        [event("session.updated"), event("session.updated"), event("session.updated")]
    )
    session.load_availability = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await run_bridge(session, SilentWebSocket(), settle=0.05)

    assert session.connection.response.create.await_count == 1  # one hello, not three


@pytest.mark.asyncio
async def test_opening_sequence_cancel_is_safe_before_and_after_start() -> None:
    session = real_session([])
    opening = OpeningSequence(session, MagicMock(), on_giveup=lambda: None)
    opening.cancel()  # nothing started yet
    await opening.start()
    opening.cancel()
    opening.cancel()  # idempotent
    assert opening.started is True


@pytest.mark.asyncio
async def test_the_ceiling_drops_the_stale_buffer_instead_of_flushing_it() -> None:
    """Flushing while the protected response is still generating is exactly what
    cancels it — the protection would cut off the sentence it protects."""
    session = real_session(
        [
            event("session.updated"),
            event("response.created", response_id="resp_hello"),
            event("response.done", response=None),
            event("input_audio_buffer.speech_started"),
            event("response.created", response_id="resp_opener"),  # never completes
        ]
    )
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]
    append = session.connection.input_audio_buffer.append

    task = asyncio.create_task(
        _handle_twilio_stream(
            websocket=SilentWebSocket(),
            realtime_session=session,
            log=MagicMock(),
            stream_sid="stream-1",
            call_sid="call-1",
        )
    )
    await asyncio.sleep(0.05)
    assert session.input_held is True
    append.reset_mock()

    await asyncio.sleep(settings.REALTIME_OPENER_HOLD_MAX_SECONDS + 0.2)
    assert session.input_held is False  # live audio flows again from here

    # The buffered seconds were DISCARDED, not replayed on top of a response that
    # might still be speaking: every append is a single live frame, never a bulk
    # flush of everything that piled up.
    one_frame = len(json.loads(SilentWebSocket.SILENCE)["media"]["payload"])
    largest = max(len(call.kwargs["audio"]) for call in append.await_args_list)
    assert largest <= one_frame

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_completion_for_another_response_does_not_release_the_hold() -> None:
    """A late response.done belonging to the hello must not free the opener."""
    session = real_session(
        [
            event("session.updated"),
            event("response.created", response_id="resp_hello"),
            event("input_audio_buffer.speech_started"),
            event("response.created", response_id="resp_opener"),
            event("response.done", response=SimpleNamespace(id="resp_hello")),
        ]
    )
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]

    task = asyncio.create_task(
        _handle_twilio_stream(
            websocket=SilentWebSocket(),
            realtime_session=session,
            log=MagicMock(),
            stream_sid="stream-1",
            call_sid="call-1",
        )
    )
    await asyncio.sleep(0.08)
    assert session.input_held is True  # still protecting resp_opener

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_tool_only_turn_does_not_consume_the_openers_protection() -> None:
    """A function-call turn emits response.created too. If it absorbed the
    protection, the real opener — the one the caller talks over — would be
    unguarded, and the original complaint would come straight back."""
    session = real_session(
        [
            event("session.updated"),
            event("response.created", response_id="resp_hello"),
            event("response.done", response=SimpleNamespace(id="resp_hello")),
            event("input_audio_buffer.speech_started"),
            event("response.created", response_id="resp_tool"),  # no audio at all
            event("response.done", response=SimpleNamespace(id="resp_tool")),
            event("response.created", response_id="resp_opener"),
            event("response.output_audio.delta", delta=""),  # this one SPEAKS
        ]
    )
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]

    task = asyncio.create_task(
        _handle_twilio_stream(
            websocket=SilentWebSocket(),
            realtime_session=session,
            log=MagicMock(),
            stream_sid="stream-1",
            call_sid="call-1",
        )
    )
    await asyncio.sleep(0.1)
    assert session.input_held is True  # the SPEAKING turn is the protected one

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_the_opening_still_happens_if_session_updated_never_arrives() -> None:
    """A rejected session config sends `error`, not `session.updated`. Without a
    fallback the callee hears silence on a billed leg until the bridge times out."""
    session = real_session([event("error", error="session config rejected")])
    session.load_availability = AsyncMock(return_value=False)  # type: ignore[method-assign]

    await run_bridge(session, SilentWebSocket(), settle=0.25)

    session.connection.response.create.assert_awaited()  # greeted anyway
