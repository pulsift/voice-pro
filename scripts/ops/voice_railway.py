"""Inspect Voice Pro Railway deployments and perform explicit model-only env swaps."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import sys
import time
from typing import Any

from ops_common import (
    BACKEND,
    BACKEND_SERVICE_ID,
    ENVIRONMENT_ID,
    PROJECT_ID,
    OpsError,
    commit_hash,
    current_variables,
    latest_deployments,
    railway_graphql,
    request_json,
)

MODEL_KEY = "OPENAI_REALTIME_MODEL"
REASONING_KEY = "OPENAI_REALTIME_REASONING_EFFORT"
ALLOWED_MODELS = {"gpt-realtime-2025-08-28", "gpt-realtime-2.1"}


def select_new_deployment(
    deployments: list[dict[str, Any]], excluded_ids: set[str]
) -> dict[str, Any] | None:
    """Return the newest deployment that did not exist before the mutation."""
    return next(
        (item for item in deployments if str(item.get("id") or "") not in excluded_ids),
        None,
    )


def show_status(
    expect_sha: str | None = None,
    wait_seconds: int = 0,
    *,
    excluded_ids: set[str] | None = None,
    expected_model: str | None = None,
    expected_reasoning: str | None = None,
) -> tuple[bool, str | None]:
    deadline = time.monotonic() + wait_seconds
    while True:
        deployments = latest_deployments()
        if not deployments:
            raise OpsError("Railway returned no backend deployments")
        latest = (
            select_new_deployment(deployments, excluded_ids)
            if excluded_ids is not None
            else deployments[0]
        )
        if latest is None:
            if time.monotonic() >= deadline:
                return False, None
            time.sleep(10)
            continue
        sha = commit_hash(latest)
        status = str(latest.get("status") or "UNKNOWN")
        variables = current_variables()
        model = variables.get(MODEL_KEY, "<default>")
        reasoning = variables.get(REASONING_KEY, "<unset>") or "<unset>"
        print(
            f"deployment={latest.get('id')} status={status} commit={sha or '<unknown>'} "
            f"model={model} reasoning={reasoning}"
        )
        matched = (
            status == "SUCCESS"
            and (not expect_sha or sha == expect_sha)
            and (not expected_model or model == expected_model)
        )
        if matched:
            health_status, health = request_json(f"{BACKEND}/health")
            print(f"backend_health_http={health_status}")
            runtime_model = (
                health.get("realtime_model") if isinstance(health, dict) else None
            )
            runtime_reasoning = (
                health.get("realtime_reasoning_effort")
                if isinstance(health, dict)
                else None
            )
            runtime_proven = (
                health_status == 200
                and (not expected_model or runtime_model == expected_model)
                and (
                    expected_model is None
                    or (runtime_reasoning or None) == (expected_reasoning or None)
                )
            )
            print(
                f"runtime_config_proven={str(runtime_proven).lower()} "
                f"runtime_model={runtime_model or '<missing>'} "
                f"runtime_reasoning={runtime_reasoning or '<unset>'}"
            )
            return runtime_proven, str(latest.get("id") or "")
        if time.monotonic() >= deadline:
            return False, str(latest.get("id") or "")
        time.sleep(10)


def upsert(name: str, value: str) -> None:
    query = """
    mutation($input: VariableUpsertInput!) { variableUpsert(input: $input) }
    """
    railway_graphql(
        query,
        {
            "input": {
                "projectId": PROJECT_ID,
                "environmentId": ENVIRONMENT_ID,
                "serviceId": BACKEND_SERVICE_ID,
                "name": name,
                "value": value,
                "skipDeploys": True,
            }
        },
    )


def delete(name: str) -> None:
    query = """
    mutation($input: VariableDeleteInput!) { variableDelete(input: $input) }
    """
    railway_graphql(
        query,
        {
            "input": {
                "projectId": PROJECT_ID,
                "environmentId": ENVIRONMENT_ID,
                "serviceId": BACKEND_SERVICE_ID,
                "name": name,
            }
        },
    )


def redeploy_latest_successful() -> tuple[str, str]:
    successful = [
        item for item in latest_deployments() if item.get("status") == "SUCCESS"
    ]
    if not successful:
        raise OpsError("no successful backend deployment is available to redeploy")
    deployment = successful[0]
    deployment_id = str(deployment["id"])
    sha = commit_hash(deployment)
    query = "mutation($id: String!) { deploymentRedeploy(id: $id) { id status } }"
    try:
        railway_graphql(query, {"id": deployment_id})
    except OpsError as exc:
        if "selection" not in str(exc).lower():
            raise
        railway_graphql(
            "mutation($id: String!) { deploymentRedeploy(id: $id) }",
            {"id": deployment_id},
        )
    return deployment_id, sha


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def runtime_model_proof(
    deployment_id: str,
    expected_model: str,
    since: datetime,
    wait_seconds: int,
) -> bool:
    """Prove the runtime opened a Realtime session with the expected model."""
    query = """
    query($deploymentId: String!, $filter: String) {
      deploymentLogs(deploymentId: $deploymentId, filter: $filter, limit: 500) {
        timestamp message severity
      }
    }
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        data = railway_graphql(
            query,
            {
                "deploymentId": deployment_id,
                "filter": "connecting_to_openai_realtime",
            },
        )
        for item in data.get("deploymentLogs") or []:
            timestamp = str(item.get("timestamp") or "")
            message = str(item.get("message") or "")
            try:
                fresh = parse_timestamp(timestamp) >= since
            except ValueError:
                fresh = False
            if (
                fresh
                and expected_model in message
                and "connecting_to_openai_realtime" in message
            ):
                print(
                    f"runtime_model_proven=true deployment={deployment_id} "
                    f"model={expected_model} timestamp={timestamp}"
                )
                return True
        if time.monotonic() >= deadline:
            print(
                f"runtime_model_proven=false deployment={deployment_id} model={expected_model}"
            )
            return False
        time.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--expect-sha")
    status_parser.add_argument("--expect-model", choices=sorted(ALLOWED_MODELS))
    status_parser.add_argument(
        "--expect-reasoning", choices=("low", "none"), default="none"
    )
    status_parser.add_argument("--wait", type=int, default=0)

    proof_parser = sub.add_parser("runtime-model")
    proof_parser.add_argument("--deployment-id", required=True)
    proof_parser.add_argument(
        "--expect-model", choices=sorted(ALLOWED_MODELS), required=True
    )
    proof_parser.add_argument(
        "--since", required=True, help="ISO timestamp captured before the call"
    )
    proof_parser.add_argument("--wait", type=int, default=120)

    model_parser = sub.add_parser("set-model")
    model_parser.add_argument("model", choices=sorted(ALLOWED_MODELS))
    model_parser.add_argument("--reasoning", choices=("low", "none"), default="none")
    model_parser.add_argument("--confirm", action="store_true")
    model_parser.add_argument("--wait", type=int, default=600)
    args = parser.parse_args()

    if args.command == "status":
        passed, _ = show_status(
            args.expect_sha,
            args.wait,
            expected_model=args.expect_model,
            expected_reasoning=None
            if args.expect_reasoning == "none"
            else args.expect_reasoning,
        )
        return 0 if passed else 2

    if args.command == "runtime-model":
        return (
            0
            if runtime_model_proof(
                args.deployment_id,
                args.expect_model,
                parse_timestamp(args.since),
                args.wait,
            )
            else 2
        )

    if not args.confirm:
        raise OpsError(
            "set-model requires --confirm because it changes the live Railway service"
        )
    before_ids = {str(item.get("id") or "") for item in latest_deployments()}
    upsert(MODEL_KEY, args.model)
    if args.reasoning == "none":
        variables = current_variables()
        if REASONING_KEY in variables:
            delete(REASONING_KEY)
    else:
        upsert(REASONING_KEY, args.reasoning)
    prior_deployment_id, sha = redeploy_latest_successful()
    print(
        f"model_variables_updated=true redeploy_source={prior_deployment_id} expected_commit={sha}"
    )
    passed, new_deployment_id = show_status(
        sha,
        args.wait,
        excluded_ids=before_ids,
        expected_model=args.model,
        expected_reasoning=None if args.reasoning == "none" else args.reasoning,
    )
    if passed:
        print(
            f"new_deployment_proven=true deployment={new_deployment_id} "
            "runtime_config_proven=true session_model_proven=false "
            "next=runtime-model-after-seeded-call"
        )
    return 0 if passed else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpsError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
