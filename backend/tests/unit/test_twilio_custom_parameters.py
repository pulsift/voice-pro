"""Twilio media-stream context is accepted only through a consumed DB grant."""

import asyncio
import json
import uuid
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import WebSocketDisconnect
from structlog.testing import capture_logs

from app.api import telephony_ws
from app.api.telephony_ws import twilio_media_stream
from app.services.telephony.twilio_service import TwilioService

AGENT_ID = str(uuid.uuid4())
WORKSPACE_ID = uuid.uuid4()
GRANT_VALUE = "grant-from-start"


def test_twiml_includes_grant_cv_and_workspace_parameters() -> None:
    twiml = TwilioService("", "").generate_answer_response(
        "wss://example.test/ws/telephony/twilio/agent-1",
        "agent-1",
        custom_parameters={
            "media_grant": "grant-1",
            "cv": "abc123",
            "workspace_id": "ws-1",
        },
    )
    assert '<Parameter name="agent_id" value="agent-1"' in twiml
    assert '<Parameter name="media_grant" value="grant-1"' in twiml
    assert '<Parameter name="cv" value="abc123"' in twiml
    assert '<Parameter name="workspace_id" value="ws-1"' in twiml
    assert 'url="wss://example.test/ws/telephony/twilio/agent-1"' in twiml


def test_twiml_skips_empty_custom_parameter_values() -> None:
    twiml = TwilioService("", "").generate_answer_response(
        "wss://example.test/ws",
        "agent-1",
        custom_parameters={"media_grant": "", "cv": "", "workspace_id": ""},
    )
    assert '<Parameter name="media_grant"' not in twiml
    assert '<Parameter name="cv"' not in twiml
    assert '<Parameter name="workspace_id"' not in twiml
    assert '<Parameter name="agent_id" value="agent-1"' in twiml


def test_twiml_backward_compatible_without_custom_parameters() -> None:
    twiml = TwilioService("", "").generate_answer_response(
        "wss://example.test/ws",
        "agent-1",
    )
    assert "<Connect>" in twiml
    assert '<Parameter name="agent_id" value="agent-1"' in twiml


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
    """Capture construction without opening an OpenAI Realtime connection."""

    last_kwargs: ClassVar[dict] = {}

    def __init__(self, **kwargs) -> None:
        FakeRealtimeSession.last_kwargs = kwargs
        self.connection = None

    async def __aenter__(self) -> "FakeRealtimeSession":
        return self

    async def __aexit__(self, *_exc) -> None:
        return None

    def get_transcript(self) -> str:
        return ""

    def get_booking_attempts(self) -> list:
        return []

    async def send_audio(self, *_args) -> None:
        return None


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


def _call_record(variables: dict[str, object] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        agent_id=uuid.UUID(AGENT_ID),
        workspace_id=WORKSPACE_ID,
        variables=variables,
    )


def _patch_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    *,
    grant_result: object | None,
) -> AsyncMock:
    consumer = AsyncMock(return_value=grant_result)
    FakeRealtimeSession.last_kwargs = {}
    monkeypatch.setattr(telephony_ws, "consume_twilio_media_grant", consumer)
    monkeypatch.setattr(telephony_ws, "GPTRealtimeSession", FakeRealtimeSession)
    monkeypatch.setattr(
        telephony_ws,
        "save_transcript_to_call_record",
        AsyncMock(return_value=None),
    )
    return consumer


def _start_message(custom_parameters: dict[str, object] | None = None) -> str:
    start: dict[str, object] = {"streamSid": "stream-1", "callSid": "call-1"}
    if custom_parameters is not None:
        start["customParameters"] = custom_parameters
    return json.dumps({"event": "start", "start": start})


