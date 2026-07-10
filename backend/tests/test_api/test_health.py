"""Tests for health check endpoints."""

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings


class TestBasicHealthCheck:
    """Test basic health check endpoint."""

    @pytest.mark.asyncio
    async def test_health_check_success(self, test_client: AsyncClient) -> None:
        """Test basic health check returns healthy status."""
        response = await test_client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["app"] == "Voice Noob API"
        assert "version" in data
        assert data["realtime_model"] == settings.OPENAI_REALTIME_MODEL
        assert data["realtime_reasoning_effort"] == (
            settings.OPENAI_REALTIME_REASONING_EFFORT or ""
        )


class TestDatabaseHealthCheck:
    """Test database health check endpoint."""

    @pytest.mark.asyncio
    async def test_db_health_check_success(self, test_client: AsyncClient) -> None:
        """Test database health check returns healthy status."""
        response = await test_client.get("/health/db")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["database"] == "connected"

    @pytest.mark.asyncio
    async def test_db_health_check_failure(self, test_client: AsyncClient) -> None:
        """Test database health check handles connection failures."""
        # Mock database error
        with patch("app.db.session.AsyncSessionLocal") as mock_session:
            mock_instance = AsyncMock()
            mock_instance.execute = AsyncMock(side_effect=Exception("Database connection failed"))
            mock_session.return_value.__aenter__.return_value = mock_instance

            # Create new client with mocked session
            from app.db.session import get_db

            async def override_get_db_error() -> Any:
                async with mock_session() as session:
                    yield session

            from app.main import app

            app.dependency_overrides[get_db] = override_get_db_error

            response = await test_client.get("/health/db")

            # Clean up override
            app.dependency_overrides.clear()

            # Service returns 503 when database is unhealthy
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "unhealthy"
            assert "database" in data


class TestRedisHealthCheck:
    """Test Redis health check endpoint."""

    @pytest.mark.asyncio
    async def test_redis_health_check_success(self, test_client: AsyncClient) -> None:
        """Test Redis health check returns healthy status."""
        response = await test_client.get("/health/redis")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["redis"] == "connected"

    @pytest.mark.asyncio
    async def test_redis_health_check_failure(self, test_client: AsyncClient) -> None:
        """Test Redis health check handles connection failures."""
        # Mock Redis error
        with patch("app.db.redis.get_redis") as mock_get_redis:
            mock_redis = AsyncMock()
            mock_redis.ping = AsyncMock(side_effect=Exception("Redis connection failed"))
            mock_get_redis.return_value = mock_redis

            from app.db.redis import get_redis
            from app.main import app

            app.dependency_overrides[get_redis] = mock_get_redis

            response = await test_client.get("/health/redis")

            # Clean up override
            app.dependency_overrides.clear()

            # Service returns 503 when Redis is unhealthy
            assert response.status_code == 503
            data = response.json()
            assert data["status"] == "unhealthy"
            assert "redis" in data


class TestHealthCheckIntegration:
    """Integration tests for health checks."""

    @pytest.mark.asyncio
    async def test_all_health_checks_sequential(self, test_client: AsyncClient) -> None:
        """Test all health endpoints in sequence."""
        # Basic health
        response1 = await test_client.get("/health")
        assert response1.status_code == 200
        assert response1.json()["status"] == "healthy"

        # Database health
        response2 = await test_client.get("/health/db")
        assert response2.status_code == 200
        assert response2.json()["status"] == "healthy"

        # Redis health
        response3 = await test_client.get("/health/redis")
        assert response3.status_code == 200
        assert response3.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_health_endpoints_no_side_effects(
        self,
        test_client: AsyncClient,
        test_session: AsyncSession,
    ) -> None:
        """Test that health checks don't modify database."""
        from sqlalchemy import select, text

        # Get initial count
        initial_count = await test_session.scalar(select(text("COUNT(*) FROM contacts")))

        # Call health endpoints multiple times
        for _ in range(5):
            await test_client.get("/health")
            await test_client.get("/health/db")
            await test_client.get("/health/redis")

        # Verify count hasn't changed
        final_count = await test_session.scalar(select(text("COUNT(*) FROM contacts")))
        assert initial_count == final_count
