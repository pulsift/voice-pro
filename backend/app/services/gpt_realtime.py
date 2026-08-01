"""GPT Realtime API service for Premium tier voice agents."""

import asyncio
import json
import re
import types
import uuid
from typing import Any

import structlog
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.integrations import get_workspace_integrations
from app.api.settings import get_user_api_keys
from app.core.auth import user_id_to_uuid
from app.core.config import settings
from app.services.availability import AvailabilityResult
from app.services.tools.registry import ToolRegistry

logger = structlog.get_logger()

# Language code to human-readable name mapping
LANGUAGE_NAMES: dict[str, str] = {
    "en-US": "English",
    "en-GB": "English (British)",
    "es-ES": "Spanish",
    "es-MX": "Spanish (Mexican)",
    "fr-FR": "French",
    "de-DE": "German",
    "it-IT": "Italian",
    "pt-BR": "Portuguese (Brazilian)",
    "pt-PT": "Portuguese",
    "nl-NL": "Dutch",
    "ja-JP": "Japanese",
    "ko-KR": "Korean",
    "zh-CN": "Chinese (Mandarin)",
    "zh-TW": "Chinese (Traditional)",
    "ru-RU": "Russian",
    "ar-SA": "Arabic",
    "hi-IN": "Hindi",
    "pl-PL": "Polish",
    "tr-TR": "Turkish",
    "vi-VN": "Vietnamese",
    "th-TH": "Thai",
    "id-ID": "Indonesian",
    "ms-MY": "Malay",
    "fil-PH": "Filipino",
}

_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")

# Sensible fallbacks so an un-personalized call never renders raw {{placeholders}}.
_DEFAULT_VARS: dict[str, str] = {
    "agentName": "Dave",
    "leadName": "there",
    "company": "your company",
    "offer_name": "what you reached out about",
    "offer_value_line": "",
    "bonus_line": "you're also set for a quick expert audit of how you're getting clients",
    "book_reason_audit_no": "either way, let's grab a quick call so the team can get you set up",
    "brief": "",
    "tzName": "Europe/Stockholm",
    # Overwritten per call by lead_timezone.resolve(); "Pacific time", not a slash.
    "tz_spoken": "your local time",
    # Filled in at answer time by GPTRealtimeSession.load_availability(); this
    # default is what the agent sees if the calendar could not be pre-loaded.
    "availability_block": (
        "The calendar could not be pre-loaded for this call. Once you know their "
        "timezone, call refresh_availability once and offer what it returns."
    ),
}

# The whole first turn. One word, deliberately: Sami's design is that the agent
# says hello and then STOPS, exactly like a person who has just been picked up on.
DEFAULT_HELLO_LINE = "Hello?"

# The stored greeting may be overridden per agent, but it may not quietly become a
# pitch: an agent row still holding the OLD full greeting ("Heyy {{leadName}}, this
# is Dave from Pulsift!") would be forced out as turn 1 and the entire hello-first
# design would silently not apply — with turn 2 then repeating the introduction.
_MAX_HELLO_WORDS = 4


def _usable_hello(configured: str, log: Any) -> str:
    """The greeting to speak first: the configured one only if it is still a greeting."""
    candidate = configured.strip()
    if not candidate:
        return DEFAULT_HELLO_LINE
    if len(candidate.split()) > _MAX_HELLO_WORDS:
        log.warning(
            "configured_greeting_too_long_using_bare_hello",
            configured_words=len(candidate.split()),
        )
        return DEFAULT_HELLO_LINE
    return candidate

# The earliest response that can be the opener: 1 is the bare hello.
_OPENER_RESPONSE_INDEX = 2

# Caps on audio held back while the opener plays (mu-law 8kHz = 8000 bytes/second).
# Only the last few seconds are ever forwarded — see release_input_hold.
_HELD_AUDIO_MAX_BYTES = 8000 * 15
_HELD_FLUSH_TAIL_BYTES = 8000 * 3


def render_template(template: str, variables: dict[str, Any] | None) -> str:
    """Fill {{placeholders}} in the agent prompt from per-call variables.

    Lead/offer data is DATA, never instructions: we strip any brace sequences from
    injected values so a value like a company literally named "}}ignore..." can't
    break out of its slot (the prompt also tells the model to never obey instructions
    hidden in DATA). Missing keys fall back to neutral defaults.
    """
    merged = dict(_DEFAULT_VARS)
    for key, val in (variables or {}).items():
        if val is not None:
            merged[key] = str(val)

    def _repl(match: "re.Match[str]") -> str:
        raw = merged.get(match.group(1), "")
        return str(raw).replace("{{", "").replace("}}", "")

    return _VAR_PATTERN.sub(_repl, template)


