"""Request tracing middleware for correlation IDs and logging."""

import time
import uuid
from collections.abc import Callable
from typing import Any

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger()
_SENSITIVE_QUERY_KEYS = frozenset(
    {"access_token", "cv", "media_grant", "signature", "token"}
)
_REDACTED_QUERY_VALUE = "[REDACTED]"


def _safe_query_params(request: Request) -> dict[str, str] | None:
    """Keep query observability without writing capabilities or lead context."""
    if not request.query_params:
        return None
    return {
        key: _REDACTED_QUERY_VALUE if key.lower() in _SENSITIVE_QUERY_KEYS else value
        for key, value in request.query_params.items()
    }


class RequestTracingMiddleware(BaseHTTPMiddleware):
    """Middleware to add request tracing with correlation IDs.

    This middleware:
    1. Generates or extracts a correlation ID for each request
    2. Adds the correlation ID to structlog context
    3. Logs request start and completion with timing
    4. Adds correlation ID to response headers
    """

    async def dispatch(self, request: Request, call_next: Callable[..., Any]) -> Response:
        """Process request with tracing."""
        # Get or generate correlation ID
        correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
        request_id = str(uuid.uuid4())[:8]  # Short ID for this specific request

        # Start timing
        start_time = time.perf_counter()

        # Bind context for all logs during this request
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id,
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            client_ip=self._get_client_ip(request),
        )

        # Log request start
        logger.info("request_started", query_params=_safe_query_params(request))

        try:
            response: Response = await call_next(request)

            # Calculate duration
            duration_ms = (time.perf_counter() - start_time) * 1000

            # Log request completion
            logger.info(
                "request_completed",
                status_code=response.status_code,
                duration_ms=round(duration_ms, 2),
            )

            # Add correlation ID to response headers
            response.headers["X-Correlation-ID"] = correlation_id
            response.headers["X-Request-ID"] = request_id

            return response

        except Exception as e:
            # Calculate duration even on error
            duration_ms = (time.perf_counter() - start_time) * 1000

            logger.exception(
                "request_failed",
                error=str(e),
                error_type=type(e).__name__,
                duration_ms=round(duration_ms, 2),
            )
            raise

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP, handling proxies."""
        # Check X-Forwarded-For header (from load balancers/proxies)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Take the first IP (original client)
            return forwarded_for.split(",")[0].strip()

        # Check X-Real-IP header
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

        # Fall back to direct client IP
        if request.client:
            return request.client.host

        return "unknown"
