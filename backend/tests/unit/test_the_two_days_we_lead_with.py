"""The two times the agent leads with are the two SOONEST days, never today.

Sami's rule, 2026-08-25: the pair it proposes should be "the weekday tomorrow and
the next weekday after that" - so on a Wednesday it offers Thursday and Friday, and
on a Friday it offers Monday and Tuesday.

Weekends are NOT computed here. Cal.com already knows the working week from Sami's
own schedule, so a weekend never reaches this list; re-deriving it would be a second
opinion about a thing the calendar already decided.

Today is excluded from the MENU, not merely from the pair: the lead list has to be
BUILT before that call, so a same-day booking is a promise we cannot keep. Sami,
2026-08-26: "today should not be on the table, they should not be able to take in a
call on the same day." So the agent cannot offer it, cannot be talked into it, and
does not hold it at all.
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


def test_today_is_not_in_the_menu_at_all() -> None:
    """Not just unofferd - unbookable. It must not reach the agent's list."""
    raw = [
        {"start": at(0, 9)}, {"start": at(0, 13)},
        {"start": at(1, 9)},
    ]
    menu = availability.build_menu(raw, TZ)

    assert [slot["day"] for slot in menu["slots"]] == [day_name(1)]


def test_a_calendar_with_only_today_reads_as_empty() -> None:
    """Honest emptiness beats a time we cannot deliver against."""
    menu = availability.build_menu([{"start": at(0, 9)}, {"start": at(0, 13)}], TZ)

    assert menu["status"] == "empty"
    assert menu["slots"] == []
    assert menu["offer_slots"] == []
