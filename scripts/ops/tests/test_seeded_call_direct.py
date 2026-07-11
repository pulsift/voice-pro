"""Adversarial offline checks for seeded_call's direct Voice Pro route."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS))

import ops_common  # noqa: E402
import seeded_call  # noqa: E402


def _run_direct() -> int:
    return seeded_call.run_direct_call(
        30,
        1,
        expected_sha="sha",
        expected_deployment_id="deployment",
        expected_model="model",
        expected_reasoning=None,
        expected_prompt_sha="prompt",
    )


def test_direct_mode_posts_only_compiled_seed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_request(url: str, **kwargs: object) -> tuple[int, object]:
        captured["url"] = url
        captured.update(kwargs)
        return (
            200,
            {
                "call_id": "provider-call",
                "to_number": ops_common.SEEDED_PHONE,
                "from_number": seeded_call.DIRECT_FROM_NUMBER,
                "agent_id": ops_common.AGENT_ID,
            },
        )

    monkeypatch.setattr(seeded_call, "request_json", fake_request)
    result = seeded_call.place_direct_voice_pro_call("runtime-token")

    assert result["call_id"] == "provider-call"
    assert captured == {
        "url": f"{ops_common.BACKEND}/api/v1/telephony/calls",
        "method": "POST",
        "body": {
            "to_number": "+963998183191",
            "from_number": "+14086649020",
            "agent_id": "06a42ae8-6169-4055-a752-8ef561d8d2aa",
            "variables": dict(seeded_call.DIRECT_TEST_VARIABLES),
        },
        "headers": {"Authorization": "Bearer runtime-token"},
    }
    assert captured["body"]["variables"]["leadPhone"] == "+963998183191"  # type: ignore[index]
    assert captured["body"]["variables"]["phone"] == "+963998183191"  # type: ignore[index]


def test_direct_mode_never_disarms_kill_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill_writes: list[bool] = []
    monkeypatch.setattr(seeded_call, "preflight", lambda **kwargs: ("token", {"old"}))
    monkeypatch.setattr(seeded_call, "kill_paused", lambda: True)
    monkeypatch.setattr(
        seeded_call,
        "set_kill_switch",
        lambda *, paused: kill_writes.append(paused) or paused,
    )
    monkeypatch.setattr(
        seeded_call,
        "place_direct_voice_pro_call",
        lambda _: {
            "call_id": "provider-call",
            "to_number": ops_common.SEEDED_PHONE,
            "from_number": seeded_call.DIRECT_FROM_NUMBER,
            "agent_id": ops_common.AGENT_ID,
        },
    )
    monkeypatch.setattr(
        seeded_call,
        "call_list",
        lambda _: [
            {
                "id": "new",
                "to_number": ops_common.SEEDED_PHONE,
                "started_at": "2100-01-01T00:00:00Z",
                "status": "initiated",
            }
        ],
    )

    assert _run_direct() == 0
    assert False not in kill_writes
    assert kill_writes == []


def test_direct_mode_preflight_failure_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posts: list[str] = []
    kill_writes: list[bool] = []
    monkeypatch.setattr(
        seeded_call,
        "preflight",
        lambda **kwargs: (_ for _ in ()).throw(ops_common.OpsError("unsafe")),
    )
    monkeypatch.setattr(
        seeded_call,
        "place_direct_voice_pro_call",
        lambda _: posts.append("posted") or {},
    )
    monkeypatch.setattr(
        seeded_call,
        "set_kill_switch",
        lambda *, paused: kill_writes.append(paused) or paused,
    )

    with pytest.raises(ops_common.OpsError, match="unsafe"):
        _run_direct()
    assert posts == []
    assert kill_writes == []


def test_direct_mode_rearms_only_on_unrelated_kill_state_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill_reads = iter([True, True, False])
    kill_writes: list[bool] = []
    monkeypatch.setattr(seeded_call, "preflight", lambda **kwargs: ("token", {"old"}))
    monkeypatch.setattr(seeded_call, "kill_paused", lambda: next(kill_reads))
    monkeypatch.setattr(
        seeded_call,
        "set_kill_switch",
        lambda *, paused: kill_writes.append(paused) or paused,
    )
    monkeypatch.setattr(
        seeded_call,
        "place_direct_voice_pro_call",
        lambda _: {
            "call_id": "provider-call",
            "to_number": ops_common.SEEDED_PHONE,
            "from_number": seeded_call.DIRECT_FROM_NUMBER,
            "agent_id": ops_common.AGENT_ID,
        },
    )
    monkeypatch.setattr(
        seeded_call,
        "call_list",
        lambda _: [
            {
                "id": "new",
                "to_number": ops_common.SEEDED_PHONE,
                "started_at": "2100-01-01T00:00:00Z",
            }
        ],
    )

    assert _run_direct() == 0
    assert kill_writes == [True]


def test_direct_mode_rearms_without_post_when_kill_changes_after_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    kill_reads = iter([False, False])
    kill_writes: list[bool] = []
    posts: list[str] = []
    monkeypatch.setattr(seeded_call, "preflight", lambda **kwargs: ("token", {"old"}))
    monkeypatch.setattr(seeded_call, "kill_paused", lambda: next(kill_reads))
    monkeypatch.setattr(
        seeded_call,
        "set_kill_switch",
        lambda *, paused: kill_writes.append(paused) or paused,
    )
    monkeypatch.setattr(
        seeded_call,
        "place_direct_voice_pro_call",
        lambda _: posts.append("posted") or {},
    )

    with pytest.raises(ops_common.OpsError, match="changed after preflight"):
        _run_direct()
    assert posts == []
    assert kill_writes == [True]
