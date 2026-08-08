"""The rig must fail when production cannot book.

This is the regression guard for 2026-08-08, and it is worth more than the fix
it guards. On that day the live agent row held `enabled_tools = []`. The tool
registry only hands the model a tool when its integration id appears in that
list, so every real call was configured with ZERO tools: no way to select a
time, save a fit answer, refresh the calendar, book, or hang up.

The agent behaved exactly as an agent with no tools behaves. It said "Thursday
at midday it is, you're all set" — there was no `book_appointment` to fail, so
nothing contradicted it. It argued about the calendar twice — there was no
`refresh_availability`, and the times were static text pasted into the prompt.
It said "let me capture that" — narrating a tool call it could not make.

And the eval rig passed 10/10 through all of it, because the rig built its own
tool list with `CRMTools.get_tool_definitions() + CallControlTools...` and never
consulted the registry or the agent row. The rig had tools. Production did not.

That is the same shape as the four failures before it: the test harness is not
the system. These tests exist so the harness cannot diverge from it again.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parents[1]
BACKEND = EVALS.parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(EVALS))

import convo_eval  # noqa: E402

PRODUCTION_TOOLS = ["crm", "call_control"]


def test_the_live_configuration_produces_every_load_bearing_tool() -> None:
    """What production is set to today must survive the guard."""
    tools = convo_eval.tool_definitions(PRODUCTION_TOOLS)
    names = set(convo_eval.tool_names(tools))
    for required in convo_eval.LOAD_BEARING_TOOLS:
        assert required in names, required


def test_the_load_bearing_list_cannot_be_quietly_shortened() -> None:
    """Pin the five names independently of the tuple that holds them.

    Found by mutation: every other test in this file reads
    `LOAD_BEARING_TOOLS`, so deleting `book_appointment` from that tuple shrinks
    the tests along with the guard and the whole file stays green — disarming
    the check on the single tool the call exists for. A test that derives its
    expectation from the thing it is testing cannot catch that.
    """
    assert set(convo_eval.LOAD_BEARING_TOOLS) == {
        "select_slot",
        "record_fit_answers",
        "book_appointment",
        "refresh_availability",
        "end_call",
    }


def test_an_empty_tool_list_refuses_to_run_rather_than_passing_green() -> None:
    """THE one. `enabled_tools = []` is the exact production state of 2026-08-08.

    Before this guard the rig ran happily and reported 10/10. Now it refuses.
    """
    with pytest.raises(convo_eval.ToolConfigError) as caught:
        convo_eval.tool_definitions([])
    message = str(caught.value)
    assert "book_appointment" in message
    assert "select_slot" in message


@pytest.mark.parametrize(
    ("enabled", "lost"),
    [
        (["call_control"], "book_appointment"),  # crm dropped: cannot book at all
        (["crm"], "end_call"),  # call_control dropped: cannot hang up
    ],
)
def test_losing_either_half_of_the_configuration_is_caught(
    enabled: list[str], lost: str
) -> None:
    """Half a toolbox is not a degraded call, it is a broken one."""
    with pytest.raises(convo_eval.ToolConfigError) as caught:
        convo_eval.tool_definitions(enabled)
    assert lost in str(caught.value)


def test_a_typo_in_the_tool_list_is_caught_rather_than_silently_ignored() -> None:
    """The registry ignores ids it does not know, so a typo reads as "no tools"
    rather than as an error. `crm` misspelled once is a silent outage."""
    with pytest.raises(convo_eval.ToolConfigError):
        convo_eval.tool_definitions(["crmm", "call_control"])


def test_granular_tool_ids_that_exclude_booking_are_caught() -> None:
    """`enabled_tool_ids` narrows an integration to a subset. A subset that drops
    booking is the same outage by a different door — and it is the door that is
    open right now, since the column exists and defaults to empty."""
    with pytest.raises(convo_eval.ToolConfigError) as caught:
        convo_eval.tool_definitions(
            PRODUCTION_TOOLS, {"crm": ["search_customer", "create_contact"]}
        )
    assert "book_appointment" in str(caught.value)


def test_the_rig_builds_its_tools_through_the_registry_not_around_it() -> None:
    """The structural guarantee, asserted rather than assumed.

    If someone reintroduces a hand-built list, `tool_definitions` stops matching
    what `ToolRegistry.get_all_tool_definitions` returns and this fails. That is
    the only property that keeps the rig honest as the tool surface changes.
    """
    from unittest.mock import MagicMock

    from app.services.tools.registry import ToolRegistry

    registry = ToolRegistry(db=MagicMock(), user_id=1, integrations={}, workspace_id=None)
    expected = registry.get_all_tool_definitions(PRODUCTION_TOOLS)
    assert convo_eval.tool_definitions(PRODUCTION_TOOLS) == expected


def test_the_production_session_and_the_rig_ask_the_registry_the_same_question() -> None:
    """`gpt_realtime._configure_session` is the code the rig is imitating. If it
    ever stops calling `get_all_tool_definitions`, this rig is imitating nothing
    and the divergence starts again silently."""
    import inspect

    from app.services.gpt_realtime import GPTRealtimeSession

    # SLF001 deliberately: reading the private method's SOURCE is the only way to
    # assert the rig imitates it. A public wrapper added just to satisfy the
    # linter would be counter-code — production surface invented for a test.
    source = inspect.getsource(GPTRealtimeSession._configure_session)  # noqa: SLF001
    assert 'self.agent_config.get("enabled_tools"' in source
    assert "get_all_tool_definitions(enabled_tools)" in source