def build_instructions_with_language(
    system_prompt: str,
    language: str,
    enabled_tools: list[str] | None = None,
    timezone: str | None = None,
    runtime_rules: list[str] | None = None,
) -> str:
    """Build comprehensive voice agent instructions.

    Wraps the user's custom system prompt with voice-specific configuration
    including language requirements, conversation guidelines, and tool context.

    Args:
        system_prompt: The agent's custom system prompt (from frontend UI)
        language: Language code (e.g., "en-US", "es-ES")
        enabled_tools: List of enabled tool IDs (optional, for context)
        timezone: Workspace timezone (e.g., "America/New_York", "UTC")
        runtime_rules: Code-owned state overrides appended to the RULES block

    Returns:
        Complete instructions string optimized for voice conversations
    """
    language_name = LANGUAGE_NAMES.get(language, language)
    tz_name = timezone or "UTC"

    # Get current date/time in the workspace timezone for context
    from datetime import datetime

    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
        now = datetime.now(tz)
        current_datetime = now.strftime("%A, %B %d, %Y at %I:%M %p")
    except Exception:
        # Fallback if timezone is invalid
        current_datetime = datetime.now().strftime("%A, %B %d, %Y at %I:%M %p")

    runtime_rule_block = "".join(
        f"\n- {rule}" for rule in (runtime_rules or [])
    )

    # Build the complete voice agent instructions
    instructions = f"""[CONTEXT]
Language: {language_name}
Timezone: {tz_name}
Current: {current_datetime}

[RULES]
- Speak ONLY in {language_name}
- Any time you are given without a timezone is in {tz_name}
- Keep responses concise - this is voice, not text
- Never speak a tool name, an id, a timestamp or a field name out loud{runtime_rule_block}

[YOUR ROLE]
{system_prompt}"""

    return instructions


class TranscriptEntry:
    """Single transcript entry representing one turn in the conversation."""

    def __init__(self, role: str, content: str, timestamp: str | None = None) -> None:
        from datetime import UTC, datetime

        self.role = role  # "user" or "assistant"
        self.content = content
        self.timestamp = timestamp or datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content, "timestamp": self.timestamp}


