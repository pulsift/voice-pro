"""Call recording: legal-consent policy, Twilio wiring, and the recording webhook.

The policy is fail-safe OFF by design — every one of these tests exists to pin that
down, because the failure mode we're guarding against (recording a two-party-consent
state) is a legal exposure, not a missing feature.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api.telephony import resolve_recording_flag, twilio_recording_callback
from app.core.config import settings
from app.services.telephony import recording_policy
from app.services.telephony.recording_policy import (
    ALL_PARTY_CONSENT_STATES,
    AREA_CODE_TO_STATE,
    recording_allowed,
    recording_decision,
    state_for_number,
)
from app.services.telephony.twilio_service import TwilioService

# ---------------------------------------------------------------------------
# The consent map itself
# ---------------------------------------------------------------------------


EXPECTED_ALL_PARTY = frozenset(
    {"CA", "CT", "DE", "FL", "IL", "MD", "MA", "MI", "MT", "NV", "NH", "OR", "PA", "WA"}
)


def test_all_party_set_is_the_conservative_list():
    assert EXPECTED_ALL_PARTY == ALL_PARTY_CONSENT_STATES


def test_every_all_party_state_actually_appears_in_the_area_code_map():
    # A consent state with no area codes mapped would be silently unenforceable.
    mapped_states = set(AREA_CODE_TO_STATE.values())
    assert mapped_states >= ALL_PARTY_CONSENT_STATES


def test_area_code_map_is_well_formed():
    for npa, state in AREA_CODE_TO_STATE.items():
        assert len(npa) == 3, npa
        assert npa.isdigit(), npa
        assert npa[0] not in "01", npa  # valid NANP area codes start 2-9
        assert len(state) == 2, state
        assert state.isupper(), state


# ---------------------------------------------------------------------------
# recording_allowed: the one-line contract
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "state"),
    [
        ("+12125550123", "NY"),  # New York City
        ("+16025550123", "AZ"),  # Phoenix
        ("+12145550123", "TX"),  # Dallas
        ("+13035550123", "CO"),  # Denver
        ("+14045550123", "GA"),  # Atlanta
        ("+16155550123", "TN"),  # Nashville
    ],
)
def test_one_party_consent_states_allow_recording(number, state):
    assert state_for_number(number) == state
    assert recording_allowed(number) is True


@pytest.mark.parametrize(
    ("number", "state"),
    [
        ("+14155550123", "CA"),  # San Francisco
        ("+13105550123", "CA"),  # Los Angeles
        ("+13055550123", "FL"),  # Miami
        ("+13125550123", "IL"),  # Chicago
        ("+12065550123", "WA"),  # Seattle
        ("+16175550123", "MA"),  # Boston
        ("+12155550123", "PA"),  # Philadelphia
        ("+13135550123", "MI"),  # Detroit
        ("+17025550123", "NV"),  # Las Vegas
        ("+15035550123", "OR"),  # Portland
        ("+12035550123", "CT"),
        ("+13025550123", "DE"),
        ("+14105550123", "MD"),
        ("+14065550123", "MT"),
        ("+16035550123", "NH"),
    ],
)
def test_all_party_consent_states_deny_recording(number, state):
    assert state_for_number(number) == state
    assert recording_allowed(number) is False


@pytest.mark.parametrize(
    "number",
    [
        "+442071234567",  # UK
        "+4420712345",  # UK, 10 digits after + — must NOT be read as NPA 442 (CA)
        "+61255501234",  # Australia
        "+14165550123",  # Toronto, Canada — NANP but not a US state
        "+16045550123",  # Vancouver, Canada
        "+17875550123",  # Puerto Rico — US territory, deliberately unmapped
        "+18005550123",  # toll-free, no geography
        "+18885550123",  # toll-free
        "+19005550123",  # premium
        "+19995550123",  # unassigned area code
    ],
)
def test_unknown_or_foreign_numbers_deny_recording(number):
    assert recording_allowed(number) is False


@pytest.mark.parametrize(
    "number",
    [
        "",
        "   ",
        "+1",
        "555",
        "212555",  # too short
        "+1212555012",  # 9 national digits
        "+121255501234",  # 11 national digits
        "not-a-number",
        "+1abc5550123",
        "+11125550123",  # area code starts with 1 — invalid NANP
        "+12120550123",  # exchange starts with 0 — invalid NANP
    ],
)
def test_unparseable_numbers_deny_recording(number):
    assert recording_allowed(number) is False


def test_none_input_denies_recording():
    assert recording_allowed(None) is False  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "number",
    [
        "+12125550123",
        "12125550123",
        "2125550123",
        "(212) 555-0123",
        "212-555-0123",
        " 212.555.0123 ",
    ],
)
def test_common_us_formats_all_parse_to_the_same_answer(number):
    assert state_for_number(number) == "NY"
    assert recording_allowed(number) is True


# ---------------------------------------------------------------------------
# recording_decision: the loggable reason
# ---------------------------------------------------------------------------


def test_decision_reports_state_and_reason():
    assert recording_decision("+12125550123") == (True, "NY", recording_policy.REASON_ONE_PARTY)
    assert recording_decision("+14155550123") == (False, "CA", recording_policy.REASON_ALL_PARTY)
    assert recording_decision("+14165550123") == (
        False,
        None,
        recording_policy.REASON_UNKNOWN_AREA_CODE,
    )
    assert recording_decision("garbage") == (False, None, recording_policy.REASON_UNPARSEABLE)


# ---------------------------------------------------------------------------
# The route-level gate: agent toggle AND consent policy AND kill switch
# ---------------------------------------------------------------------------


def test_gate_records_only_when_agent_wants_it_and_the_state_allows_it():
    record, state, reason = resolve_recording_flag(agent_enabled=True, to_number="+12125550123")
    assert record is True
    assert state == "NY"
    assert reason == recording_policy.REASON_ONE_PARTY


def test_gate_agent_toggle_is_no_longer_dead_but_cannot_override_the_law():
    # The toggle is now real...
    assert resolve_recording_flag(agent_enabled=False, to_number="+12125550123")[0] is False
    # ...but it never beats a two-party-consent state.
    record, state, reason = resolve_recording_flag(agent_enabled=True, to_number="+14155550123")
    assert record is False
    assert state == "CA"
    assert reason == recording_policy.REASON_ALL_PARTY


def test_gate_denies_unknown_numbers_even_when_the_agent_wants_recording():
    assert resolve_recording_flag(agent_enabled=True, to_number="+442071234567")[0] is False
    assert resolve_recording_flag(agent_enabled=True, to_number="+14165550123")[0] is False
    assert resolve_recording_flag(agent_enabled=True, to_number="")[0] is False


def test_gate_platform_kill_switch_beats_everything(monkeypatch):
    monkeypatch.setattr(settings, "CALL_RECORDING_ENABLED", False)
    record, state, _reason = resolve_recording_flag(agent_enabled=True, to_number="+12125550123")
    assert record is False
    assert state == "NY"  # the reason still reports the consent verdict


# ---------------------------------------------------------------------------
# TwilioService.initiate_call passes the recording kwargs through
# ---------------------------------------------------------------------------


def _twilio_with_mock_client() -> tuple[TwilioService, MagicMock]:
    service = TwilioService("AC_test", "token_test")
    client = MagicMock()
    client.calls.create.return_value = MagicMock(sid="CA-recorded-1")
    service.client = client
    return service, client


@pytest.mark.asyncio
async def test_initiate_call_passes_recording_kwargs_when_asked():
    service, client = _twilio_with_mock_client()

    await service.initiate_call(
        to_number="+12125550123",
        from_number="+12125550199",
        webhook_url="https://api.test/webhooks/twilio/answer?agent_id=a1",
        agent_id="a1",
        record=True,
        recording_callback_url="https://api.test/webhooks/twilio/recording",
    )

    kwargs = client.calls.create.call_args.kwargs
    assert kwargs["record"] is True
    assert kwargs["recording_channels"] == "dual"
    assert kwargs["recording_status_callback"] == "https://api.test/webhooks/twilio/recording"
    assert kwargs["recording_status_callback_event"] == ["completed"]
    # The pre-existing dial parameters must be untouched.
    assert kwargs["to"] == "+12125550123"
    assert kwargs["from_"] == "+12125550199"
    assert kwargs["status_callback"] == "https://api.test/webhooks/twilio/status?agent_id=a1"


@pytest.mark.asyncio
async def test_initiate_call_omits_recording_kwargs_by_default():
    # Backward compatibility: the old 4-arg call must dial exactly as before.
    service, client = _twilio_with_mock_client()

    info = await service.initiate_call(
        to_number="+12125550123",
        from_number="+12125550199",
        webhook_url="https://api.test/webhooks/twilio/answer",
        agent_id="a1",
    )

    kwargs = client.calls.create.call_args.kwargs
    assert "record" not in kwargs
    assert "recording_channels" not in kwargs
    assert "recording_status_callback" not in kwargs
    assert info.call_id == "CA-recorded-1"


@pytest.mark.asyncio
async def test_initiate_call_with_record_false_omits_recording_kwargs():
    service, client = _twilio_with_mock_client()

    await service.initiate_call(
        to_number="+14155550123",
        from_number="+12125550199",
        webhook_url="https://api.test/webhooks/twilio/answer",
        agent_id="a1",
        record=False,
        recording_callback_url=None,
    )

    assert "record" not in client.calls.create.call_args.kwargs


# ---------------------------------------------------------------------------
# The recording webhook writes CallRecord.recording_url
# ---------------------------------------------------------------------------


def _db_returning(record):
    result = MagicMock()
    result.scalar_one_or_none.return_value = record
    return MagicMock(execute=AsyncMock(return_value=result), commit=AsyncMock())


async def _run_recording_callback(record, **overrides):
    db = _db_returning(record)
    params = {
        "recording_sid": "RE-1",
        "recording_url": "https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1",
        "recording_status": "completed",
        "call_sid": "CA-rec-1",
    }
    params.update(overrides)
    with patch("app.api.telephony.verify_twilio_webhook", AsyncMock()):
        response = await twilio_recording_callback(request=MagicMock(), db=db, **params)
    return response, db


@pytest.mark.asyncio
async def test_recording_callback_writes_mp3_url_to_call_record():
    record = MagicMock(id=uuid.uuid4(), recording_url=None)
    response, db = await _run_recording_callback(record)

    assert response == {"status": "received"}
    assert (
        record.recording_url == "https://api.twilio.com/2010-04-01/Accounts/AC1/Recordings/RE1.mp3"
    )
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_recording_callback_acknowledges_unknown_call_sid_without_raising():
    # An unknown CallSid must NOT 4xx/5xx — Twilio would retry-spam the dead callback.
    response, db = await _run_recording_callback(None, call_sid="CA-does-not-exist")

    assert response == {"status": "ignored"}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), [("call_sid", ""), ("recording_url", "")])
async def test_recording_callback_ignores_incomplete_payloads(field, value):
    record = MagicMock(id=uuid.uuid4(), recording_url=None)
    response, db = await _run_recording_callback(record, **{field: value})

    assert response == {"status": "ignored"}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_recording_callback_requires_a_valid_twilio_signature():
    from fastapi import HTTPException

    db = _db_returning(MagicMock())
    with (
        patch(
            "app.api.telephony.verify_twilio_webhook",
            AsyncMock(
                side_effect=HTTPException(status_code=403, detail="Invalid Twilio signature")
            ),
        ),
        pytest.raises(HTTPException) as exc,
    ):
        await twilio_recording_callback(
            request=MagicMock(),
            db=db,
            recording_sid="RE-1",
            recording_url="https://api.twilio.com/rec/RE1",
            recording_status="completed",
            call_sid="CA-rec-1",
        )

    assert exc.value.status_code == 403
    db.execute.assert_not_awaited()


# --- explicit consent outranks the geographic approximation -------------------
# Geography is only ever a PROXY for consent, and it cannot express "this person
# said yes" — which is what the law actually asks for. The allowlist can, and it is
# how our own handsets get recorded so the agent can be listened to and diagnosed.


def test_consenting_number_is_recordable_even_where_geography_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swedish = "+46700171894"
    californian = "+14155550123"
    monkeypatch.setattr(settings, "RECORDING_CONSENT_NUMBERS", f"{swedish},{californian}")

    allowed, state, reason = recording_decision(swedish)
    assert (allowed, state, reason) == (
        True,
        None,
        recording_policy.REASON_EXPLICIT_CONSENT,
    )
    # Even an all-party state yields to an actual consenting party.
    assert recording_allowed(californian) is True


def test_cosmetic_formatting_is_forgiven_but_the_number_must_be_the_same(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "RECORDING_CONSENT_NUMBERS", "+46 700 17 18 94")
    assert recording_policy.has_explicit_consent("+46700171894") is True
    assert recording_policy.has_explicit_consent("0046700171894") is True  # 00 prefix
    # The national trunk form is NOT accepted: it is a different digit string, and
    # guessing at country codes is how a legality gate starts saying yes by accident.
    assert recording_policy.has_explicit_consent("0700171894") is False


def test_two_real_numbers_sharing_their_last_digits_do_not_collide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug this pins: +1 415 555 0123 and +1 515 555 0123 are DIFFERENT people's
    phones that share their last nine digits — the leading area-code digit simply
    falls off. A suffix rule would let one consenting number authorise recording a
    stranger, inside the function that decides whether recording is legal."""
    monkeypatch.setattr(settings, "RECORDING_CONSENT_NUMBERS", "+14155550123")
    assert recording_policy.has_explicit_consent("+14155550123") is True
    assert recording_policy.has_explicit_consent("+15155550123") is False
    assert recording_policy.has_explicit_consent("+13466317856") is False


