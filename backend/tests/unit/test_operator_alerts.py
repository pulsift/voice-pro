"""Focused contracts for operator alert delivery.

Destination resolution here reimplements the house Slack contract from
pulsift-reply-router's reply_router/slack.py (bot token preferred, legacy
webhook a fallback, logs lane collapses into ops when unset) - these tests
pin that resolution order, plus the durability guarantee the whole mechanism
exists for: an alert that has nowhere configured to go stays pending and
retryable, it is never marked delivered.
"""

# ruff: noqa: SLF001 - these tests intentionally verify resolution/delivery internals.

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models.operator_alert import OperatorAlert
from app.services import operator_alerts

_SLACK_ENV_VARS = (
    "SLACK_BOT_TOKEN",
    "SLACK_CHANNEL",
    "SLACK_LOGS_CHANNEL",
    "SLACK_WEBHOOK_URL",
    "SLACK_LOGS_WEBHOOK_URL",
    "SLACK_[PULSIFT]_BOT_TOKEN",
    "SLACK_[PULSIFT]_CHANNEL",
    "SLACK_[PULSIFT]_LOGS_CHANNEL",
    "SLACK_[PULSIFT]_WEBHOOK_URL",
    "SLACK_[PULSIFT]_LOGS_WEBHOOK_URL",
)


@pytest_asyncio.fixture(autouse=True)
async def reset_worker(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[None, None]:
    await operator_alerts.stop_operator_alert_worker()
    for var in _SLACK_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(operator_alerts, "_warned_missing_destination", False)
    yield
    await operator_alerts.stop_operator_alert_worker()


@pytest_asyncio.fixture
async def outbox_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'operator-alerts.db').as_posix()}"
    )

    def create_table(connection: Connection) -> None:
        OperatorAlert.metadata.create_all(connection, tables=[OperatorAlert.__table__])

    async with engine.begin() as connection:
        await connection.run_sync(create_table)
    yield engine
    await engine.dispose()


