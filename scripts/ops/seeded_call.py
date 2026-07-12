"""Place exactly one seeded Sami test call through n8n or directly through Voice Pro."""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

from ops_common import (
    AGENT_ID,
    BACKEND,
    FULFILMENT,
    REAL_CAMPAIGN_IDS,
    SEEDED_PHONE,
    OpsError,
    admin_token,
    commit_hash,
    current_variables,
    forge_seeded_reply,
    kill_paused,
    latest_deployments,
    masked_phone,
    request_json,
    set_kill_switch,
)
from safety_status import list_campaigns, seeded_lead_phone


DIRECT_FROM_NUMBER = "+16693694746"  # Pulsift Twilio number (outbound caller ID)
DIRECT_TEST_VARIABLES: dict[str, str] = {
    "agentName": "Dave",
    "leadName": "Sami",
    "company": "Pulsift",
    "leadEmail": "sami@pulsift.com",
    "leadPhone": SEEDED_PHONE,
    "phone": SEEDED_PHONE,
    "tzName": "Asia/Damascus",
    "brief": "Seeded Voice Pro booking test for Pulsift's solar lead-list offer.",
    "offer_name": "the free list of a hundred solar leads",
    "offer_value_line": (
        "it's a hundred solar businesses matched to who you actually sell to"
    ),
    "bonus_line": (
        "you're also set for an expert's audit of how you're currently getting clients"
    ),
    "book_reason_audit_no": (
        "either way, to build your hundred so they're genuinely qualified for what "
        "you do, the team needs a few details about your ideal customer"
    ),
    "meeting_purpose": "Pulsift - lead-list scoping and audit",
}


def watchdog(after: int) -> int:
    time.sleep(after)
    try:
        set_kill_switch(paused=True)
    except Exception:
        return 2
    return 0


