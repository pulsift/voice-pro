"""Contracts for credential-free recording playback.

A Twilio recording URL is protected by HTTP Basic auth using the ACCOUNT
credentials, so a browser opening one pops a username/password box that no human
login can satisfy — the only accepted password is the account auth token, which
must never be typed into a browser prompt. The fix is a proxy on the same share
token that serves the transcript: we fetch with the credentials we already hold
and pipe the audio back.

What must stay true:
  - the token is the only credential, and it expires with the transcript;
  - our credentials go to Twilio's media hosts and NOWHERE else;
  - a missing recording, missing credentials or an upstream error degrade to a
    plain 404 page, never to an error the reader has to interpret;
  - the dashboard never points an <audio> element at the provider URL.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.api import transcripts as transcripts_api
from app.api.transcripts import public_call_recording, render_transcript_page
from app.core.config import settings
from app.services.call_events import build_recording_url

TOKEN = "tr_AbCdEfGhIjKlMnOp"
TWILIO_URL = (
    "https://api.twilio.com/2010-04-01/Accounts/AC123/Recordings/RE456.mp3"
)


def make_record(**overrides: Any) -> MagicMock:
    record = MagicMock()
    record.id = uuid.uuid4()
    record.share_token = TOKEN
    record.recording_url = TWILIO_URL
    record.transcript = "[User]: Hello?\n\n[Assistant]: Hi there."
    record.to_number = "+15551234567"
    record.duration_seconds = 95
    record.started_at = datetime(2026, 7, 21, 14, 32, tzinfo=UTC)
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


def make_db(record: Any) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = record
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    return db


def make_request(headers: dict[str, str] | None = None) -> MagicMock:
    request = MagicMock()
    request.headers = headers or {}
    return request


class FakeUpstream:
    """Stands in for httpx.AsyncClient, recording how it was called."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *_args: Any, **_kwargs: Any) -> "FakeUpstream":
        return self

    async def __aenter__(self) -> "FakeUpstream":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def audio_response(status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        content=b"ID3-fake-mp3-bytes",
        headers={"content-type": "audio/mpeg", "content-length": "18", **(headers or {})},
        request=httpx.Request("GET", TWILIO_URL),
    )


@pytest.fixture(autouse=True)
def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TWILIO_ACCOUNT_SID", "AC123", raising=False)
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", "super-secret", raising=False)


@pytest.mark.asyncio
async def test_the_token_alone_plays_the_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = FakeUpstream(audio_response())
    monkeypatch.setattr(transcripts_api.httpx, "AsyncClient", upstream)

    response = await public_call_recording(TOKEN, make_request(), make_db(make_record()))

    assert response.status_code == 200
    assert response.media_type == "audio/mpeg"
    assert response.body == b"ID3-fake-mp3-bytes"
    # Fetched with OUR credentials, so the listener is never asked for any.
    assert upstream.calls[0]["auth"] == ("AC123", "super-secret")


@pytest.mark.asyncio
async def test_credentials_never_leave_for_a_host_we_do_not_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = FakeUpstream(audio_response())
    monkeypatch.setattr(transcripts_api.httpx, "AsyncClient", upstream)
    record = make_record(recording_url="https://evil.example/RE456.mp3")

    response = await public_call_recording(TOKEN, make_request(), make_db(record))

    assert response.status_code == 404
    assert upstream.calls == []  # nothing was fetched, so nothing was sent


@pytest.mark.asyncio
async def test_range_requests_are_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """iOS asks for a byte range before it will play anything at all."""
    partial = audio_response(status=206, headers={"content-range": "bytes 0-17/18",
                                                  "accept-ranges": "bytes"})
    upstream = FakeUpstream(partial)
    monkeypatch.setattr(transcripts_api.httpx, "AsyncClient", upstream)

    response = await public_call_recording(
        TOKEN, make_request({"range": "bytes=0-17"}), make_db(make_record()))

    assert response.status_code == 206
    assert upstream.calls[0]["headers"] == {"Range": "bytes=0-17"}
    assert response.headers["content-range"] == "bytes 0-17/18"


@pytest.mark.asyncio
async def test_an_expired_or_forged_token_gets_the_same_plain_page() -> None:
    response = await public_call_recording(TOKEN, make_request(), make_db(None))

    assert response.status_code == 404
    assert b"Transcript unavailable" in response.body


@pytest.mark.asyncio
async def test_a_call_with_no_recording_is_not_an_error_page_with_detail() -> None:
    record = make_record(recording_url=None)

    response = await public_call_recording(TOKEN, make_request(), make_db(record))

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_missing_credentials_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "TWILIO_AUTH_TOKEN", None, raising=False)
    upstream = FakeUpstream(audio_response())
    monkeypatch.setattr(transcripts_api.httpx, "AsyncClient", upstream)

    response = await public_call_recording(TOKEN, make_request(), make_db(make_record()))

    assert response.status_code == 404
    assert upstream.calls == []


@pytest.mark.asyncio
async def test_upstream_failure_never_surfaces_provider_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = FakeUpstream(httpx.ConnectError("boom"))
    monkeypatch.setattr(transcripts_api.httpx, "AsyncClient", upstream)

    response = await public_call_recording(TOKEN, make_request(), make_db(make_record()))

    assert response.status_code == 502
    assert b"boom" not in response.body


@pytest.mark.asyncio
async def test_an_upstream_4xx_reads_as_a_dead_link(monkeypatch: pytest.MonkeyPatch) -> None:
    upstream = FakeUpstream(audio_response(status=404))
    monkeypatch.setattr(transcripts_api.httpx, "AsyncClient", upstream)

    response = await public_call_recording(TOKEN, make_request(), make_db(make_record()))

    assert response.status_code == 404
    assert b"Transcript unavailable" in response.body


def test_the_transcript_page_offers_the_recording_on_the_same_token() -> None:
    page = render_transcript_page(make_record())

    assert f'src="{TOKEN}/recording"' in page
    # The provider URL never reaches the reader's browser.
    assert "api.twilio.com" not in page


def test_a_call_without_a_recording_shows_no_player() -> None:
    page = render_transcript_page(make_record(recording_url=None))

    assert "<audio" not in page


def test_the_playback_link_is_built_from_the_public_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://voice.example", raising=False)

    assert build_recording_url(TOKEN) == (
        f"https://voice.example/api/public/transcripts/{TOKEN}/recording")
    assert build_recording_url(None) is None
