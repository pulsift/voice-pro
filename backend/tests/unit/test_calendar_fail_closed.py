"""Finding #19: live router calls fail closed on calendar ambiguity."""

import base64
import inspect
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException

from app.api.telephony import InitiateCallRequest, initiate_call
from app.core.config import settings
from app.services.availability import (
    CALCOM_REQUIRED_BACKEND,
    CALENDAR_BACKEND_VARIABLE,
)
from app.services.calcom_client import CalendarAvailabilityError, get_open_slots
from app.services.telephony.base import CallDirection, CallInfo
from app.services.tools.crm_tools import CRMTools


def _agent_result(agent: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = agent
    return result


def _membership_result(workspace_id: uuid.UUID) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        SimpleNamespace(workspace_id=workspace_id, is_default=True)
    ]
    return result


def _scalar_result(value: object) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _malformed_slots_context(payload: object) -> MagicMock:
    response = MagicMock(status_code=200)
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    client = MagicMock(get=AsyncMock(return_value=response))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    return context


@pytest.mark.asyncio
async def test_malformed_calcom_payload_is_unavailable_not_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 42)
    context = _malformed_slots_context({"data": []})

    with (
        patch("app.services.calcom_client.httpx.AsyncClient", return_value=context),
        pytest.raises(CalendarAvailabilityError),
    ):
        await get_open_slots("UTC")


@pytest.mark.asyncio
async def test_required_calcom_call_never_falls_back_to_internal_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", None)
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", None)
    db = MagicMock(execute=AsyncMock())
    tools = CRMTools(
        db=db,
        user_id=1,
        variables={CALENDAR_BACKEND_VARIABLE: CALCOM_REQUIRED_BACKEND},
    )

    availability = await tools.check_availability(date="2026-08-03")
    booking = await tools.book_appointment(
        scheduled_at="2026-08-03T09:00:00Z",
        contact_phone="+14085550100",
    )

    assert availability == {"success": False, "error": "calendar_unavailable"}
    assert booking == {"success": False, "error": "calendar_unavailable"}
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_generic_agent_keeps_internal_calendar_when_calcom_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", None)
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", None)
    result = MagicMock()
    result.scalars.return_value.all.return_value = []
    db = MagicMock(execute=AsyncMock(return_value=result))
    tools = CRMTools(db=db, user_id=1, variables={})

    availability = await tools.check_availability(date="2026-08-03")

    assert availability["success"] is True
    assert len(availability["available_slots"]) == 8
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbound_call_rejects_incomplete_calcom_after_ownership_before_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, user_id=1)
    db = MagicMock(add=MagicMock(), commit=AsyncMock())
    db.execute = AsyncMock(
        side_effect=[
            _agent_result(agent),
            _membership_result(workspace_id),
            _scalar_result(uuid.uuid4()),
        ]
    )
    get_telnyx = AsyncMock()
    get_twilio = AsyncMock()
    bound_log = MagicMock()
    logger_mock = MagicMock()
    logger_mock.bind.return_value = bound_log
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "calendar-api-secret")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", None)

    with (
        patch("app.api.telephony.get_telnyx_service", new=get_telnyx),
        patch("app.api.telephony.get_twilio_service", new=get_twilio),
        patch("app.api.telephony.logger", new=logger_mock),
        pytest.raises(HTTPException) as exc_info,
    ):
        await inspect.unwrap(initiate_call)(
            InitiateCallRequest(
                to_number="+14085550101",
                from_number="+14085550100",
                agent_id=str(agent_id),
            ),
            MagicMock(base_url="https://voice.example/"),
            MagicMock(id=1),
            db,
            workspace_id=None,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {"code": "calendar_unavailable"}
    assert db.execute.await_count == 3
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
    get_telnyx.assert_not_awaited()
    get_twilio.assert_not_awaited()
    error_call = bound_log.error.call_args
    assert error_call.kwargs["missing_settings"] == ("CALCOM_EVENT_TYPE_ID",)
    assert "calendar-api-secret" not in repr(bound_log.mock_calls)


@pytest.mark.asyncio
async def test_outbound_call_forces_server_calendar_marker_over_client_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, user_id=1)
    locked = MagicMock()
    db = MagicMock(add=MagicMock(), commit=AsyncMock())
    locked.scalar_one.side_effect = lambda: db.add.call_args.args[0]
    db.execute = AsyncMock(
        side_effect=[
            _agent_result(agent),
            _membership_result(workspace_id),
            _scalar_result(uuid.uuid4()),
            locked,
        ]
    )
    service = MagicMock()
    service.initiate_call = AsyncMock(
        return_value=CallInfo(
            call_id="provider-call-id",
            call_control_id="provider-control-id",
            from_number="+14085550100",
            to_number="+14085550101",
            direction=CallDirection.OUTBOUND,
            agent_id=str(agent_id),
        )
    )
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 42)
    monkeypatch.setattr(settings, "TELEPHONY_OUTBOUND_PROVIDER", "telnyx")

    with (
        patch("app.api.telephony.get_telnyx_service", new=AsyncMock(return_value=service)),
        patch("app.api.telephony.get_twilio_service", new=AsyncMock(return_value=None)),
    ):
        await inspect.unwrap(initiate_call)(
            InitiateCallRequest(
                to_number="+14085550101",
                from_number="+14085550100",
                agent_id=str(agent_id),
                variables={CALENDAR_BACKEND_VARIABLE: "client_override", "leadName": "Lead"},
            ),
            MagicMock(base_url="https://voice.example/"),
            MagicMock(id=1),
            db,
            workspace_id=None,
        )

    record = db.add.call_args.args[0]
    assert record.variables[CALENDAR_BACKEND_VARIABLE] == CALCOM_REQUIRED_BACKEND
    webhook_url = service.initiate_call.await_args.kwargs["webhook_url"]
    encoded = parse_qs(urlsplit(webhook_url).query)["cv"][0]
    streamed_variables = json.loads(base64.urlsafe_b64decode(encoded).decode())
    assert streamed_variables[CALENDAR_BACKEND_VARIABLE] == CALCOM_REQUIRED_BACKEND
    assert streamed_variables["leadName"] == "Lead"
