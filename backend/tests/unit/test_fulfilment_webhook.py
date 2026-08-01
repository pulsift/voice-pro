"""Focused contracts for durable promised-list webhook delivery."""

# ruff: noqa: SLF001 - these tests intentionally verify exact-token worker internals.

import asyncio
import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.models.fulfilment_outbox import FulfilmentOutbox
from app.services import fulfilment_webhook
from app.services.tools.crm_tools import CRMTools

START = "2026-08-03T14:00:00Z"
PAYLOAD = {
    "name": "Ada",
    "company": "Analytical Engines",
    "email": "ada@example.com",
    "phone": "+15550000001",
    "icp": {"states": ["Texas"], "min_kw": 50},
    "campaign_id": "campaign-1",
    "conversation_id": "conversation-1",
}


@pytest_asyncio.fixture(autouse=True)
async def reset_worker(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[None, None]:
    await fulfilment_webhook.stop_fulfilment_worker()
    monkeypatch.setattr(settings, "FULFIL_WEBHOOK_URL", "https://fulfilment.test")
    monkeypatch.setattr(settings, "FULFIL_WEBHOOK_SECRET", "webhook-secret")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 123)
    monkeypatch.setattr(fulfilment_webhook, "_warned_missing_url", False)
    monkeypatch.setattr(fulfilment_webhook, "_warned_missing_secret", False)
    yield
    await fulfilment_webhook.stop_fulfilment_worker()


@pytest_asyncio.fixture
async def outbox_engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'fulfilment-outbox.db').as_posix()}"
    )

    def create_table(connection: Connection) -> None:
        FulfilmentOutbox.metadata.create_all(
            connection,
            tables=[FulfilmentOutbox.__table__],
        )

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
    monkeypatch.setattr(fulfilment_webhook, "AsyncSessionLocal", factory)
    return factory


async def stage(
    *,
    payload: dict[str, object] | None = None,
    start: str = START,
    email: str = "ada@example.com",
) -> str:
    key = await fulfilment_webhook.stage_fulfilment_intent(
        start_iso=start,
        email=email,
        payload=dict(payload or PAYLOAD),
        workspace_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        user_id=7,
    )
    assert key is not None
    return key


async def load_row(
    factory: async_sessionmaker[AsyncSession],
    key: str,
) -> FulfilmentOutbox:
    async with factory() as db:
        row = await db.get(FulfilmentOutbox, key)
        assert row is not None
        return row


async def make_due(factory: async_sessionmaker[AsyncSession], key: str) -> None:
    async with factory() as db:
        row = await db.get(FulfilmentOutbox, key)
        assert row is not None
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        await db.commit()


