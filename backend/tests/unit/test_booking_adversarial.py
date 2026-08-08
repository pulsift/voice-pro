"""Adversarial contracts for duplicate-safe booking diagnostics and persistence."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.api.calls import get_call
from app.api.telephony_ws import save_transcript_to_call_record
from app.core.config import settings
from app.services.calcom_client import create_booking, sanitize_provider_text
from app.services.tools.crm_tools import CRMTools

SLOT = {"start": "2026-07-13T09:00:00Z", "label": "Monday 11:00 AM"}
ICP = {"offer_types": ["commercial solar"], "min_kw": 50, "states": ["Texas"]}


@pytest.fixture(autouse=True)
def configured_calcom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 123)
    monkeypatch.setattr(settings, "BOOKING_TEAM_TIMEZONE", "Europe/Stockholm")
    monkeypatch.setattr(
        "app.services.tools.crm_tools.stage_fulfilment_intent",
        AsyncMock(return_value="intent-key"),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.finalize_fulfilment_intent",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.claim_fulfilment_booking",
        AsyncMock(return_value=uuid.UUID(int=1)),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.authorize_fulfilment_booking",
        AsyncMock(return_value=True),
    )


def make_tools() -> CRMTools:
    return CRMTools(
        db=MagicMock(),
        user_id=1,
        variables={"leadName": "Sami", "leadEmail": "lead@example.com"},
    )


async def offer_and_select(tools: CRMTools) -> None:
    with patch(
        "app.services.calcom_client.get_open_slots",
        AsyncMock(return_value=[SLOT]),
    ):
        await tools.check_availability(time_zone="UTC")
    tools.observe_user_utterance("the first one")
    assert (await tools.select_slot("slot_1"))["success"] is True


@pytest.mark.asyncio
async def test_timeout_after_provider_commit_reconciles_without_duplicate_post() -> None:
    """An unknown POST outcome must be checked before a second POST is allowed."""
    tools = make_tools()
    await offer_and_select(tools)
    post = AsyncMock(
        return_value={
            "success": False,
            "category": "transient",
            "status_code": None,
            "raw_body": "timed out",
        }
    )
    reconcile = AsyncMock(
        side_effect=[
            {
                "success": False,
                "category": "not_found",
                "status_code": 200,
                "raw_body": "",
            },
            {
                "success": True,
                "category": "reconciled_success",
                "status_code": 200,
                "raw_body": "",
                "uid": "committed-booking",
                "start": SLOT["start"],
            },
        ]
    )
    webhook = AsyncMock(return_value=True)

    with (
        patch("app.services.calcom_client.create_booking", post),
        patch("app.services.calcom_client.find_existing_booking", reconcile),
        patch("app.services.tools.crm_tools.finalize_fulfilment_intent", webhook),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=ICP)

    assert result["success"] is True
    assert result["uid"] == "committed-booking"
    assert post.await_count == 1
    assert reconcile.await_count == 2
    reconcile.assert_awaited_with(start_iso=SLOT["start"], email="lead@example.com")
    webhook.assert_awaited_once()
    categories = [attempt["category"] for attempt in tools.get_booking_attempts()]
    assert categories[-3:] == ["not_found", "transient", "reconciled_success"]
    assert tools.get_booking_attempts()[-1]["uid"] == "committed-booking"


@pytest.mark.asyncio
async def test_repeated_booking_invocation_never_reposts_a_dispatched_intent() -> None:
    """Tool retries by the model become reconciliation-only after one POST."""
    tools = make_tools()
    await offer_and_select(tools)
    post = AsyncMock(
        return_value={
            "success": False,
            "category": "transient",
            "status_code": 503,
            "raw_body": "unavailable",
        }
    )
    reconcile = AsyncMock(
        return_value={
            "success": False,
            "category": "not_found",
            "status_code": 200,
            "raw_body": "",
        }
    )

    with (
        patch("app.services.calcom_client.create_booking", post),
        patch("app.services.calcom_client.find_existing_booking", reconcile),
        patch("app.services.tools.crm_tools.asyncio.sleep", AsyncMock()),
        patch(
            "app.services.tools.crm_tools.claim_fulfilment_booking",
            AsyncMock(side_effect=[uuid.UUID(int=1), None]),
        ),
    ):
        first = await tools.book_appointment(SLOT["start"], icp=ICP)
        second = await tools.book_appointment(SLOT["start"], icp=ICP)

    assert first["success"] is False
    assert second["success"] is False
    assert post.await_count == 1
    assert {call.kwargs["start_iso"] for call in post.await_args_list} == {SLOT["start"]}


@pytest.mark.asyncio
async def test_reconciliation_unavailable_blocks_retry_post() -> None:
    """A broken read-after-write check must fail closed rather than duplicate a booking."""
    tools = make_tools()
    await offer_and_select(tools)
    post = AsyncMock(
        return_value={
            "success": False,
            "category": "transient",
            "status_code": None,
            "raw_body": "timed out",
        }
    )
    reconcile = AsyncMock(
        side_effect=[
            {
                "success": False,
                "category": "not_found",
                "status_code": 200,
                "raw_body": "",
            },
            {
                "success": False,
                "category": "reconcile_unavailable",
                "status_code": 503,
                "raw_body": "unavailable",
            },
        ]
    )

    with (
        patch("app.services.calcom_client.create_booking", post),
        patch("app.services.calcom_client.find_existing_booking", reconcile),
        patch("app.services.tools.crm_tools.asyncio.sleep", AsyncMock()),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=ICP)

    assert result["success"] is False
    assert result["error"] == "booking_outcome_unknown"
    assert post.await_count == 1
    assert reconcile.await_count == 2
    assert tools.get_booking_attempts()[-1]["category"] == "reconcile_unavailable"


@pytest.mark.asyncio
async def test_new_session_preflight_finds_existing_booking_before_any_post() -> None:
    """Fresh in-memory state must not duplicate a booking made by an earlier session."""
    tools = make_tools()
    await offer_and_select(tools)
    post = AsyncMock()
    reconcile = AsyncMock(
        return_value={
            "success": True,
            "category": "reconciled_success",
            "status_code": 200,
            "raw_body": "",
            "uid": "earlier-session-booking",
            "start": SLOT["start"],
        }
    )
    webhook = AsyncMock(return_value=True)

    with (
        patch("app.services.calcom_client.create_booking", post),
        patch("app.services.calcom_client.find_existing_booking", reconcile),
        patch("app.services.tools.crm_tools.finalize_fulfilment_intent", webhook),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=ICP)

    assert result["uid"] == "earlier-session-booking"
    post.assert_not_awaited()
    reconcile.assert_awaited_once_with(start_iso=SLOT["start"], email="lead@example.com")
    webhook.assert_awaited_once()
    assert [attempt["operation"] for attempt in tools.get_booking_attempts()] == [
        "availability",
        "select",
        "reconcile",
    ]


@pytest.mark.asyncio
async def test_booking_transition_evidence_orders_offer_select_reconcile_create() -> None:
    tools = make_tools()
    await offer_and_select(tools)
    reconcile = AsyncMock(
        return_value={"success": False, "category": "not_found", "status_code": 200}
    )
    post = AsyncMock(
        return_value={
            "success": True,
            "category": "success",
            "status_code": 201,
            "raw_body": "",
            "uid": "new-booking",
            "start": SLOT["start"],
        }
    )

    with (
        patch("app.services.calcom_client.create_booking", post),
        patch("app.services.calcom_client.find_existing_booking", reconcile),
        patch("app.services.tools.crm_tools.finalize_fulfilment_intent"),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=ICP)

    assert result["uid"] == "new-booking"
    assert [attempt["operation"] for attempt in tools.get_booking_attempts()] == [
        "availability",
        "select",
        "reconcile",
        "create",
    ]
    assert [attempt["category"] for attempt in tools.get_booking_attempts()] == [
        "offered",
        "selected",
        "not_found",
        "success",
    ]


@pytest.mark.asyncio
async def test_fulfilment_intent_commits_before_cal_post_and_finalizes_after_success() -> None:
    tools = make_tools()
    await offer_and_select(tools)
    order: list[str] = []

    async def stage_intent(**_kwargs: object) -> str:
        order.append("stage")
        return "intent-key"

    async def reconcile_booking(**_kwargs: object) -> dict[str, object]:
        order.append("reconcile")
        return {"success": False, "category": "not_found", "status_code": 200}

    async def create_cal_booking(**_kwargs: object) -> dict[str, object]:
        order.append("create")
        return {
            "success": True,
            "category": "success",
            "status_code": 201,
            "uid": "booking-ordered",
            "start": SLOT["start"],
        }

    async def finalize_intent(intent_key: str, booking_id: object) -> bool:
        assert intent_key == "intent-key"
        assert booking_id == "booking-ordered"
        order.append("finalize")
        return True

    staged = AsyncMock(side_effect=stage_intent)
    with (
        patch(
            "app.services.tools.crm_tools.stage_fulfilment_intent",
            staged,
        ),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(side_effect=reconcile_booking),
        ),
        patch(
            "app.services.calcom_client.create_booking",
            AsyncMock(side_effect=create_cal_booking),
        ),
        patch(
            "app.services.tools.crm_tools.finalize_fulfilment_intent",
            AsyncMock(side_effect=finalize_intent),
        ),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=ICP)

    assert result["uid"] == "booking-ordered"
    assert order == ["stage", "reconcile", "create", "finalize"]
    assert staged.await_args.kwargs["start_iso"] == SLOT["start"]
    assert staged.await_args.kwargs["email"] == "lead@example.com"
    assert staged.await_args.kwargs["payload"]["company"] == ""
    assert staged.await_args.kwargs["payload"]["icp"] == ICP


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_icp",
    [
        {"offer_types": ["solar"], "states": ["Texas"], "service_area": "DFW"},
        {"offer_types": ["solar"], "states": "Texas", "min_kw": 50},
    ],
)
async def test_receiver_incompatible_icp_cannot_create_booking(
    invalid_icp: dict[str, object],
) -> None:
    tools = make_tools()
    await offer_and_select(tools)
    stage = AsyncMock()
    reconcile = AsyncMock()
    create = AsyncMock()

    with (
        patch("app.services.tools.crm_tools.stage_fulfilment_intent", stage),
        patch("app.services.calcom_client.find_existing_booking", reconcile),
        patch("app.services.calcom_client.create_booking", create),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=invalid_icp)

    assert result["success"] is False
    assert result["error"] == "invalid_icp"
    stage.assert_not_awaited()
    reconcile.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_intent_stage_failure_prevents_any_cal_read_or_write() -> None:
    tools = make_tools()
    await offer_and_select(tools)
    reconcile = AsyncMock()
    create = AsyncMock()

    with (
        patch(
            "app.services.tools.crm_tools.stage_fulfilment_intent",
            AsyncMock(side_effect=RuntimeError("database unavailable")),
        ),
        patch("app.services.calcom_client.find_existing_booking", reconcile),
        patch("app.services.calcom_client.create_booking", create),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=ICP)

    assert result["success"] is False
    assert result["error"] == "fulfilment_unavailable"
    reconcile.assert_not_awaited()
    create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_data",
    [
        {"start": SLOT["start"]},
        {"uid": "unverifiable-booking", "start": "2026-07-13T10:00:00Z"},
    ],
)
async def test_malformed_success_response_fails_closed_then_reconciles(
    provider_data: dict[str, str],
) -> None:
    """A 2xx without matching UID/start is unknown, never proof of success."""
    tools = make_tools()
    await offer_and_select(tools)
    response = MagicMock(status_code=201, text='{"data":{"email":"lead@example.com"}}')
    response.json.return_value = {"data": provider_data}
    client = MagicMock(post=AsyncMock(return_value=response))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    reconcile = AsyncMock(
        side_effect=[
            {"success": False, "category": "not_found", "status_code": 200},
            {
                "success": True,
                "category": "reconciled_success",
                "status_code": 200,
                "uid": "verified-by-read",
                "start": SLOT["start"],
            },
        ]
    )

    with (
        patch("app.services.calcom_client.httpx.AsyncClient", return_value=context),
        patch("app.services.calcom_client.find_existing_booking", reconcile),
        patch("app.services.tools.crm_tools.finalize_fulfilment_intent"),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=ICP)

    assert result["uid"] == "verified-by-read"
    client.post.assert_awaited_once()
    assert reconcile.await_args_list == [
        call(start_iso=SLOT["start"], email="lead@example.com"),
        call(start_iso=SLOT["start"], email="lead@example.com"),
    ]
    assert [attempt["operation"] for attempt in tools.get_booking_attempts()][-3:] == [
        "reconcile",
        "create",
        "reconcile",
    ]
    assert [attempt["category"] for attempt in tools.get_booking_attempts()][-2:] == [
        "transient",
        "reconciled_success",
    ]


@pytest.mark.asyncio
async def test_unreadable_success_response_reconciles_without_duplicate_post() -> None:
    """A committed booking with broken 2xx JSON must be recovered by a read."""
    tools = make_tools()
    await offer_and_select(tools)
    response = MagicMock(status_code=201, text='{"data":')
    response.json.side_effect = ValueError("truncated JSON")
    client = MagicMock(post=AsyncMock(return_value=response))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)
    reconcile = AsyncMock(
        side_effect=[
            {"success": False, "category": "not_found", "status_code": 200},
            {
                "success": True,
                "category": "reconciled_success",
                "status_code": 200,
                "uid": "committed-despite-broken-json",
                "start": SLOT["start"],
            },
        ]
    )
    webhook = AsyncMock(return_value=True)

    with (
        patch("app.services.calcom_client.httpx.AsyncClient", return_value=context),
        patch("app.services.calcom_client.find_existing_booking", reconcile),
        patch("app.services.tools.crm_tools.finalize_fulfilment_intent", webhook),
    ):
        result = await tools.book_appointment(SLOT["start"], icp=ICP)

    assert result["uid"] == "committed-despite-broken-json"
    client.post.assert_awaited_once()
    assert reconcile.await_args_list == [
        call(start_iso=SLOT["start"], email="lead@example.com"),
        call(start_iso=SLOT["start"], email="lead@example.com"),
    ]
    webhook.assert_awaited_once()
    assert [attempt["category"] for attempt in tools.get_booking_attempts()][-3:] == [
        "not_found",
        "transient",
        "reconciled_success",
    ]


def test_provider_diagnostics_redact_pii_secrets_and_attendee_metadata() -> None:
    raw = (
        '{"email":"lead@example.com","phone":"+1 408 555 0101",'
        '"token":"super-secret-token","attendee":{"name":"Sami",'
        '"email":"nested@example.com"},"metadata":{"notes":"private ICP notes"}}'
    )

    sanitized = sanitize_provider_text(raw)

    for sensitive in (
        "lead@example.com",
        "+1 408 555 0101",
        "super-secret-token",
        "nested@example.com",
        "Sami",
        "private ICP notes",
    ):
        assert sensitive not in sanitized
    assert sanitized.count("[redacted]") == 5


def test_provider_diagnostics_redact_pii_inside_nested_json_string() -> None:
    raw = (
        r'{"message":"{\"email\":\"nested@example.com\",'
        r'\"phone\":\"+1 408 555 0101\",\"token\":\"nested-secret\"}"}'
    )

    sanitized = sanitize_provider_text(raw)

    assert "nested@example.com" not in sanitized
    assert "+1 408 555 0101" not in sanitized
    assert "nested-secret" not in sanitized
    assert "[email]" in sanitized
    assert "[phone]" in sanitized
    assert "[redacted]" in sanitized


@pytest.mark.asyncio
async def test_successful_provider_response_never_persists_raw_body() -> None:
    response = MagicMock(
        status_code=201,
        text='{"data":{"uid":"booking-1","attendee":{"email":"lead@example.com"}}}',
    )
    response.json.return_value = {
        "data": {"uid": "booking-1", "start": SLOT["start"], "attendee": {"email": "x@y.com"}}
    }
    client = MagicMock(post=AsyncMock(return_value=response))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.calcom_client.httpx.AsyncClient", return_value=context):
        result = await create_booking(
            start_iso=SLOT["start"],
            name="Sami",
            email="lead@example.com",
            lead_tz="UTC",
        )

    assert result["success"] is True
    assert result["raw_body"] == ""


@pytest.mark.asyncio
async def test_call_api_serializes_null_booking_attempts_as_empty_list() -> None:
    call_id = uuid.uuid4()
    record = MagicMock(
        id=call_id,
        provider="telnyx",
        provider_call_id="provider-call-id",
        agent_id=None,
        agent=None,
        contact_id=None,
        contact=None,
        workspace_id=None,
        workspace=None,
        direction="outbound",
        status="completed",
        from_number="+14085550100",
        to_number="+14085550101",
        duration_seconds=30,
        recording_url=None,
        transcript=None,
        booking_attempts=None,
        started_at=datetime.now(UTC),
        answered_at=None,
        ended_at=None,
    )
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = record
    db = MagicMock()
    db.execute = AsyncMock(return_value=query_result)

    response = await get_call(str(call_id), MagicMock(id=1), db)

    assert response.model_dump(mode="json")["booking_attempts"] == []


@pytest.mark.asyncio
async def test_call_record_fallback_refuses_ambiguous_candidates() -> None:
    owner_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    first = MagicMock(transcript=None, booking_attempts=None)
    second = MagicMock(transcript=None, booking_attempts=None)
    exact = MagicMock()
    exact.scalars.return_value.all.return_value = []
    fallback = MagicMock()
    fallback.scalars.return_value.all.return_value = [first, second]
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[exact, fallback])
    db.commit = AsyncMock()
    log = MagicMock()

    await save_transcript_to_call_record(
        "media-stream-call-id",
        "[User]: hello",
        db,
        log,
        agent_id=str(agent_id),
        booking_attempts=[{"attempt": 1, "category": "rejected"}],
        owner_user_id=owner_id,
        workspace_id=workspace_id,
        provider="telnyx",
        expected_to_number="+14085550101",
    )

    db.commit.assert_not_awaited()
    assert first.transcript is None
    assert second.transcript is None
    log.warning.assert_called()


@pytest.mark.asyncio
async def test_call_record_fallback_scopes_every_available_identity_dimension() -> None:
    owner_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    exact = MagicMock()
    exact.scalars.return_value.all.return_value = []
    fallback = MagicMock()
    fallback.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[exact, fallback])

    await save_transcript_to_call_record(
        "media-stream-call-id",
        "",
        db,
        MagicMock(),
        agent_id=str(agent_id),
        booking_attempts=[],
        owner_user_id=owner_id,
        workspace_id=workspace_id,
        provider="telnyx",
        expected_to_number="+14085550101",
    )

    fallback_query = db.execute.await_args_list[1].args[0]
    compiled = fallback_query.compile()
    sql = str(compiled)
    params = compiled.params
    assert "call_records.agent_id" in sql
    assert "call_records.user_id" in sql
    assert "call_records.workspace_id" in sql
    assert "call_records.provider" in sql
    assert "call_records.to_number" in sql
    assert agent_id in params.values()
    assert owner_id in params.values()
    assert workspace_id in params.values()
    assert "telnyx" in params.values()
    assert "+14085550101" in params.values()


@pytest.mark.asyncio
async def test_call_record_fallback_scopes_missing_workspace_to_null() -> None:
    owner_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    exact = MagicMock()
    exact.scalars.return_value.all.return_value = []
    fallback = MagicMock()
    fallback.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[exact, fallback])

    await save_transcript_to_call_record(
        "media-stream-call-id",
        "",
        db,
        MagicMock(),
        agent_id=str(agent_id),
        booking_attempts=[],
        owner_user_id=owner_id,
        workspace_id=None,
        provider="telnyx",
        expected_to_number="+14085550101",
    )

    fallback_query = db.execute.await_args_list[1].args[0]
    assert "call_records.workspace_id IS NULL" in str(fallback_query.compile())


@pytest.mark.asyncio
async def test_exact_call_record_lookup_is_scoped_and_refuses_duplicates() -> None:
    owner_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    exact = MagicMock()
    exact.scalars.return_value.all.return_value = [MagicMock(), MagicMock()]
    db = MagicMock()
    db.execute = AsyncMock(return_value=exact)
    db.commit = AsyncMock()

    await save_transcript_to_call_record(
        "shared-provider-id",
        "[User]: evidence",
        db,
        MagicMock(),
        agent_id=str(uuid.uuid4()),
        booking_attempts=[],
        owner_user_id=owner_id,
        workspace_id=workspace_id,
        provider="telnyx",
    )

    db.commit.assert_not_awaited()
    assert db.execute.await_count == 1
    exact_query = db.execute.await_args.args[0]
    assert getattr(exact_query, "_for_update_arg", None) is not None
    compiled = exact_query.compile()
    sql = str(compiled)
    params = compiled.params
    assert "call_records.provider_call_id" in sql
    assert "call_records.provider" in sql
    assert "call_records.user_id" in sql
    assert "call_records.workspace_id" in sql
    assert "shared-provider-id" in params.values()
    assert "telnyx" in params.values()
    assert owner_id in params.values()
    assert workspace_id in params.values()


@pytest.mark.asyncio
async def test_reconnect_cleanup_merges_attempts_and_preserves_longer_transcript() -> None:
    owner_id = uuid.uuid4()
    existing_attempt = {"attempt": 1, "operation": "create", "category": "transient"}
    new_attempt = {"attempt": 2, "operation": "reconcile", "category": "not_found"}
    original_transcript = "[User]: this is the longer preserved transcript"
    record = MagicMock(
        id=uuid.uuid4(),
        transcript=original_transcript,
        booking_attempts=[existing_attempt],
    )
    exact = MagicMock()
    exact.scalars.return_value.all.return_value = [record]
    db = MagicMock()
    db.execute = AsyncMock(return_value=exact)
    db.commit = AsyncMock()

    await save_transcript_to_call_record(
        "provider-call-id",
        "[User]: short",
        db,
        MagicMock(),
        booking_attempts=[existing_attempt, new_attempt],
        owner_user_id=owner_id,
        workspace_id=None,
        provider="telnyx",
    )

    assert record.transcript == original_transcript
    assert record.booking_attempts == [existing_attempt, new_attempt]
    db.commit.assert_awaited_once_with()

    db.commit.reset_mock()
    await save_transcript_to_call_record(
        "provider-call-id",
        "[User]: short",
        db,
        MagicMock(),
        booking_attempts=[existing_attempt],
        owner_user_id=owner_id,
        workspace_id=None,
        provider="telnyx",
    )

    assert record.transcript == original_transcript
    assert record.booking_attempts == [existing_attempt, new_attempt]
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconnect_cleanup_replaces_only_with_longer_transcript() -> None:
    record = MagicMock(
        id=uuid.uuid4(),
        transcript="[User]: short",
        booking_attempts=[],
    )
    exact = MagicMock()
    exact.scalars.return_value.all.return_value = [record]
    db = MagicMock()
    db.execute = AsyncMock(return_value=exact)
    db.commit = AsyncMock()
    longer = "[User]: this transcript contains more durable call evidence"

    await save_transcript_to_call_record(
        "provider-call-id",
        longer,
        db,
        MagicMock(),
        booking_attempts=[],
        owner_user_id=uuid.uuid4(),
        workspace_id=None,
        provider="telnyx",
    )

    assert record.transcript == longer
    db.commit.assert_awaited_once_with()


# --- survivors of the 2026-08-08 mutation sweep --------------------------------
#
# Each of these was a behaviour the code already had and NOTHING was watching.
# Found by breaking it on purpose and seeing the whole suite stay green.


@pytest.mark.asyncio
async def test_a_list_is_never_promised_with_no_fit_answers_to_build_it_from():
    """The two fit questions ARE the product: they are what the hundred leads get
    built from. Booking without them promises a list nobody can build, and the
    prospect finds out days later."""
    tools = make_tools()
    await offer_and_select(tools)

    post = AsyncMock()
    with patch("app.services.calcom_client.create_booking", post):
        result = await tools.book_appointment(SLOT["start"], icp=None)

    assert result["success"] is False
    assert result["error"] == "missing_icp"
    post.assert_not_awaited(), "a booking was attempted with no fit answers"
    # ...and the agent is told what to do about it, in words it can act on.
    assert "fit questions" in result["message"]


@pytest.mark.asyncio
async def test_an_empty_fit_answer_object_counts_as_no_fit_answers():
    tools = make_tools()
    await offer_and_select(tools)
    post = AsyncMock()
    with patch("app.services.calcom_client.create_booking", post):
        result = await tools.book_appointment(SLOT["start"], icp={})
    assert result["error"] == "missing_icp"
    post.assert_not_awaited()