class GPTRealtimeSession:
    """Manages a GPT Realtime API session for a voice call.

    Handles:
    - WebSocket connection to OpenAI Realtime API
    - Internal tool integration
    - Audio streaming
    - Tool call routing to internal tool handlers
    - Transcript accumulation
    """

    def __init__(
        self,
        db: AsyncSession,
        user_id: int,
        agent_config: dict[str, Any],
        session_id: str | None = None,
        workspace_id: uuid.UUID | None = None,
        variables: dict[str, Any] | None = None,
    ) -> None:
        """Initialize GPT Realtime session.

        Args:
            db: Database session
            user_id: User ID (int, from users.id)
            agent_config: Agent configuration (system prompt, enabled integrations, etc.)
            session_id: Optional session ID
            workspace_id: Workspace UUID (required for API key isolation)
        """
        self.db = db
        self.user_id = user_id  # int for ToolRegistry (Contact queries)
        self.user_id_uuid = user_id_to_uuid(user_id)  # UUID for UserSettings queries
        self.workspace_id = workspace_id  # For workspace-isolated API key lookup
        self.agent_config = agent_config
        self.variables = (
            variables or {}
        )  # Per-call lead/offer data (prompt fill + booking attendee)
        self.session_id = session_id or str(uuid.uuid4())
        self.connection: Any = None
        self.tool_registry: ToolRegistry | None = None
        self.client: AsyncOpenAI | None = None
        # Transcript accumulation
        self._transcript_entries: list[TranscriptEntry] = []
        self._current_assistant_text: str = ""
        # --- opening sequence (hello-first) -------------------------------------
        # The agent's first turn is a bare "Hello?" spoken the moment the line is
        # answered, then silence until the caller speaks. Both outcomes work: they
        # hear it and answer (we flow into the opener), or the pickup was too early,
        # they never heard it, and we are already in the position of waiting for
        # them to speak first.
        self._hello_line: str = DEFAULT_HELLO_LINE
        self._hello_sent: bool = False
        self._caller_has_spoken: bool = False
        self._response_count: int = 0
        self._opener_delivered: bool = False
        self._held_response_id: str | None = None
        self._held_response_spoke: bool = False
        # Opener protection: while the opener is being spoken, caller audio is HELD
        # (buffered) rather than forwarded, so a "yeah?" or "hello?" landing on top
        # of it cannot make the server cancel the turn half-way. Sami's ask was that
        # the opener runs all the way to "caught you at an okay time?" — and holding
        # beats dropping, because nothing they said is lost: it is flushed the
        # instant the opener finishes.
        self._input_hold: bool = False
        self._held_audio: bytearray = bytearray()
        self._held_audio_dropped: int = 0
        # Rendering inputs kept so instructions can be re-pushed mid-session once
        # the availability menu arrives.
        self._instructions_language: str = "en-US"
        self._instructions_timezone: str = "UTC"
        self._availability_lock = asyncio.Lock()
        self._availability_revision = 0
        self._availability_preload_started = False
        self.realtime_model = settings.OPENAI_REALTIME_MODEL
        self.realtime_reasoning_effort = settings.OPENAI_REALTIME_REASONING_EFFORT
        self.logger = logger.bind(
            component="gpt_realtime",
            session_id=self.session_id,
            user_id=str(user_id),
            workspace_id=str(workspace_id) if workspace_id else None,
        )

    async def initialize(self) -> None:
        """Initialize the Realtime session with internal tools."""
        self.logger.info("gpt_realtime_session_initializing")

        # Get user's API keys from settings (uses UUID)
        # Workspace isolation: only use workspace-specific API keys, no fallback
        user_settings = await get_user_api_keys(
            self.user_id_uuid, self.db, workspace_id=self.workspace_id
        )
        api_key = user_settings.openai_api_key if user_settings else None
        key_source = "workspace"

        # Fall back to the user-level key, then the platform env key. Single-tenant
        # own-tool: the browser path already does this; strict per-workspace isolation
        # is a multi-tenant feature we don't need, and it would 400 the call if the
        # agent's workspace happens not to carry the key.
        if not api_key and self.workspace_id:
            user_level = await get_user_api_keys(self.user_id_uuid, self.db, workspace_id=None)
            api_key = user_level.openai_api_key if user_level else None
            key_source = "user"
        if not api_key:
            api_key = settings.OPENAI_API_KEY
            key_source = "platform_env"

        if not api_key:
            self.logger.warning("openai_key_not_configured", workspace_id=str(self.workspace_id))
            raise ValueError(
                "OpenAI API key not configured. Add it in Settings (workspace or account level)."
            )
        self.logger.info("using_openai_key", source=key_source)

        # Initialize OpenAI client with user's or global API key
        self.client = AsyncOpenAI(api_key=api_key)

        # Get integration credentials for the workspace
        integrations: dict[str, Any] = {}
        if self.workspace_id:
            integrations = await get_workspace_integrations(
                self.user_id_uuid, self.workspace_id, self.db
            )

        # Initialize tool registry with enabled tools, workspace context, and per-call vars
        self.tool_registry = ToolRegistry(
            self.db,
            self.user_id,
            integrations=integrations,
            workspace_id=self.workspace_id,
            variables=self.variables,
        )
        self.tool_registry.crm_tools.set_live_availability_loader(
            self._refresh_live_availability
        )
        self.tool_registry.crm_tools.set_live_availability_invalidator(
            self._invalidate_live_availability
        )

        # Connect to OpenAI Realtime API
        await self._connect_realtime_api()

        self.logger.info("gpt_realtime_session_initialized")

    async def _connect_realtime_api(self) -> None:
        """Establish connection to OpenAI Realtime API using official SDK."""
        if not self.client:
            raise ValueError("OpenAI client not initialized")

        model = self.realtime_model
        reasoning_effort = self._effective_reasoning_effort()
        self.logger.info(
            "connecting_to_openai_realtime",
            model=model,
            reasoning_effort=reasoning_effort,
        )

        try:
            # Use the GA realtime.connect() (the beta namespace + flat session shape
            # was disabled by OpenAI -> "beta_api_shape_disabled"; SDK 2.8.1 has GA).
            self.connection = await self.client.realtime.connect(model=model).__aenter__()

            self.logger.info("realtime_connection_established")

            # Configure session with internal tools
            await self._configure_session()

            self.logger.info("connected_to_openai_realtime")

        except Exception as e:
            self.logger.exception(
                "realtime_connection_failed", error=str(e), error_type=type(e).__name__
            )
            raise

    async def _configure_session(self) -> None:
        """Configure Realtime API session with agent settings and internal tools."""
        if not self.connection or not self.tool_registry:
            self.logger.warning(
                "session_config_skipped",
                has_connection=bool(self.connection),
                has_registry=bool(self.tool_registry),
            )
            return

        # Get tool definitions from registry
        enabled_tools = self.agent_config.get("enabled_tools", [])
        tools = self.tool_registry.get_all_tool_definitions(enabled_tools)

        # Get workspace timezone if available
        workspace_timezone = "UTC"
        if self.workspace_id:
            from app.models.workspace import Workspace

            result = await self.db.execute(
                select(Workspace).where(Workspace.id == self.workspace_id)
            )
            workspace = result.scalar_one_or_none()
            if workspace and workspace.settings:
                workspace_timezone = workspace.settings.get("timezone", "UTC")

        # Build instructions with language directive and timezone
        language = self.agent_config.get("language", "en-US")
        # Default to marin for natural conversational tone
        voice = self.agent_config.get("voice", "marin")
        self._instructions_language = language
        self._instructions_timezone = workspace_timezone
        instructions = self._render_instructions()

        # GA Realtime session shape (nested audio config). Audio is G.711 mu-law 8kHz
        # both ways to match Telnyx PCMU media with no transcoding. (speed/temperature
        # dropped — not part of the GA session shape; were beta-only here.)
        session_config: dict[str, Any] = {
            "type": "realtime",
            "output_modalities": ["audio"],
            "instructions": instructions,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    "transcription": {"model": "whisper-1"},
                    # Less eager turn-taking so it stops cutting the caller off.
                    # Env-tunable: noisy PSTN routes need a higher threshold so
                    # line noise can't commit phantom caller turns.
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": settings.REALTIME_VAD_THRESHOLD,
                        "prefix_padding_ms": settings.REALTIME_VAD_PREFIX_PADDING_MS,
                        "silence_duration_ms": settings.REALTIME_VAD_SILENCE_DURATION_MS,
                    },
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": voice,
                    "speed": 0.9,  # slightly slower than default (caller felt it was too fast)
                },
            },
            "tools": tools,
            "tool_choice": "auto",
        }
        if settings.REALTIME_INPUT_NOISE_REDUCTION in ("near_field", "far_field"):
            session_config["audio"]["input"]["noise_reduction"] = {
                "type": settings.REALTIME_INPUT_NOISE_REDUCTION
            }
        reasoning_effort = self._effective_reasoning_effort()
        if reasoning_effort:
            session_config["reasoning"] = {"effort": reasoning_effort}

        self.logger.info("configuring_session", tool_count=len(tools), enabled_tools=enabled_tools)

        try:
            # Build session configuration using SDK
            await self.connection.session.update(session=session_config)

            self.logger.info(
                "session_configured",
                tool_count=len(tools),
            )

            # Hello-first design: the agent's opening line is settled here and
            # spoken by send_hello() as soon as the telephony loop is running (the
            # bridge owns the timing so the response can never be created before
            # the event loop is ready to stream its audio).
            self._hello_line = _usable_hello(
                str(self.agent_config.get("initial_greeting") or ""), self.logger
            )
            self.logger.info("hello_line_ready", hello=self._hello_line)
        except Exception as e:
            self.logger.exception(
                "session_config_failed", error=str(e), error_type=type(e).__name__
            )
            raise

    def _effective_reasoning_effort(self) -> str | None:
        """Return configured reasoning effort only for Realtime 2 models."""
        effort = self.realtime_reasoning_effort
        if effort in {"low", "medium", "high"} and self.realtime_model.startswith(
            "gpt-realtime-2."
        ):
            return effort
        if effort:
            self.logger.warning("invalid_realtime_reasoning_effort", effort=effort)
        return None

    async def handle_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        """Handle tool call from GPT Realtime by routing to internal tools.

        Args:
            tool_call: Tool call from GPT Realtime

        Returns:
            Tool result
        """
        if not self.tool_registry:
            return {"success": False, "error": "Tool registry not initialized"}

        tool_name = tool_call.get("name", "")
        arguments = tool_call.get("arguments", {})

        self.logger.info(
            "handling_tool_call",
            tool_name=tool_name,
            argument_keys=sorted(arguments),
        )

        # Execute tool via internal tool registry
        result = await self.tool_registry.execute_tool(tool_name, arguments)

        return result

    async def handle_function_call_event(self, event: Any) -> dict[str, Any]:
        """Handle function call from GPT Realtime.

        Args:
            event: Function call event from SDK

        Returns:
            Tool execution result with optional 'action' field for call control
        """
        call_id = event.call_id
        name = event.name

        # Parse arguments safely - GPT may send incomplete/malformed JSON
        try:
            arguments = (
                json.loads(event.arguments) if isinstance(event.arguments, str) else event.arguments
            )
        except json.JSONDecodeError as e:
            self.logger.warning(
                "function_call_json_parse_error",
                call_id=call_id,
                tool_name=name,
                raw_arguments=str(event.arguments)[:200],
                error=str(e),
            )
            # Return error to GPT so it can retry
            if self.connection:
                await self.connection.conversation.item.create(
                    item={
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps({"success": False, "error": "Invalid JSON arguments"}),
                    }
                )
            return {"success": False, "error": "Invalid JSON arguments"}

        # Execute tool via internal tool registry
        result = await self.handle_tool_call({"name": name, "arguments": arguments})

        # Send result back using SDK
        if self.connection:
            await self.connection.conversation.item.create(
                item={
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": json.dumps(result),
                }
            )
            # Trigger GPT to generate a response after the function call.
            # wait_for_user is the official noise/silence no-op: forcing a
            # response here would defeat it, so stay silent instead.
            if name != "wait_for_user":
                await self.connection.response.create()

        self.logger.info(
            "function_call_completed",
            call_id=call_id,
            tool_name=name,
            success=result.get("success"),
            action=result.get("action"),
        )

        return result

    def _render_instructions(self) -> str:
        """Render the full instruction text from the prompt template + variables."""
        system_prompt = render_template(
            self.agent_config.get("system_prompt", "You are a helpful voice assistant."),
            self.variables,
        )
        runtime_rules = None
        if self._instructions_timezone == "unresolved":
            runtime_rules = [
                (
                    "TIMEZONE CORRECTION OVERRIDE: the caller's timezone is "
                    "unresolved. This supersedes any normal-case statement in "
                    "YOUR ROLE that says it is known."
                ),
                (
                    "Ask exactly one clarification: their US state or a standard "
                    "time zone such as Eastern, Central, Mountain, or Pacific."
                ),
                (
                    "Until it resolves, do not offer or book any time and do not "
                    "reuse earlier calendar times."
                ),
            ]
        return build_instructions_with_language(
            system_prompt,
            self._instructions_language,
            timezone=self._instructions_timezone,
            runtime_rules=runtime_rules,
        )

    def _availability_session_update(self) -> dict[str, Any]:
        if not self.tool_registry:
            raise RuntimeError("Tool registry not initialized")
        enabled_tools = self.agent_config.get("enabled_tools", [])
        return {
            "type": "realtime",
            "instructions": self._render_instructions(),
            "tools": self.tool_registry.get_all_tool_definitions(enabled_tools),
            "tool_choice": "auto",
        }

    async def _apply_availability_menu_locked(
        self,
        menu: AvailabilityResult,
        lead_tz: str,
        *,
        origin: str,
        revision: int,
        apply_timezone: bool = True,
    ) -> tuple[AvailabilityResult, int] | None:
        """Publish one revision to Realtime and the booking gate under the lock."""
        if revision != self._availability_revision:
            self.logger.info(
                "availability_result_discarded_stale",
                origin=origin,
                revision=revision,
                current_revision=self._availability_revision,
            )
            return None
        if not self.connection or not self.tool_registry:
            return None

        status = menu["status"]
        from app.services import lead_timezone

        timezone = str(menu.get("timezone") or lead_tz)
        if origin == "preloaded" and status == "unavailable":
            self.variables["tzName"] = timezone
            self.variables["tz_spoken"] = lead_timezone.spoken_zone_name(timezone)
            self._instructions_timezone = timezone
            self.logger.warning("availability_preload_unavailable", timezone=timezone)
            return menu, 0
        previous_timezone = self._instructions_timezone
        missing = object()
        previous_values = {
            key: self.variables.get(key, missing)
            for key in ("tzName", "tz_spoken", "availability_block")
        }
        if apply_timezone:
            self.variables["tzName"] = timezone
            self.variables["tz_spoken"] = lead_timezone.spoken_zone_name(timezone)
            self._instructions_timezone = timezone
        else:
            self.variables["tzName"] = "unresolved"
            self.variables["tz_spoken"] = "unresolved"
            self._instructions_timezone = "unresolved"
        self.variables["availability_block"] = str(menu["block"])

        try:
            await self.connection.session.update(
                session=self._availability_session_update()
            )
        except Exception:
            self._instructions_timezone = previous_timezone
            for key, value in previous_values.items():
                if value is missing:
                    self.variables.pop(key, None)
                else:
                    self.variables[key] = value
            raise

        adopted = self.tool_registry.crm_tools.apply_availability_menu(
            menu, origin=origin
        )
        self.logger.info(
            "availability_applied",
            origin=origin,
            revision=revision,
            status=status,
            slot_count=adopted,
            timezone=timezone,
        )
        return menu, adopted

    async def _refresh_live_availability(
        self, lead_tz: str, origin: str
    ) -> AvailabilityResult | None:
        """Serialize a caller-driven read and publish one authoritative revision."""
        from app.services.availability import fetch_menu

        async with self._availability_lock:
            self._availability_revision += 1
            revision = self._availability_revision
            menu = await fetch_menu(lead_tz)
            if menu["status"] == "unavailable":
                menu["block"] = (
                    "The calendar is unavailable right now. Do not invent, offer, "
                    "or book a time, and do not call refresh_availability again on "
                    "this call. Tell the caller the team will follow up."
                )
            applied = await self._apply_availability_menu_locked(
                menu,
                lead_tz,
                origin=origin,
                revision=revision,
            )
            return applied[0] if applied else None

    async def _invalidate_live_availability(self, reason: str) -> None:
        """Invalidate old prompt and tool state after an unresolved correction."""
        from app.services.availability import empty_menu

        async with self._availability_lock:
            self._availability_revision += 1
            revision = self._availability_revision
            menu = empty_menu(self._instructions_timezone)
            menu["block"] = (
                "The caller corrected their timezone, but it could not be resolved. "
                "Do not offer or book any previously shown time. Ask once for their "
                "US state or a standard time zone such as Eastern, Central, "
                "Mountain, or Pacific."
            )
            applied = await self._apply_availability_menu_locked(
                menu,
                self._instructions_timezone,
                origin="offered",
                revision=revision,
                apply_timezone=False,
            )
            if applied is None:
                raise RuntimeError(f"Availability invalidation failed: {reason}")

    async def load_availability(self) -> bool:
        """Pre-load the calendar into the agent's head, not into a mid-call tool call.

        Runs as a background task the moment the media stream opens, so the "Hello?"
        never waits on Cal.com. When the menu lands (a few hundred ms later, while
        the caller is still saying hello) the live session's instructions are
        re-pushed with the real open times, and the booking tools adopt the very
        same slot ids — so the times the agent can SAY are exactly the times
        select_slot will accept.

        Returns True when a non-empty menu was applied. Never raises.
        """
        if not self.connection or not self.tool_registry:
            return False
        try:
            from app.services import lead_timezone
            from app.services.availability import fetch_menu

            async with self._availability_lock:
                if self._availability_preload_started or self._availability_revision:
                    return False
                self._availability_preload_started = True
                revision = self._availability_revision

            # Derived, not asked for: an explicit timezone on the record, else the
            # lead's state, else the area code of the number we are calling.
            lead_tz, tz_source = lead_timezone.resolve(self.variables)
            self.logger.info(
                "lead_timezone_resolved", timezone=lead_tz, source=tz_source
            )

            menu = await fetch_menu(lead_tz)
            async with self._availability_lock:
                applied = await self._apply_availability_menu_locked(
                    menu,
                    lead_tz,
                    origin="preloaded",
                    revision=revision,
                )
            return bool(applied and applied[1] > 0)
        except Exception as e:
            # A calendar hiccup must never cost us the call: the agent keeps the
            # default block, which tells it to call refresh_availability instead.
            self.logger.warning("availability_preload_failed", error_type=type(e).__name__)
            return False

    async def send_hello(self) -> bool:
        """Turn 1: say the bare hello, then stop.

        Forced with a per-response instruction override rather than left to the
        prompt, because this one line must be identical on every call — one word,
        no pitch, nothing to interrupt. `tool_choice: none` keeps the model from
        reaching for a tool before anyone has even spoken.
        """
        if self._hello_sent or not self.connection:
            return False
        self._hello_sent = True
        try:
            await self.connection.response.create(
                response={
                    "instructions": (
                        "The call has just been answered. Say exactly this, warmly, "
                        f'and nothing else: "{self._hello_line}" '
                        "Then stop and wait for them."
                    ),
                    "output_modalities": ["audio"],
                    "tool_choice": "none",
                }
            )
            self.logger.info("hello_sent", hello=self._hello_line[:60])
            return True
        except Exception as e:
            self.logger.exception("hello_failed", error=str(e))
            return False

    async def send_presence_check(self) -> bool:
        """Nobody spoke after the hello — ask once whether they can hear us."""
        if not self.connection or self._caller_has_spoken:
            return False
        try:
            await self.connection.response.create(
                response={
                    "instructions": (
                        "There has been silence since you said hello. Say exactly: "
                        '"Hello? Can you hear me?" Nothing else.'
                    ),
                    "output_modalities": ["audio"],
                    "tool_choice": "none",
                }
            )
            self.logger.info("presence_check_sent")
            return True
        except Exception as e:
            self.logger.warning("presence_check_failed", error=str(e))
            return False

    def note_caller_spoke(self) -> None:
        """Record that a human (or a machine greeting) has made a sound."""
        self._caller_has_spoken = True

    @property
    def caller_has_spoken(self) -> bool:
        """Whether anything has been heard from the far end yet."""
        return self._caller_has_spoken

    @property
    def input_held(self) -> bool:
        """Whether caller audio is currently being buffered instead of forwarded."""
        return self._input_hold

    def note_response_created(self, response_id: str | None = None) -> None:
        """Protect the opener from being cut in half, tied to ITS response id.

        Held ONLY once the caller has actually spoken, and that condition is
        load-bearing, not cosmetic: while input is held no audio reaches OpenAI, so
        `input_audio_buffer.speech_started` cannot fire — and that event is what
        disarms the dead-air watchdog. A hold engaged before anyone has spoken would
        therefore make a talking human look like silence and get them hung up on.
        The opener is always a reply to their first sound, so this costs nothing.

        The hold remembers WHICH response it is protecting, so a late `response.done`
        belonging to the hello (or to a cancelled turn) cannot release the opener's
        hold early. When the API gives us no id we fall back to releasing on any
        completion, which is the old behaviour and still bounded by the ceiling timer.
        """
        self._response_count += 1
        if (
            self._response_count >= _OPENER_RESPONSE_INDEX
            and self._caller_has_spoken
            and not self._opener_delivered
        ):
            self._input_hold = True
            self._held_response_id = response_id
            self._held_response_spoke = False
            self.logger.info(
                "opener_protection_engaged",
                response_index=self._response_count,
                response_id=response_id,
            )

    def note_assistant_audio(self) -> None:
        """The response currently being protected actually produced speech.

        This is what distinguishes the opener from a response that only carried a
        function call: a tool-call turn emits `response.created` too, so protecting
        "the second response" blindly let a tool call absorb the protection and left
        the real opener — the one the caller can talk over — unguarded.
        """
        if self._input_hold:
            self._held_response_spoke = True

    async def release_input_hold(
        self, *, reason: str, response_id: str | None = None, flush: bool = True
    ) -> None:
        """Stop holding caller audio; forward the tail of what was held.

        Called on the opener's response.done, on error, and by a hard timeout, so
        there is no path where the caller stays unheard because a response never
        completed.

        A completion for a DIFFERENT response is ignored: only the turn we are
        actually protecting (or an unconditional release — error, ceiling, teardown)
        may end the hold.

        Only the TAIL is forwarded (`_HELD_FLUSH_TAIL_BYTES`). Replaying ten seconds
        of held audio in one append hands server VAD a timeline it will split into
        several turns, and the caller then hears the agent answer three things they
        said ten seconds ago, in a row. The last thing they said is the thing that
        needs answering.

        `flush=False` discards the buffer instead: used by the ceiling timer, because
        appending audio while the protected response is still generating is exactly
        what cancels it — the mechanism would otherwise cut off the very sentence it
        exists to protect.
        """
        if not self._input_hold:
            return
        if (
            response_id is not None
            and self._held_response_id is not None
            and response_id != self._held_response_id
        ):
            self.logger.info(
                "opener_hold_kept_for_other_response",
                completed=response_id,
                protecting=self._held_response_id,
            )
            return
        self._held_response_id = None
        self._input_hold = False
        held, self._held_audio = bytes(self._held_audio), bytearray()
        dropped, self._held_audio_dropped = self._held_audio_dropped, 0
        # A turn that produced no speech was not the opener (a tool call, or a
        # cancelled response), so the protection has not been used up yet.
        if self._held_response_spoke:
            self._opener_delivered = True
        self._held_response_spoke = False
        tail = held[-_HELD_FLUSH_TAIL_BYTES:] if flush else b""
        self.logger.info(
            "opener_protection_released",
            reason=reason,
            held_bytes=len(held),
            forwarded_bytes=len(tail),
            dropped_bytes=dropped,
            opener_delivered=self._opener_delivered,
        )
        if tail:
            await self.send_audio(tail)

    async def send_audio(self, audio_data: bytes) -> None:
        """Send audio input to GPT Realtime using SDK.

        Args:
            audio_data: PCM16 audio data (raw bytes)
        """
        if not self.connection:
            self.logger.error("send_audio_failed_no_connection")
            return

        # Opener protection: hold, don't drop. The tail is forwarded the moment the
        # opener finishes, so an interruption during it is answered a second late
        # rather than lost. (The old drop-everything gate is gone: nothing could
        # close it any more once the timed fallback greeting was removed.)
        if self._input_hold:
            room = _HELD_AUDIO_MAX_BYTES - len(self._held_audio)
            if room > 0:
                self._held_audio.extend(audio_data[:room])
            self._held_audio_dropped += max(len(audio_data) - max(room, 0), 0)
            return

        try:
            import base64

            # Convert raw bytes to base64 string as required by OpenAI Realtime API
            audio_base64 = base64.b64encode(audio_data).decode("utf-8")

            # Use SDK's input_audio_buffer.append method
            await self.connection.input_audio_buffer.append(audio=audio_base64)
            self.logger.debug(
                "audio_sent_to_realtime",
                size_bytes=len(audio_data),
                base64_length=len(audio_base64),
            )
        except Exception as e:
            self.logger.exception("send_audio_error", error=str(e), error_type=type(e).__name__)

    def add_user_transcript(self, text: str) -> None:
        """Add a user transcript entry.

        Args:
            text: Transcribed user speech
        """
        if text.strip():
            self._transcript_entries.append(TranscriptEntry(role="user", content=text.strip()))
            self.logger.debug("user_transcript_added", text_length=len(text))

    def observe_user_transcript(self, text: str) -> None:
        """Observe a completed caller turn, independently of transcript persistence."""
        utterance = text.strip()
        if not utterance:
            return

        if self.tool_registry:
            self.tool_registry.observe_user_utterance(utterance)

        if self.agent_config.get("enable_transcript", False):
            self.add_user_transcript(utterance)

        self.logger.debug(
            "user_utterance_observed",
            text_length=len(utterance),
            persisted=bool(self.agent_config.get("enable_transcript", False)),
        )

    def add_assistant_transcript(self, text: str) -> None:
        """Add an assistant transcript entry.

        Args:
            text: Assistant response text
        """
        if text.strip():
            self._transcript_entries.append(TranscriptEntry(role="assistant", content=text.strip()))
            self.logger.debug("assistant_transcript_added", text_length=len(text))

    def accumulate_assistant_text(self, delta: str) -> None:
        """Accumulate assistant text delta for transcript.

        Args:
            delta: Text delta from response.text.delta event
        """
        self._current_assistant_text += delta

    def flush_assistant_text(self) -> None:
        """Complete one assistant turn: hand it to the booking state, then persist.

        The booking tools need what the agent SAID regardless of whether transcripts
        are being stored — "yes" and "midday" only mean something next to the question
        that prompted them. Persistence stays opt-in, exactly like the caller side.
        """
        spoken = self._current_assistant_text.strip()
        self._current_assistant_text = ""
        if not spoken:
            return
        if self.tool_registry:
            self.tool_registry.observe_assistant_utterance(spoken)
        if self.agent_config.get("enable_transcript", False):
            self.add_assistant_transcript(spoken)

    def get_transcript(self) -> str:
        """Get the full transcript as formatted text.

        Returns:
            Formatted transcript string
        """
        lines = []
        for entry in self._transcript_entries:
            role_label = "User" if entry.role == "user" else "Assistant"
            lines.append(f"[{role_label}]: {entry.content}")
        return "\n\n".join(lines)

    def get_transcript_entries(self) -> list[dict[str, str]]:
        """Get transcript entries as list of dicts.

        Returns:
            List of transcript entry dictionaries
        """
        return [entry.to_dict() for entry in self._transcript_entries]

    def get_booking_attempts(self) -> list[dict[str, Any]]:
        """Return sanitized booking diagnostics captured during this call."""
        if not self.tool_registry:
            return []
        return self.tool_registry.get_booking_attempts()

    async def cleanup(self) -> None:
        """Cleanup resources."""
        self.logger.info("gpt_realtime_session_cleanup_started")

        # Flush any remaining assistant text
        self.flush_assistant_text()

        # Close Realtime connection
        if self.connection:
            try:
                # Try close() method first (if available)
                if hasattr(self.connection, "close"):
                    await self.connection.close()
                # Otherwise try aclose() for async generators
                elif hasattr(self.connection, "aclose"):
                    await self.connection.aclose()
                self.logger.info("realtime_connection_closed")
            except Exception as e:
                self.logger.warning("connection_close_failed", error=str(e))

        # Cleanup tool registry
        if self.tool_registry:
            # No cleanup needed for internal tools
            pass

        self.logger.info(
            "gpt_realtime_session_cleanup_completed",
            transcript_entries=len(self._transcript_entries),
        )

    async def __aenter__(self) -> "GPTRealtimeSession":
        """Async context manager entry."""
        await self.initialize()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: types.TracebackType | None,
    ) -> None:
        """Async context manager exit."""
        await self.cleanup()
