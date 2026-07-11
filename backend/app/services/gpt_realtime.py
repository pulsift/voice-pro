"""GPT Realtime API service for Premium tier voice agents."""

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
}


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
) -> str:
    """Build comprehensive voice agent instructions.

    Wraps the user's custom system prompt with voice-specific configuration
    including language requirements, conversation guidelines, and tool context.

    Args:
        system_prompt: The agent's custom system prompt (from frontend UI)
        language: Language code (e.g., "en-US", "es-ES")
        enabled_tools: List of enabled tool IDs (optional, for context)
        timezone: Workspace timezone (e.g., "America/New_York", "UTC")

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

    # Build the complete voice agent instructions
    instructions = f"""[CONTEXT]
Language: {language_name}
Timezone: {tz_name}
Current: {current_datetime}

[RULES]
- Speak ONLY in {language_name}
- All times are in {tz_name} timezone
- For booking tools, use ISO format with timezone offset (e.g., 2024-12-01T14:00:00-05:00)
- Keep responses concise - this is voice, not text
- Summarize tool results naturally

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
        # Initial greeting (triggered after event loop starts to avoid race condition)
        self._pending_initial_greeting: str | None = None
        self._greeting_triggered: bool = False
        # Inbound-audio gate. Open by default; closed while an initial greeting is
        # pending so the caller's early "hello" can't trigger VAD and cancel the
        # greeting mid-birth. Re-opened when the greeting response completes (or on
        # error, fail-open). Covers both the Telnyx and Twilio bridges since both
        # feed audio through send_audio().
        self._input_gate_open: bool = True
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
        system_prompt = self.agent_config.get("system_prompt", "You are a helpful voice assistant.")
        # Fill per-call {{placeholders}} (lead name/company/offer/tz, etc.) before wrapping.
        system_prompt = render_template(system_prompt, self.variables)
        language = self.agent_config.get("language", "en-US")
        # Default to marin for natural conversational tone
        voice = self.agent_config.get("voice", "marin")
        instructions = build_instructions_with_language(
            system_prompt, language, timezone=workspace_timezone
        )

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
                    # Less eager turn-taking so it stops cutting the caller off:
                    # wait ~0.7s of silence before responding, slightly higher threshold.
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.6,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 700,
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

            # Store initial greeting for later - triggered after event loop starts
            # to avoid race condition where audio events arrive before listener is ready
            initial_greeting = self.agent_config.get("initial_greeting")
            if initial_greeting:
                self._pending_initial_greeting = initial_greeting
                # Close the inbound-audio gate until the greeting finishes.
                self._input_gate_open = False
                self.logger.info(
                    "initial_greeting_pending",
                    greeting=initial_greeting[:50],
                )
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

    async def process_realtime_events(self) -> None:
        """Process events from OpenAI Realtime API using official SDK.

        This is the main event loop that:
        1. Receives events from OpenAI
        2. Handles tool calls by routing to internal tool handlers
        3. Sends responses back to OpenAI
        """
        if not self.connection:
            raise RuntimeError("Realtime connection not established")

        try:
            async for event in self.connection:
                try:
                    event_type = event.type

                    self.logger.debug("realtime_event_received", event_type=event_type)

                    # Handle function/tool calls
                    if event_type == "response.function_call_arguments.done":
                        await self.handle_function_call_event(event)

                    # Handle audio output
                    elif event_type == "response.audio.delta":
                        # Audio data available in event.delta
                        pass

                    # Handle transcription
                    elif event_type == "conversation.item.input_audio_transcription.completed":
                        self.observe_user_transcript(getattr(event, "transcript", ""))

                    # Handle errors
                    elif event_type == "error":
                        self.logger.error("realtime_api_error", error=event.error)

                except Exception as e:
                    self.logger.exception("event_processing_error", error=str(e))

        except Exception as e:
            self.logger.exception("realtime_event_loop_error", error=str(e))
            raise

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
            # Trigger GPT to generate a response after the function call
            await self.connection.response.create()

        self.logger.info(
            "function_call_completed",
            call_id=call_id,
            tool_name=name,
            success=result.get("success"),
            action=result.get("action"),
        )

        return result

    async def trigger_initial_greeting(self) -> bool:
        """Trigger the initial greeting if one is pending.

        This should be called AFTER the event listener has started to avoid
        race conditions where audio events arrive before the listener is ready.

        Returns:
            True if greeting was triggered, False if no greeting pending or already triggered
        """
        if not self._pending_initial_greeting or self._greeting_triggered:
            return False

        if not self.connection:
            self.logger.warning("cannot_trigger_greeting_no_connection")
            return False

        self._greeting_triggered = True
        greeting = self._pending_initial_greeting

        self.logger.info("triggering_initial_greeting", greeting=greeting[:50])

        try:
            # Clear any buffered input audio to prevent line noise from
            # triggering VAD and cancelling the greeting response
            await self.connection.input_audio_buffer.clear()

            # Standard OpenAI Realtime pattern:
            # 1. Create a conversation item with the prompt
            # 2. Call response.create() to trigger the response
            # This follows the official OpenAI examples
            await self.connection.conversation.item.create(
                item={
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"[Call connected. Say this greeting now: {greeting}]",
                        }
                    ],
                }
            )
            # Trigger response generation (no parameters needed)
            await self.connection.response.create()
            return True
        except Exception as e:
            self.logger.exception("initial_greeting_failed", error=str(e))
            return False

    def open_input_gate(self) -> None:
        """Allow caller audio through again (greeting finished, or fail-open on error)."""
        if not self._input_gate_open:
            self._input_gate_open = True
            self.logger.info("input_gate_opened")

    async def send_audio(self, audio_data: bytes) -> None:
        """Send audio input to GPT Realtime using SDK.

        Args:
            audio_data: PCM16 audio data (raw bytes)
        """
        if not self.connection:
            self.logger.error("send_audio_failed_no_connection")
            return

        # Drop caller audio while the greeting is playing so it can't cancel it.
        if not self._input_gate_open:
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
        """Flush accumulated assistant text to transcript."""
        if self._current_assistant_text.strip():
            self.add_assistant_transcript(self._current_assistant_text)
        self._current_assistant_text = ""

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
