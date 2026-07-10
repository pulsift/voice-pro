"""Offline tests for pure Voice Pro ops behavior."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

OPS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(OPS))

import cal_booking  # noqa: E402
import call_pull  # noqa: E402
import ops_common  # noqa: E402
import prompt_sync  # noqa: E402
import safety_status  # noqa: E402
import seeded_call  # noqa: E402
import voice_railway  # noqa: E402


def test_campaign_rows_accepts_known_shapes() -> None:
    row = {"id": "1", "name": "campaign"}
    assert safety_status.campaign_rows({"data": [row]}) == [row]
    assert safety_status.campaign_rows({"data": {"campaigns": [row]}}) == [row]
    assert safety_status.campaign_rows({"items": [row]}) == [row]


def test_booking_status_accepts_envelopes() -> None:
    assert cal_booking.booking_status({"data": {"status": "CANCELLED"}}) == "cancelled"
    assert cal_booking.booking_status({"bookingStatus": "accepted"}) == "accepted"
    assert cal_booking.booking_status("bad") == "unknown"


def test_sanitize_booking_attempts_drops_pii_and_secrets() -> None:
    attempts = ops_common.sanitize_booking_attempts(
        [
            {
                "uid": "abc",
                "status_code": 200,
                "attendee_email": "private@example.com",
                "authorization": "secret",
            }
        ]
    )
    assert attempts == [{"uid": "abc", "status_code": 200}]


def test_prompt_hash_normalizes_newlines() -> None:
    assert prompt_sync.sha256("one\r\ntwo\n") == prompt_sync.sha256("one\ntwo")


def test_patch_prompt_rejects_unknown_source() -> None:
    with pytest.raises(ops_common.OpsError, match="prompt anchor count"):
        prompt_sync.patch_prompt("unrelated prompt")


def test_masked_phone_shows_only_last_four() -> None:
    assert ops_common.masked_phone("+963998183191") == "***3191"


def test_temp_workflows_disable_all_execution_data() -> None:
    assert ops_common.no_execution_data_settings() == {
        "saveDataErrorExecution": "none",
        "saveDataSuccessExecution": "none",
        "saveExecutionProgress": False,
        "saveManualExecutions": False,
    }


def test_temp_workflow_delete_failure_is_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_n8n(
        path: str, *, method: str = "GET", body: object | None = None
    ) -> object:
        del body
        if path == "/api/v1/workflows" and method == "POST":
            return {"id": "temp-id"}
        if path == "/api/v1/workflows/temp-id" and method == "DELETE":
            raise ops_common.OpsError("delete failed")
        return {}

    monkeypatch.setattr(ops_common, "n8n_api", fake_n8n)
    monkeypatch.setattr(ops_common, "request_json", lambda *args, **kwargs: (200, {}))
    monkeypatch.setattr(ops_common.time, "sleep", lambda _: None)
    with pytest.raises(ops_common.OpsError, match="FATAL.*cleanup failed"):
        ops_common.run_temporary_workflow(
            name="test",
            hook_path="test-hook",
            nodes=[],
            connections={},
        )


def test_kill_read_is_narrow_and_never_queries_whole_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> object:
        captured.update(kwargs)
        return [{"paused": True}]

    monkeypatch.setattr(ops_common, "run_temporary_workflow", fake_run)
    assert ops_common.read_kill_state() is True
    serialized = repr(captured)
    assert "jsonb_object_agg" not in serialized
    assert "kill_switch" in serialized
    assert "kill_token" not in serialized


def test_seeded_forge_keeps_hmac_inside_n8n(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> object:
        captured.update(kwargs)
        return [{"accepted": True, "conversation_id": "cid"}]

    monkeypatch.setattr(ops_common, "run_temporary_workflow", fake_run)
    assert ops_common.forge_seeded_reply() == "cid"
    serialized = repr(captured)
    assert "jsonb_object_agg" not in serialized
    assert "sendkit_hmac_positive" in serialized
    assert "Sign Internally" in serialized
    assert "secret" not in repr(captured.get("connections"))


def test_prompt_failed_verification_auto_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writes: list[str] = []
    reads = iter(["wrong-target", "old"])

    def fake_admin(
        path: str, *, method: str = "GET", body: object | None = None
    ) -> object:
        del path, method
        assert isinstance(body, dict)
        writes.append(str(body["system_prompt"]))
        return {}

    monkeypatch.setattr(prompt_sync, "admin_request", fake_admin)
    monkeypatch.setattr(prompt_sync, "live_prompt", lambda: next(reads))
    with pytest.raises(ops_common.OpsError, match="auto-rollback restored"):
        prompt_sync.replace_with_rollback("old", "target", validate_target=False)
    assert writes == ["target", "old"]


def test_new_deployment_selection_excludes_preexisting_ids() -> None:
    deployments = [{"id": "new"}, {"id": "old"}]
    assert voice_railway.select_new_deployment(deployments, {"old"}) == {"id": "new"}
    assert voice_railway.select_new_deployment([{"id": "old"}], {"old"}) is None


def test_status_requires_exact_new_deployment_and_runtime_health_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        voice_railway,
        "latest_deployments",
        lambda: [
            {
                "id": "new",
                "status": "SUCCESS",
                "meta": {"commitHash": "sha"},
            },
            {
                "id": "old",
                "status": "SUCCESS",
                "meta": {"commitHash": "sha"},
            },
        ],
    )
    monkeypatch.setattr(
        voice_railway,
        "current_variables",
        lambda: {
            voice_railway.MODEL_KEY: "gpt-realtime-2.1",
            voice_railway.REASONING_KEY: "low",
        },
    )
    monkeypatch.setattr(
        voice_railway,
        "request_json",
        lambda *args, **kwargs: (
            200,
            {
                "realtime_model": "gpt-realtime-2.1",
                "realtime_reasoning_effort": "low",
            },
        ),
    )
    assert voice_railway.show_status(
        "sha",
        0,
        excluded_ids={"old"},
        expected_model="gpt-realtime-2.1",
        expected_reasoning="low",
    ) == (True, "new")


def test_status_does_not_accept_preexisting_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        voice_railway,
        "latest_deployments",
        lambda: [{"id": "old", "status": "SUCCESS", "meta": {"commitHash": "sha"}}],
    )
    assert voice_railway.show_status("sha", 0, excluded_ids={"old"}) == (False, None)


def test_runtime_model_proof_rejects_stale_and_accepts_fresh_exact_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = {
        "deploymentLogs": [
            {
                "timestamp": "2026-07-10T17:59:59Z",
                "message": "connecting_to_openai_realtime model=gpt-realtime-2.1",
            },
            {
                "timestamp": "2026-07-10T18:00:01Z",
                "message": "connecting_to_openai_realtime model=gpt-realtime-2025-08-28",
            },
            {
                "timestamp": "2026-07-10T18:00:02Z",
                "message": "connecting_to_openai_realtime model=gpt-realtime-2.1",
            },
        ]
    }
    monkeypatch.setattr(voice_railway, "railway_graphql", lambda *args, **kwargs: logs)
    assert voice_railway.runtime_model_proof(
        "deployment",
        "gpt-realtime-2.1",
        datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc),
        0,
    )


def test_seeded_call_detects_only_new_exact_destination_record() -> None:
    started = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)
    calls = [
        {
            "id": "old",
            "to_number": ops_common.SEEDED_PHONE,
            "started_at": "2026-07-10T18:01:00Z",
        },
        {
            "id": "wrong-number",
            "to_number": "+10000000000",
            "started_at": "2026-07-10T18:02:00Z",
        },
        {
            "id": "new",
            "to_number": ops_common.SEEDED_PHONE,
            "started_at": "2026-07-10T18:03:00Z",
        },
    ]
    assert seeded_call.find_new_seeded_call(calls, {"old"}, started) == calls[2]


def test_seeded_call_preflight_failure_never_starts_watchdog_or_disarms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        seeded_call,
        "preflight",
        lambda **kwargs: (_ for _ in ()).throw(ops_common.OpsError("unsafe")),
    )
    watchdog_started = False
    kill_calls: list[bool] = []

    def fake_watchdog(_: int) -> object:
        nonlocal watchdog_started
        watchdog_started = True
        return object()

    monkeypatch.setattr(seeded_call, "spawn_watchdog", fake_watchdog)
    monkeypatch.setattr(
        seeded_call,
        "set_kill_switch",
        lambda *, paused: kill_calls.append(paused) or paused,
    )
    with pytest.raises(ops_common.OpsError, match="unsafe"):
        seeded_call.run_call(
            30,
            1,
            expected_sha="sha",
            expected_deployment_id="dep",
            expected_model="model",
            expected_reasoning=None,
            expected_prompt_sha="prompt",
        )
    assert not watchdog_started
    assert kill_calls == []


def test_seeded_call_rearms_on_first_new_call_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeWatchdog:
        terminated = False

        def terminate(self) -> None:
            self.terminated = True

    fake_watchdog = FakeWatchdog()
    kill_calls: list[bool] = []
    monkeypatch.setattr(seeded_call, "preflight", lambda **kwargs: ("token", {"old"}))
    monkeypatch.setattr(seeded_call, "spawn_watchdog", lambda _: fake_watchdog)
    monkeypatch.setattr(
        seeded_call,
        "set_kill_switch",
        lambda *, paused: kill_calls.append(paused) or paused,
    )
    monkeypatch.setattr(seeded_call, "forge_seeded_reply", lambda: "cid")
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
    assert (
        seeded_call.run_call(
            30,
            1,
            expected_sha="sha",
            expected_deployment_id="dep",
            expected_model="model",
            expected_reasoning=None,
            expected_prompt_sha="prompt",
        )
        == 0
    )
    assert kill_calls == [False, True]
    assert fake_watchdog.terminated


def test_booking_sequence_assertion_rejects_premature_or_missing_selection() -> None:
    call_pull.assert_booking_sequence(["availability", "select", "book"])
    with pytest.raises(ops_common.OpsError, match="not availability"):
        call_pull.assert_booking_sequence(["availability", "book", "select"])
    with pytest.raises(ops_common.OpsError, match="incomplete"):
        call_pull.assert_booking_sequence(["availability", "book"])
