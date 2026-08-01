"""Twilio outbound migration: provider gate + env-cred resolution."""

import inspect
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api import telephony
from app.api.telephony import InitiateCallRequest, get_twilio_service, select_outbound_provider
from app.core.config import settings
from app.services import campaign_worker
from app.services.campaign_worker import CampaignWorker
from app.services.telephony.telnyx_service import TelnyxService
from app.services.telephony.twilio_service import TwilioService

# --- provider gate (pure) ---------------------------------------------------

def test_gate_prefers_twilio_by_default_even_with_telnyx_present():
    # The whole point: Telnyx stays dormant when Twilio is configured.
    assert select_outbound_provider("twilio", has_telnyx=True, has_twilio=True) == "twilio"
    assert select_outbound_provider(None, has_telnyx=True, has_twilio=True) == "twilio"


def test_gate_does_not_fall_back_when_selected_provider_is_absent():
    assert select_outbound_provider("twilio", has_telnyx=True, has_twilio=False) is None
    assert select_outbound_provider("telnyx", has_telnyx=False, has_twilio=True) is None


def test_gate_telnyx_preference_uses_telnyx():
    assert select_outbound_provider("telnyx", has_telnyx=True, has_twilio=True) == "telnyx"


def test_gate_none_when_neither_configured():
    assert select_outbound_provider("twilio", has_telnyx=False, has_twilio=False) is None


def test_gate_invalid_provider_fails_closed():
    assert select_outbound_provider("invalid", has_telnyx=True, has_twilio=True) is None


# --- env-based Twilio resolution (mirrors get_telnyx_service) ----------------

@pytest.mark.asyncio
async def test_get_twilio_service_resolves_from_env(monkeypatch):
    # No per-workspace creds -> should fall back to platform env creds.
    monkeypatch.setattr(telephony, "get_user_api_keys", AsyncMock(return_value=None))
    monkeypatch.setattr(telephony.settings, "TWILIO_ACCOUNT_SID", "AC_env", raising=False)
    monkeypatch.setattr(telephony.settings, "TWILIO_AUTH_TOKEN", "tok_env", raising=False)

    svc = await get_twilio_service(user_id=1, db=MagicMock(), workspace_id=None)
    assert isinstance(svc, TwilioService)
    assert svc.account_sid == "AC_env"


@pytest.mark.asyncio
async def test_get_twilio_service_none_when_unconfigured(monkeypatch):
    monkeypatch.setattr(telephony, "get_user_api_keys", AsyncMock(return_value=None))
    monkeypatch.setattr(telephony.settings, "TWILIO_ACCOUNT_SID", None, raising=False)
    monkeypatch.setattr(telephony.settings, "TWILIO_AUTH_TOKEN", None, raising=False)

    svc = await get_twilio_service(user_id=1, db=MagicMock(), workspace_id=None)
    assert svc is None


def _provider_settings(
    *,
    telnyx: bool = True,
    twilio: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        telnyx_api_key="test-telnyx-key" if telnyx else None,
        telnyx_public_key="test-telnyx-public-key" if telnyx else None,
        twilio_account_sid="AC_test" if twilio else None,
        twilio_auth_token="test-twilio-token" if twilio else None,
    )


def _disable_platform_telephony_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TELNYX_API_KEY", None)
    monkeypatch.setattr(settings, "TELNYX_PUBLIC_KEY", None)
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", None)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preferred", "expected_type"),
    [
        (None, TwilioService),
        ("twilio", TwilioService),
        ("telnyx", TelnyxService),
    ],
)
async def test_campaign_worker_uses_only_the_selected_provider(
    monkeypatch: pytest.MonkeyPatch,
    preferred: str | None,
    expected_type: type[TwilioService] | type[TelnyxService],
) -> None:
    _disable_platform_telephony_credentials(monkeypatch)
    monkeypatch.setattr(settings, "TELEPHONY_OUTBOUND_PROVIDER", preferred)
    monkeypatch.setattr(
        campaign_worker,
        "get_user_api_keys",
        AsyncMock(return_value=_provider_settings()),
    )
    campaign = SimpleNamespace(user_id=uuid.uuid4(), workspace_id=uuid.uuid4())

    service = await CampaignWorker()._get_telephony_service(campaign, MagicMock())  # noqa: SLF001

    assert isinstance(service, expected_type)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("preferred", "user_settings"),
    [
        ("twilio", _provider_settings(twilio=False)),
        ("telnyx", _provider_settings(telnyx=False)),
        ("invalid", _provider_settings()),
    ],
)
async def test_campaign_worker_does_not_fallback_from_unavailable_or_invalid_selection(
    monkeypatch: pytest.MonkeyPatch,
    preferred: str,
    user_settings: SimpleNamespace,
) -> None:
    _disable_platform_telephony_credentials(monkeypatch)
    monkeypatch.setattr(settings, "TELEPHONY_OUTBOUND_PROVIDER", preferred)
    monkeypatch.setattr(
        campaign_worker,
        "get_user_api_keys",
        AsyncMock(return_value=user_settings),
    )
    campaign = SimpleNamespace(user_id=uuid.uuid4(), workspace_id=uuid.uuid4())

    service = await CampaignWorker()._get_telephony_service(campaign, MagicMock())  # noqa: SLF001

    assert service is None


@pytest.mark.asyncio
async def test_direct_call_does_not_dial_telnyx_when_selected_twilio_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    agent_result = MagicMock()
    agent_result.scalar_one_or_none.return_value = SimpleNamespace(
        id=agent_id,
        user_id=1,
        enable_recording=False,
    )
    memberships = MagicMock()
    memberships.scalars.return_value.all.return_value = [
        SimpleNamespace(workspace_id=workspace_id, is_default=True)
    ]
    caller_id = MagicMock()
    caller_id.scalar_one_or_none.return_value = uuid.uuid4()
    db = MagicMock(add=MagicMock(), commit=AsyncMock())
    db.execute = AsyncMock(side_effect=[agent_result, memberships, caller_id])
    telnyx_service = MagicMock(initiate_call=AsyncMock())
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-calendar-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 42)
    monkeypatch.setattr(settings, "TELEPHONY_OUTBOUND_PROVIDER", "twilio")

    with (
        patch(
            "app.api.telephony.get_telnyx_service",
            new=AsyncMock(return_value=telnyx_service),
        ),
        patch("app.api.telephony.get_twilio_service", new=AsyncMock(return_value=None)),
        pytest.raises(HTTPException) as exc_info,
    ):
        await inspect.unwrap(telephony.initiate_call)(
            InitiateCallRequest(
                to_number="+14085550101",
                from_number="+14085550100",
                agent_id=str(agent_id),
            ),
            MagicMock(base_url="https://voice.example/"),
            MagicMock(id=1),
            db,
            workspace_id=None,
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "code": "telephony_provider_unavailable",
        "provider": "twilio",
    }
    telnyx_service.initiate_call.assert_not_awaited()
    db.add.assert_not_called()
    db.commit.assert_not_awaited()
