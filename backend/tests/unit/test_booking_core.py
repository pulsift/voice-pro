"""Focused tests for transcript-bound Cal.com booking state."""

from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

from app.core.config import settings
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


def make_tools(**variables: str) -> CRMTools:
    return CRMTools(
        db=MagicMock(),
        user_id=1,
        variables={"leadName": "Sami", "leadEmail": "seeded@example.com", **variables},
    )


def test_normalize_timezone_contract() -> None:
    assert normalize_timezone("Europe/Stockholm", None, "UTC") == "Europe/Stockholm"
    assert normalize_timezone("  Syrian TIME-zone. ", None, "UTC") == "Asia/Damascus"
    assert normalize_timezone("unknown place", "America/New_York", "UTC") == "America/New_York"
    assert normalize_timezone("unknown", "also unknown", "UTC") == "UTC"
    assert normalize_timezone("unknown", "also unknown", "bad/default") is None


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
        "app.services.calcom_client.get_business_slots", AsyncMock(return_value=[SLOT_1, SLOT_2])
    ) as get_slots:
        offered = await tools.check_availability(time_zone="Syrian time zone")

    assert offered["timezone"] == "Asia/Damascus"
    assert [slot["slot_id"] for slot in offered["slots"]] == ["slot_1", "slot_2"]
    get_slots.assert_awaited_once_with(lead_tz="Asia/Damascus")
    assert (await tools.select_slot("slot_2"))["error"] == "selection_not_heard"

    tools.observe_user_utterance("thank you")
    assert (await tools.select_slot("slot_2"))["error"] == "ambiguous_slot_selection"

    tools.observe_user_utterance("the second one")
    selected = await tools.select_slot("slot_2")
    assert selected == {
        "success": True,
        "slot_id": "slot_2",
        "start": SLOT_2["start"],
        "when": SLOT_2["label"],
    }


