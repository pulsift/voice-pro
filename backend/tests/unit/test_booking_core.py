"""Focused tests for transcript-bound Cal.com booking state."""

import uuid
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

from app.core.config import settings
from app.services.availability import LOOKAHEAD_DAYS
from app.services.calcom_client import create_booking, normalize_timezone
from app.services.tools.crm_tools import CRMTools

SLOT_1 = {"start": "2026-07-13T09:00:00Z", "label": "Monday 11:00 AM"}
SLOT_2 = {"start": "2026-07-13T13:00:00Z", "label": "Monday 3:00 PM"}
ICP = {"offer_types": ["commercial solar"], "min_kw": 50, "states": ["Texas"]}


@pytest.fixture(autouse=True)
def configured_calcom(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "CALCOM_API_KEY", "test-key")
    monkeypatch.setattr(settings, "CALCOM_EVENT_TYPE_ID", 123)
    monkeypatch.setattr(settings, "BOOKING_TEAM_TIMEZONE", "Europe/Stockholm")
    monkeypatch.setattr(
        "app.services.tools.crm_tools.stage_fulfilment_intent",
        AsyncMock(return_value="intent-key"),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.finalize_fulfilment_intent",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.claim_fulfilment_booking",
        AsyncMock(return_value=uuid.UUID(int=1)),
    )
    monkeypatch.setattr(
        "app.services.tools.crm_tools.authorize_fulfilment_booking",
        AsyncMock(return_value=True),
    )


def make_tools(**variables: str) -> CRMTools:
    return CRMTools(
        db=MagicMock(),
        user_id=1,
        variables={"leadName": "Sami", "leadEmail": "seeded@example.com", **variables},
    )


def test_normalize_timezone_contract() -> None:
    assert normalize_timezone("Europe/Stockholm", None, "UTC") == "Europe/Stockholm"
    assert normalize_timezone("  Syrian TIME-zone. ", None, "UTC") == "Asia/Damascus"
    assert normalize_timezone("Arizona", None, "UTC") == "America/Phoenix"
    assert normalize_timezone("unknown place", "America/New_York", "UTC") is None
    assert normalize_timezone(None, "America/New_York", "UTC") == "America/New_York"
    assert normalize_timezone(None, "also unknown", "UTC") == "UTC"
    assert normalize_timezone("unknown", "also unknown", "bad/default") is None


@pytest.mark.asyncio
async def test_unknown_explicit_timezone_requests_clarification_without_fetching() -> None:
    tools = make_tools(tzName="America/New_York")
    invalidator = AsyncMock()
    tools.set_live_availability_invalidator(invalidator)

    with patch("app.services.calcom_client.get_open_slots", new=AsyncMock()) as get_slots:
        result = await tools.check_availability(time_zone="somewhere near Atlantis")
        without_clarification = await tools.check_availability()

    assert result["error"] == "timezone_unresolved"
    assert "standard time zone" in result["message"]
    assert without_clarification["error"] == "timezone_unresolved"
    assert "do not fall back" in without_clarification["message"]
    invalidator.assert_awaited_once_with("timezone_unresolved")
    get_slots.assert_not_awaited()


def test_tool_schema_makes_email_optional_and_select_slot_transcript_free() -> None:
    definitions = {tool["name"]: tool for tool in CRMTools.get_tool_definitions()}

    assert "email" not in definitions["book_appointment"]["parameters"]["required"]
    assert definitions["select_slot"]["parameters"]["required"] == ["slot_id"]
    assert set(definitions["select_slot"]["parameters"]["properties"]) == {"slot_id"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "category"),
    [
        (409, "conflict"),
        (429, "transient"),
        (500, "transient"),
        (400, "rejected"),
        (422, "rejected"),
    ],
)
async def test_calcom_http_outcome_classification(status_code: int, category: str) -> None:
    response = MagicMock(status_code=status_code, text="x" * 1200)
    client = MagicMock(post=AsyncMock(return_value=response))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.calcom_client.httpx.AsyncClient", return_value=context):
        result = await create_booking(
            start_iso=SLOT_1["start"],
            name="Sami",
            email="lead@example.com",
            lead_tz="UTC",
        )

    assert result["category"] == category
    assert result["status_code"] == status_code
    assert len(result["raw_body"]) == 1000


