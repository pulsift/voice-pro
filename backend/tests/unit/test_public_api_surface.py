"""What the machine exposes to someone with no credentials.

Only two doors face the open internet: the provider webhooks (signature-checked)
and the fork's public embed widget. Everything else needs a token — except, until
2026-08-08, the OpenAPI schema, which listed every endpoint and parameter of a
service that dials real people. Probed live; it answered.
"""

import pytest

from app.core.config import settings


def _build_app():
    """A fresh app object reflecting the CURRENT settings."""
    import importlib

    from app import main as main_module

    return importlib.reload(main_module).app


@pytest.fixture(autouse=True)
def _restore_app_module():
    yield
    import importlib

    from app import main as main_module

    importlib.reload(main_module)


def test_production_publishes_no_api_schema_and_no_docs(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "production")
    app = _build_app()

    assert app.openapi_url is None
    assert app.docs_url is None
    assert app.redoc_url is None
    paths = {route.path for route in app.routes}
    assert "/openapi.json" not in paths
    assert "/docs" not in paths


def test_everywhere_else_keeps_the_docs_because_they_are_useful(monkeypatch):
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "development")
    app = _build_app()

    assert app.openapi_url == "/openapi.json"
    assert app.docs_url == "/docs"


def test_health_stays_open_in_production(monkeypatch):
    """Railway's own probe has no credentials."""
    monkeypatch.setattr(settings, "APP_ENVIRONMENT", "production")
    app = _build_app()
    assert "/health" in {route.path for route in app.routes}
