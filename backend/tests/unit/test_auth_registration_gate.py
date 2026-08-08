"""There is no way to create an account on this machine, and login still works.

The fork shipped a registration endpoint behind an APP_ENVIRONMENT gate. On
2026-08-08 two things turned out to be true at once: the setting was never set on
the live service, so the gate would not have fired — and the endpoint had never
worked anyway, because its rate limiter looks for a parameter named `request` and
the handler called it `http_request`, so every call 500'd before reaching the
gate.

The test that "proved" the gate called `inspect.unwrap(register)` to strip the
decorator first. That is how a broken endpoint and a passing test lived side by
side for weeks: the test measured the handler, and nobody could reach the
handler. It is exactly the shape of failure this whole sweep exists to find.

The endpoint is gone. These tests pin its absence, because deleting something is
only safe if something notices it coming back.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.api import auth as auth_module
from app.api.auth import login
from app.core.config import settings


def test_no_route_on_this_machine_creates_an_account():
    paths = {route.path for route in auth_module.router.routes}
    assert "/api/v1/auth/register" not in paths
    assert not any("register" in path or "signup" in path for path in paths), paths


def test_the_registration_request_model_is_gone_too():
    assert not hasattr(auth_module, "RegisterRequest")
    assert not hasattr(auth_module, "register")


def test_every_rate_limited_endpoint_names_its_request_parameter_correctly():
    """The bug that hid the whole thing: slowapi looks for a parameter literally
    named `request`, and silently 500s the endpoint when it is called something
    else. Nothing was checking, so a rate-limited endpoint could be dead on
    arrival and look configured."""
    import inspect

    for route in auth_module.router.routes:
        endpoint = getattr(route, "endpoint", None)
        if endpoint is None or not hasattr(endpoint, "__wrapped__"):
            continue
        parameters = inspect.signature(inspect.unwrap(endpoint)).parameters
        assert "request" in parameters, (
            f"{route.path} is rate limited but has no `request` parameter, so it "
            "500s on every call"
        )


@pytest.mark.asyncio
async def test_login_still_works_in_production():
    settings.APP_ENVIRONMENT = "production"
    user = SimpleNamespace(id=7, hashed_password="stored-hash")  # noqa: S106
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = user
    db = MagicMock(execute=AsyncMock(return_value=query_result))
    form = SimpleNamespace(
        username="sami@example.com",
        password="correct-password",  # noqa: S106
    )

    import inspect

    with (
        patch("app.api.auth.verify_password", return_value=True),
        patch("app.api.auth.create_access_token", return_value="existing-user-token"),
    ):
        response = await inspect.unwrap(login)(MagicMock(), form, db)

    assert response.access_token == "existing-user-token"
    assert response.token_type == "bearer"
