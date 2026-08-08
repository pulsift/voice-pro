# ruff: noqa: SLF001 - this file exists to test one private method and the
# counter behind it. A public wrapper added only to satisfy the linter would be
# production surface invented for a test.
"""A broken line gets closed, not waited on forever.

`wait_for_user` is how the agent stays quiet through a stray noise — correct
once. Twice running with nothing usable means the LINE is at fault, and silence
becomes the worst possible answer: the caller hears dead air while we hold a
billed leg open until the bridge times out.

The prompt has carried that rule in prose for weeks. It does not hold, because
it asks the model to count. On 2026-08-08 the eval's garbled-line scenario fed
four unusable turns; the agent called `wait_for_user` three times running and
never hung up. Counting is arithmetic, so it lives in code now and the model
keeps only the words.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.gpt_realtime import DEAD_AIR_WAIT_LIMIT, GPTRealtimeSession


def session() -> GPTRealtimeSession:
    made = GPTRealtimeSession.__new__(GPTRealtimeSession)
    made._consecutive_waits = 0
    made.session_id = "s-1"
    made.logger = MagicMock()
    return made


def wait(made: GPTRealtimeSession) -> dict[str, object]:
    return made._apply_dead_air_limit("wait_for_user", {"success": True})


def test_one_stray_noise_is_still_met_with_silence() -> None:
    """The whole point of wait_for_user. A single cough must not end a call."""
    result = wait(session())
    assert "dead_air_limit_reached" not in result


def test_two_unusable_turns_running_close_the_call() -> None:
    """THE regression. Sami's rule: do not try a third time."""
    made = session()
    wait(made)
    result = wait(made)
    assert result["dead_air_limit_reached"] is True
    message = str(result["message"]).lower()
    assert "breaking up" in message
    assert "email" in message
    assert "end_call" in message


def test_the_bail_out_never_asks_them_anything() -> None:
    """They are unreachable — a question is something they cannot answer, and it
    would leave the agent waiting again on the same dead line."""
    made = session()
    wait(made)
    message = str(wait(made)["message"]).lower()
    assert "?" not in message
    assert "do not ask again" in message


def test_a_real_answer_resets_the_count() -> None:
    """Any other tool firing is evidence the caller got through. One bad turn
    early in a call must not stack with one twenty turns later."""
    made = session()
    wait(made)
    made._apply_dead_air_limit("record_fit_answers", {"success": True})
    assert made._consecutive_waits == 0
    assert "dead_air_limit_reached" not in wait(made)


def test_the_counter_resets_after_bailing_so_it_cannot_fire_twice() -> None:
    """The bail-out already asks for end_call. Re-firing would stack a second
    goodbye onto a call that is closing."""
    made = session()
    wait(made)
    assert wait(made)["dead_air_limit_reached"] is True
    assert made._consecutive_waits == 0
    assert "dead_air_limit_reached" not in wait(made)


def test_the_original_result_is_preserved_not_replaced() -> None:
    made = session()
    made._apply_dead_air_limit("wait_for_user", {"success": True, "keep": "me"})
    result = made._apply_dead_air_limit("wait_for_user", {"success": True, "keep": "me"})
    assert result["success"] is True
    assert result["keep"] == "me"


def test_a_non_wait_tool_is_returned_untouched() -> None:
    made = session()
    original = {"success": True, "when": "Tuesday at midday"}
    assert made._apply_dead_air_limit("select_slot", original) == original


def test_the_limit_is_small_enough_to_matter() -> None:
    """A limit of five would mean a minute of dead air on a broken line."""
    assert DEAD_AIR_WAIT_LIMIT == 2


@pytest.mark.asyncio
async def test_hitting_the_limit_makes_the_agent_speak_rather_than_stay_silent() -> None:
    """wait_for_user deliberately suppresses the follow-up response — that is how
    it stays quiet. At the limit that suppression is exactly the bug, so the
    response MUST be requested or the instruction is never spoken."""
    made = session()
    made.connection = MagicMock()
    made.connection.conversation.item.create = _async_noop()
    made.connection.response.create = _async_noop()
    made.handle_tool_call = _async_value({"success": True})

    event = SimpleNamespace(call_id="c1", name="wait_for_user", arguments="{}")
    await made.handle_function_call_event(event)
    assert made.connection.response.create.await_count == 0, "first wait must stay silent"

    await made.handle_function_call_event(event)
    assert made.connection.response.create.await_count == 1, (
        "at the limit the agent has to speak the closing line"
    )


def _async_noop() -> MagicMock:
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=None)


def _async_value(value: object) -> MagicMock:
    from unittest.mock import AsyncMock

    return AsyncMock(return_value=value)
