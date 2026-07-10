"""Shared, secret-safe primitives for Voice Pro production operations."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any

try:
    import winreg
except ImportError:  # pragma: no cover - Windows is the production operator host
    winreg = None  # type: ignore[assignment]

BACKEND = "https://backend-production-7d1e.up.railway.app"
FULFILMENT = "https://pulsift-fulfilment-production.up.railway.app"
N8N = "https://n8n-production-2e51.up.railway.app"
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


def n8n_api(path: str, *, method: str = "GET", body: object | None = None) -> Any:
    status, payload = request_json(
        N8N + path,
        method=method,
        body=body,
        headers={"X-N8N-API-KEY": user_env("N8N_API_KEY")},
    )
    require_status(status, {200, 201, 204}, f"n8n {method} {path}")
    return payload


def no_execution_data_settings() -> dict[str, object]:
    """n8n settings that prevent temporary workflow payload persistence."""
    return {
        "saveDataErrorExecution": "none",
        "saveDataSuccessExecution": "none",
        "saveExecutionProgress": False,
        "saveManualExecutions": False,
    }


def run_temporary_workflow(
    *,
    name: str,
    hook_path: str,
    nodes: list[dict[str, object]],
    connections: dict[str, object],
) -> Any:
    """Run a no-history workflow and fail fatally if it cannot be deleted."""
    workflow = {
        "name": name,
        "nodes": nodes,
        "connections": connections,
        "settings": no_execution_data_settings(),
    }
    created = n8n_api("/api/v1/workflows", method="POST", body=workflow)
    workflow_id = str(created.get("id") or "") if isinstance(created, dict) else ""
    if not workflow_id:
        raise OpsError("n8n did not return a temporary workflow ID")
    result: Any = None
    primary_error: Exception | None = None
    try:
        n8n_api(f"/api/v1/workflows/{workflow_id}/activate", method="POST")
        time.sleep(2)
        status, result = request_json(f"{N8N}/webhook/{hook_path}", timeout=60)
        require_status(status, {200}, "temporary n8n operation")
    except Exception as exc:  # cleanup must run for every failure mode
        primary_error = exc
    try:
        try:
            n8n_api(f"/api/v1/workflows/{workflow_id}/deactivate", method="POST")
        except OpsError:
            pass
        n8n_api(f"/api/v1/workflows/{workflow_id}", method="DELETE")
    except Exception as exc:
        raise OpsError(
            f"FATAL: temporary n8n workflow cleanup failed for id={workflow_id}"
        ) from exc
    if primary_error is not None:
        if isinstance(primary_error, OpsError):
            raise primary_error
        raise OpsError(
            f"temporary n8n operation failed ({type(primary_error).__name__})"
        ) from primary_error
    return result


def _postgres_node(*, name: str, query: str, position: list[int]) -> dict[str, object]:
    return {
        "parameters": {"operation": "executeQuery", "query": query, "options": {}},
        "name": name,
        "type": "n8n-nodes-base.postgres",
        "typeVersion": 2.4,
        "credentials": {
            "postgres": {
                "id": POSTGRES_CREDENTIAL_ID,
                "name": POSTGRES_CREDENTIAL_NAME,
            }
        },
        "position": position,
    }


def _webhook_node(hook_path: str) -> dict[str, object]:
    return {
        "parameters": {
            "httpMethod": "GET",
            "path": hook_path,
            "responseMode": "lastNode",
        },
        "name": "Webhook",
        "type": "n8n-nodes-base.webhook",
        "typeVersion": 2,
        "position": [0, 0],
    }


def read_kill_state() -> bool:
    """Read only the non-secret kill boolean through a no-history workflow."""
    suffix = uuid.uuid4().hex
    hook_path = f"ops-kill-read-{suffix}"
    query = (
        "SELECT (value->>'paused')::boolean AS paused "
        "FROM voiceagent.config WHERE key='kill_switch';"
    )
    payload = run_temporary_workflow(
        name=f"ZZTMP Voice Pro kill read {suffix[:8]}",
        hook_path=hook_path,
        nodes=[
            _webhook_node(hook_path),
            _postgres_node(name="Kill State", query=query, position=[220, 0]),
        ],
        connections={
            "Webhook": {"main": [[{"node": "Kill State", "type": "main", "index": 0}]]}
        },
    )
    row = payload[0] if isinstance(payload, list) and payload else payload
    if not isinstance(row, dict) or not isinstance(row.get("paused"), bool):
        raise OpsError("kill-switch state is missing or invalid")
    return bool(row["paused"])


def kill_paused() -> bool:
    return read_kill_state()


def set_kill_switch(*, paused: bool) -> bool:
    suffix = uuid.uuid4().hex
    hook_path = f"ops-kill-set-{suffix}"
    sql_bool = "true" if paused else "false"
    query = (
        "UPDATE voiceagent.config SET value=jsonb_build_object('paused', "
        f"{sql_bool}), updated_at=now() WHERE key='kill_switch' "
        "RETURNING (value->>'paused')::boolean AS paused;"
    )
    payload = run_temporary_workflow(
        name=f"ZZTMP Voice Pro kill set {suffix[:8]}",
        hook_path=hook_path,
        nodes=[
            _webhook_node(hook_path),
            _postgres_node(name="Set Kill", query=query, position=[220, 0]),
        ],
        connections={
            "Webhook": {"main": [[{"node": "Set Kill", "type": "main", "index": 0}]]}
        },
    )
    row = payload[0] if isinstance(payload, list) and payload else payload
    observed = bool(row.get("paused")) if isinstance(row, dict) else not paused
    if observed is not paused:
        raise OpsError("kill-switch update returned the wrong state")
    return observed


def forge_seeded_reply() -> str:
    """Sign and send the seeded event inside n8n; the HMAC never leaves n8n."""
    suffix = uuid.uuid4().hex
    hook_path = f"ops-seeded-forge-{suffix}"
    sign_code = f"""
