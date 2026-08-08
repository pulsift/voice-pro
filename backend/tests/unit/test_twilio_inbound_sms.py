"""Texts to the agent's number must stop vanishing.

`+16693694746` is the caller ID the voice agent dials prospects from, and its
`sms_url` at Twilio has never been set. A prospect who sees a missed call and
texts back reaches nothing: Twilio accepts the message and drops it, silently,
with nobody told. Five SMS rows exist in the database and every one is Telnyx.

The trap these tests exist for is not "does the HMAC work" — it is that Railway
terminates TLS, so `request.url` inside the app says `http://` while Twilio
signed `https://`. Rebuilding the signed string from the request fails EVERY
signature, which is indistinguishable from the security working correctly while
every inbound message is refused.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from twilio.request_validator import RequestValidator

from app.api import sms as sms_module
from app.core.config import settings
from app.db.session import get_db

TOKEN = "test-auth-token"
ORIGIN = "https://backend-production-7d1e.up.railway.app"
PATH = "/webhooks/twilio/sms"

INBOUND = {
    "MessageSid": "SM0123456789abcdef",
    # A deliberately fake SID. The real one is an account identifier, and GitHub's
    # push protection blocks it — correctly, since a fixture never needs it.
    "AccountSid": "ACfeedfacefeedfacefeedfacefeedface",
    "From": "+18053806275",
    "To": "+16693694746",
    "Body": "who just called me?",
    "NumMedia": "0",
}


class FakeSession:
    """Enough of AsyncSession for one insert and one lookup."""

    def __init__(self, existing: object | None = None) -> None:
        self.existing = existing
        self.added: list[object] = []
        self.commits = 0

    async def scalar(self, _statement: object) -> object | None:
        return self.existing

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commits += 1


@pytest.fixture
def session(monkeypatch: pytest.MonkeyPatch) -> FakeSession:
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", ORIGIN)
    return FakeSession()


@pytest.fixture
def client(session: FakeSession) -> TestClient:
    app = FastAPI()
    app.include_router(sms_module.webhook_router)

    async def _db() -> object:
        yield session

    app.dependency_overrides[get_db] = _db
    return TestClient(app)


def signed(params: dict[str, str], *, url: str = ORIGIN + PATH, token: str = TOKEN) -> str:
    return RequestValidator(token).compute_signature(url, params)


def post(client: TestClient, params: dict[str, str], signature: str | None = None):
    return client.post(
        PATH,
        data=params,
        headers={"X-Twilio-Signature": signature if signature is not None else signed(params)},
    )


# --- the message actually lands --------------------------------------------------


def test_a_signed_inbound_text_is_stored(client: TestClient, session: FakeSession) -> None:
    """THE point of the endpoint. Before it, this message did not exist anywhere."""
    response = post(client, INBOUND)

    assert response.status_code == 204
    assert len(session.added) == 1
    stored = session.added[0]
    assert stored.provider == "twilio"
    assert stored.direction == "inbound"
    assert stored.from_number == "+18053806275"
    assert stored.to_number == "+16693694746"
    assert stored.text == "who just called me?"
    assert stored.provider_message_id == "SM0123456789abcdef"
    assert session.commits == 1


def test_the_reply_body_is_empty_not_json(client: TestClient) -> None:
    """Twilio logs warning 12300 on every message whose response is not TwiML or
    empty — which pollutes the exact debugger you read when a webhook misbehaves."""
    response = post(client, INBOUND)
    assert response.status_code == 204
    assert response.content == b""


def test_the_raw_payload_is_kept_whole(client: TestClient, session: FakeSession) -> None:
    """Every other boundary in this system is pinned to a real captured payload.
    Keeping the raw form is what makes that possible for the first real text."""
    post(client, INBOUND)
    assert session.added[0].raw == INBOUND


def test_media_counts_are_recorded(client: TestClient, session: FakeSession) -> None:
    params = {**INBOUND, "NumMedia": "2", "MessageSid": "SMmedia"}
    post(client, params)
    assert session.added[0].num_media == 2


def test_a_missing_media_count_does_not_crash_the_webhook(
    client: TestClient, session: FakeSession
) -> None:
    """A dropped message is worse than a wrong count: Twilio gives up after
    retries and the text is gone for good."""
    params = {k: v for k, v in INBOUND.items() if k != "NumMedia"}
    assert post(client, params).status_code == 204
    assert session.added[0].num_media == 0


# --- the proxy trap --------------------------------------------------------------


def test_the_signature_is_checked_against_the_public_url_not_the_proxied_one(
    client: TestClient, session: FakeSession
) -> None:
    """THE regression this file exists for.

    Twilio signs `https://...`. Behind Railway's TLS termination the app sees
    `http://` and possibly an internal host. If the handler rebuilt the signed
    string from the request, this correctly-signed message would be rejected —
    and so would every other one, forever, looking like security all the while.
    """
    assert post(client, INBOUND).status_code == 204
    assert len(session.added) == 1, "a genuine Twilio message was refused"


def test_a_signature_computed_over_the_proxied_url_is_refused(client: TestClient) -> None:
    """The other direction: signing what the APP sees must not be accepted, or
    the check is validating the wrong string and proves nothing."""
    proxied = signed(INBOUND, url="http://testserver" + PATH)
    assert post(client, INBOUND, proxied).status_code == 403


def test_an_unconfigured_public_origin_refuses_rather_than_guessing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no canonical origin there is no honest way to verify. Guessing from
    the request is the bug; 503 says so out loud."""
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    monkeypatch.setattr(settings, "PUBLIC_URL", None)
    assert post(client, INBOUND).status_code == 503


