"""Sensitive Twilio call context must reach providers without entering logs."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

from app.core import webhook_security
from app.middleware.request_tracing import RequestTracingMiddleware
from app.services.telephony.telnyx_service import TelnyxService
from app.services.telephony.twilio_service import TwilioService

CV_SENTINEL = "cv-value-must-never-be-logged"
GRANT_SENTINEL = "grant-value-must-never-be-logged"
WEBHOOK_URL = f"https://voice.example/webhooks/twilio/answer?agent_id=agent-1&cv={CV_SENTINEL}"


def _request(
    *,
    path: str = "/webhooks/twilio/answer",
    query: str = "",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [(b"host", b"voice.example"), *(headers or [])],
            "client": ("127.0.0.1", 12345),
            "server": ("voice.example", 443),
        }
    )


@pytest.mark.asyncio
async def test_request_tracing_redacts_cv_and_capability_query_values() -> None:
    middleware = RequestTracingMiddleware(MagicMock())
    request = _request(
        query=(f"cv={CV_SENTINEL}&media_grant={GRANT_SENTINEL}&call_record_id=safe-record-id")
    )
    call_next = AsyncMock(return_value=Response("ok"))

    with patch("app.middleware.request_tracing.logger") as trace_logger:
        response = await middleware.dispatch(request, call_next)

    rendered = repr(trace_logger.info.call_args_list)
    assert response.status_code == 200
    assert CV_SENTINEL not in rendered
    assert GRANT_SENTINEL not in rendered
    assert "[REDACTED]" in rendered
    assert "safe-record-id" in rendered


@pytest.mark.asyncio
async def test_invalid_twilio_signature_logs_path_not_full_cv_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request(
        query=f"agent_id=agent-1&cv={CV_SENTINEL}",
        headers=[(b"x-twilio-signature", b"invalid")],
    )
    validator = MagicMock(return_value=False)
    monkeypatch.setattr(webhook_security.settings, "TWILIO_AUTH_TOKEN", "configured")
    monkeypatch.setattr(webhook_security.settings, "DEBUG", False)
    monkeypatch.setattr(webhook_security, "validate_twilio_signature", validator)
    monkeypatch.setattr(
        webhook_security,
        "get_twilio_webhook_params",
        AsyncMock(return_value={}),
    )

    security_logger = MagicMock()
    monkeypatch.setattr(webhook_security, "logger", security_logger)
    with pytest.raises(HTTPException) as exc:
        await webhook_security.verify_twilio_webhook(request)

    validated_url = validator.call_args.args[1]
    assert CV_SENTINEL in validated_url
    assert exc.value.status_code == 403
    rendered = repr(security_logger.warning.call_args_list)
    assert CV_SENTINEL not in rendered
    assert "/webhooks/twilio/answer" in rendered


@pytest.mark.asyncio
async def test_twilio_provider_receives_exact_cv_url_but_does_not_log_it() -> None:
    call = MagicMock(sid="CA-test")
    client = MagicMock()
    client.calls.create.return_value = call
    with patch("app.services.telephony.twilio_service.Client", return_value=client):
        service = TwilioService("AC-test", "auth-test")

    service.logger = MagicMock()
    await service.initiate_call(
        to_number="+14155550101",
        from_number="+14155550100",
        webhook_url=WEBHOOK_URL,
        agent_id="agent-1",
    )

    create_kwargs = client.calls.create.call_args.kwargs
    assert create_kwargs["url"] == WEBHOOK_URL
    assert CV_SENTINEL in create_kwargs["url"]
    rendered = repr(service.logger.info.call_args_list)
    assert CV_SENTINEL not in rendered
    assert "has_webhook" in rendered


@pytest.mark.asyncio
async def test_telnyx_provider_receives_exact_cv_url_but_does_not_log_it() -> None:
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "data": {"call_sid": "call-sid-1", "call_control_id": "control-1"}
    }
    client = MagicMock(post=AsyncMock(return_value=response))
    service = TelnyxService("test-key")
    service.logger = MagicMock()
    with (
        patch.object(service, "_get_http_client", new=AsyncMock(return_value=client)),
        patch.object(service, "_get_connection_id", new=AsyncMock(return_value="app-1")),
    ):
        await service.initiate_call(
            to_number="+14155550101",
            from_number="+14155550100",
            webhook_url=WEBHOOK_URL.replace("/twilio/", "/telnyx/"),
            agent_id="agent-1",
        )

    form = client.post.await_args.kwargs["data"]
    assert CV_SENTINEL in form["Url"]
    assert form["Url"] == WEBHOOK_URL.replace("/twilio/", "/telnyx/")
    rendered = repr(service.logger.info.call_args_list)
    assert CV_SENTINEL not in rendered
    assert "has_webhook" in rendered
