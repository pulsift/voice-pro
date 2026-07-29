"""Outbound SMS provider routing: Telnyx numbers -> Telnyx, Twilio numbers -> Twilio SDK."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from twilio.base.exceptions import TwilioRestException

from app.api import sms
from app.api.sms import SendSmsRequest
from app.models.phone_number import PhoneNumber

TELNYX_NUMBER = "+13334445555"
TWILIO_NUMBER = "+12223334444"


def _phone_row(number: str, provider: str) -> PhoneNumber:
    return PhoneNumber(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        phone_number=number,
        provider=provider,
        provider_id="pid",
        can_send_sms=True,
        status="active",
    )


def _mock_db(phone_row: PhoneNumber | None) -> MagicMock:
    """A DB stub that resolves the from-number lookup and fakes flush defaults."""
    db = MagicMock()
    db.scalar = AsyncMock(return_value=phone_row)
    db.commit = AsyncMock()

    def _add(obj: object) -> None:
        # SQLAlchemy would populate these at flush; the response model needs them.
        obj.id = uuid.uuid4()  # type: ignore[attr-defined]
        obj.created_at = datetime.now(UTC)  # type: ignore[attr-defined]

    db.add = MagicMock(side_effect=_add)
    return db


def _twilio_client(monkeypatch, *, message_sid: str = "SM123", raises: Exception | None = None):
    """Patch app.api.sms.Client with a mock; return the mock client instance."""
    message = MagicMock()
    message.sid = message_sid
    message.status = "queued"

    client = MagicMock()
    if raises is not None:
        client.messages.create.side_effect = raises
    else:
        client.messages.create.return_value = message

    monkeypatch.setattr(sms, "Client", MagicMock(return_value=client))
    return client


def _telnyx_tools(monkeypatch, result: dict | None = None):
    """Patch app.api.sms.TelnyxSMSTools; return the mock class."""
    tools = MagicMock()
    tools.send_sms = AsyncMock(
        return_value=result or {"success": True, "message_id": "telnyx-msg-1"}
    )
    tools.close = AsyncMock()
    cls = MagicMock(return_value=tools)
    monkeypatch.setattr(sms, "TelnyxSMSTools", cls)
    return cls


def _twilio_env(monkeypatch, sid: str | None = "AC_env", auth: str | None = "tok_env") -> None:
    monkeypatch.setattr(sms, "get_user_api_keys", AsyncMock(return_value=None))
    monkeypatch.setattr(sms.settings, "TWILIO_ACCOUNT_SID", sid, raising=False)
    monkeypatch.setattr(sms.settings, "TWILIO_AUTH_TOKEN", auth, raising=False)


def _telnyx_env(monkeypatch, key: str | None = "telnyx_env_key") -> None:
    monkeypatch.setattr(sms, "get_user_api_keys", AsyncMock(return_value=None))
    monkeypatch.setattr(sms.settings, "TELNYX_API_KEY", key, raising=False)


@pytest.fixture
def user() -> MagicMock:
    u = MagicMock()
    u.id = 1
    return u


# --- routing ---------------------------------------------------------------


async def test_twilio_number_sends_via_twilio_sdk(monkeypatch, user):
    _twilio_env(monkeypatch)
    client = _twilio_client(monkeypatch)
    telnyx_cls = _telnyx_tools(monkeypatch)
    db = _mock_db(_phone_row(TWILIO_NUMBER, "twilio"))

    await sms.send_sms(
        payload=SendSmsRequest(to="+15551230000", body="hi", from_number=TWILIO_NUMBER),
        current_user=user,
        db=db,
    )

    client.messages.create.assert_called_once_with(
        to="+15551230000", from_=TWILIO_NUMBER, body="hi"
    )
    telnyx_cls.assert_not_called()

    stored = db.add.call_args[0][0]
    assert stored.provider == "twilio"
    assert stored.provider_message_id == "SM123"
    assert stored.direction == "outbound"
    assert stored.from_number == TWILIO_NUMBER
    assert stored.to_number == "+15551230000"
    db.commit.assert_awaited_once()


async def test_telnyx_number_keeps_old_path(monkeypatch, user):
    _telnyx_env(monkeypatch)
    client = _twilio_client(monkeypatch)
    telnyx_cls = _telnyx_tools(monkeypatch)
    db = _mock_db(_phone_row(TELNYX_NUMBER, "telnyx"))

    await sms.send_sms(
        payload=SendSmsRequest(to="+15551230000", body="hi", from_number=TELNYX_NUMBER),
        current_user=user,
        db=db,
    )

    telnyx_cls.assert_called_once_with(api_key="telnyx_env_key", from_number=TELNYX_NUMBER)
    telnyx_cls.return_value.send_sms.assert_awaited_once_with(to="+15551230000", body="hi")
    client.messages.create.assert_not_called()

    stored = db.add.call_args[0][0]
    assert stored.provider == "telnyx"
    assert stored.provider_message_id == "telnyx-msg-1"


async def test_unregistered_number_defaults_to_telnyx(monkeypatch, user):
    _telnyx_env(monkeypatch)
    _twilio_client(monkeypatch)
    telnyx_cls = _telnyx_tools(monkeypatch)
    db = _mock_db(None)

    await sms.send_sms(
        payload=SendSmsRequest(to="+15551230000", body="hi", from_number="+19998887777"),
        current_user=user,
        db=db,
    )

    telnyx_cls.assert_called_once()
    assert db.add.call_args[0][0].provider == "telnyx"


async def test_unknown_provider_is_a_400(monkeypatch, user):
    _twilio_env(monkeypatch)
    client = _twilio_client(monkeypatch)
    telnyx_cls = _telnyx_tools(monkeypatch)
    db = _mock_db(_phone_row("+14445556666", "vonage"))

    with pytest.raises(HTTPException) as exc:
        await sms.send_sms(
            payload=SendSmsRequest(to="+15551230000", body="hi", from_number="+14445556666"),
            current_user=user,
            db=db,
        )

    assert exc.value.status_code == 400
    assert "vonage" in exc.value.detail
    client.messages.create.assert_not_called()
    telnyx_cls.assert_not_called()
    db.add.assert_not_called()


# --- errors ----------------------------------------------------------------


async def test_twilio_rest_error_maps_to_telnyx_error_shape(monkeypatch, user):
    """A Twilio 4xx must surface as the same 502 + detail the Telnyx path returns."""
    _twilio_env(monkeypatch)
    _twilio_client(
        monkeypatch,
        raises=TwilioRestException(
            400, "https://api.twilio.com", "The 'To' number is invalid", 21211
        ),
    )
    _telnyx_tools(monkeypatch)
    db = _mock_db(_phone_row(TWILIO_NUMBER, "twilio"))

    with pytest.raises(HTTPException) as exc:
        await sms.send_sms(
            payload=SendSmsRequest(to="bogus", body="hi", from_number=TWILIO_NUMBER),
            current_user=user,
            db=db,
        )

    assert exc.value.status_code == 502
    assert "The 'To' number is invalid" in exc.value.detail
    assert "21211" in exc.value.detail
    db.add.assert_not_called()


async def test_missing_twilio_credentials_is_a_400(monkeypatch, user):
    _twilio_env(monkeypatch, sid=None, auth=None)
    client = _twilio_client(monkeypatch)
    db = _mock_db(_phone_row(TWILIO_NUMBER, "twilio"))

    with pytest.raises(HTTPException) as exc:
        await sms.send_sms(
            payload=SendSmsRequest(to="+15551230000", body="hi", from_number=TWILIO_NUMBER),
            current_user=user,
            db=db,
        )

    assert exc.value.status_code == 400
    assert "Twilio" in exc.value.detail
    client.messages.create.assert_not_called()


# --- default from-number ---------------------------------------------------


async def test_default_from_number_falls_back_to_twilio(monkeypatch, user):
    """No SMS-capable Telnyx number registered -> a Twilio number is picked."""
    _twilio_env(monkeypatch)
    client = _twilio_client(monkeypatch)
    _telnyx_tools(monkeypatch)
    twilio_row = _phone_row(TWILIO_NUMBER, "twilio")
    db = _mock_db(None)
    # telnyx default lookup -> none, twilio default lookup -> row, provider lookup -> row
    db.scalar = AsyncMock(side_effect=[None, twilio_row, twilio_row])

    await sms.send_sms(
        payload=SendSmsRequest(to="+15551230000", body="hi"), current_user=user, db=db
    )

    assert client.messages.create.call_args.kwargs["from_"] == TWILIO_NUMBER


async def test_default_from_number_prefers_telnyx(monkeypatch, user):
    _telnyx_env(monkeypatch)
    _twilio_client(monkeypatch)
    telnyx_cls = _telnyx_tools(monkeypatch)
    telnyx_row = _phone_row(TELNYX_NUMBER, "telnyx")
    db = _mock_db(telnyx_row)

    await sms.send_sms(
        payload=SendSmsRequest(to="+15551230000", body="hi"), current_user=user, db=db
    )

    assert telnyx_cls.call_args.kwargs["from_number"] == TELNYX_NUMBER


async def test_no_sendable_number_is_a_400(monkeypatch, user):
    _telnyx_env(monkeypatch)
    db = _mock_db(None)

    with pytest.raises(HTTPException) as exc:
        await sms.send_sms(
            payload=SendSmsRequest(to="+15551230000", body="hi"), current_user=user, db=db
        )

    assert exc.value.status_code == 400
    assert "No SMS-capable phone number available" in exc.value.detail
