"""Production registration gate without changing existing login behavior."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.auth import RegisterRequest, login, register
from app.core.config import settings


@pytest.mark.asyncio
async def test_production_refuses_public_registration_before_database_access(monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "production")
    db = MagicMock(execute=AsyncMock())

    with pytest.raises(HTTPException) as exc_info:
        await inspect.unwrap(register)(
            RegisterRequest(
                email="outsider@example.com",
                username="Outsider",
                password="not-used",  # noqa: S106
            ),
            MagicMock(),
            db,
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Public registration is disabled"
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_development_registration_remains_available(monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "development")
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = None
    db = MagicMock(
        execute=AsyncMock(return_value=query_result),
        add=MagicMock(),
        commit=AsyncMock(),
    )

    async def assign_user_id(user: object) -> None:
        user.id = 7

    db.refresh = AsyncMock(side_effect=assign_user_id)

    with patch("app.api.auth.get_password_hash", return_value="hashed-password"):
        response = await inspect.unwrap(register)(
            RegisterRequest(
                email="local@example.com",
                username="Local User",
                password="local-password",  # noqa: S106
            ),
            MagicMock(),
            db,
        )

    assert response.id == 7
    assert response.email == "local@example.com"
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_existing_login_remains_usable_in_production(monkeypatch) -> None:
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "production")
    user = SimpleNamespace(id=7, hashed_password="stored-hash")  # noqa: S106
    query_result = MagicMock()
    query_result.scalar_one_or_none.return_value = user
    db = MagicMock(execute=AsyncMock(return_value=query_result))
    form = SimpleNamespace(
        username="sami@example.com",
        password="correct-password",  # noqa: S106
    )

    with (
        patch("app.api.auth.verify_password", return_value=True),
        patch("app.api.auth.create_access_token", return_value="existing-user-token"),
    ):
        response = await inspect.unwrap(login)(MagicMock(), form, db)

    assert response.access_token == "existing-user-token"
    assert response.token_type == "bearer"
