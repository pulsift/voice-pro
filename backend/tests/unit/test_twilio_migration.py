"""Twilio outbound migration: provider gate + env-cred resolution."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api import telephony
from app.api.telephony import get_twilio_service, select_outbound_provider
from app.services.telephony.twilio_service import TwilioService

# --- provider gate (pure) ---------------------------------------------------

def test_gate_prefers_twilio_by_default_even_with_telnyx_present():
    # The whole point: Telnyx stays dormant when Twilio is configured.
    assert select_outbound_provider("twilio", has_telnyx=True, has_twilio=True) == "twilio"
    assert select_outbound_provider(None, has_telnyx=True, has_twilio=True) == "twilio"


def test_gate_falls_back_to_telnyx_only_when_twilio_absent():
    assert select_outbound_provider("twilio", has_telnyx=True, has_twilio=False) == "telnyx"


def test_gate_telnyx_preference_uses_telnyx():
    assert select_outbound_provider("telnyx", has_telnyx=True, has_twilio=True) == "telnyx"
    assert select_outbound_provider("telnyx", has_telnyx=False, has_twilio=True) == "twilio"


def test_gate_none_when_neither_configured():
    assert select_outbound_provider("twilio", has_telnyx=False, has_twilio=False) is None


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