def spawn_watchdog(after: int) -> subprocess.Popen[bytes]:
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    return subprocess.Popen(
        [sys.executable, str(__file__), "--watchdog", "--after", str(after)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def call_list(token: str) -> list[dict[str, object]]:
    status, payload = request_json(
        f"{BACKEND}/api/v1/calls?direction=outbound&page=1&page_size=20",
        headers={"Authorization": f"Bearer {token}"},
    )
    if status != 200:
        raise OpsError(f"CallRecord poll returned HTTP {status}")
    if not isinstance(payload, dict):
        return []
    calls = payload.get("calls") or []
    return [call for call in calls if isinstance(call, dict)]


def find_new_seeded_call(
    calls: list[dict[str, object]],
    baseline_ids: set[str],
    started_after: datetime,
) -> dict[str, object] | None:
    for call in calls:
        call_id = str(call.get("id") or "")
        if call_id in baseline_ids or call.get("to_number") != SEEDED_PHONE:
            continue
        raw_started = str(call.get("started_at") or "").replace("Z", "+00:00")
        try:
            call_started = datetime.fromisoformat(raw_started)
            if call_started.tzinfo is None:
                call_started = call_started.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if call_started >= started_after:
            return call
    return None


def prompt_hash(text: str) -> str:
    normalized = text.replace("\r\n", "\n").rstrip("\n")
    return hashlib.sha256(normalized.encode()).hexdigest()


def preflight(
    *,
    expected_sha: str,
    expected_deployment_id: str,
    expected_model: str,
    expected_reasoning: str | None,
    expected_prompt_sha: str,
) -> tuple[str, set[str]]:
    """Fail closed before the kill switch can be disarmed."""
    if SEEDED_PHONE != "+963998183191":
        raise OpsError("seeded destination invariant failed")
    # Prove the destination BEFORE anything can disarm: read the lead the dialer will
    # actually call and require its stored phone to equal the seed. A constant check
    # alone would not catch a lead fixture whose phone drifted to a stranger's number.
    if seeded_lead_phone() != SEEDED_PHONE:
        raise OpsError(
            "seeded lead's stored phone does not match the seed; refusing to disarm"
        )
    token = admin_token()
    backend_status, backend_health = request_json(f"{BACKEND}/health")
    if backend_status != 200 or not isinstance(backend_health, dict):
        raise OpsError(f"backend health returned HTTP {backend_status}")
    # Treat unset reasoning uniformly: /health emits "" while the CLI passes None for
    # the old-model path, so normalize both sides or the old-model call never runs.
    if backend_health.get("realtime_model") != expected_model or (
        backend_health.get("realtime_reasoning_effort") or None
    ) != (expected_reasoning or None):
        raise OpsError(
            "backend runtime model/config does not match the expected test config"
        )
    fulfilment_status, fulfilment = request_json(f"{FULFILMENT}/health")
    if (
        fulfilment_status != 200
        or not isinstance(fulfilment, dict)
        or fulfilment.get("stub_mode") is not True
    ):
        raise OpsError("fulfilment is not confirmed in stub mode")

    campaigns = list_campaigns()
    campaign_states = {
        str(item.get("_id") or item.get("id") or ""): str(
            item.get("status") or ""
        ).lower()
        for item in campaigns
    }
    if any(
        campaign_states.get(campaign_id) != "draft" for campaign_id in REAL_CAMPAIGN_IDS
    ):
        raise OpsError("one or more real solar campaigns is missing or not draft")
    if not kill_paused():
        raise OpsError("kill switch was not ON at preflight")

    deployments = latest_deployments()
    if not deployments:
        raise OpsError("Railway returned no backend deployment")
    latest = deployments[0]
    if (
        latest.get("status") != "SUCCESS"
        or commit_hash(latest) != expected_sha
        or str(latest.get("id") or "") != expected_deployment_id
    ):
        raise OpsError(
            "latest backend deployment is not the exact expected SUCCESS deployment"
        )
    variables = current_variables()
    if variables.get("OPENAI_REALTIME_MODEL") != expected_model:
        raise OpsError("Railway model variable does not match the expected test model")

    agent_status, agent = request_json(
        f"{BACKEND}/api/v1/agents/{AGENT_ID}",
        headers={"Authorization": f"Bearer {token}"},
    )
    if agent_status != 200 or not isinstance(agent, dict):
        raise OpsError(f"agent preflight returned HTTP {agent_status}")
    if prompt_hash(str(agent.get("system_prompt") or "")) != expected_prompt_sha:
        raise OpsError("live prompt hash does not match the expected tested prompt")

    baseline_ids = {str(call.get("id") or "") for call in call_list(token)}
    print(
        f"preflight_safe=true deployment={latest.get('id')} commit={expected_sha} "
        f"model={expected_model} prompt_sha256={expected_prompt_sha}"
    )
    return token, baseline_ids


def place_direct_voice_pro_call(token: str) -> dict[str, object]:
    """Dial the compiled seed through Voice Pro without touching the n8n kill gate."""
    body = {
        "to_number": SEEDED_PHONE,
        "from_number": DIRECT_FROM_NUMBER,
        "agent_id": AGENT_ID,
        "variables": dict(DIRECT_TEST_VARIABLES),
    }
    status, payload = request_json(
        f"{BACKEND}/api/v1/telephony/calls",
        method="POST",
        body=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    if status not in {200, 201}:
        raise OpsError(f"direct Voice Pro call returned HTTP {status}")
    if not isinstance(payload, dict):
        raise OpsError("direct Voice Pro call returned an invalid response")
    if (
        payload.get("to_number") != SEEDED_PHONE
        or payload.get("from_number") != DIRECT_FROM_NUMBER
        or payload.get("agent_id") != AGENT_ID
        or not payload.get("call_id")
    ):
        raise OpsError("direct Voice Pro call response did not match the compiled seed")
    return payload


def run_direct_call(
    window: int,
    poll: int,
    *,
    expected_sha: str,
    expected_deployment_id: str,
    expected_model: str,
    expected_reasoning: str | None,
    expected_prompt_sha: str,
) -> int:
    """Call Voice Pro directly while the n8n kill switch remains continuously ON."""
    token, baseline_ids = preflight(
        expected_sha=expected_sha,
        expected_deployment_id=expected_deployment_id,
        expected_model=expected_model,
        expected_reasoning=expected_reasoning,
        expected_prompt_sha=expected_prompt_sha,
    )
    started = datetime.now(timezone.utc)
    print(f"call_probe_started_at={started.isoformat()}")
    try:
        if not kill_paused():
            raise OpsError("kill switch changed after preflight; refusing direct call")
        response = place_direct_voice_pro_call(token)
        print(
            "direct_voice_pro_call_accepted=true "
            f"call_id={response.get('call_id')} "
            f"destination={masked_phone(SEEDED_PHONE)} kill_switch_on=true"
        )
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            if not kill_paused():
                raise OpsError("kill switch changed during direct-call polling")
            call = find_new_seeded_call(call_list(token), baseline_ids, started)
            if call:
                status = str(call.get("status") or "unknown").lower()
                print(f"call_id={call.get('id')} status={status}")
                print("kill_switch_on=true direct_mode=true")
                return 0
            time.sleep(poll)
        print("call_window_expired=true")
        return 2
    finally:
        # Direct mode never opens the gate. If unrelated state drift turns it off,
        # fail safe by re-arming; this branch can only ever write paused=True.
        try:
            still_paused = kill_paused()
        except Exception:
            still_paused = False
        if not still_paused:
            observed = set_kill_switch(paused=True)
            print(f"kill_switch_on={str(observed).lower()}")


def run_call(
    window: int,
    poll: int,
    *,
    expected_sha: str,
    expected_deployment_id: str,
    expected_model: str,
    expected_reasoning: str | None,
    expected_prompt_sha: str,
) -> int:
    token, baseline_ids = preflight(
        expected_sha=expected_sha,
        expected_deployment_id=expected_deployment_id,
        expected_model=expected_model,
        expected_reasoning=expected_reasoning,
        expected_prompt_sha=expected_prompt_sha,
    )
    started = datetime.now(timezone.utc)
    print(f"call_probe_started_at={started.isoformat()}")
    watchdog_process = spawn_watchdog(window + 15)
    conversation_id = ""
    rearmed = False
    try:
        if set_kill_switch(paused=False):
            raise OpsError(
                "kill switch remained ON after the explicit test-call disarm"
            )
        print(f"kill_switch_on=false destination={masked_phone(SEEDED_PHONE)}")
        conversation_id = forge_seeded_reply()
        print(f"seeded_reply_accepted=true conversation_id={conversation_id}")
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            call = find_new_seeded_call(call_list(token), baseline_ids, started)
            if call:
                status = str(call.get("status") or "unknown").lower()
                rearmed = set_kill_switch(paused=True)
                print(f"call_id={call.get('id')} status={status}")
                print("kill_switch_on=true rearmed_on_first_new_call_record=true")
                return 0
            time.sleep(poll)
        print("call_window_expired=true")
        return 2
    finally:
        try:
            if not rearmed:
                observed = set_kill_switch(paused=True)
                rearmed = observed
                print(f"kill_switch_on={str(observed).lower()}")
        finally:
            if rearmed:
                watchdog_process.terminate()
            else:
                print(
                    "kill_switch_rearm_unverified=true watchdog_retained=true",
                    file=sys.stderr,
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--confirm", action="store_true")
    parser.add_argument(
        "--mode",
        choices=("n8n", "direct"),
        default="n8n",
        help="n8n opens its guarded gate; direct calls Voice Pro with the gate kept ON",
    )
    parser.add_argument("--window", type=int, default=300, choices=range(30, 301))
    parser.add_argument("--poll", type=int, default=5)
    parser.add_argument("--expected-sha", required=False)
    parser.add_argument("--expected-deployment-id", required=False)
    parser.add_argument(
        "--expected-model",
        choices=("gpt-realtime-2025-08-28", "gpt-realtime-2.1"),
        required=False,
    )
    parser.add_argument("--expected-prompt-sha", required=False)
    parser.add_argument("--expected-reasoning", choices=("low", "none"), default="none")
    parser.add_argument("--watchdog", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--after", type=int, default=315, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.watchdog:
        return watchdog(args.after)
    if not args.confirm:
        raise OpsError("seeded call requires --confirm")
    if (
        not args.expected_sha
        or not args.expected_deployment_id
        or not args.expected_model
        or not args.expected_prompt_sha
    ):
        raise OpsError(
            "seeded call requires --expected-sha, --expected-deployment-id, "
            "--expected-model, and --expected-prompt-sha"
        )
    runner = run_direct_call if args.mode == "direct" else run_call
    return runner(
        args.window,
        args.poll,
        expected_sha=args.expected_sha,
        expected_deployment_id=args.expected_deployment_id,
        expected_model=args.expected_model,
        expected_reasoning=None
        if args.expected_reasoning == "none"
        else args.expected_reasoning,
        expected_prompt_sha=args.expected_prompt_sha,
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpsError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
