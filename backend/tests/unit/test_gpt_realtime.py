"""Focused tests for phone-call Realtime configuration and utterance routing."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.telephony_ws import save_transcript_to_call_record
from app.core.config import Settings
from app.services.gpt_realtime import GPTRealtimeSession
from app.services.tools.registry import ToolRegistry


def make_session(*, enable_transcript: bool = False) -> GPTRealtimeSession:
    """Build a session without opening database or network resources."""
    return GPTRealtimeSession(
        db=MagicMock(),
        user_id=1,
        agent_config={"enable_transcript": enable_transcript},
    )


def test_realtime_model_has_proven_safe_default() -> None:
    assert Settings.model_fields["OPENAI_REALTIME_MODEL"].default == ("gpt-realtime-2025-08-28")
    assert Settings.model_fields["OPENAI_REALTIME_REASONING_EFFORT"].default is None


def test_input_gate_open_by_default() -> None:
    # No greeting configured => gate must stay open, or caller audio would deadlock.
    assert make_session()._input_gate_open is True  # noqa: SLF001


@pytest.mark.asyncio
async def test_send_audio_dropped_while_gate_closed_then_forwarded() -> None:
    session = make_session()
    session._input_gate_open = False  # noqa: SLF001 - greeting in progress
    session.connection = MagicMock()
    session.connection.input_audio_buffer.append = AsyncMock()

    await session.send_audio(b"\x00\x01")
    session.connection.input_audio_buffer.append.assert_not_awaited()  # dropped mid-greeting

    session.open_input_gate()  # greeting response.done
    assert session._input_gate_open is True  # noqa: SLF001
    await session.send_audio(b"\x00\x01")
    session.connection.input_audio_buffer.append.assert_awaited_once()  # now forwarded


def test_completed_utterance_reaches_tools_when_history_is_disabled() -> None:
    session = make_session(enable_transcript=False)
    session.tool_registry = MagicMock()

    session.observe_user_transcript("  the second one  ")

    session.tool_registry.observe_user_utterance.assert_called_once_with("the second one")
    assert session.get_transcript_entries() == []


def test_completed_utterance_is_also_persisted_when_history_is_enabled() -> None:
    session = make_session(enable_transcript=True)
    session.tool_registry = MagicMock()

    session.observe_user_transcript("the first one")

    session.tool_registry.observe_user_utterance.assert_called_once_with("the first one")
    assert session.get_transcript() == "[User]: the first one"


def test_tool_registry_forwards_completed_utterance_to_crm_state() -> None:
    registry = object.__new__(ToolRegistry)
    registry.crm_tools = MagicMock()

    registry.observe_user_utterance("later")

    registry.crm_tools.observe_user_utterance.assert_called_once_with("later")


def test_booking_attempts_delegate_from_session_to_crm_state() -> None:
    attempts = [{"attempt": 1, "category": "rejected", "status_code": 400}]
    session = make_session()
    session.tool_registry = MagicMock()
    session.tool_registry.get_booking_attempts.return_value = attempts

    assert session.get_booking_attempts() == attempts
    session.tool_registry.get_booking_attempts.assert_called_once_with()


@pytest.mark.asyncio
async def test_call_artifacts_persist_booking_attempts_without_transcript() -> None:
    owner_id = uuid.uuid4()
    record = MagicMock(id=uuid.uuid4(), transcript=None, booking_attempts=None)
    scalar_result = MagicMock()
    scalar_result.scalars.return_value.all.return_value = [record]
    db = MagicMock()
    db.execute = AsyncMock(return_value=scalar_result)
    db.commit = AsyncMock()
    log = MagicMock()
    attempts = [{"attempt": 1, "category": "rejected", "status_code": 400}]

    await save_transcript_to_call_record(
        "provider-call-id",
        "",
        db,
        log,
        booking_attempts=attempts,
        owner_user_id=owner_id,
        workspace_id=None,
        provider="telnyx",
    )

    assert record.transcript is None
    assert record.booking_attempts == attempts
    db.commit.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_select_slot_routes_to_crm_tools() -> None:
    registry = object.__new__(ToolRegistry)
    registry.crm_tools = MagicMock()
    registry.crm_tools.execute_tool = AsyncMock(return_value={"success": True})

    result = await registry.execute_tool("select_slot", {"slot_id": "slot_2"})

    assert result == {"success": True}
    registry.crm_tools.execute_tool.assert_awaited_once_with("select_slot", {"slot_id": "slot_2"})


@pytest.mark.asyncio
async def test_phone_connection_uses_configured_model() -> None:
    session = make_session()
    session.realtime_model = "gpt-realtime-2.1"
    session.realtime_reasoning_effort = "low"
    connection = MagicMock()
    context_manager = MagicMock()
    context_manager.__aenter__ = AsyncMock(return_value=connection)
    connect = MagicMock(return_value=context_manager)
    session.client = MagicMock(realtime=MagicMock(connect=connect))
    session._configure_session = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001

    await session._connect_realtime_api()  # noqa: SLF001

    connect.assert_called_once_with(model="gpt-realtime-2.1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "configured_effort", "expected_reasoning"),
    [
        ("gpt-realtime-2.1", "low", {"effort": "low"}),
        ("gpt-realtime-2025-08-28", "low", None),
        ("gpt-realtime-2.1", None, None),
        ("gpt-realtime-2.1", "invalid", None),
    ],
)
async def test_reasoning_effort_is_only_sent_for_realtime_2(
    model: str,
    configured_effort: str | None,
    expected_reasoning: dict[str, str] | None,
) -> None:
    session = make_session()
    session.realtime_model = model
    session.realtime_reasoning_effort = configured_effort
    update = AsyncMock()
    session.connection = MagicMock(session=MagicMock(update=update))
    session.tool_registry = MagicMock()
    session.tool_registry.get_all_tool_definitions.return_value = []

    await session._configure_session()  # noqa: SLF001

    sent: dict[str, Any] = update.await_args.kwargs["session"]
    assert sent.get("reasoning") == expected_reasoning


@pytest.mark.asyncio
async def test_tool_logging_records_argument_keys_without_values() -> None:
    session = make_session()
    session.logger = MagicMock()
    session.tool_registry = MagicMock()
    session.tool_registry.execute_tool = AsyncMock(return_value={"success": True})
    arguments = {
        "email": "private@example.com",
        "notes": "sensitive qualification notes",
    }

    result = await session.handle_tool_call({"name": "book_appointment", "arguments": arguments})

    assert result == {"success": True}
    session.logger.info.assert_called_once_with(
        "handling_tool_call",
        tool_name="book_appointment",
        argument_keys=["email", "notes"],
    )
    logged = repr(session.logger.info.call_args)
    assert "private@example.com" not in logged
    assert "sensitive qualification notes" not in logged
