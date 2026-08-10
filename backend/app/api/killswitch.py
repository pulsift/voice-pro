"""The one control that stops the machine spending money, on the dashboard.

It has always existed — the reply-router owns it and refuses to place a call
while it is on — but reaching it meant a terminal and a token. On 2026-08-10 the
agent rang a prospect Sami was handling personally, and the thing he needed at
that moment was a button, not a shell.

Two rules shape this file:

  * **The token never reaches the browser.** The router gates the switch on a
    shared secret; a dashboard user proves who they are with their own login and
    this proxies the call. Putting the kill token in frontend code would make the
    control weaker than the incident it exists for.
  * **Fail closed, and say so.** If the router cannot be reached we report
    `unknown`, never "calls are running". A safety control that guesses
    optimistically when it cannot see is worse than no control, because it is
    believed.
"""

from typing import Any, Literal

import httpx
import structlog
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.auth import CurrentUser
from app.core.config import settings

router = APIRouter(prefix="/api/v1/killswitch", tags=["killswitch"])
logger = structlog.get_logger()

# The router is the authority. Reaching it should take a moment or fail — a
# dashboard that hangs on this button is a dashboard nobody trusts in a hurry.
ROUTER_TIMEOUT_SECONDS = 8.0
HTTP_OK = 200


class KillSwitchState(BaseModel):
    """What is true right now, in words the page can render without deciding."""

    state: Literal["paused", "running", "unknown"]
    changed_at: str | None = None
    # Present only when the state is unknown, so the page can say WHY rather
    # than showing an empty control.
    error: str | None = None


class KillSwitchRequest(BaseModel):
    paused: bool


def _router_config() -> tuple[str, str]:
    base = (settings.ROUTER_BASE_URL or "").rstrip("/")
    token = settings.ROUTER_KILL_TOKEN or ""
    if not base or not token:
        raise HTTPException(
            status_code=503,
            detail="The kill switch is not wired up: ROUTER_BASE_URL / ROUTER_KILL_TOKEN unset",
        )
    return base, token


def _read_state(payload: dict[str, Any]) -> KillSwitchState:
    if not payload.get("ok"):
        return KillSwitchState(state="unknown", error=str(payload.get("error") or "refused"))
    return KillSwitchState(
        state="paused" if payload.get("kill_switch") else "running",
        changed_at=str(payload.get("changed_at") or "") or None,
    )


@router.get("", response_model=KillSwitchState)
async def read_killswitch(current_user: CurrentUser) -> KillSwitchState:
    """Whether calls are currently allowed. Never guesses."""
    base, token = _router_config()
    try:
        async with httpx.AsyncClient(timeout=ROUTER_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{base}/api/v1/killswitch", headers={"X-Kill-Token": token}
            )
    except httpx.HTTPError as exc:
        logger.warning("killswitch_read_failed", error_type=type(exc).__name__)
        return KillSwitchState(state="unknown", error="the reply router is unreachable")
    if response.status_code != HTTP_OK:
        return KillSwitchState(state="unknown", error=f"router returned {response.status_code}")
    return _read_state(response.json())


@router.post("", response_model=KillSwitchState)
async def set_killswitch(
    request: KillSwitchRequest, current_user: CurrentUser
) -> KillSwitchState:
    """Stop or resume dialling.

    Logged at WARNING either way, with who did it. This is the one control whose
    history someone will want on a bad morning, and the audit trail is worth more
    than the log noise.
    """
    base, token = _router_config()
    logger.warning(
        "killswitch_change_requested",
        paused=request.paused,
        user_id=getattr(current_user, "id", None),
    )
    try:
        async with httpx.AsyncClient(timeout=ROUTER_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{base}/api/v1/killswitch",
                params={"set": "on" if request.paused else "off"},
                headers={"X-Kill-Token": token},
            )
    except httpx.HTTPError as exc:
        logger.exception("killswitch_write_failed", error_type=type(exc).__name__)
        raise HTTPException(
            status_code=502, detail="Could not reach the reply router to change the switch"
        ) from exc

    if response.status_code != HTTP_OK:
        raise HTTPException(
            status_code=502, detail=f"The reply router refused ({response.status_code})"
        )
    payload = response.json()
    if not payload.get("ok"):
        raise HTTPException(
            status_code=502, detail=f"The reply router refused: {payload.get('error')}"
        )
    logger.warning("killswitch_changed", paused=bool(payload.get("kill_switch")))
    return _read_state(payload)