@pytest.mark.asyncio
async def test_calcom_timeout_is_transient() -> None:
    client = MagicMock(post=AsyncMock(side_effect=httpx.ReadTimeout("timed out")))
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=None)

    with patch("app.services.calcom_client.httpx.AsyncClient", return_value=context):
        result = await create_booking(
            start_iso=SLOT_1["start"],
            name="Sami",
            email="lead@example.com",
            lead_tz="UTC",
        )

    assert result["category"] == "transient"
    assert result["status_code"] is None


@pytest.mark.asyncio
async def test_selection_requires_new_unambiguous_post_offer_utterance() -> None:
    tools = make_tools()
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=[SLOT_1, SLOT_2])
    ) as get_slots:
        offered = await tools.check_availability(time_zone="Syrian time zone")

    assert offered["timezone"] == "Asia/Damascus"
    assert [slot["slot_id"] for slot in offered["slots"]] == ["slot_1", "slot_2"]
    # ONE calendar read for the whole lookahead window: availability is a menu built
    # once, not two slots re-fetched per question.
    get_slots.assert_awaited_once_with(lead_tz="Asia/Damascus", days=LOOKAHEAD_DAYS)
    assert (await tools.select_slot("slot_2"))["error"] == "selection_not_heard"

    tools.observe_user_utterance("thank you")
    assert (await tools.select_slot("slot_2"))["error"] == "ambiguous_slot_selection"

    tools.observe_user_utterance("the second one")
    selected = await tools.select_slot("slot_2")
    # The spoken label is generated from the slot itself (13:00Z = 16:00 in Damascus),
    # so what the agent says is always the lead's own clock, in words.
    assert selected == {
        "success": True,
        "slot_id": "slot_2",
        "start": SLOT_2["start"],
        "when": "Monday at four in the afternoon",
    }


@pytest.mark.asyncio
async def test_day_or_time_selection_must_identify_exactly_one_slot() -> None:
    tools = make_tools()
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=[SLOT_1, SLOT_2])
    ):
        await tools.check_availability(time_zone="Europe/Stockholm")

    tools.observe_user_utterance("Monday")
    assert (await tools.select_slot("slot_1"))["error"] == "ambiguous_slot_selection"
    tools.observe_user_utterance("3 pm")
    assert (await tools.select_slot("slot_2"))["success"] is True


@pytest.mark.asyncio
async def test_spoken_bare_hour_selects_the_only_matching_offered_slot() -> None:
    tools = make_tools()
    slots = [
        {"start": "2026-07-13T10:00:00Z", "label": "Monday 10:00 AM"},
        {"start": "2026-07-13T15:00:00Z", "label": "Monday 3:00 PM"},
    ]
    with patch("app.services.calcom_client.get_open_slots", AsyncMock(return_value=slots)):
        await tools.check_availability(time_zone="UTC")

    tools.observe_user_utterance("ten")

    selected = await tools.select_slot("slot_1")
    assert selected["success"] is True
    assert selected["start"] == slots[0]["start"]


@pytest.mark.asyncio
async def test_bare_digit_hour_and_dotted_meridiem_select_a_slot() -> None:
    """Live-call regression (2026-07-13): 'Tuesday at 1 my time' and
    'Tuesday at 1 p.m.' were clear picks but the matcher rejected both."""
    tools = make_tools()
    slots = [
        {"start": "2026-07-14T07:00:00Z", "label": "Tuesday 10:00 AM"},  # 10:00 +03
        {"start": "2026-07-14T10:00:00Z", "label": "Tuesday 1:00 PM"},  # 13:00 +03
    ]
    with patch("app.services.calcom_client.get_open_slots", AsyncMock(return_value=slots)):
        await tools.check_availability(time_zone="Asia/Damascus")

    tools.observe_user_utterance("All right, let's just go for Tuesday at 1 my time.")
    selected = await tools.select_slot("slot_2")
    assert selected["success"] is True
    assert selected["start"] == slots[1]["start"]

    with patch("app.services.calcom_client.get_open_slots", AsyncMock(return_value=slots)):
        await tools.check_availability(time_zone="Asia/Damascus")
    tools.observe_user_utterance("Sorry, I want the Tuesday at 1 p.m.")
    selected = await tools.select_slot("slot_2")
    assert selected["success"] is True


