"""Adversarial tests for DB-backed, single-use Twilio media grants."""

import importlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException, Response
from structlog.testing import capture_logs

from app.api.telephony import (
    _lock_twilio_outbound_answer_record,
    twilio_answer_webhook,
    twilio_voice_webhook,
)
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.services.telephony.media_grant import (
    arm_twilio_media_grant,
    consume_twilio_media_grant,
    create_twilio_media_grant_token,
    cv_sha256,
)


def _transient_record(
    *,
    call_sid: str = "CA-actual",
    variables: dict[str, object] | None = None,
) -> CallRecord:
    return CallRecord(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        provider="twilio",
        provider_call_id=call_sid,
        agent_id=uuid.uuid4(),
        direction=CallDirection.OUTBOUND.value,
        status=CallStatus.INITIATED.value,
        from_number="+14155550100",
        to_number="+14155550101",
        variables=variables,
    )


def _twiml_parameters(response: Response) -> dict[str, str]:
    body = response.body.decode()
    pairs = re.findall(r'<Parameter name="([^"]+)" value="([^"]*)"', body)
    return dict(pairs)


def test_arm_retry_is_one_deterministic_grant_and_consumed_grant_never_rearms() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    record = _transient_record()
    first = arm_twilio_media_grant(record, "cv-1", now=now)
    first_expiry = record.media_grant_expires_at
    second = arm_twilio_media_grant(record, "cv-1", now=now + timedelta(seconds=10))

    assert first == second == create_twilio_media_grant_token(record.id)
    assert record.media_grant_expires_at > first_expiry
    assert record.media_grant_cv_sha256 == cv_sha256("cv-1")

    record.media_grant_consumed_at = now + timedelta(seconds=20)
    assert arm_twilio_media_grant(record, "cv-1", now=now + timedelta(seconds=30)) is None
    assert arm_twilio_media_grant(record, "different-cv", now=now) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("token", ["", "not-a-grant", "mg1.bad.bad"])