const crypto = require('crypto');
const secret = String($('Load HMAC').first().json.secret || '');
if (!secret || secret === 'PENDING') throw new Error('HMAC unavailable');
const conversationId = crypto.randomUUID();
const body = {{data: {{conversationId, leadId: '{SEEDED_LEAD_ID}', leadEmail: '{SEEDED_EMAIL}', messageId: crypto.randomUUID(), replyText: 'Yes, this is interesting - happy to hop on a quick call to hear more.'}}}};
const raw = JSON.stringify(body);
const signature = crypto.createHmac('sha256', secret).update(raw).digest('hex');
return [{{json: {{conversation_id: conversationId, body, signature}}}}];
""".strip()
    nodes: list[dict[str, object]] = [
        _webhook_node(hook_path),
        _postgres_node(
            name="Load HMAC",
            query="SELECT value #>> '{}' AS secret FROM voiceagent.config WHERE key='sendkit_hmac_positive';",
            position=[220, 0],
        ),
        {
            "parameters": {"jsCode": sign_code},
            "name": "Sign Internally",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [440, 0],
        },
        {
            "parameters": {
                "method": "POST",
                "url": f"{N8N}/webhook/sendkit-positive",
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [
                        {"name": "Content-Type", "value": "application/json"},
                        {
                            "name": "x-sendkit-signature",
                            "value": "={{ 'sha256=' + $json.signature }}",
                        },
                    ]
                },
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify($json.body) }}",
                "options": {},
            },
            "name": "Send Positive",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [660, 0],
        },
        {
            "parameters": {
                "jsCode": "return [{json:{accepted:true,conversation_id:$('Sign Internally').first().json.conversation_id}}];"
            },
            "name": "Sanitize",
            "type": "n8n-nodes-base.code",
            "typeVersion": 2,
            "position": [880, 0],
        },
    ]
    payload = run_temporary_workflow(
        name=f"ZZTMP Voice Pro seeded forge {suffix[:8]}",
        hook_path=hook_path,
        nodes=nodes,
        connections={
            "Webhook": {"main": [[{"node": "Load HMAC", "type": "main", "index": 0}]]},
            "Load HMAC": {
                "main": [[{"node": "Sign Internally", "type": "main", "index": 0}]]
            },
            "Sign Internally": {
                "main": [[{"node": "Send Positive", "type": "main", "index": 0}]]
            },
            "Send Positive": {
                "main": [[{"node": "Sanitize", "type": "main", "index": 0}]]
            },
        },
    )
    row = payload[0] if isinstance(payload, list) and payload else payload
    conversation_id = (
        str(row.get("conversation_id") or "") if isinstance(row, dict) else ""
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
