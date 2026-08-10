"""The stop-dialling control, seen from the dashboard.

Built the evening the agent rang a prospect Sami was handling by hand. The
switch already existed; reaching it took a terminal and a shared token, which in
the moment you need it is the same as not having one.

What these pin is not "does the HTTP call work" — it is the two properties that
make a safety control trustworthy:

  1. The token stays on the server. If it ever reaches a browser, the control is
     weaker than the incident it exists for.
  2. It NEVER guesses "running". An indicator that reports the safe-looking
     state when it cannot see is worse than no indicator, because it is believed.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from app.api.killswitch import _read_state, _router_config
from app.core.config import settings


@pytest.fixture(autouse=True)
def wired(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ROUTER_BASE_URL", "https://router.test")
    monkeypatch.setattr(settings, "ROUTER_KILL_TOKEN", "kill-token-value")


def test_an_unconfigured_switch_refuses_rather_than_pretending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing token must not render as a working button."""
    monkeypatch.setattr(settings, "ROUTER_KILL_TOKEN", None)

    with pytest.raises(HTTPException) as exc:
        _router_config()

    assert exc.value.status_code == 503


def test_a_refused_read_is_unknown_never_running() -> None:
    """The whole point. `ok: false` must not become "calls are running"."""
    assert _read_state({"ok": False, "error": "token_mismatch"}).state == "unknown"
    assert _read_state({"ok": False}).state == "unknown"


def test_the_two_real_states_are_read_straight() -> None:
    assert _read_state({"ok": True, "kill_switch": True}).state == "paused"
    assert _read_state({"ok": True, "kill_switch": False}).state == "running"


@pytest.mark.asyncio
async def test_an_unreachable_router_reads_unknown_not_running() -> None:
    """A network failure is the case most likely to happen at the worst moment."""
    from app.api.killswitch import read_killswitch

    with patch("httpx.AsyncClient.get", AsyncMock(side_effect=httpx.ConnectError("down"))):
        state = await read_killswitch(current_user=object())

    assert state.state == "unknown"
    assert state.error


@pytest.mark.asyncio
async def test_stopping_sends_the_token_in_a_header_never_the_url() -> None:
    """S4: a token in a query string ends up in proxy logs and browser history."""
    from app.api.killswitch import KillSwitchRequest, set_killswitch

    captured: dict[str, object] = {}

    async def fake_post(self: object, url: str, **kwargs: object) -> httpx.Response:
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        captured["headers"] = kwargs.get("headers")
        return httpx.Response(200, json={"ok": True, "kill_switch": True, "changed_at": "now"})

    with patch("httpx.AsyncClient.post", fake_post):
        state = await set_killswitch(KillSwitchRequest(paused=True), current_user=object())

    assert state.state == "paused"
    assert captured["headers"] == {"X-Kill-Token": "kill-token-value"}
    assert captured["params"] == {"set": "on"}
    assert "kill-token-value" not in str(captured["url"])


@pytest.mark.asyncio
async def test_resuming_asks_the_router_to_turn_it_off() -> None:
    from app.api.killswitch import KillSwitchRequest, set_killswitch

    captured: dict[str, object] = {}

    async def fake_post(self: object, url: str, **kwargs: object) -> httpx.Response:
        captured["params"] = kwargs.get("params")
        return httpx.Response(200, json={"ok": True, "kill_switch": False})

    with patch("httpx.AsyncClient.post", fake_post):
        state = await set_killswitch(KillSwitchRequest(paused=False), current_user=object())

    assert captured["params"] == {"set": "off"}
    assert state.state == "running"


@pytest.mark.asyncio
async def test_a_router_that_refuses_the_change_raises_rather_than_reporting_success() -> None:
    """Silently reporting success on a failed STOP is the worst outcome here."""
    from app.api.killswitch import KillSwitchRequest, set_killswitch

    async def fake_post(self: object, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "token_mismatch"})

    with patch("httpx.AsyncClient.post", fake_post), pytest.raises(HTTPException) as exc:
        await set_killswitch(KillSwitchRequest(paused=True), current_user=object())

    assert exc.value.status_code == 502