@pytest.mark.asyncio
async def test_day_part_answers_select_the_matching_slot() -> None:
    """'The morning one' / 'the afternoon' are natural answers when the two
    times are re-offered by name (the ordinal question is banned)."""
    tools = make_tools()
    slots = [
        {"start": "2026-07-14T07:00:00Z", "label": "Tuesday 10:00 AM"},  # 10:00 +03
        {"start": "2026-07-14T10:00:00Z", "label": "Tuesday 1:00 PM"},  # 13:00 +03
    ]
    with patch("app.services.calcom_client.get_open_slots", AsyncMock(return_value=slots)):
        await tools.check_availability(time_zone="Asia/Damascus")

    tools.observe_user_utterance("Let's do the morning one.")
    assert (await tools.select_slot("slot_1"))["success"] is True

    with patch("app.services.calcom_client.get_open_slots", AsyncMock(return_value=slots)):
        await tools.check_availability(time_zone="Asia/Damascus")
    tools.observe_user_utterance("The afternoon works better for me.")
    assert (await tools.select_slot("slot_2"))["success"] is True

    # Two morning slots -> a day-part answer alone must stay ambiguous.
    morning_slots = [
        {"start": "2026-07-14T05:00:00Z", "label": "Tuesday 8:00 AM"},
        {"start": "2026-07-14T07:00:00Z", "label": "Tuesday 10:00 AM"},
    ]
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=morning_slots)
    ):
        await tools.check_availability(time_zone="Asia/Damascus")
    tools.observe_user_utterance("the morning one")
    assert (await tools.select_slot("slot_1"))["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_bare_digit_still_ambiguous_when_it_matches_both_slots() -> None:
    tools = make_tools()
    slots = [
        {"start": "2026-07-14T01:00:00Z", "label": "Tuesday 1:00 AM"},
        {"start": "2026-07-14T13:00:00Z", "label": "Tuesday 1:00 PM"},
    ]
    with patch("app.services.calcom_client.get_open_slots", AsyncMock(return_value=slots)):
        await tools.check_availability(time_zone="UTC")

    tools.observe_user_utterance("Tuesday at 1 works")
    result = await tools.select_slot("slot_2")
    assert result["error"] == "ambiguous_slot_selection"


@pytest.mark.asyncio
async def test_booking_is_pinned_seeded_email_and_duplicate_safe() -> None:
    tools = make_tools()
    create_booking = AsyncMock(
        return_value={
            "success": True,
            "category": "success",
            "status_code": 201,
            "raw_body": '{"ok":true}',
            "uid": "booking-1",
        }
    )
    webhook = AsyncMock(return_value=True)
    with (
        patch(
            "app.services.calcom_client.get_open_slots",
            AsyncMock(return_value=[SLOT_1, SLOT_2]),
        ),
        patch("app.services.calcom_client.create_booking", create_booking),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
        patch("app.services.tools.crm_tools.finalize_fulfilment_intent", webhook),
    ):
        assert (await tools.book_appointment(SLOT_1["start"], icp=ICP))["error"] == (
            "slots_not_offered"
        )
        await tools.check_availability(time_zone="Europe/Stockholm")
        assert (await tools.book_appointment(SLOT_1["start"], icp=ICP))["error"] == (
            "slot_not_selected"
        )
        tools.observe_user_utterance("the second one")
        await tools.select_slot("slot_2")
        assert (await tools.book_appointment(SLOT_1["start"], icp=ICP))["error"] == (
            "slot_mismatch"
        )
        booked = await tools.book_appointment("2026-07-13T13:00:00+00:00", icp=ICP)
        duplicate = await tools.book_appointment(SLOT_2["start"], icp=ICP)

    assert booked == duplicate
    assert booked["uid"] == "booking-1"
    create_booking.assert_awaited_once_with(
        start_iso=SLOT_2["start"],
        name="Sami",
        email="seeded@example.com",
        lead_tz="Europe/Stockholm",
        notes='ICP: {"offer_types": ["commercial solar"], "min_kw": 50, "states": ["Texas"]}',
    )
    webhook.assert_awaited_once()
    assert any(
        attempt.get("operation") == "create" and attempt.get("category") == "success"
        for attempt in tools.get_booking_attempts()
    )


@pytest.mark.asyncio
async def test_live_email_overrides_seed_and_missing_placeholder_is_rejected() -> None:
    tools = make_tools(leadEmail="{{leadEmail}}")
    with patch("app.services.calcom_client.get_open_slots", AsyncMock(return_value=[SLOT_1])):
        await tools.check_availability(time_zone="UTC")
    tools.observe_user_utterance("the first one")
    await tools.select_slot("slot_1")

    assert (await tools.book_appointment(SLOT_1["start"], icp=ICP))["error"] == "missing_email"
    create_booking = AsyncMock(
        return_value={"success": True, "category": "success", "status_code": 201, "uid": "b2"}
    )
    with (
        patch("app.services.calcom_client.create_booking", create_booking),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
        patch("app.services.tools.crm_tools.finalize_fulfilment_intent"),
    ):
        await tools.book_appointment(SLOT_1["start"], email="live@example.com", icp=ICP)
    assert create_booking.await_args.kwargs["email"] == "live@example.com"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("outcomes", "expected_error", "expected_calls"),
    [
        (
            [
                {"success": False, "category": "transient", "status_code": 429},
                {"success": False, "category": "transient", "status_code": 500},
            ],
            "booking_outcome_unknown",
            1,
        ),
        (
            [{"success": False, "category": "rejected", "status_code": 400}],
            "booking_rejected",
            1,
        ),
        (
            [{"success": False, "category": "rejected", "status_code": 422}],
            "booking_rejected",
            1,
        ),
    ],
)
async def test_retry_once_and_non_retryable_matrix(
    outcomes: list[dict[str, object]], expected_error: str, expected_calls: int
) -> None:
    tools = make_tools()
    create_booking = AsyncMock(side_effect=outcomes)
    find_existing_booking = AsyncMock(
        return_value={
            "success": False,
            "category": "not_found",
            "status_code": 200,
            "raw_body": "",
        }
    )
    with (
        patch(
            "app.services.calcom_client.get_open_slots",
            AsyncMock(return_value=[SLOT_1, SLOT_2]),
        ),
        patch("app.services.calcom_client.create_booking", create_booking),
        patch("app.services.calcom_client.find_existing_booking", find_existing_booking),
        patch("app.services.tools.crm_tools.asyncio.sleep", AsyncMock()),
    ):
        await tools.check_availability(time_zone="UTC")
        tools.observe_user_utterance("first")
        await tools.select_slot("slot_1")
        result = await tools.book_appointment(SLOT_1["start"], icp=ICP)

    assert result["error"] == expected_error
    assert create_booking.await_count == expected_calls
    attempts = tools.get_booking_attempts()
    assert sum(attempt["operation"] == "create" for attempt in attempts) == expected_calls
    expected_reconciliations = 2 if outcomes[0]["category"] == "transient" else 1
    assert find_existing_booking.await_count == expected_reconciliations


