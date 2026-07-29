"""Contracts for public transcript share links + 30-day retention (B2).

The token is the only credential: it must be unguessable, minted exactly once, and
die together with the data it points at. The page itself is opened logged-out on a
phone, so it must be self-contained and must never echo raw call content into HTML.
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.calls import get_call
from app.api.telephony_ws import _merge_call_artifacts, save_transcript_to_call_record
from app.api.transcripts import (
    parse_transcript,
    public_transcript_page,
    purge_expired_transcripts,
    render_transcript_page,
)
from app.core.config import settings
from app.models.call_record import CallRecord
from app.services.call_events import build_call_ended_payload

TRANSCRIPT = "[User]: Hello?\n\n[Assistant]: Hi Ada, quick one about your solar leads."


def make_record(**overrides: Any) -> CallRecord:
    defaults: dict[str, Any] = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "provider": "twilio",
        "provider_call_id": "CA-share-1",
        "direction": "outbound",
        "status": "completed",
        "from_number": "+15550000001",
        "to_number": "+15551234567",
        "duration_seconds": 95,
        "started_at": datetime(2026, 7, 21, 14, 32, tzinfo=UTC),
        "answered_at": datetime(2026, 7, 21, 14, 32, tzinfo=UTC),
        "transcript": TRANSCRIPT,
        "share_token": "tr_AbCdEfGhIjKlMnOp",
    }
    defaults.update(overrides)
    return CallRecord(**defaults)


# ---------------------------------------------------------------------------
# Token minting
# ---------------------------------------------------------------------------


def test_share_token_minted_when_a_transcript_lands() -> None:
    record = MagicMock(transcript=None, booking_attempts=None, variables=None, share_token=None)

    changed = _merge_call_artifacts(record, TRANSCRIPT, None, None)

    assert changed is True
    assert record.share_token.startswith("tr_")
    # Base62 secret, long enough that guessing is not a strategy.
    assert re.fullmatch(r"tr_[A-Za-z0-9]{16}", record.share_token)


def test_share_token_is_never_rotated_on_a_later_save() -> None:
    record = MagicMock(
        transcript="[User]: short",
        booking_attempts=None,
        variables=None,
        share_token="tr_originaltoken01",  # noqa: S106 - fake token, not a secret
    )

    _merge_call_artifacts(record, TRANSCRIPT, None, None)

    assert record.share_token == "tr_originaltoken01"


def test_no_token_minted_without_a_transcript() -> None:
    record = MagicMock(transcript=None, booking_attempts=None, variables=None, share_token=None)

    changed = _merge_call_artifacts(record, "   ", [], None)

    assert changed is False
    assert record.share_token is None


@pytest.mark.asyncio
async def test_saving_a_transcript_persists_the_minted_token() -> None:
    record = MagicMock(
        id=uuid.uuid4(),
        transcript=None,
        booking_attempts=None,
        variables=None,
        share_token=None,
    )
    exact = MagicMock()
    exact.scalars.return_value.all.return_value = [record]
    db = MagicMock()
    db.execute = AsyncMock(return_value=exact)
    db.commit = AsyncMock()

    await save_transcript_to_call_record(
        "CA-share-1",
        TRANSCRIPT,
        db,
        MagicMock(),
        booking_attempts=[],
        owner_user_id=uuid.uuid4(),
        workspace_id=None,
        provider="twilio",
    )

    db.commit.assert_awaited_once_with()
    assert record.share_token.startswith("tr_")


# ---------------------------------------------------------------------------
# AMD verdict marking
# ---------------------------------------------------------------------------


def test_amd_verdict_merges_into_variables_without_clobbering() -> None:
    record = MagicMock(
        transcript=None,
        booking_attempts=None,
        variables={"leadName": "Ada"},
        share_token=None,
    )

    changed = _merge_call_artifacts(record, "", None, "machine-vm")

    assert changed is True
    assert record.variables == {"leadName": "Ada", "amd": "machine-vm"}


def test_amd_verdict_rewrite_is_a_noop_when_unchanged() -> None:
    record = MagicMock(
        transcript=None,
        booking_attempts=None,
        variables={"amd": "human"},
        share_token=None,
    )

    changed = _merge_call_artifacts(record, "", None, "human")

    assert changed is False


# ---------------------------------------------------------------------------
# call-ended payload
# ---------------------------------------------------------------------------


def test_payload_carries_transcript_url_and_voicemail_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://backend.test/")
    record = make_record(variables={"leadName": "Ada", "amd": "machine-vm"})

    payload = build_call_ended_payload(record)

    assert payload["transcript_url"] == (
        "https://backend.test/api/public/transcripts/tr_AbCdEfGhIjKlMnOp"
    )
    assert payload["voicemail"] is True
    # Existing contract untouched.
    assert payload["call_id"] == str(record.id)
    assert payload["variables"]["leadName"] == "Ada"


def test_payload_voicemail_false_for_a_human_answer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://backend.test")
    record = make_record(variables={"amd": "human"})

    payload = build_call_ended_payload(record)

    assert payload["voicemail"] is False


def test_payload_transcript_url_falls_back_to_public_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    monkeypatch.setattr(settings, "PUBLIC_URL", "https://webhooks.test")

    payload = build_call_ended_payload(make_record())

    assert payload["transcript_url"] == (
        "https://webhooks.test/api/public/transcripts/tr_AbCdEfGhIjKlMnOp"
    )


def test_payload_transcript_url_is_none_without_a_token_or_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://backend.test")
    assert build_call_ended_payload(make_record(share_token=None))["transcript_url"] is None

    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", None)
    monkeypatch.setattr(settings, "PUBLIC_URL", None)
    assert build_call_ended_payload(make_record())["transcript_url"] is None
    assert build_call_ended_payload(make_record())["voicemail"] is False


# ---------------------------------------------------------------------------
# Transcript parsing + page rendering
# ---------------------------------------------------------------------------


def test_parse_transcript_splits_speaker_turns() -> None:
    assert parse_transcript(TRANSCRIPT) == [
        ("user", "Hello?"),
        ("assistant", "Hi Ada, quick one about your solar leads."),
    ]


def test_parse_transcript_keeps_continuation_lines_with_their_turn() -> None:
    turns = parse_transcript("[User]: line one\nline two\n\n[Assistant]: reply")

    assert turns == [("user", "line one\nline two"), ("assistant", "reply")]


def test_parse_transcript_survives_unlabelled_and_empty_input() -> None:
    assert parse_transcript("no markers at all") == [("note", "no markers at all")]
    assert parse_transcript(None) == []
    assert parse_transcript("") == []


def test_page_is_self_contained_and_shows_masked_metadata() -> None:
    html_page = render_transcript_page(make_record())

    assert html_page.startswith("<!DOCTYPE html>")
    assert "<style>" in html_page
    # No external assets: nothing to block, nothing to leak a referrer to.
    assert "src=" not in html_page
    assert "<script" not in html_page
    assert "21 Jul 2026, 14:32 UTC" in html_page
    assert "1m 35s" in html_page
    # Only the last four digits of the number ever reach the page.
    assert "4567" in html_page
    assert "+15551234567" not in html_page
    assert "555123" not in html_page


def test_page_renders_both_speakers_as_bubbles() -> None:
    html_page = render_transcript_page(make_record())

    assert '<div class="turn user">' in html_page
    assert '<div class="turn assistant">' in html_page
    assert "Hi Ada, quick one about your solar leads." in html_page


def test_page_escapes_call_content() -> None:
    hostile = '[User]: <img src=x onerror="alert(1)"> & friends'

    html_page = render_transcript_page(make_record(transcript=hostile))

    assert "<img" not in html_page
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; friends" in html_page


def test_page_handles_an_empty_transcript() -> None:
    html_page = render_transcript_page(make_record(transcript=""))

    assert "No conversation was captured" in html_page


# ---------------------------------------------------------------------------
# Public endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_known_token_serves_the_page_without_auth() -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = make_record()
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    response = await public_transcript_page("tr_AbCdEfGhIjKlMnOp", db)

    assert response.status_code == 200
    assert b"Call transcript" in response.body
    # The lookup is by token alone - no user scope, no session.
    sql = str(db.execute.await_args.args[0].compile())
    assert "call_records.share_token" in sql


@pytest.mark.asyncio
async def test_unknown_token_gets_a_plain_404_page() -> None:
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    response = await public_transcript_page("tr_doesnotexist01", db)

    assert response.status_code == 404
    assert b"Transcript unavailable" in response.body
    # An expired link and a forged link are indistinguishable.
    assert b"retention policy" in response.body


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_sweep_nulls_transcript_token_and_recording(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "TRANSCRIPT_RETENTION_DAYS", 30)
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(rowcount=3))
    db.commit = AsyncMock()

    purged = await purge_expired_transcripts(db)

    assert purged == 3
    db.commit.assert_awaited_once_with()
    compiled = db.execute.await_args.args[0].compile()
    sql = str(compiled)
    assert "UPDATE call_records" in sql
    for column in ("transcript", "share_token", "recording_url"):
        assert f"{column}=" in sql.replace(" = ", "=")
        assert compiled.params[column] is None
    assert "call_records.ended_at <" in sql

    cutoff = next(
        value
        for key, value in compiled.params.items()
        if key.startswith("ended_at") and isinstance(value, datetime)
    )
    expected = datetime.now(UTC) - timedelta(days=30)
    assert abs((cutoff - expected).total_seconds()) < 60


# ---------------------------------------------------------------------------
# Dashboard surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_call_api_exposes_per_call_variables() -> None:
    call_id = uuid.uuid4()
    record = MagicMock(
        id=call_id,
        provider="twilio",
        provider_call_id="CA-share-1",
        agent_id=None,
        agent=None,
        contact_id=None,
        contact=None,
        workspace_id=None,
        workspace=None,
        direction="outbound",
        status="completed",
        from_number="+15550000001",
        to_number="+15551234567",
        duration_seconds=95,
        recording_url=None,
        transcript=TRANSCRIPT,
        booking_attempts=None,
        variables={"leadName": "Ada", "amd": "human"},
        started_at=datetime.now(UTC),
        answered_at=None,
        ended_at=None,
    )
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = record
    db = MagicMock()
    db.execute = AsyncMock(return_value=query_result)

    response = await get_call(str(call_id), MagicMock(id=1), db)

    assert response.variables == {"leadName": "Ada", "amd": "human"}