@pytest.mark.asyncio
async def test_stream_consumes_grant_before_agent_and_uses_canonical_db_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored_variables = {"leadName": "Sami", "company": "Canonical Co"}
    db = _fake_db_with_agent()
    consumer = _patch_endpoint(
        monkeypatch,
        grant_result=_call_record(stored_variables),
    )

    async def consume_first(**kwargs: object) -> object:
        db.execute.assert_not_awaited()
        assert FakeRealtimeSession.last_kwargs == {}
        return _call_record(stored_variables)

    consumer.side_effect = consume_first
    websocket = FakeWebSocket(
        [
            json.dumps({"event": "connected", "protocol": "Call"}),
            _start_message(
                {
                    "media_grant": GRANT_VALUE,
                    "cv": "untrusted-transport-cv",
                    "workspace_id": str(WORKSPACE_ID),
                }
            ),
            json.dumps({"event": "stop"}),
        ],
        query_params={
            "media_grant": "query-grant-must-not-win",
            "cv": "query-cv-must-not-win",
            "workspace_id": str(uuid.uuid4()),
        },
    )

    await asyncio.wait_for(twilio_media_stream(websocket, AGENT_ID, db=db), timeout=1.0)

    consumer.assert_awaited_once_with(
        db=db,
        token=GRANT_VALUE,
        call_sid="call-1",
        agent_id=AGENT_ID,
        workspace_id=str(WORKSPACE_ID),
        cv="untrusted-transport-cv",
    )
    db.execute.assert_awaited_once()
    assert FakeRealtimeSession.last_kwargs["workspace_id"] == WORKSPACE_ID
    assert FakeRealtimeSession.last_kwargs["variables"] == stored_variables
    greeting = FakeRealtimeSession.last_kwargs["agent_config"]["initial_greeting"]
    assert greeting == "Hey Sami, quick one about Canonical Co."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "custom_parameters",
    [
        {},
        {"media_grant": "malformed"},
        {"media_grant": "expired"},
        {"media_grant": "already-consumed"},
        {
            "media_grant": {"nested": "not-a-string"},
            "cv": ["not", "a", "string"],
            "workspace_id": 42,
        },
    ],
    ids=["missing", "invalid", "expired", "replayed", "non-string"],
)
async def test_rejected_grant_closes_before_agent_or_gpt_initialization(
    monkeypatch: pytest.MonkeyPatch,
    custom_parameters: dict[str, object],
) -> None:
    consumer = _patch_endpoint(monkeypatch, grant_result=None)
    db = _fake_db_with_agent()
    websocket = FakeWebSocket(
        [_start_message(custom_parameters)],
        query_params={
            "media_grant": "query-fallback-is-forbidden",
            "cv": "query-cv",
            "workspace_id": str(WORKSPACE_ID),
        },
    )

    await asyncio.wait_for(twilio_media_stream(websocket, AGENT_ID, db=db), timeout=1.0)

    if any(not isinstance(value, str) for value in custom_parameters.values()):
        consumer.assert_not_awaited()
    else:
        passed = consumer.await_args.kwargs
        assert passed["token"] == custom_parameters.get("media_grant", "")
        assert passed["cv"] == custom_parameters.get("cv", "")
        assert passed["workspace_id"] == custom_parameters.get("workspace_id", "")
    db.execute.assert_not_awaited()
    assert FakeRealtimeSession.last_kwargs == {}
    websocket.close.assert_awaited_once_with(code=4003, reason="Invalid media grant")


@pytest.mark.asyncio
async def test_stream_logs_neither_grant_nor_cv_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "mg1.secret-token-value"
    cv = "secret-cv-value"
    _patch_endpoint(
        monkeypatch,
        grant_result=_call_record({"leadName": "Sami", "company": "Safe Co"}),
    )
    websocket = FakeWebSocket(
        [
            _start_message({"media_grant": token, "cv": cv, "workspace_id": str(WORKSPACE_ID)}),
            json.dumps({"event": "stop"}),
        ]
    )

    with capture_logs() as logs:
        await asyncio.wait_for(
            twilio_media_stream(websocket, AGENT_ID, db=_fake_db_with_agent()),
            timeout=1.0,
        )

    rendered_logs = repr(logs)
    assert token not in rendered_logs
    assert cv not in rendered_logs
