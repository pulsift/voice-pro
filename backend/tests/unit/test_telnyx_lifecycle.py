"""Offline contracts for Telnyx callback and media lifecycle telemetry."""

import inspect
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.api.telephony import (
    InitiateCallRequest,
    initiate_call,
    resolve_outbound_workspace_id,
    telnyx_answer_webhook,
    telnyx_status_callback,
    update_campaign_contact_from_call,
)
from app.api.telephony_ws import (
    resolve_media_workspace_id,
    telnyx_media_stream,
    update_telnyx_media_lifecycle,
)
from app.models.call_record import CallStatus
from app.models.campaign import CampaignContactStatus
from app.services.campaign_worker import CampaignWorker
from app.services.telephony.base import CallDirection, CallInfo
from app.services.telephony.telnyx_service import TelnyxService, is_unknown_telnyx_dial_outcome


def _record() -> MagicMock:
    return MagicMock(
        id=uuid.uuid4(),
        provider="telnyx",
        provider_call_id="call-sid-1",
        status=CallStatus.INITIATED.value,
        answered_at=None,
        ended_at=None,
        duration_seconds=0,
        direction="outbound",
        agent_id=uuid.uuid4(),
        contact_id=None,
        from_number="+14085550100",
        to_number="+14085550101",
    )


def _query_result(record: MagicMock | None) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = [] if record is None else [record]
    return result


def _form_request(form: dict[str, str]) -> MagicMock:
    request = MagicMock()
    request.json = AsyncMock(side_effect=ValueError("not json"))
    request.form = AsyncMock(return_value=form)
    return request


@pytest.mark.asyncio
async def test_texml_form_callbacks_record_answer_end_and_provider_duration() -> None:
    record = _record()
    db = MagicMock()
    db.execute = AsyncMock(return_value=_query_result(record))
    db.commit = AsyncMock()

    answered = _form_request(
        {
            "CallSid": "call-sid-1",
            "CallStatus": "in-progress",
            "Timestamp": "2026-07-11T01:00:00Z",
        }
    )
    completed = _form_request(
        {
            "CallSid": "call-sid-1",
            "CallStatus": "completed",
            "CallDuration": "42",
            "Timestamp": "2026-07-11T01:00:42Z",
        }
    )

    with (
        patch("app.api.telephony.verify_telnyx_webhook", new=AsyncMock(return_value=True)),
        patch(
            "app.api.telephony.update_campaign_contact_from_call",
            new=AsyncMock(),
        ) as update_campaign,
    ):
        await telnyx_status_callback(answered, db)
        assert record.status == CallStatus.IN_PROGRESS.value
        assert record.answered_at == datetime(2026, 7, 11, 1, 0, tzinfo=UTC)

        await telnyx_status_callback(completed, db)

    assert record.status == CallStatus.COMPLETED.value
    assert record.ended_at == datetime(2026, 7, 11, 1, 0, 42, tzinfo=UTC)
    assert record.duration_seconds == 42
    assert db.commit.await_count == 2
    update_campaign.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_terminal_callback_remains_idempotent_in_call_record() -> None:
    record = _record()
    db = MagicMock()
    db.execute = AsyncMock(return_value=_query_result(record))
    db.commit = AsyncMock()
    request = _form_request(
        {
            "CallSid": "call-sid-1",
            "CallStatus": "no-answer",
            "CallDuration": "0",
        }
    )

    with (
        patch("app.api.telephony.verify_telnyx_webhook", new=AsyncMock(return_value=True)),
        patch(
            "app.api.telephony.update_campaign_contact_from_call",
            new=AsyncMock(),
        ) as update_campaign,
    ):
        await telnyx_status_callback(request, db)
        await telnyx_status_callback(request, db)

    assert record.status == CallStatus.NO_ANSWER.value
    assert record.duration_seconds == 0
    assert update_campaign.await_count == 2


@pytest.mark.asyncio
async def test_call_control_json_uses_event_time_and_hangup_cause() -> None:
    record = _record()
    db = MagicMock()
    db.execute = AsyncMock(return_value=_query_result(record))
    db.commit = AsyncMock()
    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "data": {
                "event_type": "call.hangup",
                "occurred_at": "2026-07-11T02:03:04Z",
                "payload": {
                    "call_control_id": "call-sid-1",
                    "hangup_cause": "USER_BUSY",
                    "duration_secs": 0,
                },
            }
        }
    )

    with (
        patch("app.api.telephony.verify_telnyx_webhook", new=AsyncMock(return_value=True)),
        patch("app.api.telephony.update_campaign_contact_from_call", new=AsyncMock()),
    ):
        await telnyx_status_callback(request, db)

    assert record.status == CallStatus.BUSY.value
    assert record.ended_at == datetime(2026, 7, 11, 2, 3, 4, tzinfo=UTC)