@pytest.mark.asyncio
async def test_missing_url_still_persists_intent_and_worker_does_not_post(
    monkeypatch: pytest.MonkeyPatch,
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(settings, "FULFIL_WEBHOOK_URL", None)
    key = await stage()
    post = AsyncMock()

    with patch.object(fulfilment_webhook, "_post_once", post):
        assert await fulfilment_webhook.dispatch_due_fulfilment() is False

    row = await load_row(outbox_session_factory, key)
    assert row.state == "awaiting_booking"
    assert row.last_error == "FULFIL_WEBHOOK_URL not configured"
    post.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_secret_retains_finalized_work_without_unsigned_post(
    monkeypatch: pytest.MonkeyPatch,
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await stage()
    assert await fulfilment_webhook.finalize_fulfilment_intent(key, "booking-secret")
    monkeypatch.setattr(settings, "FULFIL_WEBHOOK_SECRET", None)
    post = AsyncMock()

    with patch.object(fulfilment_webhook, "_post_once", post):
        assert await fulfilment_webhook.dispatch_due_fulfilment() is False

    row = await load_row(outbox_session_factory, key)
    assert row.state == "pending"
    assert row.last_error == "FULFIL_WEBHOOK_SECRET not configured"
    post.assert_not_awaited()
    with pytest.raises(RuntimeError, match="FULFIL_WEBHOOK_SECRET"):
        fulfilment_webhook._headers_for_body(b"{}")


@pytest.mark.asyncio
async def test_conflicting_restage_does_not_poison_authoritative_pending_row(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await stage()
    assert await fulfilment_webhook.finalize_fulfilment_intent(key, "booking-original")
    drifted = {**PAYLOAD, "icp": {"states": ["California"]}}

    with pytest.raises(fulfilment_webhook.FulfilmentIntentConflictError):
        await stage(payload=drifted)

    row = await load_row(outbox_session_factory, key)
    assert row.state == "pending"
    assert row.booking_id == "booking-original"
    assert json.loads(row.intent_body) == PAYLOAD


@pytest.mark.asyncio
async def test_finalize_freezes_one_body_and_idempotent_replay_preserves_active_lease(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await stage()
    assert await fulfilment_webhook.finalize_fulfilment_intent(key, "booking-frozen")
    token = uuid.uuid4()
    async with outbox_session_factory() as db:
        row = await db.get(FulfilmentOutbox, key)
        assert row is not None
        row.state = "sending"
        row.claim_token = token
        row.claimed_at = datetime.now(UTC)
        await db.commit()

    assert await fulfilment_webhook.finalize_fulfilment_intent(key, "booking-frozen")

    row = await load_row(outbox_session_factory, key)
    assert row.state == "sending"
    assert row.claim_token == token
    assert row.payload_body is not None
    body = row.payload_body.encode()
    assert hashlib.sha256(body).hexdigest() == row.payload_sha256
    assert json.loads(body) == {**PAYLOAD, "booking_id": "booking-frozen"}


@pytest.mark.asyncio
async def test_booking_claim_expiry_fences_stale_owner_before_dispatch(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await stage()
    now = datetime.now(UTC)
    stale_token = await fulfilment_webhook.claim_fulfilment_booking(
        key,
        now=now - timedelta(seconds=fulfilment_webhook._LEASE_SECONDS + 1),
    )
    assert stale_token is not None
    assert not await fulfilment_webhook.authorize_fulfilment_booking(key, stale_token)

    live_token = await fulfilment_webhook.claim_fulfilment_booking(key, now=now)
    assert live_token is not None
    assert live_token != stale_token
    assert not await fulfilment_webhook.authorize_fulfilment_booking(key, stale_token)
    assert await fulfilment_webhook.authorize_fulfilment_booking(key, live_token)
    assert await fulfilment_webhook.claim_fulfilment_booking(key, now=now) is None

    row = await load_row(outbox_session_factory, key)
    assert row.state == "booking_dispatched"
    assert row.booking_dispatched_at is not None
    assert row.claim_token == live_token


@pytest.mark.asyncio
async def test_extra_uid_preserves_first_frozen_booking_and_remains_deliverable(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await stage()
    assert await fulfilment_webhook.finalize_fulfilment_intent(key, "booking-first")
    original = await load_row(outbox_session_factory, key)
    original_body = original.payload_body

    with pytest.raises(fulfilment_webhook.ExtraBookingConflictError):
        await fulfilment_webhook.finalize_fulfilment_intent(key, "booking-extra")

    preserved = await load_row(outbox_session_factory, key)
    assert preserved.state == "pending"
    assert preserved.booking_id == "booking-first"
    assert preserved.payload_body == original_body

    with patch.object(fulfilment_webhook, "_post_once", AsyncMock(return_value=204)):
        assert await fulfilment_webhook.dispatch_due_fulfilment() is True
    assert (await load_row(outbox_session_factory, key)).state == "sent"


@pytest.mark.asyncio
async def test_worker_repairs_crash_after_cal_success_with_stored_event_type(
    monkeypatch: pytest.MonkeyPatch,
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await stage()
    async with outbox_session_factory() as db:
        row = await db.get(FulfilmentOutbox, key)
        assert row is not None
        row.attempts = 99
        await db.commit()
    await make_due(outbox_session_factory, key)
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 999)
    reconcile = AsyncMock(
        return_value={
            "success": True,
            "category": "reconciled_success",
            "uid": "booking-repaired",
        }
    )

    with patch("app.services.calcom_client.find_existing_booking", reconcile):
        assert await fulfilment_webhook.dispatch_due_fulfilment() is True

    reconcile.assert_awaited_once_with(
        start_iso=START,
        email="ada@example.com",
        event_type_id=123,
    )
    row = await load_row(outbox_session_factory, key)
    assert row.state == "pending"
    assert row.booking_id == "booking-repaired"
    assert row.attempts == 0

    with patch.object(fulfilment_webhook, "_post_once", AsyncMock(return_value=204)):
        assert await fulfilment_webhook.dispatch_due_fulfilment() is True
    assert (await load_row(outbox_session_factory, key)).state == "sent"


@pytest.mark.asyncio
async def test_expired_intent_cancels_only_after_cal_proves_not_found(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await stage()
    async with outbox_session_factory() as db:
        row = await db.get(FulfilmentOutbox, key)
        assert row is not None
        row.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
        row.reconcile_until = datetime.now(UTC) - timedelta(seconds=1)
        row.attempts = 99
        await db.commit()

    reconcile = AsyncMock(return_value={"success": False, "category": "not_found"})
    with patch("app.services.calcom_client.find_existing_booking", reconcile):
        assert await fulfilment_webhook.dispatch_due_fulfilment() is True

    row = await load_row(outbox_session_factory, key)
    assert row.state == "cancelled"
    assert row.booking_id is None

    assert await stage() == key
    restarted = await load_row(outbox_session_factory, key)
    assert restarted.state == "awaiting_booking"
    assert restarted.attempts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "expected_state"),
    [
        (201, "sent"),
        (400, "blocked"),
        (401, "pending"),
        (403, "pending"),
        (404, "pending"),
        (405, "pending"),
        (409, "blocked"),
        (422, "blocked"),
        (408, "pending"),
        (425, "pending"),
        (429, "pending"),
        (500, "pending"),
    ],
)
async def test_http_outcomes_are_classified_durably(
    status_code: int,
    expected_state: str,
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await stage()
    assert await fulfilment_webhook.finalize_fulfilment_intent(key, f"booking-{status_code}")

    with patch.object(
        fulfilment_webhook,
        "_post_once",
        AsyncMock(return_value=status_code),
    ):
        assert await fulfilment_webhook.dispatch_due_fulfilment() is True

    row = await load_row(outbox_session_factory, key)
    assert row.state == expected_state
    assert row.attempts == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 404, 405])
async def test_auth_failure_delivers_after_receiver_configuration_is_repaired(
    status_code: int,
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await stage()
    assert await fulfilment_webhook.finalize_fulfilment_intent(
        key, f"booking-auth-{status_code}"
    )
    post = AsyncMock(side_effect=[status_code, 204])

    with patch.object(fulfilment_webhook, "_post_once", post):
        assert await fulfilment_webhook.dispatch_due_fulfilment() is True
        first = await load_row(outbox_session_factory, key)
        assert first.state == "pending"
        assert first.last_error == f"HTTP {status_code}"

        await make_due(outbox_session_factory, key)
        assert await fulfilment_webhook.dispatch_due_fulfilment() is True

    repaired = await load_row(outbox_session_factory, key)
    assert repaired.state == "sent"
    assert repaired.attempts == 2
    assert post.await_count == 2


@pytest.mark.asyncio
async def test_booking_uid_collision_blocks_only_the_later_intent(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await stage()
    assert await fulfilment_webhook.finalize_fulfilment_intent(first, "shared-booking")
    second_payload = {**PAYLOAD, "email": "grace@example.com"}
    second = await stage(
        payload=second_payload,
        start="2026-08-03T15:00:00Z",
        email="grace@example.com",
    )

    with pytest.raises(fulfilment_webhook.FulfilmentIntentConflictError):
        await fulfilment_webhook.finalize_fulfilment_intent(second, "shared-booking")

    assert (await load_row(outbox_session_factory, first)).state == "pending"
    assert (await load_row(outbox_session_factory, second)).state == "blocked"


@pytest.mark.asyncio
async def test_concurrent_same_intent_issues_exactly_one_cal_create(
    monkeypatch: pytest.MonkeyPatch,
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "cal-key")
    intent_key = await stage()
    workspace_id = uuid.UUID("11111111-1111-1111-1111-111111111111")

    def make_tools() -> CRMTools:
        tools = CRMTools(
            db=MagicMock(),
            user_id=7,
            workspace_id=workspace_id,
            variables={
                "leadName": PAYLOAD["name"],
                "leadEmail": PAYLOAD["email"],
                "leadPhone": PAYLOAD["phone"],
                "company": PAYLOAD["company"],
                "campaign_id": PAYLOAD["campaign_id"],
                "conversation_id": PAYLOAD["conversation_id"],
            },
        )
        tools._offered_slots = [
            {"slot_id": "slot_1", "start": START, "label": "Monday 2 PM"}
        ]
        tools._selected_slot_id = "slot_1"
        tools._selected_start = START
        tools._normalized_timezone = "UTC"
        return tools

    arrivals = 0
    both_preflighted = asyncio.Event()

    async def reconcile(**_kwargs: object) -> dict[str, object]:
        nonlocal arrivals
        arrivals += 1
        if arrivals == 2:
            both_preflighted.set()
        await asyncio.wait_for(both_preflighted.wait(), timeout=2)
        return {
            "success": False,
            "category": "not_found",
            "status_code": 200,
            "raw_body": "",
        }

    create = AsyncMock(
        return_value={
            "success": True,
            "category": "success",
            "status_code": 201,
            "uid": "booking-race-winner",
        }
    )
    first = make_tools()
    second = make_tools()
    with (
        patch("app.services.calcom_client.find_existing_booking", reconcile),
        patch("app.services.calcom_client.create_booking", create),
    ):
        results = await asyncio.gather(
            first.book_appointment(START, icp=PAYLOAD["icp"]),
            second.book_appointment(START, icp=PAYLOAD["icp"]),
        )

    assert sum(bool(result.get("success")) for result in results) == 1
    assert create.await_count == 1
    row = await load_row(outbox_session_factory, intent_key)
    assert row.state == "pending"
    assert row.booking_id == "booking-race-winner"
    assert row.booking_dispatched_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("loser_category", ["conflict", "rejected"])
async def test_negative_concurrent_loser_cannot_cancel_winner_crash_recovery(
    monkeypatch: pytest.MonkeyPatch,
    outbox_session_factory: async_sessionmaker[AsyncSession],
    loser_category: str,
) -> None:
    """A loser response cannot erase a same-intent booking that already landed."""
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "cal-key")
    intent_key = await stage()
    tools = CRMTools(
        db=MagicMock(),
        user_id=7,
        workspace_id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
        variables={
            "leadName": PAYLOAD["name"],
            "leadEmail": PAYLOAD["email"],
            "leadPhone": PAYLOAD["phone"],
            "company": PAYLOAD["company"],
            "campaign_id": PAYLOAD["campaign_id"],
            "conversation_id": PAYLOAD["conversation_id"],
        },
    )
    tools._offered_slots = [
        {"slot_id": "slot_1", "start": START, "label": "Monday 2 PM"}
    ]
    tools._selected_slot_id = "slot_1"
    tools._selected_start = START
    tools._normalized_timezone = "UTC"
    create = AsyncMock(
        return_value={
            "success": False,
            "category": loser_category,
            "status_code": 409 if loser_category == "conflict" else 400,
            "raw_body": "",
        }
    )
    empty_menu = {
        "status": "empty",
        "timezone": "UTC",
        "slots": [],
        "block": "No current openings.",
    }

    with (
        patch(
            "app.services.tools.crm_tools.stage_fulfilment_intent",
            AsyncMock(return_value=intent_key),
        ),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
        patch("app.services.calcom_client.create_booking", create),
        patch.object(
            tools, "_load_availability_menu", AsyncMock(return_value=empty_menu)
        ),
    ):
        result = await tools.book_appointment(START, icp=PAYLOAD["icp"])

    assert result["success"] is False
    retained = await load_row(outbox_session_factory, intent_key)
    assert retained.state == "booking_dispatched"
    assert retained.booking_dispatched_at is not None

    # The concurrent winner's Cal POST landed, then crashed before finalizing.
    # The retained intent must still reconcile that booking and become deliverable.
    await make_due(outbox_session_factory, intent_key)
    reconcile = AsyncMock(
        return_value={"success": True, "uid": "winner-booking", "category": "success"}
    )
    with patch("app.services.calcom_client.find_existing_booking", reconcile):
        assert await fulfilment_webhook.dispatch_due_fulfilment() is True

    reconcile.assert_awaited_once_with(
        start_iso=START,
        email="ada@example.com",
        event_type_id=123,
    )
    row = await load_row(outbox_session_factory, intent_key)
    assert row.state == "pending"
    assert row.booking_id == "winner-booking"


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_every_mutation_needs_exact_token(
    outbox_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    key = await stage()
    assert await fulfilment_webhook.finalize_fulfilment_intent(key, "booking-lease")
    stale_token = uuid.uuid4()
    now = datetime.now(UTC)
    async with outbox_session_factory() as db:
        row = await db.get(FulfilmentOutbox, key)
        assert row is not None
        row.state = "sending"
        row.claim_token = stale_token
        row.claimed_at = now - timedelta(seconds=fulfilment_webhook._LEASE_SECONDS + 1)
        row.attempts = 1
        await db.commit()

    claim = await fulfilment_webhook._claim_due_event(now=now)
    assert claim is not None
    assert claim.token != stale_token
    assert claim.action == "deliver"
    stale = fulfilment_webhook._Claim(key, stale_token, 1, "deliver")
    assert await fulfilment_webhook._ack_claim(stale) is False
    assert await fulfilment_webhook._retry_claim(stale, "stale", delay_seconds=1) is False
    assert await fulfilment_webhook._block_claim(stale, "stale") is False
    assert await fulfilment_webhook._ack_claim(claim) is True
    assert (await load_row(outbox_session_factory, key)).state == "sent"


def test_signature_covers_exact_frozen_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FULFIL_WEBHOOK_SECRET", "signing-secret")
    body = b'{"booking_id":"booking-signed","email":"ada@example.com"}'

    headers = fulfilment_webhook._headers_for_body(body)

    digest = hmac.new(b"signing-secret", body, hashlib.sha256).hexdigest()
    assert headers == {
        "Content-Type": "application/json",
        "X-Fulfil-Signature": f"sha256={digest}",
    }


@pytest.mark.asyncio
async def test_worker_start_is_idempotent_and_stop_cancels_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()

    async def fake_worker(*, interval_seconds: float) -> None:
        assert interval_seconds == fulfilment_webhook._WORKER_POLL_SECONDS
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(fulfilment_webhook, "_worker_loop", fake_worker)
    await fulfilment_webhook.start_fulfilment_worker()
    first = fulfilment_webhook._worker_task
    await entered.wait()
    await fulfilment_webhook.start_fulfilment_worker()

    assert first is not None
    assert fulfilment_webhook._worker_task is first
    await fulfilment_webhook.stop_fulfilment_worker()
    assert first.cancelled()
    assert fulfilment_webhook._worker_task is None
