"""Twilio <Parameter> personalization transport.

Twilio strips query strings from <Stream> URLs, so cv/workspace_id must travel as
TwiML <Parameter> values and be read back from the start event's customParameters.
Query params remain a fallback for inbound/legacy streams; Telnyx is untouched.
"""

import asyncio
import base64
import json
import uuid
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect

from app.api import telephony_ws
from app.api.telephony_ws import twilio_media_stream
from app.services.telephony.twilio_service import TwilioService

AGENT_ID = str(uuid.uuid4())
WORKSPACE_ID = uuid.uuid4()


def _cv_blob(variables: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(variables).encode()).decode().rstrip("=")


# --- TwiML generation ---------------------------------------------------------


def test_twiml_includes_cv_and_workspace_parameters():
    svc = TwilioService("", "")
    twiml = svc.generate_answer_response(
        "wss://example.test/ws/telephony/twilio/agent-1",
        "agent-1",
        custom_parameters={"cv": "abc123", "workspace_id": "ws-1"},
    )
    assert '<Parameter name="agent_id" value="agent-1"' in twiml
    assert '<Parameter name="cv" value="abc123"' in twiml
    assert '<Parameter name="workspace_id" value="ws-1"' in twiml
    assert 'url="wss://example.test/ws/telephony/twilio/agent-1"' in twiml


def test_twiml_skips_empty_custom_parameter_values():
    # Inbound path passes empty strings — they must not become empty <Parameter>s.
    svc = TwilioService("", "")
    twiml = svc.generate_answer_response(
        "wss://example.test/ws",
        "agent-1",
        custom_parameters={"cv": "", "workspace_id": ""},
    )
    assert '<Parameter name="cv"' not in twiml
    assert '<Parameter name="workspace_id"' not in twiml
    assert '<Parameter name="agent_id" value="agent-1"' in twiml


def test_twiml_backward_compatible_without_custom_parameters():
    svc = TwilioService("", "")
    twiml = svc.generate_answer_response("wss://example.test/ws", "agent-1")
    assert "<Connect>" in twiml
    assert '<Parameter name="agent_id" value="agent-1"' in twiml


# --- media-stream endpoint: customParameters primary, query params fallback ----


class FakeWebSocket:
    def __init__(self, messages: list[str], query_params: dict[str, str] | None = None) -> None:
        self.messages = list(messages)
        self.query_params = query_params or {}
        self.accept = AsyncMock()
        self.close = AsyncMock()
        self.send_text = AsyncMock()

    async def receive_text(self) -> str:
        if self.messages:
            return self.messages.pop(0)
        raise WebSocketDisconnect


class FakeRealtimeSession:
    """Captures constructor kwargs; context-manages to itself; no realtime connection."""

    last_kwargs: ClassVar[dict] = {}

    def __init__(self, **kwargs) -> None:
        FakeRealtimeSession.last_kwargs = kwargs
        self.connection = None

    async def __aenter__(self) -> "FakeRealtimeSession":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    def get_transcript(self) -> str:
        return ""

    def get_booking_attempts(self) -> list:
        return []

    def send_audio(self, *_): ...


def _fake_db_with_agent() -> MagicMock:
    agent = MagicMock()
    agent.id = uuid.UUID(AGENT_ID)
    agent.is_active = True
    agent.user_id = 1
    agent.system_prompt = "prompt"
    agent.enabled_tools = []
    agent.language = "en"
    agent.voice = "cedar"
    agent.enable_transcript = False
    agent.initial_greeting = "Hey {{leadName}}, quick one about {{company}}."
    agent.name = "test-agent"

    result = MagicMock()
    result.scalar_one_or_none.return_value = agent
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def _patch_endpoint(monkeypatch) -> AsyncMock:
    resolver = AsyncMock(return_value=WORKSPACE_ID)
    monkeypatch.setattr(telephony_ws, "resolve_media_workspace_id", resolver)
    monkeypatch.setattr(telephony_ws, "GPTRealtimeSession", FakeRealtimeSession)
    monkeypatch.setattr(telephony_ws, "save_transcript_to_call_record", AsyncMock())
    return resolver


@pytest.mark.asyncio
async def test_media_stream_reads_cv_and_workspace_from_custom_parameters(monkeypatch):
    resolver = _patch_endpoint(monkeypatch)
    cv = _cv_blob({"leadName": "Sami", "company": "Acme Solar"})
    websocket = FakeWebSocket(
        [
            json.dumps({"event": "connected", "protocol": "Call"}),
            json.dumps(
                {
                    "event": "start",
                    "start": {
                        "streamSid": "stream-1",
                        "callSid": "call-1",
                        "customParameters": {"cv": cv, "workspace_id": str(WORKSPACE_ID)},
                    },
                }
            ),
            json.dumps({"event": "stop"}),
        ]
    )

    await asyncio.wait_for(
        twilio_media_stream(websocket, AGENT_ID, db=_fake_db_with_agent()), timeout=1.0
    )

    # workspace came from customParameters, not query params
    assert resolver.await_args.args[1] == str(WORKSPACE_ID)
    # cv decoded into session variables + greeting rendered from them
    assert FakeRealtimeSession.last_kwargs["variables"] == {
        "leadName": "Sami",
        "company": "Acme Solar",
    }
    greeting = FakeRealtimeSession.last_kwargs["agent_config"]["initial_greeting"]
    assert greeting == "Hey Sami, quick one about Acme Solar."


@pytest.mark.asyncio
async def test_media_stream_falls_back_to_query_params(monkeypatch):
    # Inbound/legacy streams: no customParameters in start — query params still work.
    resolver = _patch_endpoint(monkeypatch)
    cv = _cv_blob({"leadName": "Dana"})
    websocket = FakeWebSocket(
        [
            json.dumps({"event": "connected"}),
            json.dumps(
                {"event": "start", "start": {"streamSid": "stream-2", "callSid": "call-2"}}
            ),
            json.dumps({"event": "stop"}),
        ],
        query_params={"cv": cv, "workspace_id": str(WORKSPACE_ID)},
    )

    await asyncio.wait_for(
        twilio_media_stream(websocket, AGENT_ID, db=_fake_db_with_agent()), timeout=1.0
    )

    assert resolver.await_args.args[1] == str(WORKSPACE_ID)
    assert FakeRealtimeSession.last_kwargs["variables"] == {"leadName": "Dana"}


@pytest.mark.asyncio
async def test_media_stream_custom_parameters_beat_query_params(monkeypatch):
    # If both exist (transition window), the <Parameter> channel is authoritative.
    _patch_endpoint(monkeypatch)
    cv_params = _cv_blob({"leadName": "FromParameter"})
    cv_query = _cv_blob({"leadName": "FromQuery"})
    websocket = FakeWebSocket(
        [
            json.dumps(
                {
                    "event": "start",
                    "start": {
                        "streamSid": "stream-3",
                        "callSid": "call-3",
                        "customParameters": {"cv": cv_params},
                    },
                }
            ),
            json.dumps({"event": "stop"}),
        ],
        query_params={"cv": cv_query},
    )

    await asyncio.wait_for(
        twilio_media_stream(websocket, AGENT_ID, db=_fake_db_with_agent()), timeout=1.0
    )

    assert FakeRealtimeSession.last_kwargs["variables"] == {"leadName": "FromParameter"}
