"""Finding #3: one router attempt key can cause at most one provider dial."""

# ruff: noqa: SLF001 - the contract lives in intentionally private endpoint helpers.

import importlib
import inspect
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.api import telephony
from app.core.auth import user_id_to_uuid
from app.core.config import settings
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.services.availability import CALENDAR_BACKEND_VARIABLE
from app.services.call_events import build_call_ended_payload
from app.services.telephony.base import (
    CallDirection as ProviderCallDirection,
)
from app.services.telephony.base import (
    CallInfo,
)
from app.services.telephony.twilio_service import TwilioDialOutcomeUnknownError

_AGENT_ID = uuid.UUID("e7af834a-4c5d-45c0-8825-b1de6f801cb2")
_WORKSPACE_ID = uuid.UUID("07dc12f2-88e7-46bb-bf76-358984476a0e")
_FROM = "+14155550100"
_TO = "+14155550101"


def _scalar_list_result(*records: object) -> MagicMock:
    result = MagicMock()
    result.scalars.return_value.all.return_value = list(records)
    return result


def _agent_result() -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = SimpleNamespace(
        id=_AGENT_ID,
        user_id=1,
        enable_recording=False,
    )
    return result


def _locked_result(db: MagicMock) -> MagicMock:
    result = MagicMock()
    result.scalar_one.side_effect = lambda: db.add.call_args.args[0]
    return result


def _record_locked_result(record: CallRecord) -> MagicMock:
    result = MagicMock()
    result.scalar_one.return_value = record
    return result


def _dispatch_result(
    *, record: CallRecord | None = None, db: MagicMock | None = None
) -> MagicMock:
    result = MagicMock()

    def claim() -> CallRecord:
        claimed = record if record is not None else db.add.call_args.args[0]
        claimed.dial_attempt_state = "dispatching"
        return claimed

    result.scalar_one_or_none.side_effect = claim
    return result


def _no_dispatch_result() -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    return result


def _new_attempt_results(db: MagicMock) -> list[MagicMock]:
    return [
        _scalar_list_result(),
        _agent_result(),
        _dispatch_result(db=db),
        _locked_result(db),
    ]


def _request(
    attempt_id: uuid.UUID,
    *,
    to_number: str = _TO,
    variables: dict[str, object] | None = None,
) -> telephony.IdempotentInitiateCallRequest:
    return telephony.IdempotentInitiateCallRequest(
        dial_attempt_id=attempt_id,
        to_number=to_number,
        from_number=_FROM,
        agent_id=str(_AGENT_ID),
        variables=variables,
    )


def _legacy_request(
    request: telephony.IdempotentInitiateCallRequest,
) -> telephony.InitiateCallRequest:
    return telephony.InitiateCallRequest.model_validate(
        request.model_dump(exclude={"dial_attempt_id"})
    )


def _request_hash(
    request: telephony.IdempotentInitiateCallRequest,
    requested_workspace_id: uuid.UUID | None = None,
) -> str:
    return telephony._dial_request_sha256(
        _legacy_request(request),
        requested_workspace_id,
    )


def _record(
    request: telephony.IdempotentInitiateCallRequest,
    *,
    provider: str = "twilio",
    provider_call_id: str = "pending:nonce",
    state: str = "dispatch_ready_v2",
    result: dict[str, object] | None = None,
    request_hash: str | None = None,
    owner_id: uuid.UUID | None = None,
) -> CallRecord:
    return CallRecord(
        id=uuid.uuid4(),
        user_id=owner_id or user_id_to_uuid(1),
        workspace_id=_WORKSPACE_ID,
        provider=provider,
        provider_call_id=provider_call_id,
        agent_id=_AGENT_ID,
        direction=CallDirection.OUTBOUND.value,
        status=CallStatus.INITIATED.value,
        from_number=request.from_number,
        to_number=request.to_number,
        variables={
            **dict(request.variables or {}),
            CALENDAR_BACKEND_VARIABLE: "calcom_required",
            "dialAttemptId": str(request.dial_attempt_id),
            "dial_attempt_id": str(request.dial_attempt_id),
        },
        dial_attempt_id=request.dial_attempt_id,
        dial_request_sha256=request_hash or _request_hash(request),
        dial_attempt_state=state,
        dial_attempt_result=result,
    )


