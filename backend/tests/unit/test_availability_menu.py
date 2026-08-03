"""The agent's pre-loaded calendar: grouping, thinning, spoken labels, degradation.

The point of these tests is the behaviour Sami asked for — the agent should be able to
ANSWER "got anything Friday?" from data it already holds, in words a person says, and
never be left mute if the calendar is unreachable.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import availability
from app.services.tools.crm_tools import CRMTools


def iso(day: int, hour: int, minute: int = 0) -> str:
    return f"2026-07-{day:02d}T{hour:02d}:{minute:02d}:00Z"


# --- spoken time: the agent may never say digits out loud ----------------------


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [
        (9, 0, "nine in the morning"),
        (10, 30, "half past ten in the morning"),
        (12, 0, "midday"),
        (13, 0, "one in the afternoon"),
        (16, 30, "half past four in the afternoon"),
        (14, 15, "quarter past two in the afternoon"),
        (16, 45, "quarter to five in the afternoon"),
        (18, 0, "six in the evening"),
        (9, 7, "nine 07 in the morning"),
    ],
)
def test_spoken_time_is_words_not_digits(hour: int, minute: int, expected: str) -> None:
    from datetime import datetime

    assert availability.spoken_time(datetime(2026, 7, 13, hour, minute)) == expected


def test_quarter_to_counts_up_to_the_next_hour() -> None:
    from datetime import datetime

    # 11:45 is "quarter to twelve", not "quarter to eleven".
    assert availability.spoken_time(datetime(2026, 7, 13, 11, 45)) == (
        "quarter to twelve in the morning"
    )


# --- menu shape ---------------------------------------------------------------


def test_menu_groups_by_day_and_numbers_slots_chronologically() -> None:
    raw = [{"start": iso(13, 9)}, {"start": iso(13, 14)}, {"start": iso(14, 11)}]
    menu = availability.build_menu(raw, "UTC")

    assert menu["status"] == "available"
    assert [slot["slot_id"] for slot in menu["slots"]] == ["slot_1", "slot_2", "slot_3"]
    assert [slot["day"] for slot in menu["slots"]] == ["Monday", "Monday", "Tuesday"]
    assert menu["slots"][1]["label"] == "Monday at two in the afternoon"
    # The block is what the agent reads its times off, day by day, with the ids it
    # needs for select_slot but never says.
    assert "- Monday:" in menu["block"]
    assert "- Tuesday:" in menu["block"]
    assert "[slot_3]" in menu["block"]


def test_menu_thins_a_dense_day_across_it_rather_than_clustering() -> None:
    # Cal.com returns every half hour; offering "nine, nine thirty, ten" is useless.
    raw = [{"start": iso(13, hour, minute)} for hour in range(9, 17) for minute in (0, 30)]
    menu = availability.build_menu(raw, "UTC", max_per_day=4)

    times = [slot["time"] for slot in menu["slots"]]
    assert len(times) == 4
    assert times[0] == "9:00 AM"  # earliest kept
    assert times[-1] == "4:30 PM"  # latest kept
    assert len(set(times)) == 4  # spread across the day, not clustered


def test_menu_respects_the_total_cap_across_days() -> None:
    raw = [{"start": iso(day, hour)} for day in (13, 14, 15, 16, 17) for hour in (9, 11, 14, 16)]
    menu = availability.build_menu(raw, "UTC", max_slots=6)
    assert len(menu["slots"]) == 6


def test_menu_is_rendered_in_the_leads_own_clock() -> None:
    # 16:00 UTC is 9am Pacific: the same instant, spoken as the lead hears it.
    menu = availability.build_menu([{"start": iso(13, 16)}], "America/Los_Angeles")
    assert menu["slots"][0]["label"] == "Monday at nine in the morning"


def test_bad_timezone_degrades_to_an_empty_menu_not_an_exception() -> None:
    menu = availability.build_menu([{"start": iso(13, 9)}], "Nowhere/Land")
    assert menu["status"] == "unavailable"
    assert menu["slots"] == []
    assert "refresh_availability" in menu["block"]


def test_unparseable_slots_are_skipped_not_fatal() -> None:
    raw = [{"start": "not-a-date"}, {}, {"start": iso(13, 9)}]
    menu = availability.build_menu(raw, "UTC")
    assert len(menu["slots"]) == 1


@pytest.mark.asyncio
async def test_fetch_menu_never_raises_when_the_calendar_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "k")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 1)
    with patch(
        "app.services.calcom_client.get_open_slots",
        AsyncMock(side_effect=RuntimeError("cal.com 500")),
    ):
        menu = await availability.fetch_menu("UTC")
    assert menu["status"] == "unavailable"
    assert menu["slots"] == []
    assert menu["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_fetch_menu_skips_cleanly_when_calcom_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", None)
    menu = await availability.fetch_menu("UTC")
    assert menu["status"] == "unavailable"
    assert menu["slots"] == []


@pytest.mark.asyncio
async def test_fetch_menu_distinguishes_a_healthy_empty_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "k")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 1)
    with patch(
        "app.services.calcom_client.get_open_slots",
        AsyncMock(return_value=[]),
    ):
        menu = await availability.fetch_menu("UTC")

    assert menu["status"] == "empty"
    assert menu["slots"] == []
    assert "no open business-hours times" in menu["block"]

# --- the booking tools adopt the menu ----------------------------------------


def make_tools() -> CRMTools:
    return CRMTools(db=MagicMock(), user_id=1, variables={"leadName": "Sami"})


@pytest.mark.asyncio
async def test_preloaded_menu_makes_a_time_selectable_with_no_calendar_call() -> None:
    tools = make_tools()
    menu = availability.build_menu([{"start": iso(13, 9)}, {"start": iso(13, 14)}], "UTC")

    assert tools.seed_offered_slots(menu["slots"], "UTC") == 2

    # The caller names one of the pre-loaded times; no tool asked the calendar
    # anything during the conversation.
    tools.observe_user_utterance("two in the afternoon works")
    selected = await tools.select_slot("slot_2")
    assert selected["success"] is True
    assert selected["when"] == "Monday at two in the afternoon"


@pytest.mark.asyncio
async def test_preloaded_menu_still_refuses_to_pick_for_the_caller() -> None:
    """A menu the agent can read is not permission to choose on their behalf."""
    tools = make_tools()
    menu = availability.build_menu([{"start": iso(13, 9)}, {"start": iso(13, 14)}], "UTC")
    tools.seed_offered_slots(menu["slots"], "UTC")

    tools.observe_user_utterance("yeah sure whatever")
    assert (await tools.select_slot("slot_1"))["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_a_mid_call_refresh_needs_a_fresh_answer() -> None:
    """A re-offer must be answered anew — a word said BEFORE it cannot select from
    it. Pre-call seeding is the only case where turn zero counts, because nobody
    has spoken yet."""
    tools = make_tools()
    menu = availability.build_menu([{"start": iso(13, 9)}, {"start": iso(13, 14)}], "UTC")
    tools.observe_user_utterance("nine works")
    tools.seed_offered_slots(menu["slots"], "UTC", origin="offered")

    assert (await tools.select_slot("slot_1"))["error"] == "selection_not_heard"


def test_seeding_an_empty_menu_leaves_the_tool_path_in_charge() -> None:
    tools = make_tools()
    assert tools.seed_offered_slots([], "UTC") == 0
    assert tools._offered_slots == []  # noqa: SLF001


# --- a menu the caller was never read demands a clearer answer -----------------
# Codex review, 2026-07-30: the pre-call seed makes the offered set exist BEFORE
# anything was said aloud, so "the caller chose one of the times I offered" is not
# yet true of it. A bare number must not be able to select from it.


@pytest.mark.asyncio
async def test_a_bare_duration_cannot_select_from_a_never_spoken_menu() -> None:
    tools = make_tools()
    # 14:00 UTC is a two o'clock opening, which "two minutes" would otherwise match.
    menu = availability.build_menu([{"start": iso(13, 14)}, {"start": iso(14, 9)}], "UTC")
    tools.seed_offered_slots(menu["slots"], "UTC")

    tools.observe_user_utterance("hang on, give me two minutes")
    assert (await tools.select_slot("slot_1"))["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_a_properly_named_time_still_selects_from_a_never_spoken_menu() -> None:
    """The guard must not block the good case: they named a real day and time."""
    tools = make_tools()
    menu = availability.build_menu([{"start": iso(13, 14)}, {"start": iso(14, 9)}], "UTC")
    tools.seed_offered_slots(menu["slots"], "UTC")

    tools.observe_user_utterance("Monday at two in the afternoon is good")
    assert (await tools.select_slot("slot_1"))["success"] is True


@pytest.mark.asyncio
async def test_once_times_are_offered_aloud_the_ordinary_guard_applies() -> None:
    """A tool-driven offer WAS spoken, so "the second one" is a valid answer to it."""
    tools = make_tools()
    menu = availability.build_menu([{"start": iso(13, 9)}, {"start": iso(13, 14)}], "UTC")
    tools.observe_user_utterance("what have you got?")
    tools.seed_offered_slots(menu["slots"], "UTC", origin="offered")

    tools.observe_user_utterance("the second one")
    assert (await tools.select_slot("slot_2"))["success"] is True


# --- the caller's reply is read in the CONTEXT of what the agent just offered ---
# Live call 2026-07-31. The agent asked "would Monday at midday work?", Sami said
# "yes, it would" -> refused (he had not NAMED a time). It re-offered, he said
# "midday" -> refused again, because a 16-slot menu has a midday on four days so the
# time alone was ambiguous. A person in that conversation understood both answers.


def four_day_menu() -> dict:
    """A midday on four separate days — the shape that broke the live call."""
    return availability.build_menu(
        [{"start": iso(day, hour)} for day in (13, 14, 15, 16) for hour in (12, 15)],
        "UTC",
    )


@pytest.mark.asyncio
async def test_yes_to_a_single_proposed_time_is_a_choice() -> None:
    tools = make_tools()
    menu = four_day_menu()
    tools.seed_offered_slots(menu["slots"], "UTC")

    tools.observe_assistant_utterance("Would Monday at midday work?")
    tools.observe_user_utterance("Yes, it would.")

    selected = await tools.select_slot("slot_1")
    assert selected["success"] is True
    assert selected["when"] == "Monday at midday"


@pytest.mark.asyncio
async def test_a_bare_time_resolves_against_the_day_just_named() -> None:
    tools = make_tools()
    menu = four_day_menu()
    tools.seed_offered_slots(menu["slots"], "UTC")

    tools.observe_assistant_utterance(
        "For Monday, would you like midday, or three in the afternoon?"
    )
    tools.observe_user_utterance("Uh, midday.")

    selected = await tools.select_slot("slot_1")
    assert selected["success"] is True
    assert selected["when"] == "Monday at midday"


@pytest.mark.asyncio
async def test_a_bare_time_with_no_context_is_still_ambiguous() -> None:
    """Four middays and nothing offered aloud: refusing and re-asking is correct."""
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_user_utterance("midday works for me")
    assert (await tools.select_slot("slot_1"))["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_yes_to_two_proposed_times_is_still_not_a_choice() -> None:
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("Monday at midday, or Monday at three?")
    tools.observe_user_utterance("Yeah.")

    assert (await tools.select_slot("slot_1"))["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_agreement_never_selects_when_no_time_was_proposed() -> None:
    """"Yes" answering "are you a real person?" must not book anything."""
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("I'm Pulsift's AI assistant, actually.")
    tools.observe_user_utterance("Yes, fair enough.")

    assert (await tools.select_slot("slot_1"))["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_yes_but_a_different_time_follows_the_caller_not_the_agent() -> None:
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("Would Monday at midday work?")
    tools.observe_user_utterance("Yes, but could we do Tuesday at midday instead?")

    selected = await tools.select_slot("slot_3")  # Tuesday midday
    assert selected["success"] is True
    assert selected["when"] == "Tuesday at midday"


@pytest.mark.asyncio
async def test_ordinals_index_what_was_offered_not_the_whole_menu() -> None:
    """With 8 slots, "the first one" must mean the first of the two just named."""
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance(
        "I've got Wednesday at midday, or Wednesday at three in the afternoon."
    )
    tools.observe_user_utterance("The first one.")

    selected = await tools.select_slot("slot_5")  # Wednesday midday, not Monday
    assert selected["success"] is True
    assert selected["when"] == "Wednesday at midday"


# Codex machine review #6 (2026-08-01). Naming a time is not choosing it, and a
# sentence that starts with "sure" can end anywhere. Every case below could book a
# time the caller never agreed to, which is the one thing the gate exists to stop.


@pytest.mark.asyncio
async def test_a_time_inside_a_refusal_is_not_a_choice() -> None:
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("Would Tuesday at midday work?")
    tools.observe_user_utterance("No, Tuesday at midday doesn't work.")

    assert (await tools.select_slot("slot_3"))["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_agreement_that_carries_on_into_a_maybe_is_not_a_choice() -> None:
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("Would Monday at midday work?")
    tools.observe_user_utterance("Sure, let me check my diary and get back to you.")

    assert (await tools.select_slot("slot_1"))["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_asking_to_speak_later_does_not_pick_the_later_slot() -> None:
    """"Can we talk later?" is about the call, not the calendar."""
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("Monday at midday, or Monday at three?")
    tools.observe_user_utterance("Can we talk later?")

    assert (await tools.select_slot("slot_2"))["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_the_later_one_still_selects_the_second_offered_slot() -> None:
    """The bounded phrase is a real choice and must keep working."""
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("Monday at midday, or Monday at three?")
    tools.observe_user_utterance("The later one, please.")

    selected = await tools.select_slot("slot_2")
    assert selected["success"] is True
    assert selected["when"] == "Monday at three in the afternoon"


@pytest.mark.asyncio
async def test_hedged_yes_with_a_condition_is_not_a_choice() -> None:
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("Would Monday at midday work?")
    tools.observe_user_utterance("Fine, but I'm not sure yet.")

    assert (await tools.select_slot("slot_1"))["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_a_polite_plain_yes_still_books() -> None:
    """The veto must not make the agent deaf to an ordinary acceptance."""
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("Would Monday at midday work?")
    tools.observe_user_utterance("Perfect, thanks.")

    selected = await tools.select_slot("slot_1")
    assert selected["success"] is True
    assert selected["when"] == "Monday at midday"


@pytest.mark.asyncio
async def test_swapping_to_another_time_is_still_a_choice() -> None:
    """"Instead" carries no refusal — picking a different time IS deciding."""
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("Would Monday at midday work?")
    tools.observe_user_utterance("Tuesday at midday instead, please.")

    selected = await tools.select_slot("slot_3")
    assert selected["success"] is True
    assert selected["when"] == "Tuesday at midday"


# --- the two-slot OFFER FIRST line: chosen in code, never worked out live ------
# Live call 2026-08-02. The agent offered two slots on the SAME day and narrated
# its own picking process out loud three times ("let me line that up with what
# we've got available"). A realtime speech model has no silent scratchpad — its
# output tokens ARE the audio — so choosing a spread live always leaks into
# speech. The fix moves the choice into code and hands the model a finished
# sentence.


def test_fallback_1_morning_and_midday_on_different_days() -> None:
    # Monday nine in the morning, Tuesday one in the afternoon (inside the
    # midday/early-afternoon band) -- the preferred shape.
    raw = [{"start": iso(13, 9)}, {"start": iso(14, 13)}]
    menu = availability.build_menu(raw, "UTC")

    offer = menu["offer_slots"]
    assert [s["day"] for s in offer] == ["Monday", "Tuesday"]
    assert offer[0]["label"] == "Monday at nine in the morning"
    assert offer[1]["label"] == "Tuesday at one in the afternoon"
    assert (
        "OFFER FIRST: Monday at nine in the morning, or Tuesday at one in the "
        "afternoon." in menu["block"]
    )


def test_fallback_2_same_two_bands_on_the_only_available_day() -> None:
    # Only Monday is open, but it has both a morning and a midday opening.
    raw = [{"start": iso(13, 9)}, {"start": iso(13, 13)}]
    menu = availability.build_menu(raw, "UTC")

    offer = menu["offer_slots"]
    assert [s["day"] for s in offer] == ["Monday", "Monday"]
    assert [s["time"] for s in offer] == ["9:00 AM", "1:00 PM"]
    assert (
        "OFFER FIRST: Monday at nine in the morning, or Monday at one in the "
        "afternoon." in menu["block"]
    )


def test_fallback_3_two_earliest_slots_on_different_days() -> None:
    # Two evening slots on different days -- neither band matches, so the
    # earliest-per-day fallback takes over.
    raw = [{"start": iso(13, 18)}, {"start": iso(14, 19)}]
    menu = availability.build_menu(raw, "UTC")

    offer = menu["offer_slots"]
    assert [s["day"] for s in offer] == ["Monday", "Tuesday"]
    assert [s["time"] for s in offer] == ["6:00 PM", "7:00 PM"]


def test_fallback_4_two_earliest_slots_outright_when_only_one_day_has_no_bands() -> None:
    # A single day, both slots outside both bands: nothing left to prefer.
    raw = [{"start": iso(13, 18)}, {"start": iso(13, 19)}]
    menu = availability.build_menu(raw, "UTC")

    offer = menu["offer_slots"]
    assert [s["day"] for s in offer] == ["Monday", "Monday"]
    assert [s["time"] for s in offer] == ["6:00 PM", "7:00 PM"]


def test_fallback_5_a_single_slot_renders_the_one_slot_line() -> None:
    raw = [{"start": iso(13, 9)}]
    menu = availability.build_menu(raw, "UTC")

    assert [s["day"] for s in menu["offer_slots"]] == ["Monday"]
    assert "OFFER FIRST: Monday at nine in the morning." in menu["block"]
    assert " or " not in menu["block"].split("\n")[0]


def test_fallback_6_no_slots_renders_no_offer_line() -> None:
    menu = availability.build_menu([], "UTC")
    assert menu["offer_slots"] == []
    assert "OFFER FIRST" not in menu["block"]


def test_offer_first_line_is_spoken_words_naming_only_day_and_time() -> None:
    raw = [{"start": iso(13, 9)}, {"start": iso(14, 13, 30)}]
    menu = availability.build_menu(raw, "UTC")

    offer_line = menu["block"].splitlines()[0]
    assert offer_line.startswith("OFFER FIRST: ")
    assert not any(char.isdigit() for char in offer_line)
    # Day name only -- never a calendar date.
    assert "2026" not in offer_line
    assert "Monday" in offer_line
    assert "Tuesday" in offer_line


def test_full_menu_still_follows_the_offer_first_line() -> None:
    raw = [{"start": iso(13, 9)}, {"start": iso(14, 13)}]
    menu = availability.build_menu(raw, "UTC")

    assert menu["block"].startswith("OFFER FIRST: ")
    assert "- Monday:" in menu["block"]
    assert "- Tuesday:" in menu["block"]
    assert "[slot_1]" in menu["block"]
    assert "[slot_2]" in menu["block"]


def test_empty_calendar_block_is_unchanged_by_the_offer_first_feature() -> None:
    menu = availability.build_menu([], "UTC")
    assert menu["status"] == "empty"
    assert menu["offer_slots"] == []
    assert menu["block"] == (
        "The calendar has no open business-hours times in the current window. "
        "Do not invent or promise a time."
    )


def test_unavailable_calendar_block_is_unchanged_by_the_offer_first_feature() -> None:
    menu = availability.empty_menu("UTC")
    assert menu["status"] == "unavailable"
    assert menu["offer_slots"] == []
    assert "OFFER FIRST" not in menu["block"]
    assert "refresh_availability" in menu["block"]


@pytest.mark.asyncio
async def test_call_me_back_another_time_books_nothing() -> None:
    tools = make_tools()
    tools.seed_offered_slots(four_day_menu()["slots"], "UTC")

    tools.observe_assistant_utterance("Would Monday at midday work?")
    tools.observe_user_utterance("Call me back another time.")

    assert (await tools.select_slot("slot_1"))["error"] == "ambiguous_slot_selection"
