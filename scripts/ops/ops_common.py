"""Shared, secret-safe primitives for Voice Pro production operations."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - Windows is the production operator host
    winreg = None  # type: ignore[assignment]

BACKEND = "https://backend-production-7d1e.up.railway.app"
FULFILMENT = "https://pulsift-fulfilment-production.up.railway.app"
ROUTER = "https://reply-router-production.up.railway.app"
RAILWAY_GRAPHQL = "https://backboard.railway.com/graphql/v2"
CALCOM = "https://api.cal.com/v2"
SENDKIT = "https://api.sendkit.ai"

PROJECT_ID = "355ac005-de93-49ae-9c3e-424d6678ee83"
ENVIRONMENT_ID = "1056526b-e665-4967-86db-8d52791d0863"
BACKEND_SERVICE_ID = "8ae05502-52a6-4b9f-b474-2016b130be85"
AGENT_ID = "06a42ae8-6169-4055-a752-8ef561d8d2aa"

TEST_CAMPAIGN_ID = "6a50ea95757679d541f1effc"
REAL_CAMPAIGN_IDS = (
    "6a27a73cf154038d09a8b6ba",
    "6a3aba3813df2111473bf0b2",
    "6a3aba3813df2111473bf0da",
    "6a3aba3913df2111473bf102",
    "6a3aba3913df2111473bf12a",
)
SEEDED_LEAD_ID = "6a50eacf757679d541f20728"
SEEDED_EMAIL = "sami@pulsift.com"
SEEDED_PHONE = "+963998183191"

MIGRATION_NOTE = Path(
    r"C:\SecondBrain\Projects\vapi-voice-agent\migration-to-selfhosted.md"
)
POSTGRES_CREDENTIAL_ID = "4J6a1UYOsHcDVNoo"
POSTGRES_CREDENTIAL_NAME = "Railway Postgres (voiceagent)"


class OpsError(RuntimeError):
    """An operator-visible failure that never contains a secret value."""


def user_env(name: str) -> str:
    """Read a secret from the process or Windows user environment without printing it."""
    value = os.environ.get(name)
    if value:
        return value
    if winreg is not None:
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
            try:
                value, _ = winreg.QueryValueEx(key, name)
            finally:
                winreg.CloseKey(key)
        except OSError:
            value = ""
        if value:
            return str(value)
    raise OpsError(f"required user environment variable is missing: {name}")


def request_json(
    url: str,
    *,
    method: str = "GET",
    body: object | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
) -> tuple[int, Any]:
    """Make a JSON request and return HTTP status plus parsed or truncated text body."""
    request_headers = {"User-Agent": "Voice-Pro-Ops/1.0", **(headers or {})}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers=request_headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            if not raw:
                return response.status, {}
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw.decode(errors="replace")[:500]
    except urllib.error.HTTPError as exc:
        raw = exc.read() or b""
        try:
            parsed: Any = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw.decode(errors="replace")[:500]
        return exc.code, parsed


def require_status(status: int, expected: set[int], label: str) -> None:
    if status not in expected:
        raise OpsError(f"{label} returned HTTP {status}")


def admin_credentials() -> tuple[str, str]:
    """Read Voice Pro admin credentials from the canonical migration note."""
    text = MIGRATION_NOTE.read_text(encoding="utf-8")
    match = re.search(
        r"Admin dashboard login:\*\*\s*`([^`]+)`\s*/\s*`([^`]+)`",
        text,
    )
    if not match:
        raise OpsError("admin credential location was not found in the migration note")
    return match.group(1), match.group(2)


def admin_token() -> str:
    email, password = admin_credentials()
    data = urllib.parse.urlencode({"username": email, "password": password}).encode()
    request = urllib.request.Request(
        f"{BACKEND}/api/v1/auth/login",
        method="POST",
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Voice-Pro-Ops/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        raise OpsError(f"Voice Pro admin login returned HTTP {exc.code}") from None
    token = str(payload.get("access_token") or payload.get("token") or "")
    if not token:
        raise OpsError("Voice Pro admin login returned no token")
    return token


def admin_request(path: str, *, method: str = "GET", body: object | None = None) -> Any:
    token = admin_token()
    status, payload = request_json(
        BACKEND + path,
        method=method,
        body=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    require_status(status, {200, 201}, f"Voice Pro {method} {path}")
    return payload


def railway_graphql(query: str, variables: dict[str, object]) -> Any:
    status, payload = request_json(
        RAILWAY_GRAPHQL,
        method="POST",
        body={"query": query, "variables": variables},
        headers={"Authorization": f"Bearer {user_env('RAILWAY_API_KEY')}"},
        timeout=45,
    )
    require_status(status, {200}, "Railway GraphQL")
    if not isinstance(payload, dict):
        raise OpsError("Railway returned a non-JSON response")
    if payload.get("errors"):
        messages = [
            str(item.get("message", "unknown")) for item in payload["errors"][:3]
        ]
        raise OpsError("Railway GraphQL error: " + "; ".join(messages))
    return payload.get("data") or {}


def latest_deployments(limit: int = 20) -> list[dict[str, Any]]:
    query = """
    query($input: DeploymentListInput!, $first: Int!) {
      deployments(input: $input, first: $first) {
        edges { node { id status createdAt meta } }
      }
    }
    """
    data = railway_graphql(
        query,
        {
            "input": {
                "projectId": PROJECT_ID,
                "environmentId": ENVIRONMENT_ID,
                "serviceId": BACKEND_SERVICE_ID,
            },
            "first": limit,
        },
    )
    return [edge["node"] for edge in data.get("deployments", {}).get("edges", [])]


def commit_hash(deployment: dict[str, Any]) -> str:
    meta = deployment.get("meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            meta = {}
    return str(meta.get("commitHash") or meta.get("commit_hash") or "")


def current_variables() -> dict[str, str]:
    query = """
    query($projectId: String!, $environmentId: String!, $serviceId: String!) {
      variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
    }
    """
    data = railway_graphql(
        query,
        {
            "projectId": PROJECT_ID,
            "environmentId": ENVIRONMENT_ID,
            "serviceId": BACKEND_SERVICE_ID,
        },
    )
    values = data.get("variables") or {}
    return {str(key): str(value) for key, value in values.items()}


def router_request(
    path: str, *, method: str = "GET", body: object | None = None
) -> Any:
    """Call the reply router's operator API with the shared kill token.

    Reading the kill switch, moving it, and forging a seeded reply all used to
    run inside a throwaway n8n workflow, so the HMAC and the database
    credentials never reached this laptop. n8n was retired on 2026-08-03 and
    took the mechanism with it, which left `safety_status` and `seeded_call`
    broken without either of them saying so.

    The router keeps the same property with an endpoint instead of a workflow:
    the signing secret stays server-side, and the seed's conversation id is
    test-prefixed by the server rather than by whoever remembers to.
    """
    status, payload = request_json(
        ROUTER + path,
        method=method,
        body=body,
        headers={"X-Kill-Token": user_env("ROUTER_KILL_TOKEN")},
        timeout=90,
    )
    require_status(status, {200}, f"router {method} {path}")
    if isinstance(payload, dict) and payload.get("ok") is False:
        raise OpsError(f"router {method} {path} refused: {payload.get('error')}")
    return payload


def kill_state() -> tuple[bool, str]:
    """(calls paused?, when that last changed). The stamp is the audit trail on
    the one control that stops us spending money."""
    payload = router_request("/api/v1/killswitch")
    paused = payload.get("kill_switch") if isinstance(payload, dict) else None
    if not isinstance(paused, bool):
        raise OpsError("kill-switch state is missing or invalid")
    changed_at = str(payload.get("changed_at") or "") if isinstance(payload, dict) else ""
    return paused, changed_at


def read_kill_state() -> bool:
    return kill_state()[0]


def kill_paused() -> bool:
    return read_kill_state()


def set_kill_switch(*, paused: bool) -> bool:
    payload = router_request(
        f"/api/v1/killswitch?set={'on' if paused else 'off'}", method="POST"
    )
    observed = payload.get("kill_switch") if isinstance(payload, dict) else None
    if observed is not paused:
        raise OpsError("kill-switch update returned the wrong state")
    return observed


def forge_seeded_reply() -> str:
    """One seeded positive reply, signed and run by the router itself."""
    payload = router_request(
        "/api/v1/ops/seed-positive",
        method="POST",
        body={
            "lead_id": SEEDED_LEAD_ID,
            "lead_email": SEEDED_EMAIL,
            "reply_text": (
                "Yes, this is interesting - happy to hop on a quick call to hear more."
            ),
        },
    )
    conversation_id = (
        str(payload.get("conversation_id") or "") if isinstance(payload, dict) else ""
    )
    if not conversation_id:
        raise OpsError("seeded positive reply returned no conversation ID")
    return conversation_id


def masked_phone(phone: str | None) -> str:
    if not phone:
        return "<none>"
    digits = re.sub(r"\D", "", phone)
    return "***" + digits[-4:] if len(digits) >= 4 else "***"


def sanitize_booking_attempts(value: object) -> list[dict[str, object]]:
    attempts = value if isinstance(value, list) else []
    safe_keys = {
        "attempt",
        "category",
        "event",
        "operation",
        "status_code",
        "slot_id",
        "selected_start",
        "start",
        "success",
        "uid",
        "error",
        "response_body",
        "timestamp",
        "tool",
        "timezone",
    }
    sanitized: list[dict[str, object]] = []
    for item in attempts:
        if isinstance(item, dict):
            sanitized.append({key: item[key] for key in safe_keys if key in item})
    return sanitized