def _configure_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
    service: MagicMock,
    *,
    provider: str = "twilio",
) -> tuple[AsyncMock, AsyncMock]:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 42)
    monkeypatch.setattr(settings, "TELEPHONY_OUTBOUND_PROVIDER", provider)
    monkeypatch.setattr(
        telephony,
        "_initiate_new_keyed_call",
        inspect.unwrap(telephony._initiate_new_keyed_call),
    )
    resolve = AsyncMock(return_value=_WORKSPACE_ID)
    require_caller = AsyncMock(return_value=None)
    monkeypatch.setattr(telephony, "resolve_outbound_workspace_id", resolve)
    monkeypatch.setattr(telephony, "require_owned_caller_id", require_caller)
    monkeypatch.setattr(
        telephony,
        "get_twilio_service",
        AsyncMock(return_value=service if provider == "twilio" else None),
    )
    monkeypatch.setattr(
        telephony,
        "get_telnyx_service",
        AsyncMock(return_value=service if provider == "telnyx" else None),
    )
    return resolve, require_caller


async def _invoke(
    request: telephony.IdempotentInitiateCallRequest,
    db: MagicMock,
    *,
    workspace_id: str | None = None,
) -> telephony.CallResponse | telephony.JSONResponse:
    endpoint = inspect.unwrap(telephony.initiate_call_idempotent)
    return await endpoint(
        request,
        MagicMock(base_url="https://voice.example/"),
        SimpleNamespace(id=1),
        db,
        workspace_id=workspace_id,
    )


@pytest.mark.asyncio
async def test_first_keyed_request_claims_dispatch_before_provider_and_persists_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = uuid.uuid4()
    request = _request(
        attempt_id,
        variables={"leadName": "Ada", "dialAttemptId": "spoofed"},
    )
    db = MagicMock(add=MagicMock(), commit=AsyncMock(), rollback=AsyncMock())
    db.execute = AsyncMock(
        side_effect=_new_attempt_results(db)
    )
    service = MagicMock()

    async def dial_after_reservation(**_kwargs: object) -> CallInfo:
        assert db.commit.await_count == 2
        reserved = db.add.call_args.args[0]
        assert reserved.dial_attempt_id == attempt_id
        assert reserved.dial_attempt_state == "dispatching"
        assert len(reserved.dial_request_sha256) == 64
        assert reserved.variables["dialAttemptId"] == str(attempt_id)
        assert reserved.variables["dial_attempt_id"] == str(attempt_id)
        return CallInfo(
            call_id="CA-keyed-1",
            call_control_id="CA-keyed-1",
            from_number=_FROM,
            to_number=_TO,
            direction=ProviderCallDirection.OUTBOUND,
            agent_id=str(_AGENT_ID),
        )

    service.initiate_call = AsyncMock(side_effect=dial_after_reservation)
    _configure_new_attempt(monkeypatch, service)

    response = await _invoke(request, db)

    record = db.add.call_args.args[0]
    assert response.call_id == "CA-keyed-1"
    assert response.dial_attempt_id == str(attempt_id)
    assert response.dial_attempt_status == "accepted"
    assert record.provider_call_id == "CA-keyed-1"
    assert record.dial_attempt_state == "accepted"
    assert record.dial_attempt_result["call_id"] == "CA-keyed-1"
    service.initiate_call.assert_awaited_once()
    assert db.commit.await_count == 3


