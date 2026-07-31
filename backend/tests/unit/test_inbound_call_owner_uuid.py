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