def test_short_or_empty_allowlist_entries_can_never_match_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "RECORDING_CONSENT_NUMBERS", "7855, , 1, +")
    assert recording_policy.has_explicit_consent("+13466317855") is False


def test_unset_allowlist_leaves_the_policy_exactly_as_it_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "RECORDING_CONSENT_NUMBERS", "")
    assert recording_allowed("+14155550123") is False  # CA, all-party
    assert recording_allowed("+46700171894") is False  # non-NANP
    assert recording_allowed("+12125550123") is True  # NY, one-party


# ---------------------------------------------------------------------------
# The agent must not know calls are ever recorded
# ---------------------------------------------------------------------------
#
# Sami's ruling, 2026-08-07, in his words: exclude it "(1) to prevent issues
# from VA having unnecessary context and (2) having it hallucinate and give a
# dangerous response". Recording stays a carrier-side decision made from the
# consent map above; the agent is never told, never asked to disclose, and has
# no fallback line about it. This is the guard that keeps a future edit from
# leaking it back in — a one-line addition to the prompt would otherwise be
# invisible until a prospect heard it.

# Phrases, not the bare stem: `record_fit_answers` writes down what the lead
# said about their business and has nothing to do with audio.
_RECORDING_PATTERNS = (
    r"record\w*\s+(?:this|the|your|our|every|each|all)?\s*calls?\b",
    r"calls?\s+(?:is|are|may|might|will|can)\s+(?:be\s+)?(?:being\s+)?record",
    r"\bbeing\s+record",
    r"\btranscrib\w*",
    r"\btranscripts?\b",
    r"\bmonitored\b",
    r"this call may be",
    r"quality and training",
)


