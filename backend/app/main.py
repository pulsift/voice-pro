"""Main FastAPI application entry point."""

# Fix passlib bcrypt version detection issue FIRST before any imports
# passlib tries to access bcrypt.__about__.__version__ which doesn't exist in newer bcrypt
# This must be done before passlib is imported anywhere
from types import SimpleNamespace

import bcrypt as _bcrypt_module

if not hasattr(_bcrypt_module, "__about__"):
    _bcrypt_module.__about__ = SimpleNamespace(__version__=_bcrypt_module.__version__)  # type: ignore[attr-defined]

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select

from app.api import (
    agents,
    auth,
    calls,
    campaigns,
    compliance,
    crm,
    embed,
    health,
    integrations,
    phone_numbers,
    realtime,
    sms,
    telephony,
    telephony_ws,
    tools,
    workspaces,
)
from app.api import settings as settings_api
from app.api.auth import get_password_hash
from app.core.config import settings
from app.core.limiter import limiter
from app.db.redis import close_redis, get_redis
from app.db.session import AsyncSessionLocal, engine
from app.middleware.request_tracing import RequestTracingMiddleware
from app.middleware.security import SecurityHeadersMiddleware
from app.models.user import User
from app.services.campaign_worker import start_campaign_worker, stop_campaign_worker

# Configure structured logging with async processors
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.WARNING if not settings.DEBUG else logging.DEBUG
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:  # noqa: PLR0915
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    logger.info("Starting application", app_name=settings.APP_NAME)

    try:
        # Initialize Redis (fatal if fails)
        await get_redis()
        logger.info("Redis connection established")
    except Exception:
        logger.exception("Failed to initialize Redis - application cannot start")
        raise  # Re-raise to prevent app startup

    # Create default admin user if no users exist
    try:
        async with AsyncSessionLocal() as db:
            result = await db.execute(select(func.count(User.id)))
            user_count = result.scalar() or 0

            if user_count == 0:
                admin_user = User(
                    email=settings.ADMIN_EMAIL,
                    hashed_password=get_password_hash(settings.ADMIN_PASSWORD),
                    full_name=settings.ADMIN_NAME,
                    is_active=True,
                    is_superuser=True,
                )
                db.add(admin_user)
                await db.commit()
                logger.info(
                    "Created default admin user",
                    email=settings.ADMIN_EMAIL,
                    name=settings.ADMIN_NAME,
                )
            else:
                logger.debug("Users already exist, skipping admin user creation")
    except Exception:
        logger.exception("Failed to check/create admin user - continuing anyway")

    # Initialize Sentry if configured (non-fatal)
    if settings.SENTRY_DSN:
        try:
            import sentry_sdk

            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                environment=settings.SENTRY_ENVIRONMENT,
                traces_sample_rate=settings.SENTRY_TRACES_SAMPLE_RATE,
            )
            logger.info("Sentry initialized")
        except Exception:
            logger.exception("Failed to initialize Sentry - continuing without error tracking")

    # Start campaign worker (non-fatal)
    try:
        # Use PUBLIC_URL from settings if available, otherwise default to localhost
        base_url = settings.PUBLIC_URL or f"http://{settings.HOST}:{settings.PORT}"
        await start_campaign_worker(base_url=base_url)
        logger.info("Campaign worker started", base_url=base_url)
    except Exception:
        logger.exception("Failed to start campaign worker - campaigns will not process")

    yield

    # Shutdown
    logger.info("Shutting down application")

    # Stop campaign worker
    try:
        await stop_campaign_worker()
        logger.info("Campaign worker stopped")
    except Exception:
        logger.exception("Error stopping campaign worker")

    # Close Redis connection
    try:
        await close_redis()
        logger.info("Redis connection closed")
    except Exception:
        logger.exception("Error closing Redis connection")

    # Dispose database engine and close all connections
    try:
        await engine.dispose()
        logger.info("Database connections closed")
    except Exception:
        logger.exception("Error closing database connections")


# Create FastAPI app
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    debug=settings.DEBUG,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# Add rate limiting
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# Add request tracing middleware (runs first, wraps everything)
app.add_middleware(RequestTracingMiddleware)

# Add security headers middleware
app.add_middleware(SecurityHeadersMiddleware)

# Add CORS middleware (must be added AFTER security headers so it runs first)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=settings.CORS_ALLOW_CREDENTIALS,
    allow_methods=settings.CORS_ALLOW_METHODS,
    allow_headers=settings.CORS_ALLOW_HEADERS,
)

# Include routers
app.include_router(health.router, tags=["health"])
app.include_router(crm.router, prefix=settings.API_V1_PREFIX)
app.include_router(workspaces.router, prefix=settings.API_V1_PREFIX)
app.include_router(agents.router)
app.include_router(settings_api.router)
app.include_router(realtime.router)
app.include_router(realtime.webrtc_router)  # WebRTC session endpoint
app.include_router(tools.router)  # Tool execution endpoint
app.include_router(telephony.router)  # Telephony API (phone numbers, calls)
app.include_router(telephony.webhook_router)  # Twilio/Telnyx webhooks
app.include_router(sms.router)  # SMS inbox API (read inbound texts)
app.include_router(sms.webhook_router)  # Telnyx inbound-SMS webhook
app.include_router(telephony_ws.router)  # Telephony WebSocket for media streams
app.include_router(calls.router)  # Call history API
app.include_router(campaigns.router, prefix=settings.API_V1_PREFIX)  # Campaigns API
app.include_router(phone_numbers.router)  # Phone numbers API
app.include_router(auth.router)  # Authentication API
app.include_router(compliance.router)  # Compliance API (GDPR/CCPA)
app.include_router(integrations.router)  # Integrations API (external tools)
app.include_router(embed.router)  # Public embed API (unauthenticated)
app.include_router(embed.ws_router)  # Public embed WebSocket


@app.get("/")
async def root() -> dict[str, str]:
    """Root endpoint."""
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "version": settings.APP_VERSION,
        "docs": "/docs",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=settings.RELOAD,
    )
