"""Deriving the lead's timezone instead of asking for it on the call.

Before this existed the router passed no timezone at all, so every US lead was
offered slots computed in Europe/Stockholm unless the agent spent a turn asking.
"""

import pytest

from app.core.config import settings
from app.services import lead_timezone


def test_area_code_gives_the_state_and_its_zone() -> None:
    assert lead_timezone.timezone_for_number("+14155550123") == "America/Los_Angeles"  # CA
    assert lead_timezone.timezone_for_number("+12125550123") == "America/New_York"  # NY
    assert lead_timezone.timezone_for_number("+13125550123") == "America/Chicago"  # IL
    assert lead_timezone.timezone_for_number("+16025550123") == "America/Phoenix"  # AZ


def test_split_state_area_codes_use_their_real_zone_not_the_states_dominant_one() -> None:
    # El Paso is Mountain in a Central state; Knoxville is Eastern in a Central one.
    assert lead_timezone.timezone_for_number("+19155550123") == "America/Denver"  # TX
    assert lead_timezone.timezone_for_number("+18655550123") == "America/New_York"  # TN
    assert lead_timezone.timezone_for_number("+12145550123") == "America/Chicago"  # TX
    assert lead_timezone.timezone_for_number("+16155550123") == "America/Chicago"  # TN


def test_non_nanp_and_junk_numbers_give_nothing() -> None:
    assert lead_timezone.timezone_for_number("+46700171894") is None
    assert lead_timezone.timezone_for_number("") is None
    assert lead_timezone.timezone_for_number(None) is None


def test_state_names_and_codes_both_resolve() -> None:
    assert lead_timezone.timezone_for_state("CA") == "America/Los_Angeles"
    assert lead_timezone.timezone_for_state("california") == "America/Los_Angeles"
    assert lead_timezone.timezone_for_state(" Tx. ") == "America/Chicago"
    assert lead_timezone.timezone_for_state("Atlantis") is None


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("UTC", "UTC"),
        ("EST", "America/New_York"),
        ("PDT", "America/Los_Angeles"),
        ("CT", "America/New_York"),
        ("America/Los_Angeles", "America/Los_Angeles"),
        ("Arizona", "America/Phoenix"),
        ("Pacific time", "America/Los_Angeles"),
        ("eastern time zone", "America/New_York"),
        ("Chicago", "America/Chicago"),
        ("  Syrian TIME-zone. ", "Asia/Damascus"),
        ("syrian timezone", "Asia/Damascus"),
    ],
)
def test_explicit_spoken_timezone_resolution(spoken: str, expected: str) -> None:
    assert lead_timezone.resolve_explicit(spoken) == expected


def test_unrecognized_explicit_timezone_has_no_fallback() -> None:
    assert lead_timezone.resolve_explicit("somewhere near Atlantis") is None


def test_resolution_order_explicit_then_state_then_number() -> None:
    assert lead_timezone.resolve({"tzName": "America/Denver", "state": "CA"}) == (
        "America/Denver",
        "variable",
    )
    assert lead_timezone.resolve({"state": "CA", "leadPhone": "+12125550123"}) == (
        "America/Los_Angeles",
        "state",
    )
    assert lead_timezone.resolve({"leadPhone": "+12125550123"}) == (
        "America/New_York",
        "area_code",
    )


def test_an_invalid_explicit_timezone_is_ignored_not_trusted() -> None:
    zone, source = lead_timezone.resolve({"tzName": "Mars/Olympus", "state": "NY"})
    assert (zone, source) == ("America/New_York", "state")


def test_unknown_everything_falls_back_to_the_team(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "BOOKING_TEAM_TIMEZONE", "Europe/Stockholm")
    assert lead_timezone.resolve({"leadPhone": "+46700171894"}) == (
        "Europe/Stockholm",
        "team_default",
    )
    assert lead_timezone.resolve(None) == ("Europe/Stockholm", "team_default")


def test_zone_names_are_spoken_not_slashed() -> None:
    assert lead_timezone.spoken_zone_name("America/Los_Angeles") == "Pacific time"
    assert lead_timezone.spoken_zone_name("America/Indiana/Indianapolis") == "Eastern time"
    # Unknown zones still come out speakable rather than as an IANA path.
    assert lead_timezone.spoken_zone_name("Asia/Damascus") == "Damascus time"
    assert "/" not in lead_timezone.spoken_zone_name("Europe/Stockholm")


def test_every_state_in_the_table_maps_to_a_real_zone() -> None:
    from zoneinfo import ZoneInfo

    for state, zone in lead_timezone.STATE_TO_TIMEZONE.items():
        assert ZoneInfo(zone), state


def test_every_area_code_override_is_a_real_area_code_of_that_state() -> None:
    from app.services.telephony.recording_policy import AREA_CODE_TO_STATE

    for npa in lead_timezone.AREA_CODE_TIMEZONE_OVERRIDES:
        assert npa in AREA_CODE_TO_STATE, npa
