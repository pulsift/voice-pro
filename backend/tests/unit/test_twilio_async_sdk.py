"""Adversarial contracts for non-blocking, bounded Twilio SDK access."""

import asyncio
import inspect
import threading
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from requests.exceptions import Timeout as RequestsTimeout
from twilio.base.exceptions import TwilioRestException

from app.api.telephony import InitiateCallRequest, initiate_call
from app.core.config import settings
from app.models.call_record import CallStatus
from app.services.telephony import twilio_service as twilio_module
from app.services.telephony.twilio_service import (
    TwilioDialOutcomeUnknownError,
    TwilioSdkTimeoutError,
    TwilioService,
    _is_ambiguous_twilio_dial_error,
    _run_twilio_sdk,
)


def _scalar_result(value: object | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _mock_service() -> tuple[TwilioService, MagicMock]:
    service = TwilioService("AC-test", "token-test")
    client = MagicMock()
    number = SimpleNamespace(
        sid="PN-test",
        phone_number="+14155550100",
        friendly_name="Test",
        capabilities={"voice": True, "sms": True, "mms": False},
    )
    call = SimpleNamespace(
        sid="CA-test",
        from_formatted="+14155550100",
        from_="+14155550100",
        to_formatted="+14155550101",
        to="+14155550101",
        direction="outbound-api",
        status="queued",
        duration=None,
    )
    client.calls.create.return_value = call
    client.calls.return_value.update.return_value = call
    client.calls.return_value.fetch.return_value = call
    client.incoming_phone_numbers.list.return_value = [number]
    client.incoming_phone_numbers.create.return_value = number
    client.incoming_phone_numbers.return_value.delete.return_value = True
    client.incoming_phone_numbers.return_value.update.return_value = number
    client.available_phone_numbers.return_value.local.list.return_value = [number]
    service.client = client
    return service, client


def test_client_has_explicit_timeout_and_no_sdk_retries() -> None:
    with (
        patch.object(twilio_module, "TwilioHttpClient") as http_client,
        patch.object(twilio_module, "Client") as client,
    ):
        TwilioService("AC-test", "token-test")

    http_client.assert_called_once_with(
        timeout=10.0,
        max_retries=0,
    )
    client.assert_called_once_with(
        "AC-test",
        "token-test",
        http_client=http_client.return_value,
    )


@pytest.mark.asyncio
async def test_all_eight_sdk_network_operations_use_to_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, _client = _mock_service()

    async def immediate(operation, *args, **kwargs):
        return operation(*args, **kwargs)

    to_thread = AsyncMock(side_effect=immediate)
    monkeypatch.setattr(twilio_module.asyncio, "to_thread", to_thread)

    await service.initiate_call(
        to_number="+14155550101",
        from_number="+14155550100",
        webhook_url="https://voice.example/webhooks/twilio/answer",
    )
    assert await service.hangup_call("CA-test")
    await service.list_phone_numbers()
    await service.search_phone_numbers()
    await service.purchase_phone_number("+14155550100")
    assert await service.release_phone_number("PN-test")
    assert await service.configure_phone_number_webhook(
        "PN-test",
        "https://voice.example/webhooks/twilio/voice",
    )
    assert await service.get_call_info("CA-test") is not None

    assert to_thread.await_count == 8


def test_local_twiml_generation_is_not_offloaded() -> None:
    service, _client = _mock_service()
    with patch.object(twilio_module, "_run_twilio_sdk", new=AsyncMock()) as runner:
        answer = service.generate_answer_response(
            "wss://voice.example/ws/telephony/twilio/agent-1",
            "agent-1",
        )
        gather = service.generate_gather_response(
            "Press one",
            "https://voice.example/gather",
        )

    assert "<Stream" in answer
    assert "<Gather" in gather
    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_timeout_holds_slot_until_worker_exits_and_retrieves_late_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    semaphore = asyncio.BoundedSemaphore(1)
    monkeypatch.setattr(twilio_module, "_TWILIO_SDK_SEMAPHORE", semaphore)
    monkeypatch.setattr(twilio_module, "_TWILIO_SDK_TIMEOUT_SECONDS", 1.0)
    started = threading.Event()
    release = threading.Event()
    second_started = threading.Event()
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

    def late_failure() -> None:
        started.set()
        release.wait(timeout=1)
        raise RuntimeError("late worker failure")

    def second_operation() -> str:
        second_started.set()
        return "second"

    second: asyncio.Task[str] | None = None
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(_run_twilio_sdk(late_failure), timeout=0.02)
        assert started.is_set()

        second = asyncio.create_task(_run_twilio_sdk(second_operation))
        await asyncio.sleep(0.03)
        assert not second_started.is_set()
        assert not second.done()

        release.set()
        assert await asyncio.wait_for(second, timeout=0.5) == "second"
        await asyncio.sleep(0)
        assert loop_errors == []

        await asyncio.wait_for(semaphore.acquire(), timeout=0.1)
        semaphore.release()
    finally:
        release.set()
        if second is not None and not second.done():
            await asyncio.gather(second, return_exceptions=True)
        loop.set_exception_handler(previous_handler)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cause",
    [
        RequestsTimeout("Twilio request timed out"),
        TwilioRestException(503, "/Calls", "server error", method="POST"),
        TwilioSdkTimeoutError("local deadline"),
    ],
)
async def test_timeout_and_5xx_become_typed_unknown_dial_outcome(cause: Exception) -> None:
    service, _client = _mock_service()
    with (
        patch.object(twilio_module, "_run_twilio_sdk", new=AsyncMock(side_effect=cause)),
        pytest.raises(TwilioDialOutcomeUnknownError) as exc,
    ):
        await service.initiate_call(
            to_number="+14155550101",
            from_number="+14155550100",
            webhook_url="https://voice.example/webhooks/twilio/answer",
        )

    assert exc.value.__cause__ is cause


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, None, "503"])
async def test_non_5xx_or_non_integer_status_re_raises_original_error(status: object) -> None:
    service, _client = _mock_service()
    error = TwilioRestException(400, "/Calls", "definitive rejection", method="POST")
    error.status = status
    assert not _is_ambiguous_twilio_dial_error(error)

    with (
        patch.object(twilio_module, "_run_twilio_sdk", new=AsyncMock(side_effect=error)),
        pytest.raises(TwilioRestException) as exc,
    ):
        await service.initiate_call(
            to_number="+14155550101",
            from_number="+14155550100",
            webhook_url="https://voice.example/webhooks/twilio/answer",
        )

    assert exc.value is error