@pytest.mark.asyncio
async def test_transient_create_never_dispatches_a_second_post() -> None:
    tools = make_tools()
    create_booking = AsyncMock(
        side_effect=[
            {"success": False, "category": "transient", "status_code": None},
            {"success": True, "category": "success", "status_code": 201, "uid": "retry-ok"},
        ]
    )
    find_existing_booking = AsyncMock(
        return_value={
            "success": False,
            "category": "not_found",
            "status_code": 200,
            "raw_body": "",
        }
    )
    webhook = AsyncMock(return_value=True)
    with (
        patch(
            "app.services.calcom_client.get_open_slots",
            AsyncMock(return_value=[SLOT_1, SLOT_2]),
        ),
        patch("app.services.calcom_client.create_booking", create_booking),
        patch("app.services.calcom_client.find_existing_booking", find_existing_booking),
        patch("app.services.tools.crm_tools.asyncio.sleep", AsyncMock()),
        patch("app.services.tools.crm_tools.finalize_fulfilment_intent", webhook),
    ):
        await tools.check_availability(time_zone="UTC")
        tools.observe_user_utterance("first")
        await tools.select_slot("slot_1")
        result = await tools.book_appointment(SLOT_1["start"], icp=ICP)

    assert result["error"] == "booking_outcome_unknown"
    assert create_booking.await_count == 1
    assert find_existing_booking.await_count == 2
    find_existing_booking.assert_awaited_with(start_iso=SLOT_1["start"], email="seeded@example.com")
    webhook.assert_not_awaited()