@pytest.mark.asyncio
async def test_lowercase_normal_clearing_is_a_successful_completion() -> None:
    record = _record()
    db = MagicMock(execute=AsyncMock(return_value=_query_result(record)), commit=AsyncMock())
    request = _form_request(
        {
            "CallSid": "call-sid-1",
            "CallStatus": "completed",
            "HangupCause": " normal_clearing ",
        }
    )

    with patch("app.api.telephony.verify_telnyx_webhook", new=AsyncMock(return_value=True)):
        await telnyx_status_callback(request, db)

    assert record.status == CallStatus.COMPLETED.value


@pytest.mark.asyncio
async def test_generic_completed_never_erases_specific_terminal_outcome() -> None:
    record = _record()
    db = MagicMock(execute=AsyncMock(return_value=_query_result(record)), commit=AsyncMock())
    busy = _form_request({"CallSid": "call-sid-1", "CallStatus": "busy"})
    completed = _form_request({"CallSid": "call-sid-1", "CallStatus": "completed"})

    with (
        patch("app.api.telephony.verify_telnyx_webhook", new=AsyncMock(return_value=True)),
        patch("app.api.telephony.update_campaign_contact_from_call", new=AsyncMock()),
    ):
        await telnyx_status_callback(busy, db)
        await telnyx_status_callback(completed, db)

    assert record.status == CallStatus.BUSY.value


@pytest.mark.asyncio
async def test_callback_reconciles_precommitted_pending_record_before_provider_returns() -> None:
    record = _record()
    record.provider_call_id = "pending:local-correlation"
    empty = _query_result(None)
    pending = MagicMock()
    pending.scalars.return_value.all.return_value = [record.id]
    locked = MagicMock()
    locked.scalar_one.return_value = record
    db = MagicMock(
        execute=AsyncMock(side_effect=[empty, pending, locked]),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    request = _form_request(
        {
            "CallSid": "provider-call-sid",
            "CallStatus": "ringing",
            "From": record.from_number,
            "To": record.to_number,
        }
    )

    with patch("app.api.telephony.verify_telnyx_webhook", new=AsyncMock(return_value=True)):
        await telnyx_status_callback(request, db)

    assert record.provider_call_id == "provider-call-sid"
    assert record.status == CallStatus.RINGING.value
    db.commit.assert_awaited_once()
    exact_query = db.execute.await_args_list[0].args[0]
    locked_query = db.execute.await_args_list[2].args[0]
    assert getattr(exact_query, "_for_update_arg", None) is not None
    assert getattr(locked_query, "_for_update_arg", None) is not None


@pytest.mark.asyncio
async def test_outbound_telnyx_form_has_per_call_status_callback_contract() -> None:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "data": {"call_sid": "call-sid-1", "call_control_id": "control-1"}
    }
    client = MagicMock(post=AsyncMock(return_value=response))
    service = TelnyxService("test-key")
    with (
        patch.object(service, "_get_http_client", new=AsyncMock(return_value=client)),
        patch.object(service, "_get_connection_id", new=AsyncMock(return_value="app-1")),
    ):
        await service.initiate_call(
            to_number="+14085550101",
            from_number="+14085550100",
            webhook_url=(
                "https://voice.example/webhooks/telnyx/answer?agent_id=agent-1&workspace_id=ws-1"
            ),
        )

    form = client.post.await_args.kwargs["data"]
    assert form["StatusCallback"] == "https://voice.example/webhooks/telnyx/status"
    assert form["StatusCallbackMethod"] == "POST"
    assert form["StatusCallbackEvent"] == "initiated ringing answered completed"


def test_only_transport_timeout_and_server_error_have_unknown_dial_outcome() -> None:
    request = httpx.Request("POST", "https://api.telnyx.com/v2/texml/calls/app-1")
    client_error_response = httpx.Response(400, request=request)
    server_error_response = httpx.Response(503, request=request)

    assert not is_unknown_telnyx_dial_outcome(
        httpx.HTTPStatusError("bad request", request=request, response=client_error_response)
    )
    assert is_unknown_telnyx_dial_outcome(
        httpx.HTTPStatusError("server error", request=request, response=server_error_response)
    )
    assert is_unknown_telnyx_dial_outcome(httpx.ReadTimeout("timeout", request=request))