@pytest.mark.asyncio
async def test_direct_twilio_unknown_outcome_leaves_precommitted_record_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 42)
    agent_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent_result = _scalar_result(SimpleNamespace(id=agent_id, user_id=1, enable_recording=False))
    memberships = MagicMock()
    memberships.scalars.return_value.all.return_value = [
        SimpleNamespace(workspace_id=workspace_id, is_default=True)
    ]
    db = MagicMock(add=MagicMock(), commit=AsyncMock())
    db.execute = AsyncMock(side_effect=[agent_result, memberships, _scalar_result(uuid.uuid4())])
    service = MagicMock()
    cause = RequestsTimeout("response lost after Twilio may have accepted")
    unknown = TwilioDialOutcomeUnknownError("unknown")
    unknown.__cause__ = cause
    service.initiate_call = AsyncMock(side_effect=unknown)
    monkeypatch.setattr(settings, "TELEPHONY_OUTBOUND_PROVIDER", "twilio")

    with (
        patch("app.api.telephony.get_telnyx_service", new=AsyncMock(return_value=None)),
        patch("app.api.telephony.get_twilio_service", new=AsyncMock(return_value=service)),
        pytest.raises(TwilioDialOutcomeUnknownError),
    ):
        await inspect.unwrap(initiate_call)(
            InitiateCallRequest(
                to_number="+14155550101",
                from_number="+14155550100",
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
    assert db.execute.await_count == 3
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_direct_twilio_4xx_marks_precommitted_record_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 42)
    agent_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent_result = _scalar_result(SimpleNamespace(id=agent_id, user_id=1, enable_recording=False))
    memberships = MagicMock()
    memberships.scalars.return_value.all.return_value = [
        SimpleNamespace(workspace_id=workspace_id, is_default=True)
    ]
    db = MagicMock(add=MagicMock(), commit=AsyncMock())
    locked = MagicMock()
    locked.scalar_one.side_effect = lambda: db.add.call_args.args[0]
    db.execute = AsyncMock(
        side_effect=[
            agent_result,
            memberships,
            _scalar_result(uuid.uuid4()),
            locked,
        ]
    )
    service = MagicMock()
    rejection = TwilioRestException(
        400,
        "/Calls",
        "definitive rejection",
        method="POST",
    )
    service.initiate_call = AsyncMock(side_effect=rejection)
    monkeypatch.setattr(settings, "TELEPHONY_OUTBOUND_PROVIDER", "twilio")

    with (
        patch("app.api.telephony.get_telnyx_service", new=AsyncMock(return_value=None)),
        patch("app.api.telephony.get_twilio_service", new=AsyncMock(return_value=service)),
        pytest.raises(TwilioRestException) as exc,
    ):
        await inspect.unwrap(initiate_call)(
            InitiateCallRequest(
                to_number="+14155550101",
                from_number="+14155550100",
                agent_id=str(agent_id),
            ),
            MagicMock(base_url="https://voice.example/"),
            MagicMock(id=1),
            db,
            workspace_id=None,
        )

    assert exc.value is rejection
    record = db.add.call_args.args[0]
    assert record.status == CallStatus.FAILED.value
    assert record.ended_at is not None
    assert db.execute.await_count == 4
    assert db.commit.await_count == 2