def test_a_trailing_slash_on_the_origin_does_not_break_verification(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stray slash in an env var would otherwise silently kill every message."""
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", ORIGIN + "/")
    assert post(client, INBOUND).status_code == 204


# --- what is refused -------------------------------------------------------------


def test_an_unsigned_request_is_refused(client: TestClient, session: FakeSession) -> None:
    assert post(client, INBOUND, "").status_code == 403
    assert session.added == []


def test_a_wrong_signature_is_refused(client: TestClient, session: FakeSession) -> None:
    wrong = signed(INBOUND, token="someone-elses")  # noqa: S106 - a fixture value
    assert post(client, INBOUND, wrong).status_code == 403
    assert session.added == []


def test_a_tampered_body_is_refused(client: TestClient, session: FakeSession) -> None:
    """The signature covers the parameters, so changing one after signing must
    fail — otherwise anyone could forge a text from any number."""
    signature = signed(INBOUND)
    tampered = {**INBOUND, "Body": "yes please book me in"}
    assert post(client, tampered, signature).status_code == 403
    assert session.added == []


def test_an_extra_parameter_twilio_did_not_sign_is_refused(
    client: TestClient, session: FakeSession
) -> None:
    """Validation must cover EVERY received parameter, not a hand-picked list.
    An allowlist would also start rejecting real messages the day Twilio adds a
    field, which it does without notice."""
    signature = signed(INBOUND)
    assert post(client, {**INBOUND, "SmsStatus": "received"}, signature).status_code == 403
    assert session.added == []


def test_no_auth_token_refuses_rather_than_trusting_anything(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None)
    assert post(client, INBOUND).status_code == 503


# --- retries ---------------------------------------------------------------------


def test_a_retried_message_is_not_stored_twice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Twilio retries on any non-2xx or timeout, so a duplicate is ordinary
    traffic. Storing it twice splits one text into two conversation rows."""
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", TOKEN)
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", ORIGIN)
    already_there = FakeSession(existing=MagicMock(id="existing-row"))

    app = FastAPI()
    app.include_router(sms_module.webhook_router)

    async def _db() -> object:
        yield already_there

    app.dependency_overrides[get_db] = _db
    with TestClient(app) as duplicate_client:
        response = post(duplicate_client, INBOUND)

    assert response.status_code == 204
    assert already_there.added == [], "a retry created a second row"