@pytest.mark.asyncio
async def test_telnyx_answer_forwards_authoritative_workspace_to_media_stream() -> None:
    request = MagicMock()
    request.base_url = "https://voice.example/"
    workspace_id = str(uuid.uuid4())

    with patch("app.api.telephony.verify_telnyx_webhook", new=AsyncMock(return_value=True)):
        response = await telnyx_answer_webhook(
            request,
            agent_id=str(uuid.uuid4()),
            cv="encoded-lead-data",
            workspace_id=workspace_id,
        )

    body = bytes(response.body).decode()
    assert f"workspace_id={workspace_id}" in body
    assert "cv=encoded-lead-data" in body


@pytest.mark.asyncio
async def test_telnyx_pending_record_is_committed_before_external_dial() -> None:
    agent_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent = SimpleNamespace(id=agent_id, user_id=1)
    agent_result = MagicMock()
    agent_result.scalar_one_or_none.return_value = agent
    memberships = MagicMock()
    memberships.scalars.return_value.all.return_value = [
        SimpleNamespace(workspace_id=workspace_id, is_default=True)
    ]
    locked = MagicMock()
    db = MagicMock(add=MagicMock(), commit=AsyncMock())
    locked.scalar_one.side_effect = lambda: db.add.call_args.args[0]
    db.execute = AsyncMock(side_effect=[agent_result, memberships, locked])
    service = MagicMock()

    async def dial_after_precommit(**_kwargs: object) -> CallInfo:
        assert db.commit.await_count == 1
        pending_record = db.add.call_args.args[0]
        assert pending_record.provider_call_id.startswith("pending:")
        assert pending_record.workspace_id == workspace_id
        return CallInfo(
            call_id="call-sid-1",
            call_control_id="control-1",
            from_number="+14085550100",
            to_number="+14085550101",
            direction=CallDirection.OUTBOUND,
            agent_id=str(agent_id),
        )

    service.initiate_call = AsyncMock(side_effect=dial_after_precommit)
    request = MagicMock(base_url="https://voice.example/")
    current_user = MagicMock(id=1)

    with (
        patch("app.api.telephony.get_telnyx_service", new=AsyncMock(return_value=service)),
        patch("app.api.telephony.get_twilio_service", new=AsyncMock(return_value=None)),
    ):
        response = await inspect.unwrap(initiate_call)(
            InitiateCallRequest(
                to_number="+14085550101",
                from_number="+14085550100",
                agent_id=str(agent_id),
            ),
            request,
            current_user,
            db,
            workspace_id=None,
        )

    pending_record = db.add.call_args.args[0]
    assert pending_record.provider_call_id == "call-sid-1"
    assert pending_record.contact_id is None
    assert db.commit.await_count == 2
    assert response.call_id == "call-sid-1"


@pytest.mark.asyncio
async def test_timeout_after_possible_accept_stays_pending_and_callback_repairs() -> None:
    agent_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent_result = MagicMock()
    agent_result.scalar_one_or_none.return_value = SimpleNamespace(id=agent_id, user_id=1)
    memberships = MagicMock()
    memberships.scalars.return_value.all.return_value = [
        SimpleNamespace(workspace_id=workspace_id, is_default=True)
    ]
    db = MagicMock(add=MagicMock(), commit=AsyncMock())
    db.execute = AsyncMock(side_effect=[agent_result, memberships])
    service = MagicMock()
    service.initiate_call = AsyncMock(
        side_effect=httpx.ReadTimeout("response lost after provider may have accepted")
    )

    with (
        patch("app.api.telephony.get_telnyx_service", new=AsyncMock(return_value=service)),
        patch("app.api.telephony.get_twilio_service", new=AsyncMock(return_value=None)),
        pytest.raises(httpx.ReadTimeout),
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

    record = db.add.call_args.args[0]
    assert record.provider_call_id.startswith("pending:")
    assert record.status == CallStatus.INITIATED.value
    assert record.ended_at is None
    assert db.commit.await_count == 1

    empty = _query_result(None)
    pending = MagicMock()
    pending.scalars.return_value.all.return_value = [record.id]
    locked = MagicMock()
    locked.scalar_one.return_value = record
    callback_db = MagicMock(
        execute=AsyncMock(side_effect=[empty, pending, locked]),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    callback = _form_request(
        {
            "CallSid": "accepted-call-sid",
            "CallStatus": "completed",
            "From": record.from_number,
            "To": record.to_number,
        }
    )
    with patch("app.api.telephony.verify_telnyx_webhook", new=AsyncMock(return_value=True)):
        await telnyx_status_callback(callback, callback_db)

    assert record.provider_call_id == "accepted-call-sid"
    assert record.status == CallStatus.COMPLETED.value
    assert record.ended_at is not None


@pytest.mark.asyncio
async def test_telnyx_media_stream_uses_forwarded_workspace_selection() -> None:
    agent_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        user_id=1,
        is_active=True,
        name="Agent",
        system_prompt="prompt",
        enabled_tools=[],
        language="en",
        voice="cedar",
        enable_transcript=False,
        initial_greeting=None,
    )
    agent_result = MagicMock()
    agent_result.scalar_one_or_none.return_value = agent
    db = MagicMock(execute=AsyncMock(return_value=agent_result))
    websocket = MagicMock()
    websocket.query_params = {"workspace_id": str(workspace_id)}
    websocket.accept = AsyncMock()
    websocket.close = AsyncMock()
    session = MagicMock()
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=session)
    context.__aexit__ = AsyncMock(return_value=None)

    with (
        patch(
            "app.api.telephony_ws.resolve_media_workspace_id",
            new=AsyncMock(return_value=workspace_id),
        ) as resolver,
        patch("app.api.telephony_ws.GPTRealtimeSession", return_value=context) as realtime,
        patch("app.api.telephony_ws._handle_telnyx_stream", new=AsyncMock(return_value="")),
    ):
        await telnyx_media_stream(websocket, str(agent_id), db)

    resolver.assert_awaited_once_with(agent_id, str(workspace_id), db)
    assert realtime.call_args.kwargs["workspace_id"] == workspace_id