@pytest.mark.asyncio
async def test_v2_ready_replay_claims_existing_record_and_dispatches_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(uuid.uuid4(), variables={"leadName": "Ada"})
    record = _record(request, state="dispatch_ready_v2")
    db = MagicMock(add=MagicMock(), commit=AsyncMock(), rollback=AsyncMock())
    db.execute = AsyncMock(
        side_effect=[
            _scalar_list_result(record),
            _agent_result(),
            _dispatch_result(record=record),
            _record_locked_result(record),
        ]
    )
    service = MagicMock()

    async def dial_after_claim(**_kwargs: object) -> CallInfo:
        assert record.dial_attempt_state == "dispatching"
        assert db.commit.await_count == 2
        return CallInfo(
            call_id="CA-resumed",
            call_control_id="CA-resumed",
            from_number=_FROM,
            to_number=_TO,
            direction=ProviderCallDirection.OUTBOUND,
            agent_id=str(_AGENT_ID),
        )

    service.initiate_call = AsyncMock(side_effect=dial_after_claim)
    resolve, require_caller = _configure_new_attempt(monkeypatch, service)

    response = await _invoke(request, db)

    assert isinstance(response, telephony.CallResponse)
    assert response.call_id == "CA-resumed"
    assert response.call_record_id == str(record.id)
    assert record.dial_attempt_state == "accepted"
    assert record.provider_call_id == "CA-resumed"
    db.add.assert_not_called()
    resolve.assert_not_awaited()
    require_caller.assert_awaited_once()
    service.initiate_call.assert_awaited_once()
    assert db.commit.await_count == 3


