"""The two times the agent leads with are the two SOONEST days, never today.

Sami's rule, 2026-08-25: the pair it proposes should be "the weekday tomorrow and
the next weekday after that" - so on a Wednesday it offers Thursday and Friday, and
on a Friday it offers Monday and Tuesday.

Weekends are NOT computed here. Cal.com already knows the working week from Sami's
own schedule, so a weekend never reaches this list; re-deriving it would be a second
opinion about a thing the calendar already decided.

Today is excluded for a real reason: the lead list has to be BUILT before that call.
The full menu still holds today, so a caller who insists on it can have it - we just
never propose it.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.services import availability

TZ = "Europe/Stockholm"
ZONE = ZoneInfo(TZ)


def at(day_offset: int, hour: int, minute: int = 0) -> str:
    """An ISO start `day_offset` days from now, at a local wall-clock time."""
    local = (datetime.now(ZONE) + timedelta(days=day_offset)).replace(
        hour=hour, minute=minute, second=0, microsecond=0
    )
    return local.astimezone(UTC).isoformat()


def offered_days(raw: list[dict[str, str]]) -> list[str]:
    menu = availability.build_menu(raw, TZ)
    return [slot["day"] for slot in menu["offer_slots"]]


def day_name(day_offset: int) -> str:
    return (datetime.now(ZONE) + timedelta(days=day_offset)).strftime("%A")


def test_it_leads_with_the_next_two_days_and_never_today() -> None:
    raw = [
        {"start": at(0, 9)}, {"start": at(0, 13)},    # today - must not be offered
        {"start": at(1, 9)}, {"start": at(1, 13)},    # tomorrow
        {"start": at(2, 9)}, {"start": at(2, 13)},    # the day after
        {"start": at(3, 9)}, {"start": at(3, 13)},    # too far to lead with
    ]

    assert offered_days(raw) == [day_name(1), day_name(2)]


def test_a_gap_in_the_calendar_moves_the_pair_along() -> None:
    """Nothing tomorrow: lead with the next two days that DO have times."""
    raw = [
        {"start": at(0, 9)},                          # today - skipped
        {"start": at(2, 9)}, {"start": at(2, 13)},
        {"start": at(4, 9)}, {"start": at(4, 13)},
    ]

    assert offered_days(raw) == [day_name(2), day_name(4)]


def test_a_day_with_no_morning_still_leads_with_that_day() -> None:
    """The DAY is what matters. Never skip a sooner day to find a morning slot."""
    raw = [
        {"start": at(1, 15)}, {"start": at(1, 16)},   # tomorrow, afternoon only
        {"start": at(2, 9)}, {"start": at(2, 13)},
    ]

    assert offered_days(raw) == [day_name(1), day_name(2)]


def test_when_today_is_all_there_is_we_still_offer_it() -> None:
    """Never leave them with nothing - that is worse than proposing today."""
    raw = [{"start": at(0, 9)}, {"start": at(0, 13)}]

    assert offered_days(raw) == [day_name(0), day_name(0)]