@pytest.mark.asyncio
async def test_campaign_worker_precommits_trusted_call_correlation() -> None:
    worker = CampaignWorker("https://voice.example")
    campaign = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        from_phone_number="+14085550100",
        contacts_called=0,
    )
    campaign_contact = SimpleNamespace(
        id=uuid.uuid4(),
        status=CampaignContactStatus.PENDING.value,
        attempts=0,
        last_attempt_at=None,
        last_call_outcome=None,
    )
    contact = SimpleNamespace(id=42, phone_number="+14085550101")
    db = MagicMock(add=MagicMock(), commit=AsyncMock())
    locked = MagicMock()
    locked.scalar_one.side_effect = lambda: db.add.call_args.args[0]
    db.execute = AsyncMock(return_value=locked)
    service = TelnyxService("test-key")

    async def dial_after_precommit(**kwargs: object) -> CallInfo:
        assert db.commit.await_count == 1
        record = db.add.call_args.args[0]
        assert record.contact_id == 42
        assert record.workspace_id == campaign.workspace_id
        assert campaign_contact.status == CampaignContactStatus.CALLING.value
        assert f"workspace_id={campaign.workspace_id}" in str(kwargs["webhook_url"])
        return CallInfo(
            call_id="campaign-call-sid",
            call_control_id="campaign-control-id",
            from_number=campaign.from_phone_number,
            to_number=contact.phone_number,
            direction=CallDirection.OUTBOUND,
        )

    with patch.object(service, "initiate_call", new=AsyncMock(side_effect=dial_after_precommit)):
        await worker._initiate_call(  # noqa: SLF001
            campaign,
            campaign_contact,
            contact,
            service,
            db,
        )

    record = db.add.call_args.args[0]
    assert record.provider_call_id == "campaign-call-sid"
    assert record.agent_id == campaign.agent_id
    assert record.contact_id == contact.id
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_definitive_campaign_dial_rejection_releases_called_metric() -> None:
    worker = CampaignWorker("https://voice.example")
    campaign = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        from_phone_number="+14085550100",
        contacts_called=0,
    )
    campaign_contact = SimpleNamespace(
        id=uuid.uuid4(),
        status=CampaignContactStatus.PENDING.value,
        attempts=0,
        last_attempt_at=None,
        last_call_outcome=None,
    )
    contact = SimpleNamespace(id=42, phone_number="+14085550101")
    db = MagicMock(add=MagicMock(), commit=AsyncMock())
    locked = MagicMock()
    locked.scalar_one.side_effect = lambda: db.add.call_args.args[0]
    db.execute = AsyncMock(return_value=locked)
    service = TelnyxService("test-key")
    request = httpx.Request("POST", "https://api.telnyx.com/v2/texml/calls/app-1")
    rejection = httpx.HTTPStatusError(
        "bad request",
        request=request,
        response=httpx.Response(400, request=request),
    )

    with (
        patch.object(service, "initiate_call", new=AsyncMock(side_effect=rejection)),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await worker._initiate_call(  # noqa: SLF001
            campaign,
            campaign_contact,
            contact,
            service,
            db,
        )

    record = db.add.call_args.args[0]
    assert record.status == CallStatus.FAILED.value
    assert record.ended_at is not None
    assert campaign.contacts_called == 0
    assert campaign_contact.attempts == 1


@pytest.mark.asyncio
async def test_workspace_resolution_uses_single_membership_when_omitted() -> None:
    workspace_id = uuid.uuid4()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        SimpleNamespace(workspace_id=workspace_id, is_default=False)
    ]
    db = MagicMock(execute=AsyncMock(return_value=result))

    resolved = await resolve_outbound_workspace_id(
        agent_id=uuid.uuid4(), owner_user_id=1, requested_workspace_id=None, db=db
    )

    assert resolved == workspace_id


