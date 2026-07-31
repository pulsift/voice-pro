"""Webhook signature validation for Twilio and Telnyx."""

import hashlib
import hmac
from functools import wraps
from typing import Any

import structlog
from fastapi import HTTPException, Request

from app.core.config import settings

logger = structlog.get_logger()


def validate_twilio_signature(
    signature: str,
    url: str,
    params: dict[str, Any],
    auth_token: str,
) -> bool:
    """Validate Twilio webhook signature.

    Twilio signs webhooks using HMAC-SHA1 with the account auth token.
    The signature is passed in the X-Twilio-Signature header.

    Args:
        signature: The X-Twilio-Signature header value
        url: The full URL of the webhook endpoint
        params: The POST parameters from the request
        auth_token: The Twilio auth token

    Returns:
        True if signature is valid, False otherwise
    """
    if not signature or not auth_token:
        return False

    # Sort params and concatenate to URL
    sorted_params = sorted(params.items())
    data = url + "".join(f"{k}{v}" for k, v in sorted_params)

    # Create HMAC-SHA1 signature
    expected_sig = hmac.new(
        auth_token.encode("utf-8"),
        data.encode("utf-8"),
        hashlib.sha1,
    ).digest()

    import base64

    expected_sig_b64 = base64.b64encode(expected_sig).decode("utf-8")

    return hmac.compare_digest(signature, expected_sig_b64)


def validate_telnyx_signature(
    signature: str,
    timestamp: str,
    payload: bytes,
    public_key: str | None = None,
) -> bool:
    """Validate Telnyx webhook signature.

    Telnyx uses ed25519 signatures for webhook validation.
    Headers: telnyx-signature-ed25519, telnyx-timestamp

    Args:
        signature: The telnyx-signature-ed25519 header value
        timestamp: The telnyx-timestamp header value
        payload: The raw request body
        public_key: The Telnyx public key (optional, uses settings if not provided)

    Returns:
        True if signature is valid, False otherwise
    """
    if not signature or not timestamp:
        return False

    # Use provided key or fall back to settings
    key = public_key or settings.TELNYX_PUBLIC_KEY
    if not key:
        logger.warning("telnyx_public_key_not_configured")
        # If no key is configured, skip validation in development
        # In production, this should always fail
        return settings.DEBUG

    try:
        import base64

        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        # Decode the public key
        public_key_bytes = base64.b64decode(key)
        ed25519_key = Ed25519PublicKey.from_public_bytes(public_key_bytes)

        # Create the signed payload (timestamp + payload)
        signed_payload = f"{timestamp}|".encode() + payload

        # Decode and verify signature
        signature_bytes = base64.b64decode(signature)
        ed25519_key.verify(signature_bytes, signed_payload)

        return True
    except Exception as e:
        logger.warning("telnyx_signature_validation_failed", error=str(e))
        return False


async def get_twilio_webhook_params(request: Request) -> dict[str, str]:
    """Extract form parameters from Twilio webhook request."""
    form_data = await request.form()
    return {key: str(value) for key, value in form_data.items()}


async def verify_twilio_webhook(request: Request) -> bool:
    """Verify Twilio webhook signature from request.

    Args:
        request: FastAPI request object

    Returns:
        True if signature is valid or validation is skipped in debug mode

    Raises:
        HTTPException: If signature validation fails in production
    """
    # Get auth token from settings
    auth_token = settings.TWILIO_AUTH_TOKEN
    if not auth_token:
        if settings.DEBUG:
            logger.warning("twilio_auth_token_not_configured_debug_mode")
            return True
        logger.error("twilio_auth_token_not_configured")
        raise HTTPException(status_code=500, detail="Twilio not configured")

    # Get signature from header
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        if settings.DEBUG:
            logger.warning("missing_twilio_signature_debug_mode")
            return True
        logger.warning("missing_twilio_signature")
        raise HTTPException(status_code=403, detail="Missing Twilio signature")

    # Get URL and params
    url = str(request.url)
    params = await get_twilio_webhook_params(request)

    # Validate signature
    if not validate_twilio_signature(signature, url, params, auth_token):
        logger.warning("invalid_twilio_signature", path=request.url.path)
        raise HTTPException(status_code=403, detail="Invalid Twilio signature")

    return True


async def verify_telnyx_webhook(request: Request) -> bool:
    """Verify Telnyx webhook signature from request.

    Args:
        request: FastAPI request object

    Returns:
        True if signature is valid or validation is skipped in debug mode

    Raises:
        HTTPException: If signature validation fails in production
    """
    # Get signature headers
    signature = request.headers.get("telnyx-signature-ed25519", "")
    timestamp = request.headers.get("telnyx-timestamp", "")

    if not signature or not timestamp:
        if settings.DEBUG:
            logger.warning("missing_telnyx_signature_debug_mode")
            return True
        logger.warning("missing_telnyx_signature")
        raise HTTPException(status_code=403, detail="Missing Telnyx signature")

    # Get raw body
    body = await request.body()

    # Validate signature
    if not validate_telnyx_signature(signature, timestamp, body):
        logger.warning("invalid_telnyx_signature")
        raise HTTPException(status_code=403, detail="Invalid Telnyx signature")

    return True


def require_twilio_signature(func: Any) -> Any:
    """Decorator to require valid Twilio signature on webhook endpoints."""

    @wraps(func)
    async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
        await verify_twilio_webhook(request)
        return await func(request, *args, **kwargs)

    return wrapper


def require_telnyx_signature(func: Any) -> Any:
    """Decorator to require valid Telnyx signature on webhook endpoints."""

    @wraps(func)
    async def wrapper(request: Request, *args: Any, **kwargs: Any) -> Any:
        await verify_telnyx_webhook(request)
        return await func(request, *args, **kwargs)

    return wrapper
