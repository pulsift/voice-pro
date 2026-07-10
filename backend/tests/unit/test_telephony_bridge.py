"""Focused cancellation contracts for bidirectional telephony bridges."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect

from app.api.telephony_ws import _handle_telnyx_stream, _handle_twilio_stream


class BlockingConnection:
    """Async event source that records prompt cancellation."""

    def __init__(self) -> None:
        self.cancelled = asyncio.Event()

    def __aiter__(self) -> "BlockingConnection":
        return self

    async def __anext__(self) -> object:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise StopAsyncIteration


class ScriptedWebSocket:
    def __init__(self, messages: list[str], *, disconnect_after: bool = False) -> None:
        self.messages = list(messages)
        self.disconnect_after = disconnect_after
        self.blocked = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.send_text = AsyncMock()
        self.close = AsyncMock()

    async def receive_text(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        if self.disconnect_after:
            raise WebSocketDisconnect
        self.blocked.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


def make_session(connection: object) -> MagicMock:
    session = MagicMock()
    session.connection = connection
    session.send_audio = AsyncMock()
    session.trigger_initial_greeting = AsyncMock(return_value=False)
    session.handle_function_call_event = AsyncMock(return_value={"success": True})
    return session


@pytest.mark.asyncio
async def test_twilio_stop_cancels_realtime_sibling_without_timeout_wait() -> None:
    websocket = ScriptedWebSocket(
        [
            json.dumps(
                {
                    "event": "start",
                    "start": {"streamSid": "stream-1", "callSid": "call-1"},
                }
            ),
            json.dumps({"event": "stop"}),
        ]
    )
    connection = BlockingConnection()

    call_sid = await asyncio.wait_for(
        _handle_twilio_stream(websocket, make_session(connection), MagicMock()),
        timeout=0.5,
    )

    assert call_sid == "call-1"
    assert connection.cancelled.is_set()


@pytest.mark.asyncio
async def test_twilio_disconnect_cancels_realtime_sibling_without_timeout_wait() -> None:
    websocket = ScriptedWebSocket(
        [
            json.dumps(
                {
                    "event": "start",
                    "start": {"streamSid": "stream-1", "callSid": "call-1"},
                }
            )
        ],
        disconnect_after=True,
    )
    connection = BlockingConnection()

    call_sid = await asyncio.wait_for(
        _handle_twilio_stream(websocket, make_session(connection), MagicMock()),
        timeout=0.5,
    )

    assert call_sid == "call-1"
    assert connection.cancelled.is_set()


@pytest.mark.asyncio
async def test_telnyx_end_call_cancels_provider_sibling_and_closes_socket() -> None:
    websocket = ScriptedWebSocket(
        [
            json.dumps(
                {
                    "event": "start",
                    "stream_id": "stream-1",
                    "start": {"call_control_id": "call-control-1"},
                }
            )
        ]
    )

    async def realtime_events() -> object:
        await websocket.blocked.wait()
        yield SimpleNamespace(
            type="response.function_call_arguments.done",
            call_id="tool-1",
            name="end_call",
        )
        yield SimpleNamespace(type="response.done")

    session = make_session(realtime_events())
    session.handle_function_call_event = AsyncMock(
        return_value={"success": True, "action": "end_call", "reason": "complete"}
    )

    call_control_id = await asyncio.wait_for(
        _handle_telnyx_stream(websocket, session, MagicMock()),
        timeout=0.5,
    )

    assert call_control_id == "call-control-1"
    assert websocket.cancelled.is_set()
    websocket.close.assert_awaited_once_with(code=1000, reason="Call ended by agent")