@pytest.fixture
def outbox_session_factory(
    monkeypatch: pytest.MonkeyPatch,
    outbox_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    factory = async_sessionmaker(
        outbox_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    monkeypatch.setattr(operator_alerts, "AsyncSessionLocal", factory)
    return factory


async def stage(
    factory: async_sessionmaker[AsyncSession],
    *,
    dedup_key: str = "incident-1",
    message: str = "Ada needs a look.",
) -> None:
    async with factory() as db, db.begin():
        await operator_alerts.stage_operator_alert(db, dedup_key=dedup_key, message=message)


async def get_row(
    factory: async_sessionmaker[AsyncSession], dedup_key: str = "incident-1"
) -> OperatorAlert:
    async with factory() as db:
        result = await db.execute(
            select(OperatorAlert).where(OperatorAlert.dedup_key == dedup_key)
        )
        return result.scalar_one()


# --------------------------------------------------------------------------
# Destination resolution
# --------------------------------------------------------------------------


def test_bot_token_preferred_over_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "#pulsift-ops")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/legacy")

    destination = operator_alerts._resolve_destination()

    assert destination is not None
    assert destination.mode == "bot"
    assert destination.token == "xoxb-test"
    assert destination.channel == "#pulsift-ops"


def test_webhook_used_when_no_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/legacy")

    destination = operator_alerts._resolve_destination()

    assert destination is not None
    assert destination.mode == "webhook"
    assert destination.url == "https://hooks.slack.test/legacy"


def test_bot_token_without_a_channel_falls_back_to_webhook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A token alone can't address a channel - collapse to the webhook, don't
    # silently drop.
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/legacy")

    destination = operator_alerts._resolve_destination()

    assert destination is not None
    assert destination.mode == "webhook"


def test_logs_lane_falls_back_to_ops_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SLACK_CHANNEL", "#pulsift-ops")

    assert operator_alerts._channel_for(operator_alerts.LOGS) == "#pulsift-ops"

    monkeypatch.setenv("SLACK_LOGS_CHANNEL", "#pulsift-logs")

    assert operator_alerts._channel_for(operator_alerts.LOGS) == "#pulsift-logs"
    assert operator_alerts._channel_for(operator_alerts.OPS) == "#pulsift-ops"


def test_local_pulsift_env_aliases_are_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Sami's local names, per pulsift-reply-router/reply_router/slack.py.
    monkeypatch.setenv("SLACK_[PULSIFT]_BOT_TOKEN", "xoxb-local")
    monkeypatch.setenv("SLACK_[PULSIFT]_CHANNEL", "#local-ops")

    destination = operator_alerts._resolve_destination()

    assert destination is not None
    assert destination.mode == "bot"
    assert destination.token == "xoxb-local"
    assert destination.channel == "#local-ops"


# --------------------------------------------------------------------------
# Dispatch — durability first: nothing configured must never look delivered
# --------------------------------------------------------------------------


async def test_completely_unconfigured_destination_stays_pending_and_retryable(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await stage(outbox_session_factory)

    with patch.object(operator_alerts, "_post_once", AsyncMock()) as post:
        assert await operator_alerts.dispatch_due_operator_alert() is False
        post.assert_not_called()

    row = await get_row(outbox_session_factory)
    assert row.state == "pending"
    assert row.sent_at is None
    assert row.last_error == operator_alerts._MISSING_DESTINATION_ERROR


async def test_bot_delivery_is_preferred_and_acknowledged(
    monkeypatch: pytest.MonkeyPatch,
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "#pulsift-ops")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/legacy")
    await stage(outbox_session_factory, message="Ada's promised list needs a look.")

    with patch.object(operator_alerts, "_post_once", AsyncMock(return_value=200)) as post:
        assert await operator_alerts.dispatch_due_operator_alert() is True

    url, body, headers = post.call_args.args
    assert url == operator_alerts._SLACK_POST_MESSAGE_URL
    assert headers["Authorization"] == "Bearer xoxb-test"
    assert json.loads(body) == {
        "channel": "#pulsift-ops",
        "text": "Ada's promised list needs a look.",
    }

    row = await get_row(outbox_session_factory)
    assert row.state == "sent"
    assert row.sent_at is not None


async def test_webhook_fallback_delivery_when_no_bot_token(
    monkeypatch: pytest.MonkeyPatch,
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://hooks.slack.test/legacy")
    await stage(outbox_session_factory)

    with patch.object(operator_alerts, "_post_once", AsyncMock(return_value=200)) as post:
        assert await operator_alerts.dispatch_due_operator_alert() is True

    url, body, headers = post.call_args.args
    assert url == "https://hooks.slack.test/legacy"
    assert json.loads(body) == {"text": "Ada needs a look."}
    assert "Authorization" not in headers

    row = await get_row(outbox_session_factory)
    assert row.state == "sent"


async def test_missing_destination_never_reaches_post_once_for_a_pending_row(
    monkeypatch: pytest.MonkeyPatch,
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # Guards the exact failure this mechanism exists to prevent: dispatch
    # must not even attempt delivery (and therefore never risk a false ack)
    # when nothing is configured - it must not claim the row either.
    await stage(outbox_session_factory)

    for _ in range(3):
        with patch.object(operator_alerts, "_post_once", AsyncMock()) as post:
            assert await operator_alerts.dispatch_due_operator_alert() is False
            post.assert_not_called()

    row = await get_row(outbox_session_factory)
    assert row.state == "pending"
    assert row.attempts == 0
    assert row.sent_at is None


# --------------------------------------------------------------------------
# `_post_once` — a 200 with a silent Slack decline must not read as an ack
# --------------------------------------------------------------------------


async def test_slack_ok_false_is_a_retryable_failure_not_a_silent_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slack answers chat.postMessage with HTTP 200 even when it silently
    declined (bad channel, revoked token, ...) - `_post_once` must fold that
    into a non-2xx so the caller retries instead of falsely acking."""

    class _FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {"ok": False, "error": "channel_not_found"}

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def post(self, url: str, content: bytes, headers: dict) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(operator_alerts.httpx, "AsyncClient", lambda timeout: _FakeClient())

    status = await operator_alerts._post_once(
        operator_alerts._SLACK_POST_MESSAGE_URL, b"{}", {"Authorization": "Bearer x"}
    )

    assert status != 200


async def test_slack_ok_true_acks(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeResponse:
        status_code = 200

        def json(self) -> dict:
            return {"ok": True}

    class _FakeClient:
        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def post(self, url: str, content: bytes, headers: dict) -> _FakeResponse:
            return _FakeResponse()

    monkeypatch.setattr(operator_alerts.httpx, "AsyncClient", lambda timeout: _FakeClient())

    status = await operator_alerts._post_once(
        operator_alerts._SLACK_POST_MESSAGE_URL, b"{}", {"Authorization": "Bearer x"}
    )

    assert status == 200