def _recording_leaks(text: str) -> list[str]:
    import re

    lowered = text.lower()
    return [p for p in _RECORDING_PATTERNS if re.search(p, lowered)]


def _every_word_the_agent_could_read() -> list[tuple[str, str]]:
    """(where it came from, the text) for everything that reaches the model."""
    from pathlib import Path

    from app.services.gpt_realtime import build_instructions_with_language
    from app.services.tools.call_control_tools import CallControlTools
    from app.services.tools.crm_tools import CRMTools

    prompt_path = Path(__file__).resolve().parents[2] / "app/prompts/pulsift_booker.md"
    sources = [("pulsift_booker.md", prompt_path.read_text(encoding="utf-8"))]
    sources.append((
        "the instruction wrapper",
        build_instructions_with_language("", "en-US", timezone="UTC"),
    ))
    sources.append((
        "the timezone-unresolved override",
        build_instructions_with_language(
            "", "en-US", timezone="unresolved",
            runtime_rules=["TIMEZONE CORRECTION OVERRIDE: placeholder"],
        ),
    ))
    for definition in CRMTools.get_tool_definitions() + CallControlTools.get_tool_definitions():
        sources.append((f"tool {definition.get('name')}", str(definition)))
    return sources


def test_the_agent_is_never_told_that_calls_are_recorded():
    offences = [
        (where, leak)
        for where, text in _every_word_the_agent_could_read()
        for leak in _recording_leaks(text)
    ]
    assert not offences, (
        "the voice agent must have zero knowledge of call recording; found "
        f"{offences}"
    )


def test_the_guard_would_notice():
    """A guard nobody has watched go red is decoration."""
    assert _recording_leaks("This call may be recorded for quality and training.")
    assert _recording_leaks("Heads up, I'm recording this call.")
    assert _recording_leaks("I'll send you the transcript afterwards.")
    # ...and the business tool it must not trip over.
    assert not _recording_leaks(
        "record_fit_answers: save what the lead told you about their business"
    )