@pytest.mark.asyncio
async def test_v2_dispatch_atomic_update_has_exactly_one_winner() -> None:
    request = _request(uuid.uuid4())
    record = _record(request, state="dispatch_ready_v2")
    first_db = MagicMock(
        execute=AsyncMock(return_value=_dispatch_result(record=record)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    second_db = MagicMock(
        execute=AsyncMock(
            side_effect=[_no_dispatch_result(), _scalar_list_result(record)]
        ),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    winner = await telephony._claim_dispatch_ready_dial_attempt(
        call_record_id=record.id,
        dial_attempt_id=request.dial_attempt_id,
        owner_user_id=user_id_to_uuid(1),
        request_sha256=_request_hash(request),
        db=first_db,
    )
    loser = await telephony._claim_dispatch_ready_dial_attempt(
        call_record_id=record.id,
        dial_attempt_id=request.dial_attempt_id,
        owner_user_id=user_id_to_uuid(1),
        request_sha256=_request_hash(request),
        db=second_db,
    )

    assert winner is record
    assert record.dial_attempt_state == "dispatching"
    assert isinstance(loser, telephony.JSONResponse)
    assert loser.status_code == 202
    assert json.loads(loser.body)["dial_attempt_status"] == "in_progress"
    first_statement = first_db.execute.await_args.args[0]
    assert first_statement.is_update
    assert first_statement._returning
    assert "dial_attempt_state" in str(first_statement.whereclause)
    first_db.commit.assert_awaited_once()
    first_db.rollback.assert_not_awaited()
    second_db.rollback.assert_awaited_once()
    second_db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_reserved_replay_never_reaches_provider_or_mutable_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(uuid.uuid4())
    record = _record(request, state="reserved")
    db = MagicMock(
        execute=AsyncMock(return_value=_scalar_list_result(record)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    forbidden = AsyncMock(side_effect=AssertionError("legacy reservation was replayed"))
    monkeypatch.setattr(telephony, "resolve_outbound_workspace_id", forbidden)
    monkeypatch.setattr(telephony, "get_twilio_service", forbidden)
    monkeypatch.setattr(telephony, "get_telnyx_service", forbidden)

    response = await _invoke(request, db)

    assert response.status_code == 202
    assert json.loads(response.body)["dial_attempt_status"] == "in_progress"
    forbidden.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_dispatching_replay_never_reaches_provider_or_mutable_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(uuid.uuid4())
    record = _record(request, state="dispatching")
    db = MagicMock(
        execute=AsyncMock(return_value=_scalar_list_result(record)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    forbidden = AsyncMock(side_effect=AssertionError("dispatching attempt was replayed"))
    monkeypatch.setattr(telephony, "resolve_outbound_workspace_id", forbidden)
    monkeypatch.setattr(telephony, "get_twilio_service", forbidden)
    monkeypatch.setattr(telephony, "get_telnyx_service", forbidden)

    response = await _invoke(request, db)

    assert response.status_code == 202
    assert json.loads(response.body)["dial_attempt_status"] == "in_progress"
    forbidden.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_accepted_replay_bypasses_deleted_agent_and_dependency_outages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempt_id = uuid.uuid4()
    request = _request(attempt_id)
    original = telephony.CallResponse(
        call_id="CA-original",
        call_control_id="CA-original",
        from_number=_FROM,
        to_number=_TO,
        direction="outbound",
        status="initiated",
        agent_id=str(_AGENT_ID),
        call_record_id=str(uuid.uuid4()),
        dial_attempt_id=str(attempt_id),
        dial_attempt_status="accepted",
    ).model_dump(mode="json")
    record = _record(
        request,
        provider_call_id="CA-original",
        state="accepted",
        result=original,
    )
    db = MagicMock(
        execute=AsyncMock(return_value=_scalar_list_result(record)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    forbidden = AsyncMock(side_effect=AssertionError("mutable preflight ran during replay"))
    monkeypatch.setattr(telephony, "resolve_outbound_workspace_id", forbidden)
    monkeypatch.setattr(telephony, "require_owned_caller_id", forbidden)
    monkeypatch.setattr(telephony, "get_twilio_service", forbidden)
    monkeypatch.setattr(telephony, "get_telnyx_service", forbidden)
    monkeypatch.setattr(settings, "CALCOM_API_KEY", None)
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", None)

    response = await _invoke(request, db)

    assert isinstance(response, telephony.CallResponse)
    assert response.model_dump(mode="json") == original
    db.add.assert_not_called()
    forbidden.assert_not_awaited()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_replay_returns_202_and_never_reaches_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(uuid.uuid4())
    record = _record(request, state="outcome_unknown")
    db = MagicMock(
        execute=AsyncMock(return_value=_scalar_list_result(record)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    provider = AsyncMock(side_effect=AssertionError("provider lookup ran during replay"))
    monkeypatch.setattr(telephony, "get_twilio_service", provider)
    monkeypatch.setattr(telephony, "get_telnyx_service", provider)

    response = await _invoke(request, db)

    assert response.status_code == 202
    body = json.loads(response.body)
    assert body["call_id"] is None
    assert body["dial_attempt_id"] == str(request.dial_attempt_id)
    assert body["dial_attempt_status"] == "outcome_unknown"
    provider.assert_not_awaited()
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_unique_insert_race_replays_winner_without_second_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(uuid.uuid4())
    winner_response = telephony.CallResponse(
        call_id="CA-winner",
        call_control_id="CA-winner",
        from_number=_FROM,
        to_number=_TO,
        direction="outbound",
        status="initiated",
        agent_id=str(_AGENT_ID),
        call_record_id=str(uuid.uuid4()),
        dial_attempt_id=str(request.dial_attempt_id),
        dial_attempt_status="accepted",
    )
    winner = _record(
        request,
        provider_call_id="CA-winner",
        state="accepted",
        result=winner_response.model_dump(mode="json"),
    )
    duplicate = IntegrityError("INSERT", {}, RuntimeError("duplicate key"))
    db = MagicMock(
        add=MagicMock(),
        commit=AsyncMock(side_effect=[duplicate, None]),
        rollback=AsyncMock(),
    )
    db.execute = AsyncMock(
        side_effect=[_scalar_list_result(), _agent_result(), _scalar_list_result(winner)]
    )
    service = MagicMock(initiate_call=AsyncMock())
    _configure_new_attempt(monkeypatch, service)

    response = await _invoke(request, db)

    assert isinstance(response, telephony.CallResponse)
    assert response.call_id == "CA-winner"
    service.initiate_call.assert_not_awaited()
    db.rollback.assert_awaited_once()
    assert db.commit.await_count == 2


@pytest.mark.asyncio
async def test_unique_insert_race_resumes_v2_winner_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(uuid.uuid4(), variables={"leadName": "Ada"})
    winner = _record(request, state="dispatch_ready_v2")
    duplicate = IntegrityError("INSERT", {}, RuntimeError("duplicate key"))
    db = MagicMock(
        add=MagicMock(),
        commit=AsyncMock(side_effect=[duplicate, None, None, None]),
        rollback=AsyncMock(),
    )
    db.execute = AsyncMock(
        side_effect=[
            _scalar_list_result(),
            _agent_result(),
            _scalar_list_result(winner),
            _agent_result(),
            _dispatch_result(record=winner),
            _record_locked_result(winner),
        ]
    )
    service = MagicMock(
        initiate_call=AsyncMock(
            return_value=CallInfo(
                call_id="CA-race-resumed",
                call_control_id="CA-race-resumed",
                from_number=_FROM,
                to_number=_TO,
                direction=ProviderCallDirection.OUTBOUND,
                agent_id=str(_AGENT_ID),
            )
        )
    )
    resolve, require_caller = _configure_new_attempt(monkeypatch, service)

    response = await _invoke(request, db)

    assert isinstance(response, telephony.CallResponse)
    assert response.call_id == "CA-race-resumed"
    assert response.call_record_id == str(winner.id)
    assert winner.provider_call_id == "CA-race-resumed"
    assert winner.dial_attempt_state == "accepted"
    service.initiate_call.assert_awaited_once()
    resolve.assert_awaited_once()
    assert require_caller.await_count == 2
    db.rollback.assert_awaited_once()
    assert db.commit.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("mismatch", ["hash", "owner"])
async def test_reused_key_with_changed_contract_returns_generic_conflict(
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str,
) -> None:
    request = _request(uuid.uuid4())
    record = _record(
        request,
        request_hash="different" if mismatch == "hash" else None,
        owner_id=uuid.uuid4() if mismatch == "owner" else None,
    )
    db = MagicMock(
        execute=AsyncMock(return_value=_scalar_list_result(record)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    provider = AsyncMock(side_effect=AssertionError("provider lookup ran after conflict"))
    monkeypatch.setattr(telephony, "get_twilio_service", provider)
    monkeypatch.setattr(telephony, "get_telnyx_service", provider)

    with pytest.raises(HTTPException) as exc_info:
        await _invoke(request, db)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {"code": "dial_attempt_conflict"}
    provider.assert_not_awaited()
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_twilio_unknown_outcome_is_persisted_and_returns_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(uuid.uuid4())
    db = MagicMock(add=MagicMock(), commit=AsyncMock(), rollback=AsyncMock())
    db.execute = AsyncMock(
        side_effect=_new_attempt_results(db)
    )
    service = MagicMock(
        initiate_call=AsyncMock(side_effect=TwilioDialOutcomeUnknownError("unknown"))
    )
    _configure_new_attempt(monkeypatch, service)

    response = await _invoke(request, db)

    record = db.add.call_args.args[0]
    assert response.status_code == 202
    assert json.loads(response.body)["dial_attempt_status"] == "outcome_unknown"
    assert record.provider_call_id.startswith("pending:")
    assert record.dial_attempt_state == "outcome_unknown"
    assert record.status == CallStatus.INITIATED.value
    assert record.ended_at is None
    assert db.commit.await_count == 3


@pytest.mark.asyncio
async def test_telnyx_4xx_is_definitive_rejection_not_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(uuid.uuid4())
    db = MagicMock(add=MagicMock(), commit=AsyncMock(), rollback=AsyncMock())
    db.execute = AsyncMock(
        side_effect=_new_attempt_results(db)
    )
    http_request = httpx.Request("POST", "https://api.telnyx.test/texml/calls/app")
    http_response = httpx.Response(422, request=http_request)
    rejection = httpx.HTTPStatusError(
        "unprocessable",
        request=http_request,
        response=http_response,
    )
    service = MagicMock(initiate_call=AsyncMock(side_effect=rejection))
    _configure_new_attempt(monkeypatch, service, provider="telnyx")

    with pytest.raises(HTTPException) as exc_info:
        await _invoke(request, db)

    record = db.add.call_args.args[0]
    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "dial_attempt_rejected"
    assert record.dial_attempt_state == "rejected"
    assert record.status == CallStatus.FAILED.value
    assert record.ended_at is not None
    assert db.commit.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_call_id", ["", "   ", "pending:fake"])
async def test_non_real_provider_id_is_unknown_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
    provider_call_id: str,
) -> None:
    request = _request(uuid.uuid4())
    db = MagicMock(add=MagicMock(), commit=AsyncMock(), rollback=AsyncMock())
    db.execute = AsyncMock(
        side_effect=_new_attempt_results(db)
    )
    service = MagicMock(
        initiate_call=AsyncMock(
            return_value=CallInfo(
                call_id=provider_call_id,
                call_control_id=None,
                from_number=_FROM,
                to_number=_TO,
                direction=ProviderCallDirection.OUTBOUND,
                agent_id=str(_AGENT_ID),
            )
        )
    )
    _configure_new_attempt(monkeypatch, service, provider="telnyx")

    response = await _invoke(request, db)

    record = db.add.call_args.args[0]
    assert response.status_code == 202
    assert json.loads(response.body)["dial_attempt_status"] == "outcome_unknown"
    assert record.provider_call_id.startswith("pending:")
    assert record.dial_attempt_state == "outcome_unknown"


@pytest.mark.asyncio
async def test_late_callback_overrides_stale_rejection_payload() -> None:
    request = _request(uuid.uuid4())
    record = _record(
        request,
        provider_call_id="CA-callback-proof",
        state="rejected",
        result={"code": "dial_attempt_rejected", "error_type": "HTTPStatusError"},
    )
    db = MagicMock(
        execute=AsyncMock(return_value=_scalar_list_result(record)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    response = await telephony._replay_dial_attempt(
        dial_attempt_id=request.dial_attempt_id,
        owner_user_id=user_id_to_uuid(1),
        request_sha256=_request_hash(request),
        db=db,
    )

    assert isinstance(response, telephony.CallResponse)
    assert response.call_id == "CA-callback-proof"
    assert response.dial_attempt_status == "accepted"
    assert record.dial_attempt_state == "accepted"
    assert record.dial_attempt_result["call_id"] == "CA-callback-proof"


@pytest.mark.asyncio
async def test_callback_promotes_dispatching_then_replay_returns_same_sid() -> None:
    request = _request(uuid.uuid4())
    record = _record(request, state="dispatching")
    record.provider_call_id = "CA-callback-proof"
    telephony._mark_dial_attempt_accepted_by_callback(record)
    assert record.dial_attempt_state == "accepted"

    db = MagicMock(
        execute=AsyncMock(return_value=_scalar_list_result(record)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    response = await telephony._replay_dial_attempt(
        dial_attempt_id=request.dial_attempt_id,
        owner_user_id=user_id_to_uuid(1),
        request_sha256=_request_hash(request),
        db=db,
    )

    assert isinstance(response, telephony.CallResponse)
    assert response.call_id == "CA-callback-proof"
    assert response.dial_attempt_status == "accepted"


def test_request_hash_excludes_server_owned_calendar_value() -> None:
    attempt_id = uuid.uuid4()
    old_backend = _request(
        attempt_id,
        variables={"leadName": "Ada", CALENDAR_BACKEND_VARIABLE: "old-server-value"},
    )
    new_backend = _request(
        attempt_id,
        variables={"leadName": "Ada", CALENDAR_BACKEND_VARIABLE: "new-server-value"},
    )
    changed_client_data = _request(
        attempt_id,
        variables={"leadName": "Grace", CALENDAR_BACKEND_VARIABLE: "new-server-value"},
    )

    assert _request_hash(old_backend) == _request_hash(new_backend)
    assert _request_hash(old_backend) != _request_hash(changed_client_data)


@pytest.mark.asyncio
async def test_signed_callback_finder_promotes_rejected_key_and_refuses_blank_sid() -> None:
    request = _request(uuid.uuid4())
    record = _record(
        request,
        state="rejected",
        result={"code": "dial_attempt_rejected"},
    )
    record.status = CallStatus.FAILED.value
    record.ended_at = telephony.datetime.now(telephony.UTC)
    db = MagicMock(execute=AsyncMock(return_value=_scalar_list_result(record)))

    found, count = await telephony._find_twilio_lifecycle_record(
        call_record_id=str(record.id),
        call_sid="CA-callback-wins",
        from_number=_FROM,
        to_number=_TO,
        db=db,
    )

    assert found is record
    assert count == 1
    assert record.provider_call_id == "CA-callback-wins"
    assert record.dial_attempt_state == "accepted"
    assert record.dial_attempt_result is None
    assert record.status == CallStatus.INITIATED.value
    assert record.ended_at is None

    blank_record = _record(request, state="rejected", result={"code": "dial_attempt_rejected"})
    blank_db = MagicMock(execute=AsyncMock())
    missing, blank_count = await telephony._find_twilio_lifecycle_record(
        call_record_id=str(blank_record.id),
        call_sid="   ",
        from_number=_FROM,
        to_number=_TO,
        db=blank_db,
    )
    assert missing is None
    assert blank_count == 0
    assert blank_record.dial_attempt_state == "rejected"
    blank_db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_key_replays_before_new_attempt_rate_limiter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hasattr(telephony._initiate_new_keyed_call, "__wrapped__")
    request = _request(uuid.uuid4())
    accepted = _record(request, provider_call_id="CA-rate-replay", state="accepted")
    replay_db = MagicMock(
        execute=AsyncMock(return_value=_scalar_list_result(accepted)),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    limited_new_attempt = AsyncMock(
        side_effect=HTTPException(status_code=429, detail="rate limit")
    )
    monkeypatch.setattr(telephony, "_initiate_new_keyed_call", limited_new_attempt)

    response = await _invoke(request, replay_db)

    assert isinstance(response, telephony.CallResponse)
    assert response.call_id == "CA-rate-replay"
    limited_new_attempt.assert_not_awaited()

    unseen_db = MagicMock(
        execute=AsyncMock(return_value=_scalar_list_result()),
        commit=AsyncMock(),
        rollback=AsyncMock(),
        add=MagicMock(),
    )
    with pytest.raises(HTTPException) as exc_info:
        await _invoke(request, unseen_db)
    assert exc_info.value.status_code == 429
    limited_new_attempt.assert_awaited_once()
    unseen_db.add.assert_not_called()


@pytest.mark.asyncio
async def test_telnyx_proven_pre_dispatch_failure_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(uuid.uuid4())
    db = MagicMock(add=MagicMock(), commit=AsyncMock(), rollback=AsyncMock())
    db.execute = AsyncMock(
        side_effect=_new_attempt_results(db)
    )
    service = MagicMock(
        initiate_call=AsyncMock(
            side_effect=telephony.TelnyxDialNotStartedError("no TeXML application")
        )
    )
    _configure_new_attempt(monkeypatch, service, provider="telnyx")

    with pytest.raises(HTTPException) as exc_info:
        await _invoke(request, db)

    record = db.add.call_args.args[0]
    assert exc_info.value.status_code == 409
    assert record.dial_attempt_state == "rejected"
    assert record.status == CallStatus.FAILED.value

def test_model_migration_bootstrap_and_event_payload_share_attempt_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_columns = {
        "dial_attempt_id",
        "dial_request_sha256",
        "dial_attempt_state",
        "dial_attempt_result",
    }
    columns = CallRecord.__table__.columns
    assert expected_columns <= set(columns.keys())
    assert all(columns[name].nullable for name in expected_columns)
    matching_indexes = [
        index
        for index in CallRecord.__table__.indexes
        if index.name == "uq_call_records_dial_attempt_id"
    ]
    assert len(matching_indexes) == 1
    assert matching_indexes[0].unique is True
    assert telephony.CallResponse.model_fields["call_id"].is_required()

    bootstrap = importlib.import_module("railway_bootstrap")
    reconciled = {
        column
        for table, column, _coltype in bootstrap.COLUMN_RECONCILE
        if table == "call_records"
    }
    assert expected_columns <= reconciled
    assert any(
        "uq_call_records_dial_attempt_id" in statement
        and "WHERE dial_attempt_id IS NOT NULL" in statement
        for statement in bootstrap.INDEX_RECONCILE
    )

    migration = importlib.import_module(
        "migrations.versions.b7d9c2e4f601_add_dial_attempt_idempotency"
    )
    assert migration.down_revision == "f3e8a1c2d4b5"
    add_column = MagicMock()
    create_index = MagicMock()
    monkeypatch.setattr(migration.op, "add_column", add_column)
    monkeypatch.setattr(migration.op, "create_index", create_index)
    migration.upgrade()
    assert {call.args[1].name for call in add_column.call_args_list} == expected_columns
    create_index.assert_called_once()
    assert create_index.call_args.args[:3] == (
        "uq_call_records_dial_attempt_id",
        "call_records",
        ["dial_attempt_id"],
    )
    assert create_index.call_args.kwargs["unique"] is True

    request = _request(uuid.uuid4())
    record = _record(request, provider_call_id="CA-event", state="accepted")
    payload = build_call_ended_payload(record)
    assert payload["dial_attempt_id"] == str(request.dial_attempt_id)
