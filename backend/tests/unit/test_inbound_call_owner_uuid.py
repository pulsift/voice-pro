"""Inbound provider webhooks persist the same UUID owner shape as outbound calls."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.telephony import telnyx_voice_webhook, twilio_voice_webhook
from app.core.auth import user_id_to_uuid


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["twilio", "telnyx"])
async def test_inbound_webhook_converts_integer_agent_owner_to_uuid(provider: str) -> None:
    agent = SimpleNamespace(id=uuid.uuid4(), user_id=731)
    workspace_id = uuid.uuid4()
    empty = MagicMock()
    empty.scalars.return_value.all.return_value = []
    db = MagicMock(
        add=MagicMock(),
        execute=AsyncMock(return_value=empty),
        commit=AsyncMock(),
        rollback=AsyncMock(),
    )
    request = MagicMock(base_url="https://voice.example/")

    with (
        patch(
            "app.api.telephony.get_agent_by_phone_number",
            AsyncMock(return_value=agent),
        ),
        patch(
            "app.api.telephony.get_agent_workspace_id",
            AsyncMock(return_value=workspace_id),
        ),
    ):
        if provider == "twilio":
            with patch(
                "app.api.telephony.verify_twilio_webhook",
                AsyncMock(),
            ):
                response = await twilio_voice_webhook(
                    request=request,
                    db=db,
                    call_sid="CA-inbound-owner",
                    from_number="+14155550101",
                    to_number="+14155550100",
                    call_status="ringing",
                )
        else:
            request.json = AsyncMock(
                return_value={
                    "data": {
                        "event_type": "call.initiated",
                        "payload": {
                            "call_control_id": "telnyx-inbound-owner",
                            "from": "+14155550101",
                            "to": "+14155550100",
                        },
                    }
                }
            )
            with patch(
                "app.api.telephony.verify_telnyx_webhook",
                AsyncMock(),
            ):
                response = await telnyx_voice_webhook(request=request, db=db)

    assert response.status_code == 200
    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    record = db.add.call_args.args[0]
    assert record.provider == provider
    assert isinstance(record.user_id, uuid.UUID)
    assert record.user_id == user_id_to_uuid(agent.user_id)


# --- the outage this gate caused, and the test that was missing ---------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("registered_workspace", "call_workspace"),
    [
        (None, None),      # single-tenant: no workspace rows exist at all
        (None, "some"),    # a number registered before workspaces existed
        ("same", "same"),  # both present and matching
    ],
    ids=["no-workspace-anywhere", "number-predates-workspaces", "matching-workspace"],
)
async def test_an_owned_caller_id_is_accepted_with_or_without_a_workspace(
    registered_workspace: str | None, call_workspace: str | None
) -> None:
    """Ownership is the security property. Workspace is an EXTRA constraint.

    Requiring a workspace unconditionally made this gate unsatisfiable in a
    deployment that has no workspace rows, and it refused 100% of outbound calls
    for hours on 2026-08-03 — including every call the machine placed. It was
    fixed the same day and nothing has been watching it since: a mutation that
    put the requirement straight back left all 533 tests green.
    """
    from app.api.telephony import require_owned_caller_id

    shared = uuid.uuid4()
    registered = shared if registered_workspace == "same" else None
    on_the_call = shared if call_workspace == "same" else (
        uuid.uuid4() if call_workspace else None
    )

    found = MagicMock()
    found.scalar_one_or_none.return_value = uuid.uuid4()
    db = MagicMock(execute=AsyncMock(return_value=found))

    await require_owned_caller_id(
        from_number="+14155550100",
        workspace_id=on_the_call,
        owner_user_id=731,
        db=db,
    )

    conditions = str(db.execute.await_args.args[0])
    assert "phone_numbers.phone_number" in conditions
    assert "phone_numbers.user_id" in conditions
    if on_the_call is None:
        assert "workspace_id" not in conditions, (
            "a workspace was demanded when the call had none — this is the exact "
            "shape that refused every outbound call on 2026-08-03"
        )
    del registered  # the row is the mock's answer; the query shape is the contract


@pytest.mark.asyncio
async def test_a_caller_id_nobody_registered_is_still_refused() -> None:
    from fastapi import HTTPException

    from app.api.telephony import require_owned_caller_id

    missing = MagicMock()
    missing.scalar_one_or_none.return_value = None
    db = MagicMock(execute=AsyncMock(return_value=missing))

    with pytest.raises(HTTPException) as raised:
        await require_owned_caller_id(
            from_number="+14155550100", workspace_id=None, owner_user_id=731, db=db
        )
    assert raised.value.status_code == 403
