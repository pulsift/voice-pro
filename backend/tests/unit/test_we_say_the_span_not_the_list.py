"""The agent stopped reciting the calendar at people.

From the 2026-08-29 ring test, the agent said out loud: "Tuesday at eight, half
past eight, nine, half past nine, ten, half past ten, eleven, half past eleven,
midday, and later through the afternoon and evening." Nobody talks like that.

Sami's ruling: say the SPAN - "we have times from eight in the morning through to
five" - and keep every slot selectable. Available and spoken aloud are different
things, so this is a rendering change only: nothing is removed from the menu.
"""

from __future__ import annotations

from app.services import availability


def iso(day: int, hour: int, minute: int = 0) -> str:
    return f"2026-07-{day:02d}T{hour:02d}:{minute:02d}:00Z"


def span_line(block: str) -> str:
    return next(line for line in block.splitlines() if line.startswith("SPANS"))


def test_each_day_is_described_by_its_first_and_last_time() -> None:
    raw = [{"start": iso(13, 8)}, {"start": iso(13, 12)}, {"start": iso(13, 17)}]
    menu = availability.build_menu(raw, "UTC")

    line = span_line(menu["block"])
    assert "Monday, from eight in the morning through to five in the evening" in line


def test_the_span_never_claims_the_gap_between_is_free() -> None:
    """A day holding only nine and four says so as a span, not as coverage.

    The calendar has real gaps and `_thin` narrows the menu further, so any
    wording that promised everything in between would be a small lie the caller
    finds on their next sentence. select_slot walks back anything inside the
    span we do not hold.
    """
    raw = [{"start": iso(13, 9)}, {"start": iso(13, 16)}]
    line = span_line(availability.build_menu(raw, "UTC")["block"])

    assert "from nine in the morning through to four in the afternoon" in line
    for greedy in ("every", "all day", "any time", "each half hour"):
        assert greedy not in line.lower()


def test_a_day_with_one_time_is_not_described_as_a_range() -> None:
    raw = [{"start": iso(13, 9)}, {"start": iso(14, 10)}, {"start": iso(14, 15)}]
    line = span_line(availability.build_menu(raw, "UTC")["block"])

    assert "Monday, nine in the morning only" in line
    assert "Tuesday, from ten in the morning through to three in the afternoon" in line


def test_the_span_is_words_never_digits() -> None:
    raw = [{"start": iso(13, 8, 30)}, {"start": iso(13, 17)}]
    line = span_line(availability.build_menu(raw, "UTC")["block"])

    assert not any(char.isdigit() for char in line)


def test_the_agent_is_told_to_say_the_span_and_never_read_the_list() -> None:
    raw = [{"start": iso(13, 9)}, {"start": iso(14, 13)}]
    block = availability.build_menu(raw, "UTC")["block"]

    assert "never a list of times" in block
    assert "Never read individual times out loud" in block


def test_every_slot_is_still_selectable_underneath_the_span() -> None:
    """The whole point of the 2026-08-21 work was that the agent holds the week.

    Summarising how the calendar is DESCRIBED must not narrow what it can book.
    """
    raw = [{"start": iso(13, h)} for h in (8, 9, 10, 11)]
    menu = availability.build_menu(raw, "UTC")

    assert len(menu["slots"]) == 4
    for slot in menu["slots"]:
        assert f"[{slot['slot_id']}]" in menu["block"]
    assert "- Monday:" in menu["block"]


def test_an_empty_calendar_grows_no_span_line() -> None:
    assert "SPANS" not in availability.build_menu([], "UTC")["block"]
    assert "SPANS" not in availability.empty_menu("UTC")["block"]