async def test_invalid_token_rejects_without_touching_database(token: str) -> None:
    db = MagicMock(execute=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())

    result = await consume_twilio_media_grant(
        db=db,
        token=token,
        call_sid="CA-bound",
        agent_id=str(uuid.uuid4()),
        workspace_id=str(uuid.uuid4()),
        cv="cv",
    )

    assert result is None
    db.execute.assert_not_awaited()
    db.commit.assert_not_awaited()
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_grant_atomic_update_binds_every_identity_and_commits() -> None:
    now = datetime.now(UTC)
    record = _transient_record(call_sid="CA-bound")
    token = arm_twilio_media_grant(record, "bound-cv", now=now)
    result = MagicMock()
    result.scalar_one_or_none.return_value = record
    db = MagicMock(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    consumed = await consume_twilio_media_grant(
        db=db,
        token=token or "",
        call_sid=record.provider_call_id,
        agent_id=str(record.agent_id),
        workspace_id=str(record.workspace_id),
        cv="bound-cv",
        now=now + timedelta(seconds=1),
    )

    assert consumed is record
    db.commit.assert_awaited_once()
    db.rollback.assert_not_awaited()
    statement = db.execute.await_args.args[0]
    sql = str(statement.compile())
    params = set(statement.compile().params.values())
    for binding in (
        "call_records.id",
        "call_records.provider",
        "call_records.provider_call_id",
        "call_records.agent_id",
        "call_records.workspace_id",
        "call_records.media_grant_cv_sha256",
        "call_records.media_grant_expires_at",
        "call_records.media_grant_consumed_at IS NULL",
    ):
        assert binding in sql
    assert record.id in params
    assert record.provider_call_id in params
    assert record.agent_id in params
    assert record.workspace_id in params
    assert cv_sha256("bound-cv") in params


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mismatch", "bad_value"),
    [
        ("call_sid", "CA-other"),
        ("agent_id", str(uuid.uuid4())),
        ("workspace_id", str(uuid.uuid4())),
        ("cv", "other-cv"),
    ],
)
async def test_grant_rejects_each_bound_identity_mismatch(
    mismatch: str,
    bad_value: str,
) -> None:
    now = datetime.now(UTC)
    record = _transient_record(call_sid="CA-bound")
    token = arm_twilio_media_grant(record, "bound-cv", now=now)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = MagicMock(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    supplied = {
        "call_sid": record.provider_call_id,
        "agent_id": str(record.agent_id),
        "workspace_id": str(record.workspace_id),
        "cv": "bound-cv",
    }
    supplied[mismatch] = bad_value

    rejected = await consume_twilio_media_grant(
        db=db,
        token=token or "",
        now=now + timedelta(seconds=1),
        **supplied,
    )

    assert rejected is None
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    params = set(db.execute.await_args.args[0].compile().params.values())
    expected = (
        cv_sha256(bad_value)
        if mismatch == "cv"
        else (uuid.UUID(bad_value) if mismatch in {"agent_id", "workspace_id"} else bad_value)
    )
    assert expected in params


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection", ["expired", "replayed"])
async def test_expired_or_replayed_atomic_miss_rolls_back(rejection: str) -> None:
    now = datetime.now(UTC)
    record = _transient_record(call_sid="CA-bound")
    token = arm_twilio_media_grant(record, "cv", now=now)
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = MagicMock(
        execute=AsyncMock(return_value=result),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )

    rejected = await consume_twilio_media_grant(
        db=db,
        token=token or "",
        call_sid=record.provider_call_id,
        agent_id=str(record.agent_id),
        workspace_id=str(record.workspace_id),
        cv="cv",
        now=now + timedelta(seconds=121 if rejection == "expired" else 1),
    )

    assert rejected is None
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()
    sql = str(db.execute.await_args.args[0].compile())
    assert "call_records.media_grant_expires_at >" in sql
    assert "call_records.media_grant_consumed_at IS NULL" in sql


@pytest.mark.asyncio
async def test_outbound_pending_record_correlates_to_actual_call_sid() -> None:
    record = _transient_record(call_sid="pending:nonce")
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [record]
    db = MagicMock(execute=AsyncMock(return_value=rows))

    correlated = await _lock_twilio_outbound_answer_record(
        db=db,
        call_record_id=str(record.id),
        call_sid="CA-real",
        agent_id=str(record.agent_id),
        workspace_id=str(record.workspace_id),
        from_number=record.from_number,
        to_number=record.to_number,
    )

    assert correlated is record
    assert record.provider_call_id == "CA-real"
    statement = db.execute.await_args.args[0]
    sql = str(statement.compile())
    for binding in (
        "call_records.id",
        "call_records.provider",
        "call_records.provider_call_id",
        "call_records.agent_id",
        "call_records.workspace_id",
        "call_records.from_number",
        "call_records.to_number",
    ):
        assert binding in sql


@pytest.mark.asyncio
async def test_signed_outbound_answer_retry_reuses_same_unconsumed_grant() -> None:
    record = _transient_record(call_sid="pending:nonce")
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [record]
    db = MagicMock(
        execute=AsyncMock(return_value=rows),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    verifier = AsyncMock()
    request = MagicMock(base_url="https://voice.example/")

    with patch("app.api.telephony.verify_twilio_webhook", verifier):
        first = await twilio_answer_webhook(
            request=request,
            agent_id=str(record.agent_id),
            cv="bound-cv",
            workspace_id=str(record.workspace_id),
            call_record_id=str(record.id),
            db=db,
            call_sid="CA-real",
            from_number=record.from_number,
            to_number=record.to_number,
        )
        second = await twilio_answer_webhook(
            request=request,
            agent_id=str(record.agent_id),
            cv="bound-cv",
            workspace_id=str(record.workspace_id),
            call_record_id=str(record.id),
            db=db,
            call_sid="CA-real",
            from_number=record.from_number,
            to_number=record.to_number,
        )

    first_params = _twiml_parameters(first)
    second_params = _twiml_parameters(second)
    assert first_params["media_grant"] == second_params["media_grant"]
    assert first_params["cv"] == second_params["cv"] == "bound-cv"
    assert verifier.await_count == 2
    assert db.commit.await_count == 2
    db.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_answer_signature_failure_precedes_db_and_grant() -> None:
    db = MagicMock(execute=AsyncMock())
    with (
        patch(
            "app.api.telephony.verify_twilio_webhook",
            AsyncMock(side_effect=HTTPException(status_code=403, detail="bad signature")),
        ),
        patch("app.api.telephony.arm_twilio_media_grant") as arm,
        pytest.raises(HTTPException),
    ):
        await twilio_answer_webhook(
            request=MagicMock(),
            agent_id=str(uuid.uuid4()),
            cv="cv",
            workspace_id=str(uuid.uuid4()),
            call_record_id=str(uuid.uuid4()),
            db=db,
            call_sid="CA-real",
            from_number="+14155550100",
            to_number="+14155550101",
        )

    db.execute.assert_not_awaited()
    arm.assert_not_called()


@pytest.mark.asyncio
async def test_signed_inbound_answer_retry_reuses_record_and_grant() -> None:
    agent = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4())
    workspace_id = uuid.uuid4()
    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    db = MagicMock(
        add=MagicMock(),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    call_count = 0

    async def execute(_statement: object) -> MagicMock:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return empty
        retry = MagicMock()
        retry.scalars.return_value.all.return_value = [db.add.call_args.args[0]]
        return retry

    db.execute = AsyncMock(side_effect=execute)
    verifier = AsyncMock()
    with (
        patch("app.api.telephony.verify_twilio_webhook", verifier),
        patch("app.api.telephony.get_agent_by_phone_number", AsyncMock(return_value=agent)),
        patch(
            "app.api.telephony.get_agent_workspace_id",
            AsyncMock(return_value=workspace_id),
        ),
    ):
        first = await twilio_voice_webhook(
            request=MagicMock(base_url="https://voice.example/"),
            db=db,
            call_sid="CA-inbound",
            from_number="+14155550101",
            to_number="+14155550100",
            call_status="ringing",
        )
        second = await twilio_voice_webhook(
            request=MagicMock(base_url="https://voice.example/"),
            db=db,
            call_sid="CA-inbound",
            from_number="+14155550101",
            to_number="+14155550100",
            call_status="ringing",
        )

    assert _twiml_parameters(first)["media_grant"] == _twiml_parameters(second)["media_grant"]
    assert db.add.call_count == 1
    assert db.commit.await_count == 2
    assert verifier.await_count == 2


@pytest.mark.asyncio
async def test_answer_logs_neither_grant_nor_cv_value() -> None:
    record = _transient_record(call_sid="CA-real")
    rows = MagicMock()
    rows.scalars.return_value.all.return_value = [record]
    db = MagicMock(
        execute=AsyncMock(return_value=rows),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    cv = "sensitive-cv-value"

    with (
        patch("app.api.telephony.verify_twilio_webhook", AsyncMock()),
        capture_logs() as logs,
    ):
        response = await twilio_answer_webhook(
            request=MagicMock(base_url="https://voice.example/"),
            agent_id=str(record.agent_id),
            cv=cv,
            workspace_id=str(record.workspace_id),
            call_record_id=str(record.id),
            db=db,
            call_sid="CA-real",
            from_number=record.from_number,
            to_number=record.to_number,
        )

    token = _twiml_parameters(response)["media_grant"]
    rendered_logs = repr(logs)
    assert token not in rendered_logs
    assert cv not in rendered_logs


def test_model_migration_and_bootstrap_cover_nullable_grant_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "media_grant_cv_sha256",
        "media_grant_expires_at",
        "media_grant_consumed_at",
    }
    columns = CallRecord.__table__.columns
    assert expected <= set(columns.keys())
    assert all(columns[name].nullable for name in expected)

    bootstrap = importlib.import_module("railway_bootstrap")
    reconciled = {
        (table, column, coltype)
        for table, column, coltype in bootstrap.COLUMN_RECONCILE
        if column in expected
    }
    assert reconciled == {
        ("call_records", "media_grant_cv_sha256", "VARCHAR(64)"),
        ("call_records", "media_grant_expires_at", "TIMESTAMPTZ"),
        ("call_records", "media_grant_consumed_at", "TIMESTAMPTZ"),
    }

    migration = importlib.import_module("migrations.versions.f3e8a1c2d4b5_add_twilio_media_grants")
    add_column = MagicMock()
    monkeypatch.setattr(migration.op, "add_column", add_column)
    migration.upgrade()
    assert [call.args[1].name for call in add_column.call_args_list] == [
        "media_grant_cv_sha256",
        "media_grant_expires_at",
        "media_grant_consumed_at",
    ]
    assert all(call.args[1].nullable for call in add_column.call_args_list)