@pytest.mark.asyncio
async def test_day_or_time_selection_must_identify_exactly_one_slot() -> None:
    tools = make_tools()
    with patch(
        "app.services.calcom_client.get_business_slots", AsyncMock(return_value=[SLOT_1, SLOT_2])
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
    with patch("app.services.calcom_client.get_business_slots", AsyncMock(return_value=slots)):
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
    with patch("app.services.calcom_client.get_business_slots", AsyncMock(return_value=slots)):
        await tools.check_availability(time_zone="Asia/Damascus")

    tools.observe_user_utterance("All right, let's just go for Tuesday at 1 my time.")
    selected = await tools.select_slot("slot_2")
    assert selected["success"] is True
    assert selected["start"] == slots[1]["start"]

    with patch("app.services.calcom_client.get_business_slots", AsyncMock(return_value=slots)):
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
    with patch("app.services.calcom_client.get_business_slots", AsyncMock(return_value=slots)):
        await tools.check_availability(time_zone="Asia/Damascus")

    tools.observe_user_utterance("Let's do the morning one.")
    assert (await tools.select_slot("slot_1"))["success"] is True

    with patch("app.services.calcom_client.get_business_slots", AsyncMock(return_value=slots)):
        await tools.check_availability(time_zone="Asia/Damascus")
    tools.observe_user_utterance("The afternoon works better for me.")
    assert (await tools.select_slot("slot_2"))["success"] is True

    # Two morning slots -> a day-part answer alone must stay ambiguous.
    morning_slots = [
        {"start": "2026-07-14T05:00:00Z", "label": "Tuesday 8:00 AM"},
        {"start": "2026-07-14T07:00:00Z", "label": "Tuesday 10:00 AM"},
    ]
    with patch(
        "app.services.calcom_client.get_business_slots", AsyncMock(return_value=morning_slots)
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
    with patch("app.services.calcom_client.get_business_slots", AsyncMock(return_value=slots)):
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
    webhook = MagicMock()
    with (
        patch(
            "app.services.calcom_client.get_business_slots",
            AsyncMock(return_value=[SLOT_1, SLOT_2]),
        ),
        patch("app.services.calcom_client.create_booking", create_booking),
        patch(
            "app.services.calcom_client.find_existing_booking",
            AsyncMock(return_value={"success": False, "category": "not_found"}),
        ),
        patch("app.services.tools.crm_tools.schedule_fulfilment_webhook", webhook),
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
    webhook.assert_called_once()
    assert any(
        attempt.get("operation") == "create" and attempt.get("category") == "success"
        for attempt in tools.get_booking_attempts()
    )


@pytest.mark.asyncio
async def test_live_email_overrides_seed_and_missing_placeholder_is_rejected() -> None:
    tools = make_tools(leadEmail="{{leadEmail}}")
    with patch("app.services.calcom_client.get_business_slots", AsyncMock(return_value=[SLOT_1])):
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
        patch("app.services.tools.crm_tools.schedule_fulfilment_webhook"),
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
            "booking_failed",
            2,
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
            "app.services.calcom_client.get_business_slots",
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
    if expected_calls == 2:
        assert create_booking.await_args_list[0] == create_booking.await_args_list[1]
    attempts = tools.get_booking_attempts()
    assert sum(attempt["operation"] == "create" for attempt in attempts) == expected_calls
    expected_reconciliations = expected_calls + 1 if expected_calls == 2 else 1
    assert find_existing_booking.await_count == expected_reconciliations


@pytest.mark.asyncio
async def test_transient_retry_success_fires_one_webhook() -> None:
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
    webhook = MagicMock()
    with (
        patch(
            "app.services.calcom_client.get_business_slots",
            AsyncMock(return_value=[SLOT_1, SLOT_2]),
        ),
        patch("app.services.calcom_client.create_booking", create_booking),
        patch("app.services.calcom_client.find_existing_booking", find_existing_booking),
        patch("app.services.tools.crm_tools.asyncio.sleep", AsyncMock()),
        patch("app.services.tools.crm_tools.schedule_fulfilment_webhook", webhook),
    ):
        await tools.check_availability(time_zone="UTC")
        tools.observe_user_utterance("first")
        await tools.select_slot("slot_1")
        result = await tools.book_appointment(SLOT_1["start"], icp=ICP)

    assert result["uid"] == "retry-ok"
    assert create_booking.await_args_list == [create_booking.await_args_list[0]] * 2
    assert find_existing_booking.await_count == 2
    find_existing_booking.assert_awaited_with(start_iso=SLOT_1["start"], email="seeded@example.com")
    webhook.assert_called_once()


@pytest.mark.asyncio
async def test_conflict_refreshes_without_substitute_booking() -> None:
    tools = make_tools()
    fresh = [{"start": "2026-07-14T09:00:00Z", "label": "Tuesday 9:00 AM"}]
    get_slots = AsyncMock(side_effect=[[SLOT_1, SLOT_2], fresh])
    create_booking = AsyncMock(
        return_value={"success": False, "category": "conflict", "status_code": 409}
    )
    with (
        patch("app.services.calcom_client.get_business_slots", get_slots),
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
        {"slot_id": "slot_1", "when": "Tuesday 9:00 AM", "start": fresh[0]["start"]}
    ]
    assert create_booking.await_count == 1
    assert get_slots.await_args_list == [call(lead_tz="UTC"), call(lead_tz="UTC")]
    assert (await tools.select_slot("slot_1"))["error"] == "selection_not_heard"


@pytest.mark.asyncio
async def test_new_availability_invalidates_selection_and_instances_are_isolated() -> None:
    first = make_tools()
    second = make_tools()
    with patch(
        "app.services.calcom_client.get_business_slots", AsyncMock(return_value=[SLOT_1, SLOT_2])
    ):
        await first.check_availability(time_zone="UTC")
        first.observe_user_utterance("first")
        await first.select_slot("slot_1")
        await first.check_availability(time_zone="UTC")

    assert (await first.book_appointment(SLOT_1["start"], icp=ICP))["error"] == (
        "slot_not_selected"
    )
    assert (await second.select_slot("slot_1"))["error"] == "slots_not_offered"
