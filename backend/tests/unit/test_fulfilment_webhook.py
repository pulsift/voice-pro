"""Focused contracts for fulfilment webhook dispatch idempotency and signing."""

# ruff: noqa: SLF001 - these tests intentionally verify module-private dispatch state.

import asyncio
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import fulfilment_webhook


def make_http_context(*statuses: int) -> tuple[MagicMock, MagicMock]:
    responses = [MagicMock(status_code=status, text="provider response") for status in statuses]
    client = MagicMock(post=AsyncMock(side_effect=responses))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    return context, client


@pytest.fixture(autouse=True)
def reset_dispatch_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FULFIL_WEBHOOK_URL", "https://fulfilment.test")
    monkeypatch.setattr(settings, "FULFIL_WEBHOOK_SECRET", None)
    monkeypatch.setattr(fulfilment_webhook, "_warned_unsigned", False)
    fulfilment_webhook._scheduled_booking_ids.clear()
    fulfilment_webhook._background_tasks.clear()


@pytest.mark.asyncio
async def test_booking_uid_is_dispatched_once_across_fresh_call_state() -> None:
    payload = {"booking_id": "booking-uid-1", "email": "lead@example.com"}
    post = AsyncMock(return_value=True)

    with patch.object(fulfilment_webhook, "_post_with_retries", post):
        fulfilment_webhook.schedule_fulfilment_webhook(payload)
        fulfilment_webhook.schedule_fulfilment_webhook(payload.copy())
        await asyncio.gather(*tuple(fulfilment_webhook._background_tasks))

    post.assert_awaited_once_with("https://fulfilment.test/fulfil", payload)


@pytest.mark.asyncio
async def test_failed_delivery_releases_uid_for_later_retry() -> None:
    payload = {"booking_id": "booking-uid-2"}
    post = AsyncMock(side_effect=[False, True])

    with patch.object(fulfilment_webhook, "_post_with_retries", post):
        fulfilment_webhook.schedule_fulfilment_webhook(payload)
        await asyncio.gather(*tuple(fulfilment_webhook._background_tasks))
        await asyncio.sleep(0)
        fulfilment_webhook.schedule_fulfilment_webhook(payload)
        await asyncio.gather(*tuple(fulfilment_webhook._background_tasks))

    assert post.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "terminal", "expected_attempts"),
    [
        (201, True, 1),
        (400, True, 1),
        (408, False, 3),
        (429, False, 3),
        (500, False, 3),
    ],
)
async def test_http_status_classes(status: int, terminal: bool, expected_attempts: int) -> None:
    context, client = make_http_context(*([status] * expected_attempts))

    with (
        patch("app.services.fulfilment_webhook.httpx.AsyncClient", return_value=context),
        patch("app.services.fulfilment_webhook.asyncio.sleep", AsyncMock()),
    ):
        result = await fulfilment_webhook._post_with_retries(
            "https://fulfilment.test/fulfil", {"booking_id": "status-test"}
        )

    assert result is terminal
    assert client.post.await_count == expected_attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("retryable_status", [408, 429, 500])
async def test_retryable_status_can_recover(retryable_status: int) -> None:
    context, client = make_http_context(retryable_status, 204)

    with (
        patch("app.services.fulfilment_webhook.httpx.AsyncClient", return_value=context),
        patch("app.services.fulfilment_webhook.asyncio.sleep", AsyncMock()),
    ):
        result = await fulfilment_webhook._post_with_retries(
            "https://fulfilment.test/fulfil", {"booking_id": "recovery-test"}
        )

    assert result is True
    assert client.post.await_count == 2


@pytest.mark.asyncio
async def test_signature_header_covers_the_exact_bytes_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "FULFIL_WEBHOOK_SECRET", "webhook-secret")
    context, client = make_http_context(200)
    payload = {"booking_id": "signed-1", "email": "lead@example.com"}

    with patch("app.services.fulfilment_webhook.httpx.AsyncClient", return_value=context):
        result = await fulfilment_webhook._post_with_retries(
            "https://fulfilment.test/fulfil", payload
        )

    assert result is True
    _, kwargs = client.post.await_args
    body = kwargs["content"]
    expected = hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    assert kwargs["headers"]["X-Fulfil-Signature"] == f"sha256={expected}"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert json.loads(body) == payload


@pytest.mark.asyncio
async def test_unset_secret_sends_unsigned_and_warns_once() -> None:
    context, client = make_http_context(200, 200)

    with (
        patch("app.services.fulfilment_webhook.httpx.AsyncClient", return_value=context),
        patch.object(fulfilment_webhook.logger, "warning") as warning,
    ):
        await fulfilment_webhook._post_with_retries(
            "https://fulfilment.test/fulfil", {"booking_id": "unsigned-1"}
        )
        await fulfilment_webhook._post_with_retries(
            "https://fulfilment.test/fulfil", {"booking_id": "unsigned-2"}
        )

    for call in client.post.await_args_list:
        assert "X-Fulfil-Signature" not in call.kwargs["headers"]
    unsigned_warnings = [
        call for call in warning.call_args_list if call.args[0] == "fulfil_webhook_unsigned"
    ]
    assert len(unsigned_warnings) == 1


@pytest.mark.asyncio
async def test_exhausted_retryable_response_releases_scheduled_uid() -> None:
    context, _client = make_http_context(429, 429, 429)
    payload = {"booking_id": "retryable-booking-uid"}

    with (
        patch("app.services.fulfilment_webhook.httpx.AsyncClient", return_value=context),
        patch("app.services.fulfilment_webhook.asyncio.sleep", AsyncMock()),
    ):
        fulfilment_webhook.schedule_fulfilment_webhook(payload)
        await asyncio.gather(*tuple(fulfilment_webhook._background_tasks))
        await asyncio.sleep(0)

    assert payload["booking_id"] not in fulfilment_webhook._scheduled_booking_ids