@pytest.mark.asyncio
async def test_workspace_resolution_accepts_explicit_membership_in_multi_workspace_agent() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        SimpleNamespace(workspace_id=first, is_default=False),
        SimpleNamespace(workspace_id=second, is_default=False),
    ]
    db = MagicMock(execute=AsyncMock(return_value=result))

    resolved = await resolve_outbound_workspace_id(
        agent_id=uuid.uuid4(), owner_user_id=1, requested_workspace_id=second, db=db
    )

    assert resolved == second


@pytest.mark.asyncio
async def test_workspace_resolution_rejects_mismatched_membership() -> None:
    result = MagicMock()
    result.scalars.return_value.all.return_value = [
        SimpleNamespace(workspace_id=uuid.uuid4(), is_default=False)
    ]
    db = MagicMock(execute=AsyncMock(return_value=result))

    with pytest.raises(HTTPException, match="does not belong"):
        await resolve_outbound_workspace_id(
            agent_id=uuid.uuid4(),
            owner_user_id=1,
            requested_workspace_id=uuid.uuid4(),
            db=db,
        )


@pytest.mark.asyncio
async def test_media_workspace_rejects_explicit_nonmembership() -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = MagicMock(execute=AsyncMock(return_value=result))

    with pytest.raises(ValueError, match="does not belong"):
        await resolve_media_workspace_id(uuid.uuid4(), str(uuid.uuid4()), db)


@pytest.mark.asyncio
async def test_manual_call_without_contact_never_mutates_campaign() -> None:
    record = _record()
    db = MagicMock(execute=AsyncMock())

    await update_campaign_contact_from_call(record, CallStatus.COMPLETED.value, 10, db)

    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_calling_campaign_contacts_are_refused_under_lock() -> None:
    record = _record()
    record.contact_id = 42
    first, second = MagicMock(), MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = [first, second]
    db = MagicMock(execute=AsyncMock(return_value=result))

    await update_campaign_contact_from_call(record, CallStatus.COMPLETED.value, 10, db)

    assert db.execute.await_count == 1
    query = db.execute.await_args.args[0]
    assert getattr(query, "_for_update_arg", None) is not None
    compiled = query.compile()
    assert "campaign_contacts.contact_id" in str(compiled)
    assert 42 in compiled.params.values()
    assert first.last_call_id != record.id
    assert second.last_call_id != record.id


@pytest.mark.asyncio
async def test_media_lifecycle_fallback_marks_answered_then_completed() -> None:
    record = _record()
    owner_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent_id = record.agent_id
    empty = _query_result(None)
    fallback = _query_result(record)
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[empty, fallback, empty, fallback])
    db.commit = AsyncMock()
    log = MagicMock()

    kwargs = {
        "agent_id": agent_id,
        "owner_user_id": owner_id,
        "workspace_id": workspace_id,
        "expected_to_number": "+14085550101",
    }
    await update_telnyx_media_lifecycle("call-control-1", db, log, ended=False, **kwargs)
    await update_telnyx_media_lifecycle("call-control-1", db, log, ended=True, **kwargs)

    assert record.answered_at is not None
    assert record.ended_at is not None
    assert record.status == CallStatus.COMPLETED.value
    assert record.duration_seconds >= 0
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_media_lifecycle_refuses_ambiguous_fallback() -> None:
    db = MagicMock()
    ambiguous = MagicMock()
    ambiguous.scalars.return_value.all.return_value = [_record(), _record()]
    db.execute = AsyncMock(side_effect=[_query_result(None), ambiguous])
    db.commit = AsyncMock()
    log = MagicMock()

    await update_telnyx_media_lifecycle(
        "unmatched-control-id",
        db,
        log,
        agent_id=uuid.uuid4(),
        owner_user_id=uuid.uuid4(),
        workspace_id=None,
        expected_to_number=None,
        ended=False,
    )

    db.commit.assert_not_awaited()
    log.warning.assert_called_once()