@pytest.mark.asyncio
async def test_conflict_refreshes_without_substitute_booking() -> None:
    tools = make_tools()
    fresh = [{"start": "2026-07-14T09:00:00Z", "label": "Tuesday 9:00 AM"}]
    get_slots = AsyncMock(side_effect=[[SLOT_1, SLOT_2], fresh])
    create_booking = AsyncMock(
        return_value={"success": False, "category": "conflict", "status_code": 409}
    )
    with (
        patch("app.services.calcom_client.get_open_slots", get_slots),
        patch("app.services.calcom_client.create_booking", create_booking),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
    ):
        await tools.check_availability(time_zone="UTC")
        tools.observe_user_utterance("second")
        await tools.select_slot("slot_2")
        result = await tools.book_appointment(SLOT_2["start"], icp=ICP)

    assert result["error"] == "slot_conflict"
    assert result["slots"] == [
        {
            "slot_id": "slot_1",
            "when": "Tuesday at nine in the morning",
            "start": fresh[0]["start"],
        }
    ]
    assert create_booking.await_count == 1
    assert get_slots.await_args_list == [
        call(lead_tz="UTC", days=LOOKAHEAD_DAYS),
        call(lead_tz="UTC", days=LOOKAHEAD_DAYS),
    ]
    assert (await tools.select_slot("slot_1"))["error"] == "selection_not_heard"


@pytest.mark.asyncio
async def test_new_availability_invalidates_selection_and_instances_are_isolated() -> None:
    first = make_tools()
    second = make_tools()
    with patch(
        "app.services.calcom_client.get_open_slots", AsyncMock(return_value=[SLOT_1, SLOT_2])
    ):
        await first.check_availability(time_zone="UTC")
        first.observe_user_utterance("first")
        await first.select_slot("slot_1")
        await first.check_availability(time_zone="UTC")

    assert (await first.book_appointment(SLOT_1["start"], icp=ICP))["error"] == (
        "slot_not_selected"
    )
    assert (await second.select_slot("slot_1"))["error"] == "slots_not_offered"


# --- spoken part-hours ---------------------------------------------------------
# The agent now offers times the way people say them ("half past four"), so the
# caller repeats them back that way. A parser that only understood bare hours
# refused the exact phrasing the agent had just used — caught by the two-AI eval
# on 2026-07-30, one turn after the agent said "half past four in the afternoon".


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        ("friday at half past four then", {(4, 30), (16, 30)}),
        ("quarter to five works", {(4, 45), (16, 45)}),  # counts back an hour
        ("quarter past two", {(2, 15), (14, 15)}),
        ("four thirty is fine", {(4, 30), (16, 30)}),
        ("twenty five past nine", {(9, 25), (21, 25)}),  # not "five past nine"
        ("half past ten", {(10, 30), (22, 30)}),
    ],
)
def test_spoken_part_hours_are_understood(utterance: str, expected: set) -> None:
    assert CRMTools._extract_time_matches(utterance) == expected  # noqa: SLF001


def test_a_part_hour_does_not_also_claim_the_bare_hour() -> None:
    """"half past four" must not match a 4:00 slot as well — two matches on one
    clear answer would be reported as ambiguous and re-asked."""
    assert (4, 0) not in CRMTools._extract_time_matches("half past four")  # noqa: SLF001
    assert (5, 0) not in CRMTools._extract_time_matches("quarter to five")  # noqa: SLF001


def test_plain_hours_and_pronouns_are_unchanged() -> None:
    assert CRMTools._extract_time_matches("ten in the morning") == {(10, 0), (22, 0)}  # noqa: SLF001
    assert CRMTools._extract_time_matches("the morning one") == set()  # pronoun  # noqa: SLF001
    assert CRMTools._extract_time_matches("yeah sure") == set()  # noqa: SLF001
    assert CRMTools._extract_time_matches("5 pm") == {(17, 0)}  # noqa: SLF001
