"""Application configuration using Pydantic settings."""

from typing import Any, Literal

from pydantic import PostgresDsn, RedisDsn, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Application
    APP_NAME: str = "Voice Pro API"
    APP_VERSION: str = "0.1.0"
    APP_ENVIRONMENT: Literal["development", "test", "production"] = "development"
    DEBUG: bool = False
    # Structlog level override ("DEBUG"|"INFO"|"WARNING"...). Without it, prod
    # (DEBUG=False) filters to WARNING and telephony INFO (greeting, gate,
    # wait_for_user) is invisible - which has repeatedly slowed live debugging.
    LOG_LEVEL: str | None = None
    API_V1_PREFIX: str = "/api/v1"

    # Server
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    RELOAD: bool = False
    PUBLIC_URL: str | None = None  # Public URL for webhook callbacks (e.g., ngrok URL)

    # Database
    POSTGRES_SERVER: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "voicenoob"
    DATABASE_URL: PostgresDsn | None = None

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def assemble_db_connection(cls, v: str | None, info: Any) -> str:
        """Build database URL from components if not provided."""
        if isinstance(v, str):
            return v

        data = info.data
        return str(
            PostgresDsn.build(
                scheme="postgresql+asyncpg",
                username=data.get("POSTGRES_USER"),
                password=data.get("POSTGRES_PASSWORD"),
                host=data.get("POSTGRES_SERVER"),
                port=data.get("POSTGRES_PORT"),
                path=f"{data.get('POSTGRES_DB') or ''}",
            ),
        )

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_PASSWORD: str | None = None
    REDIS_URL: RedisDsn | None = None

    @field_validator("REDIS_URL", mode="before")
    @classmethod
    def assemble_redis_connection(cls, v: str | None, info: Any) -> str:
        """Build Redis URL from components if not provided."""
        if isinstance(v, str):
            return v

        data = info.data
        password_part = f":{data.get('REDIS_PASSWORD')}@" if data.get("REDIS_PASSWORD") else ""
        return f"redis://{password_part}{data.get('REDIS_HOST')}:{data.get('REDIS_PORT')}/{data.get('REDIS_DB')}"

    # Security
    SECRET_KEY: str = "change-this-to-a-random-secret-key-in-production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # CORS
    CORS_ORIGINS: list[str] = [
        "http://localhost:3000",
        "http://localhost:3001",
        "http://localhost:8000",
    ]
    CORS_ALLOW_CREDENTIALS: bool = True
    CORS_ALLOW_METHODS: list[str] = ["*"]
    CORS_ALLOW_HEADERS: list[str] = ["*"]

    # Rate Limiting
    RATE_LIMIT_PER_MINUTE: int = 60

    # Default Admin User (created on first startup if no users exist)
    ADMIN_EMAIL: str = "admin@voicenoob.com"
    ADMIN_PASSWORD: str = "admin"
    ADMIN_NAME: str = "Admin"

    # Voice & AI Services
    OPENAI_API_KEY: str | None = None
    # Keep the proven production model as the default. Deployments can opt into a
    # newer Realtime model without another code change.
    OPENAI_REALTIME_MODEL: str = "gpt-realtime-2025-08-28"
    OPENAI_REALTIME_REASONING_EFFORT: str | None = None
    # Telephony turn-detection (server VAD). Noisy PSTN routes commit noise bursts
    # as phantom caller turns at the default sensitivity; raise the threshold via
    # env without a code change. Defaults preserve prior hardcoded behaviour.
    REALTIME_VAD_THRESHOLD: float = 0.6
    REALTIME_VAD_PREFIX_PADDING_MS: int = 300
    REALTIME_VAD_SILENCE_DURATION_MS: int = 700
    # Optional input noise reduction ("near_field" | "far_field"). Filters audio
    # before VAD and the model — cuts phantom turns from line noise on PSTN.
    REALTIME_INPUT_NOISE_REDUCTION: str | None = None
    # Hello-first opening (services/gpt_realtime.py). The agent says a bare "Hello?"
    # the instant the line is answered, then waits — which covers both real cases:
    # they heard it and answer, or the pickup was too early and we are already
    # waiting for them to speak first.
    #   NUDGE:  silence after the hello before asking "can you hear me?" once.
    #   GIVEUP: further silence after that before hanging up (dead air / mute box).
    #   OPENER_HOLD: hard ceiling on holding caller audio while the opener plays,
    #     so a response that never completes can never leave the caller unheard.
    #   SESSION_READY: how long to wait for `session.updated` before greeting anyway.
    #     Everything (hello AND the dead-air hangup) hangs off that one event; if the
    #     session config were rejected the callee would otherwise hear pure silence
    #     on a billed leg until the 300s bridge timeout.
    REALTIME_POST_HELLO_NUDGE_SECONDS: float = 12.0
    REALTIME_POST_HELLO_GIVEUP_SECONDS: float = 12.0
    REALTIME_OPENER_HOLD_MAX_SECONDS: float = 12.0
    REALTIME_SESSION_READY_TIMEOUT_SECONDS: float = 3.0
    DEEPGRAM_API_KEY: str | None = None
    ELEVENLABS_API_KEY: str | None = None

    # Telephony
    TELNYX_API_KEY: str | None = None
    TELNYX_PUBLIC_KEY: str | None = None
    TWILIO_ACCOUNT_SID: str | None = None
    TWILIO_AUTH_TOKEN: str | None = None
    # Outbound caller ID (E.164) for Twilio calls, and the selected outbound provider.
    # Twilio is the default. Telnyx stays dormant unless this is explicitly set to
    # "telnyx"; missing credentials fail closed instead of switching providers.
    TWILIO_FROM_NUMBER: str | None = None
    TELEPHONY_OUTBOUND_PROVIDER: str = "twilio"

    # Call recording. Three gates must ALL be true before a call is recorded:
    #   1. CALL_RECORDING_ENABLED (this platform-wide kill switch)
    #   2. Agent.enable_recording (the per-agent operator toggle)
    #   3. recording_policy.recording_allowed(to_number) (the legal-consent gate:
    #      one-party-consent US states only, fail-safe OFF on anything unknown)
    # Flip this to False to stop all recording immediately without touching agents.
    CALL_RECORDING_ENABLED: bool = True
    # Numbers whose owner has personally consented to being recorded (comma-separated
    # E.164). Consent is the thing the law actually asks for, and the area-code map
    # cannot know it — so an explicitly consenting party is allowed even when the
    # geography check would refuse (international, unknown, or an all-party state).
    # Intended for our OWN test handsets: this is how Sami gets audio of the agent to
    # diagnose it. Never put a prospect's number here without their real consent.
    RECORDING_CONSENT_NUMBERS: str = ""

    # Cal.com booking (used by the voice agent's check_availability / book_appointment
    # when configured; otherwise the agent falls back to the internal calendar).
    CALCOM_API_KEY: str | None = None
    CALCOM_EVENT_TYPE_ID: int | None = None
    # Business-hours guardrail for offered slots: the allowed LOCAL-hours window for
    # the lead, applied in-tool so we never offer out-of-hours times even if the
    # Cal.com schedule is permissive. Evaluated in the caller's timezone; the team
    # timezone is only the fallback anchor when no valid lead timezone is known.
    BOOKING_TEAM_TIMEZONE: str = "Europe/Stockholm"
    BOOKING_HOUR_START: int = 8
    BOOKING_HOUR_END: int = 20

    # Fulfilment handoff. Every Cal.com booking intent is persisted before the booking
    # request; a durable worker POSTs confirmed bookings to
    # f"{FULFIL_WEBHOOK_URL}/fulfil" so fulfilment can build the promised lead list.
    # The POST body is signed with
    # X-Fulfil-Signature: sha256=<HMAC-SHA256 hex over the raw JSON bytes> so the
    # fulfilment service can reject forged requests. A missing URL or secret fails
    # closed: no HTTP request is sent and durable work remains pending for repair.
    FULFIL_WEBHOOK_URL: str | None = None
    FULFIL_WEBHOOK_SECRET: str | None = None

    # Call-ended event sink (optional; B4). When CALL_EVENTS_URL is set, every call
    # that reaches a terminal state POSTs a signed JSON event to
    # f"{CALL_EVENTS_URL}/webhooks/call-ended" so the reply-router can advance the
    # lead's post-call status machine. Signed with X-VoicePro-Signature:
    # sha256=<HMAC-SHA256 hex over the raw JSON bytes> keyed by CALL_EVENTS_SECRET.
    # Unset URL = disabled.
    CALL_EVENTS_URL: str | None = None
    CALL_EVENTS_SECRET: str | None = None

    # Public transcript share links (B2). The backend's own public origin, used to
    # build the shareable transcript URL carried in the call-ended event
    # (f"{base}/api/public/transcripts/{share_token}"). Falls back to PUBLIC_URL —
    # the same origin the telephony webhooks already point at — so production needs
    # no new env var. With neither set the transcript URL is simply None.
    PUBLIC_BASE_URL: str | None = None
    # How long a transcript (and its share token + recording URL) survives before
    # the daily retention sweep nulls it. Share links die with the data.
    TRANSCRIPT_RETENTION_DAYS: int = 30

    # Answering-machine detection (C2). The callee's FIRST utterance on a call is
    # classified by a cheap chat model; a voicemail/IVR verdict hangs up instead of
    # pitching a recording. Biased toward "human" on purpose: a false machine hangs
    # up on a real prospect, a false human only wastes a few seconds.
    AMD_ENABLED: bool = True
    AMD_MODEL: str = "gpt-4o-mini"

    # External Service Timeouts (seconds)
    # These are critical for preventing hung connections during voice calls
    OPENAI_TIMEOUT: float = 30.0  # LLM inference can be slow
    DEEPGRAM_TIMEOUT: float = 15.0  # Real-time STT should be fast
    ELEVENLABS_TIMEOUT: float = 20.0  # TTS synthesis timeout
    TELNYX_TIMEOUT: float = 10.0  # Telephony API calls
    TWILIO_TIMEOUT: float = 10.0  # Telephony API calls
    GOOGLE_API_TIMEOUT: float = 15.0  # Calendar, Drive, etc.
    DEFAULT_EXTERNAL_TIMEOUT: float = 30.0  # Fallback for other APIs

    # Retry Configuration
    MAX_RETRIES: int = 3  # Number of retry attempts for failed requests
    RETRY_BACKOFF_FACTOR: float = 2.0  # Exponential backoff multiplier

    # Monitoring
    SENTRY_DSN: str | None = None
    SENTRY_ENVIRONMENT: str = "development"
    SENTRY_TRACES_SAMPLE_RATE: float = 1.0

    # OpenTelemetry
    OTEL_ENABLED: bool = False
    OTEL_SERVICE_NAME: str = "voicenoob-api"
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None


settings = Settings()
