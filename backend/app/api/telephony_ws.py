"""Telephony WebSocket endpoints for Twilio and Telnyx media streaming.

These WebSocket endpoints handle the audio streams from Twilio and Telnyx,
connecting them to our AI voice agent pipeline.
"""

import asyncio
import base64
import contextlib
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import user_id_to_uuid
from app.core.config import settings
from app.db.session import get_db
from app.models.agent import Agent
from app.models.call_record import CallDirection, CallRecord, CallStatus
from app.models.workspace import AgentWorkspace
from app.services.call_events import FALLBACK_DELAY_SECONDS, schedule_call_ended_event
from app.services.gpt_realtime import GPTRealtimeSession

router = APIRouter(prefix="/ws/telephony", tags=["telephony-ws"])
logger = structlog.get_logger()

# Constants for event logging
EVENT_LOG_THRESHOLD = 20  # Log first N events, then every 100th

# Twilio sends connected + start immediately after the stream opens; if start never
# arrives the call is dead — don't hold the socket (and a DB session) open forever.
TWILIO_START_EVENT_TIMEOUT_SECONDS = 15.0

_TERMINAL_CALL_STATUSES = {
    CallStatus.COMPLETED.value,
    CallStatus.FAILED.value,
    CallStatus.BUSY.value,
    CallStatus.NO_ANSWER.value,
    CallStatus.CANCELED.value,
}


async def get_agent_workspace_id(agent_id: uuid.UUID, db: AsyncSession) -> uuid.UUID | None:
    """Get workspace ID for an agent."""
    result = await db.execute(select(AgentWorkspace).where(AgentWorkspace.agent_id == agent_id))
    memberships = result.scalars().all()
    if len(memberships) == 1:
        return memberships[0].workspace_id
    defaults = [membership.workspace_id for membership in memberships if membership.is_default]
    return defaults[0] if len(defaults) == 1 else None


async def resolve_media_workspace_id(
    agent_id: uuid.UUID,
    requested_workspace_id: str | None,
    db: AsyncSession,
) -> uuid.UUID | None:
    """Validate an explicit outbound workspace, or resolve one unambiguous fallback."""
    if not requested_workspace_id:
        result = await db.execute(select(AgentWorkspace).where(AgentWorkspace.agent_id == agent_id))
        memberships = result.scalars().all()
        if len(memberships) <= 1:
            return memberships[0].workspace_id if memberships else None
        defaults = [membership.workspace_id for membership in memberships if membership.is_default]
        if len(defaults) == 1:
            return defaults[0]
        raise ValueError("Media workspace is ambiguous")
    try:
        workspace_id = uuid.UUID(requested_workspace_id)
    except ValueError as exc:
        raise ValueError("Invalid media workspace ID") from exc
    result = await db.execute(
        select(AgentWorkspace.id).where(
            AgentWorkspace.agent_id == agent_id,
            AgentWorkspace.workspace_id == workspace_id,
        )
    )
    if result.scalar_one_or_none() is None:
        raise ValueError("Agent does not belong to media workspace")
    return workspace_id


async def update_telnyx_media_lifecycle(
    call_control_id: str,
    db: AsyncSession,
    log: Any,
    *,
    agent_id: uuid.UUID,
    owner_user_id: uuid.UUID,
    workspace_id: uuid.UUID | None,
    expected_to_number: str | None,
    ended: bool,
) -> None:
    """Use the signed-in media stream as a lifecycle fallback for TeXML callbacks."""
    exact = await db.execute(
        select(CallRecord)
        .where(
            CallRecord.provider == "telnyx",
            CallRecord.provider_call_id == call_control_id,
            CallRecord.user_id == owner_user_id,
            CallRecord.workspace_id == workspace_id,
        )
        .limit(2)
        .with_for_update()
    )
    candidates = exact.scalars().all()

    # TeXML creates a CallSid while Media Streams exposes call_control_id. If those
    # differ, accept only one recent, still-open call in the same identity scope.
    if not candidates:
        filters = [
            CallRecord.provider == "telnyx",
            CallRecord.agent_id == agent_id,
            CallRecord.user_id == owner_user_id,
            CallRecord.workspace_id == workspace_id,
            CallRecord.created_at >= datetime.now(UTC) - timedelta(minutes=20),
            CallRecord.ended_at.is_(None),
        ]
        if expected_to_number:
            filters.append(CallRecord.to_number == expected_to_number)
        fallback = await db.execute(
            select(CallRecord)
            .where(*filters)
            .order_by(CallRecord.created_at.desc())
            .limit(2)
            .with_for_update()
        )
        candidates = fallback.scalars().all()

    if len(candidates) != 1:
        log.warning(
            "telnyx_media_lifecycle_record_not_found_or_ambiguous",
            call_control_id=call_control_id,
            candidate_count=len(candidates),
            ended=ended,
        )
        return

    call_record = candidates[0]
    now = datetime.now(UTC)
    if ended:
        if not call_record.ended_at:
            call_record.ended_at = now
        if call_record.status not in _TERMINAL_CALL_STATUSES:
            call_record.status = CallStatus.COMPLETED.value
        if call_record.answered_at:
            elapsed = (call_record.ended_at - call_record.answered_at).total_seconds()
            call_record.duration_seconds = max(call_record.duration_seconds or 0, int(elapsed), 0)
    else:
        if not call_record.answered_at:
            call_record.answered_at = now
        if call_record.status not in _TERMINAL_CALL_STATUSES:
            call_record.status = CallStatus.IN_PROGRESS.value

    await db.commit()
    log.info(
        "telnyx_media_lifecycle_updated",
        record_id=str(call_record.id),
        status=call_record.status,
        ended=ended,
    )


async def save_transcript_to_call_record(  # noqa: PLR0912
    call_sid: str,
    transcript: str,
    db: AsyncSession,
    log: Any,
    agent_id: str | None = None,
    booking_attempts: list[dict[str, Any]] | None = None,
    owner_user_id: uuid.UUID | None = None,
    workspace_id: uuid.UUID | None = None,
    provider: str | None = None,
    expected_to_number: str | None = None,
) -> CallRecord | None:
    """Save transcript and sanitized booking diagnostics to the call record.

    Args:
        call_sid: Provider call ID (CallSid for Twilio, call_control_id for Telnyx)
        transcript: Formatted transcript text
        db: Database session
        log: Logger instance
        agent_id: Agent UUID (for the fallback match below)
        booking_attempts: Sanitized Cal.com attempt details for post-mortems
        owner_user_id: Owning user UUID required to scope fallback matching
        workspace_id: Workspace UUID required to scope fallback matching
        provider: Telephony provider required to scope fallback matching
        expected_to_number: Destination number, when known, for fallback matching

    Returns:
        The matched call record (artifacts now merged), or None when no
        unambiguous record was found.
    """
    if not transcript.strip() and booking_attempts is None:
        log.debug("empty_call_artifacts_skipped")
        return None

    call_record: CallRecord | None = None
    exact_match_ambiguous = False
    if owner_user_id and provider:
        exact = await db.execute(
            select(CallRecord)
            .where(
                CallRecord.provider_call_id == call_sid,
                CallRecord.provider == provider,
                CallRecord.user_id == owner_user_id,
                CallRecord.workspace_id == workspace_id,
            )
            .limit(2)
            .with_for_update()
        )
        exact_candidates = exact.scalars().all()
        if len(exact_candidates) == 1:
            call_record = exact_candidates[0]
        elif len(exact_candidates) > 1:
            exact_match_ambiguous = True
            log.warning(
                "call_record_exact_match_ambiguous",
                call_sid=call_sid,
                candidate_count=len(exact_candidates),
            )
    else:
        log.warning("call_record_scope_incomplete", call_sid=call_sid)

    # A media-stream ID can differ from the stored call-leg ID. Fall back only when
    # every stable identity dimension is available and exactly one fresh record
    # matches. Existing artifacts are merged below; never guess between concurrent calls.
    if not call_record and not exact_match_ambiguous and agent_id and owner_user_id and provider:
        from datetime import UTC, datetime, timedelta

        cutoff = datetime.now(UTC) - timedelta(minutes=20)
        filters = [
            CallRecord.agent_id == uuid.UUID(agent_id),
            CallRecord.user_id == owner_user_id,
            CallRecord.workspace_id == workspace_id,
            CallRecord.provider == provider,
            CallRecord.created_at >= cutoff,
        ]
        if expected_to_number:
            filters.append(CallRecord.to_number == expected_to_number)
        fb = await db.execute(
            select(CallRecord)
            .where(*filters)
            .order_by(CallRecord.created_at.desc())
            .limit(2)
            .with_for_update()
        )
        candidates = fb.scalars().all()
        if len(candidates) == 1:
            call_record = candidates[0]
            log.info("transcript_fallback_matched", record_id=str(call_record.id))
        elif len(candidates) > 1:
            log.warning("call_record_fallback_ambiguous", candidate_count=len(candidates))
        else:
            log.warning("call_record_fallback_not_found")

    if call_record:
        changed = False
        existing_transcript = (call_record.transcript or "").strip()
        incoming_transcript = transcript.strip()
        if incoming_transcript and len(incoming_transcript) > len(existing_transcript):
            call_record.transcript = transcript
            changed = True
        if booking_attempts is not None:
            existing_attempts = list(call_record.booking_attempts or [])
            merged_attempts = [dict(attempt) for attempt in existing_attempts]
            for attempt in booking_attempts:
                if attempt not in merged_attempts:
                    merged_attempts.append(dict(attempt))
            if call_record.booking_attempts != merged_attempts:
                call_record.booking_attempts = merged_attempts
                changed = True
        if changed:
            await db.commit()
        log.info(
            "call_artifacts_saved",
            record_id=str(call_record.id),
            transcript_length=len(call_record.transcript or ""),
            booking_attempt_count=len(call_record.booking_attempts or []),
            changed=changed,
        )
    else:
        log.warning("call_record_not_found_for_artifacts", call_sid=call_sid)
    return call_record


async def _run_bridge_tasks(
    provider_to_realtime: Any,
    realtime_to_provider: Any,
    log: Any,
    provider: str,
    timeout_seconds: float = 300.0,
) -> None:
    """Stop both bridge directions as soon as either direction terminates."""
    tasks = {
        asyncio.create_task(provider_to_realtime()),
        asyncio.create_task(realtime_to_provider()),
    }
    try:
        done, pending = await asyncio.wait(
            tasks,
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            timeout_event = {
                "twilio": "twilio_bridge_timeout",
                "telnyx": "telnyx_bridge_timeout",
            }.get(provider, "telephony_bridge_timeout")
            log.warning(
                timeout_event,
                message="Call exceeded max duration, forcing cleanup",
            )
        for task in pending:
            task.cancel()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@router.websocket("/twilio/{agent_id}")
async def twilio_media_stream(  # noqa: PLR0912, PLR0915
    websocket: WebSocket,
    agent_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """WebSocket endpoint for Twilio Media Streams.

    Twilio sends audio via Media Streams in mulaw format at 8kHz.
    This endpoint bridges that audio to our GPT Realtime session.

    Message format from Twilio:
    - {"event": "connected", "protocol": "Call", "version": "1.0.0"}
    - {"event": "start", "start": {"streamSid": "...", "callSid": "..."}}
    - {"event": "media", "media": {"payload": "base64_audio"}}
    - {"event": "stop"}
    """
    session_id = str(uuid.uuid4())
    log = logger.bind(
        endpoint="twilio_media_stream",
        agent_id=agent_id,
        session_id=session_id,
    )

    await websocket.accept()
    log.info("twilio_websocket_connected")

    stream_sid: str = ""
    call_sid: str = ""

    try:
        # Twilio strips query strings from <Stream> URLs, so per-call context (cv,
        # workspace_id) arrives as TwiML <Parameter> values inside the start event's
        # customParameters. Consume frames up to and including start BEFORE building
        # the session; media frames buffer in the socket meanwhile (same as before,
        # when session setup also preceded the read loop).
        custom_params: dict[str, str] = {}
        while True:
            pre_start_raw = await asyncio.wait_for(
                websocket.receive_text(), timeout=TWILIO_START_EVENT_TIMEOUT_SECONDS
            )
            pre_start = json.loads(pre_start_raw)
            pre_event = pre_start.get("event", "")
            if pre_event == "start":
                start_data = pre_start.get("start", {})
                stream_sid = start_data.get("streamSid", "")
                call_sid = start_data.get("callSid", "")
                raw_params = start_data.get("customParameters") or {}
                if isinstance(raw_params, dict):
                    custom_params = {str(k): str(v) for k, v in raw_params.items()}
                log.info(
                    "twilio_stream_started",
                    stream_sid=stream_sid,
                    call_sid=call_sid,
                    custom_param_keys=list(custom_params.keys()),
                )
                break
            if pre_event == "connected":
                log.info("twilio_stream_connected")
            elif pre_event == "stop":
                log.info("twilio_stream_stopped_before_start")
                return
            # anything else pre-start (unexpected) is ignored

        # Load agent configuration
        result = await db.execute(select(Agent).where(Agent.id == uuid.UUID(agent_id)))
        agent = result.scalar_one_or_none()

        if not agent:
            log.error("agent_not_found")
            await websocket.close(code=4004, reason="Agent not found")
            return

        if not agent.is_active:
            log.error("agent_not_active")
            await websocket.close(code=4003, reason="Agent is not active")
            return

        log.info("agent_loaded", agent_name=agent.name)

        # agent.user_id is now directly the integer user ID
        user_id_int = agent.user_id

        # Outbound answer webhooks carry the authoritative workspace selected by
        # initiate_call. Inbound/legacy streams may fall back only when unambiguous.
        try:
            workspace_id = await resolve_media_workspace_id(
                agent.id,
                custom_params.get("workspace_id") or websocket.query_params.get("workspace_id"),
                db,
            )
        except ValueError as exc:
            log.warning("invalid_media_workspace", error=str(exc))
            await websocket.close(code=4003, reason="Invalid workspace")
            return

        # Build agent config
        agent_config = {
            "system_prompt": agent.system_prompt,
            "enabled_tools": agent.enabled_tools,
            "language": agent.language,
            "voice": agent.voice or "shimmer",
            "enable_transcript": agent.enable_transcript,
            "initial_greeting": agent.initial_greeting,
        }

        # Per-call lead/offer variables (base64 JSON) — personalize the prompt + fill
        # the Cal.com booking attendee. Primary channel: start-event customParameters
        # (<Parameter> survives Twilio's query-string stripping); query param kept as
        # fallback for inbound/legacy streams. Telnyx keeps its query-param path.
        call_variables: dict[str, Any] = {}
        cv = custom_params.get("cv") or websocket.query_params.get("cv")
        if cv:
            try:
                padded = cv + "=" * (-len(cv) % 4)  # tolerate unpadded base64url
                decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
                if isinstance(decoded, dict):
                    call_variables = decoded
                    log.info("call_variables_loaded", keys=list(call_variables.keys()))
                else:
                    log.warning("call_variables_not_dict", got=type(decoded).__name__)
            except Exception as e:
                log.warning("call_variables_decode_failed", error=str(e))

        # Always render the greeting (defaults fill any {{placeholders}} so none leak raw).
        if agent_config.get("initial_greeting"):
            from app.services.gpt_realtime import render_template

            agent_config["initial_greeting"] = render_template(
                str(agent_config["initial_greeting"]), call_variables
            )

        # Initialize GPT Realtime session
        async with GPTRealtimeSession(
            db=db,
            user_id=user_id_int,
            agent_config=agent_config,
            session_id=session_id,
            workspace_id=workspace_id,
            variables=call_variables,
        ) as realtime_session:
            # Handle Twilio media stream (start already consumed above — seed its ids)
            call_sid = await _handle_twilio_stream(
                websocket=websocket,
                realtime_session=realtime_session,
                log=log,
                enable_transcript=agent.enable_transcript,
                stream_sid=stream_sid,
                call_sid=call_sid,
            )

            # Persist booking diagnostics on every call; transcript text remains opt-in.
            if call_sid:
                transcript = realtime_session.get_transcript() if agent.enable_transcript else ""
                call_record = await save_transcript_to_call_record(
                    call_sid,
                    transcript,
                    db,
                    log,
                    agent_id=agent_id,
                    booking_attempts=realtime_session.get_booking_attempts(),
                    owner_user_id=user_id_to_uuid(agent.user_id),
                    workspace_id=workspace_id,
                    provider="twilio",
                )
                # B4 fallback: if the terminal status callback never arrives, still
                # emit one call-ended event after a grace period (the callback path
                # wins the single-shot guard when it does arrive).
                if call_record is not None and call_record.direction == (
                    CallDirection.OUTBOUND.value
                ):
                    schedule_call_ended_event(call_record, delay_seconds=FALLBACK_DELAY_SECONDS)

    except WebSocketDisconnect:
        log.info("twilio_websocket_disconnected")
    except Exception as e:
        log.exception("twilio_websocket_error", error=str(e))
    finally:
        log.info("twilio_websocket_closed", stream_sid=stream_sid, call_sid=call_sid)


async def _handle_twilio_stream(  # noqa: PLR0915
    websocket: WebSocket,
    realtime_session: GPTRealtimeSession,
    log: Any,
    enable_transcript: bool = False,
    stream_sid: str = "",
    call_sid: str = "",
) -> str:
    """Handle Twilio Media Stream messages.

    Args:
        websocket: WebSocket connection from Twilio
        realtime_session: GPT Realtime session
        log: Logger instance
        enable_transcript: Whether to capture transcript
        stream_sid: Stream SID when the start event was already consumed by the caller
        call_sid: Call SID when the start event was already consumed by the caller

    Returns:
        The call_sid for transcript saving
    """
    should_end_call = False  # Flag to signal call should end

    async def twilio_to_realtime() -> None:
        """Forward audio from Twilio to GPT Realtime."""
        nonlocal stream_sid, call_sid, should_end_call

        try:
            while not should_end_call:
                message = await websocket.receive_text()
                data = json.loads(message)
                event = data.get("event", "")

                if event == "connected":
                    log.info("twilio_stream_connected")

                elif event == "start":
                    start_data = data.get("start", {})
                    stream_sid = start_data.get("streamSid", "")
                    call_sid = start_data.get("callSid", "")
                    log.info(
                        "twilio_stream_started",
                        stream_sid=stream_sid,
                        call_sid=call_sid,
                    )

                elif event == "media":
                    # Decode base64 mulaw audio and forward to Realtime
                    media = data.get("media", {})
                    payload = media.get("payload", "")
                    if payload:
                        audio_bytes = base64.b64decode(payload)
                        await realtime_session.send_audio(audio_bytes)

                elif event == "stop":
                    log.info("twilio_stream_stopped")
                    break

                elif event == "mark":
                    # Mark events indicate playback position
                    log.debug("twilio_mark_event", name=data.get("mark", {}).get("name"))

        except WebSocketDisconnect:
            log.info("twilio_to_realtime_disconnected")
        except Exception as e:
            log.exception("twilio_to_realtime_error", error=str(e))

    async def realtime_to_twilio() -> None:  # noqa: PLR0912, PLR0915
        """Forward audio from GPT Realtime to Twilio."""
        nonlocal should_end_call
        greeting_fallback_task: asyncio.Task[None] | None = None

        try:
            if not realtime_session.connection:
                log.error("no_realtime_connection")
                return

            log.info("realtime_to_twilio_started", waiting_for_events=True)
            event_count = 0
            pending_end_call = False  # True when end_call requested but waiting for AI to finish

            async def _greeting_fallback() -> None:
                # Callee-speaks-first: only greet if the answerer stays silent.
                await asyncio.sleep(settings.REALTIME_GREETING_FALLBACK_SECONDS)
                if await realtime_session.trigger_initial_greeting():
                    log.info("initial_greeting_triggered_by_silence_fallback")

            async for event in realtime_session.connection:
                event_type = event.type
                event_count += 1

                # Log all events for debugging
                if event_count <= EVENT_LOG_THRESHOLD or event_count % 100 == 0:
                    log.info("realtime_event_received", event_type=event_type, count=event_count)

                # Callee-speaks-first: arm the silent-answerer fallback once the
                # session is configured; the caller's own speech disarms it.
                if event_type == "session.updated" and greeting_fallback_task is None:
                    greeting_fallback_task = asyncio.create_task(_greeting_fallback())
                    log.info("greeting_fallback_armed")

                elif event_type == "input_audio_buffer.speech_started":
                    if realtime_session.consume_pending_greeting():
                        log.info("caller_spoke_first_prompt_greeting_active")
                    if greeting_fallback_task and not greeting_fallback_task.done():
                        greeting_fallback_task.cancel()

                # Handle audio output (GA: response.output_audio.delta; beta: response.audio.delta)
                elif event_type in ("response.audio.delta", "response.output_audio.delta"):
                    # Get audio delta and send to Twilio
                    # Check various possible attribute names for the audio data
                    delta_data = getattr(event, "delta", None)
                    if not delta_data:
                        # Log event attributes for debugging
                        log.warning(
                            "audio_delta_missing",
                            event_attrs=dir(event),
                            has_delta=hasattr(event, "delta"),
                        )
                        continue

                    try:
                        audio_bytes = base64.b64decode(delta_data)
                        # Encode for Twilio (already in g711_ulaw format now)
                        payload = base64.b64encode(audio_bytes).decode("utf-8")
                        log.info(
                            "sending_audio_to_twilio",
                            audio_size=len(audio_bytes),
                            stream_sid=stream_sid,
                        )
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "event": "media",
                                    "streamSid": stream_sid,
                                    "media": {"payload": payload},
                                }
                            )
                        )
                    except Exception as audio_err:
                        log.exception("audio_send_error", error=str(audio_err))

                # Handle tool calls
                elif event_type == "response.function_call_arguments.done":
                    log.info(
                        "handling_function_call",
                        call_id=event.call_id,
                        name=event.name,
                    )
                    result = await realtime_session.handle_function_call_event(event)
                    # Check if this is an end_call action
                    if result.get("action") == "end_call":
                        log.info("end_call_action_received", reason=result.get("reason"))
                        pending_end_call = True

                # Capture transcript events
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    # Always expose the completed caller turn to booking state. The
                    # session separately applies the transcript-history toggle.
                    if hasattr(event, "transcript") and event.transcript:
                        realtime_session.observe_user_transcript(event.transcript)
                        log.debug("user_utterance_observed", length=len(event.transcript))

                elif enable_transcript and event_type in (
                    "response.audio_transcript.delta",
                    "response.output_audio_transcript.delta",
                ):
                    # Assistant speech transcript delta
                    if hasattr(event, "delta") and event.delta:
                        realtime_session.accumulate_assistant_text(event.delta)

                elif enable_transcript and event_type in (
                    "response.audio_transcript.done",
                    "response.output_audio_transcript.done",
                ):
                    # Assistant speech transcript complete
                    realtime_session.flush_assistant_text()

                # Handle response completion - check if we should end the call
                elif event_type == "response.done":
                    # First response.done is the greeting's — reopen the inbound gate.
                    realtime_session.open_input_gate()
                    # Log full response details for debugging
                    response_data = getattr(event, "response", None)
                    if response_data:
                        status = getattr(response_data, "status", "unknown")
                        status_details = getattr(response_data, "status_details", None)
                        output = getattr(response_data, "output", [])
                        output_count = len(output) if output else 0
                        log.info(
                            "response_done_details",
                            status=status,
                            status_details=str(status_details) if status_details else None,
                            output_count=output_count,
                        )
                    else:
                        log.debug("realtime_event", event_type=event_type)
                    if pending_end_call:
                        log.info("ending_call_after_response_complete")
                        should_end_call = True
                        break

                # Surface Realtime API errors (previously silent) and fail-open the gate.
                elif event_type == "error":
                    log.warning("realtime_api_error", error=str(getattr(event, "error", event)))
                    realtime_session.open_input_gate()

                # Log other events
                elif event_type in [
                    "response.audio.done",
                    "response.output_audio.done",
                    "input_audio_buffer.speech_stopped",
                ]:
                    log.debug("realtime_event", event_type=event_type)

        except Exception as e:
            log.exception("realtime_to_twilio_error", error=str(e))
        finally:
            if greeting_fallback_task and not greeting_fallback_task.done():
                greeting_fallback_task.cancel()

    await _run_bridge_tasks(
        twilio_to_realtime,
        realtime_to_twilio,
        log,
        "twilio",
    )

    # Close WebSocket to hang up the call if end_call was triggered
    if should_end_call:
        log.info("closing_websocket_for_end_call")
        with contextlib.suppress(Exception):
            await websocket.close(code=1000, reason="Call ended by agent")

    return call_sid


@router.websocket("/telnyx/{agent_id}")
async def telnyx_media_stream(  # noqa: PLR0912, PLR0915
    websocket: WebSocket,
    agent_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """WebSocket endpoint for Telnyx Media Streams.

    Telnyx sends audio via Media Streams in PCMU format at 8kHz.
    This endpoint bridges that audio to our GPT Realtime session.

    Message format from Telnyx:
    - {"event": "start", "stream_id": "...", "call_control_id": "..."}
    - {"event": "media", "media": {"payload": "base64_audio"}}
    - {"event": "stop"}
    """
    session_id = str(uuid.uuid4())
    log = logger.bind(
        endpoint="telnyx_media_stream",
        agent_id=agent_id,
        session_id=session_id,
    )

    await websocket.accept()
    log.info("telnyx_websocket_connected")

    stream_id: str = ""
    call_control_id: str = ""

    try:
        # Load agent configuration
        result = await db.execute(select(Agent).where(Agent.id == uuid.UUID(agent_id)))
        agent = result.scalar_one_or_none()

        if not agent:
            log.error("agent_not_found")
            await websocket.close(code=4004, reason="Agent not found")
            return

        if not agent.is_active:
            log.error("agent_not_active")
            await websocket.close(code=4003, reason="Agent is not active")
            return

        log.info("agent_loaded", agent_name=agent.name)

        # agent.user_id is now directly the integer user ID
        user_id_int = agent.user_id

        # Outbound answer webhooks carry the authoritative workspace selected by
        # initiate_call. Inbound/legacy streams may fall back only when unambiguous.
        try:
            workspace_id = await resolve_media_workspace_id(
                agent.id,
                websocket.query_params.get("workspace_id"),
                db,
            )
        except ValueError as exc:
            log.warning("invalid_media_workspace", error=str(exc))
            await websocket.close(code=4003, reason="Invalid workspace")
            return

        # Build agent config
        agent_config = {
            "system_prompt": agent.system_prompt,
            "enabled_tools": agent.enabled_tools,
            "language": agent.language,
            "voice": agent.voice or "shimmer",
            "enable_transcript": agent.enable_transcript,
            "initial_greeting": agent.initial_greeting,
        }

        # Per-call lead/offer variables, passed through the stream URL as base64 JSON in ?cv=
        # (used to personalize the prompt + fill the Cal.com booking attendee).
        call_variables: dict[str, Any] = {}
        cv = websocket.query_params.get("cv")
        if cv:
            try:
                padded = cv + "=" * (-len(cv) % 4)  # tolerate unpadded base64url
                decoded = json.loads(base64.urlsafe_b64decode(padded.encode()).decode("utf-8"))
                if isinstance(decoded, dict):
                    call_variables = decoded
                    log.info("call_variables_loaded", keys=list(call_variables.keys()))
                else:
                    log.warning("call_variables_not_dict", got=type(decoded).__name__)
            except Exception as e:
                log.warning("call_variables_decode_failed", error=str(e))

        # Always render the greeting (defaults fill any {{placeholders}} so none leak raw).
        if agent_config.get("initial_greeting"):
            from app.services.gpt_realtime import render_template

            agent_config["initial_greeting"] = render_template(
                str(agent_config["initial_greeting"]), call_variables
            )

        # Initialize GPT Realtime session
        async with GPTRealtimeSession(
            db=db,
            user_id=user_id_int,
            agent_config=agent_config,
            session_id=session_id,
            workspace_id=workspace_id,
            variables=call_variables,
        ) as realtime_session:
            owner_user_id = user_id_to_uuid(agent.user_id)
            expected_to_number = (
                str(call_variables.get("leadPhone") or call_variables.get("phone") or "") or None
            )

            async def update_media_lifecycle_safely(
                lifecycle_call_control_id: str, *, ended: bool
            ) -> None:
                try:
                    await update_telnyx_media_lifecycle(
                        lifecycle_call_control_id,
                        db,
                        log,
                        agent_id=agent.id,
                        owner_user_id=owner_user_id,
                        workspace_id=workspace_id,
                        expected_to_number=expected_to_number,
                        ended=ended,
                    )
                except Exception as exc:
                    await db.rollback()
                    log.exception(
                        "telnyx_media_lifecycle_update_failed",
                        ended=ended,
                        error=str(exc),
                    )

            async def on_stream_started(started_call_control_id: str) -> None:
                await update_media_lifecycle_safely(started_call_control_id, ended=False)

            # Handle Telnyx media stream and capture call_control_id
            call_control_id = await _handle_telnyx_stream(
                websocket=websocket,
                realtime_session=realtime_session,
                log=log,
                enable_transcript=agent.enable_transcript,
                on_stream_started=on_stream_started,
            )

            # Persist booking diagnostics on every call; transcript text remains opt-in.
            if call_control_id:
                await update_media_lifecycle_safely(call_control_id, ended=True)
                transcript = realtime_session.get_transcript() if agent.enable_transcript else ""
                call_record = await save_transcript_to_call_record(
                    call_control_id,
                    transcript,
                    db,
                    log,
                    agent_id=agent_id,
                    booking_attempts=realtime_session.get_booking_attempts(),
                    owner_user_id=owner_user_id,
                    workspace_id=workspace_id,
                    provider="telnyx",
                    expected_to_number=expected_to_number,
                )
                # B4 fallback: if the terminal status callback never arrives, still
                # emit one call-ended event after a grace period (the callback path
                # wins the single-shot guard when it does arrive).
                if call_record is not None and call_record.direction == (
                    CallDirection.OUTBOUND.value
                ):
                    schedule_call_ended_event(call_record, delay_seconds=FALLBACK_DELAY_SECONDS)

    except WebSocketDisconnect:
        log.info("telnyx_websocket_disconnected")
    except Exception as e:
        log.exception("telnyx_websocket_error", error=str(e))
    finally:
        log.info("telnyx_websocket_closed", stream_id=stream_id, call_control_id=call_control_id)


async def _handle_telnyx_stream(  # noqa: PLR0915
    websocket: WebSocket,
    realtime_session: GPTRealtimeSession,
    log: Any,
    enable_transcript: bool = False,
    on_stream_started: Callable[[str], Awaitable[None]] | None = None,
) -> str:
    """Handle Telnyx Media Stream messages.

    Args:
        websocket: WebSocket connection from Telnyx
        realtime_session: GPT Realtime session
        log: Logger instance
        enable_transcript: Whether to capture transcript
        on_stream_started: Optional lifecycle callback invoked once the call ID is known

    Returns:
        The call_control_id for transcript saving
    """
    stream_id = ""
    call_control_id = ""
    should_end_call = False  # Flag to signal call should end

    async def telnyx_to_realtime() -> None:
        """Forward audio from Telnyx to GPT Realtime."""
        nonlocal stream_id, call_control_id, should_end_call

        try:
            while not should_end_call:
                message = await websocket.receive_text()
                data = json.loads(message)
                event = data.get("event", "")

                if event == "start":
                    stream_id = data.get("stream_id", "")
                    start_data = data.get("start", {})
                    call_control_id = start_data.get("call_control_id", "")
                    log.info(
                        "telnyx_stream_started",
                        stream_id=stream_id,
                        call_control_id=call_control_id,
                    )
                    if call_control_id and on_stream_started:
                        await on_stream_started(call_control_id)

                elif event == "media":
                    # Decode base64 PCMU audio and forward to Realtime
                    media = data.get("media", {})
                    payload = media.get("payload", "")
                    if payload:
                        audio_bytes = base64.b64decode(payload)
                        await realtime_session.send_audio(audio_bytes)

                elif event == "stop":
                    log.info("telnyx_stream_stopped")
                    break

        except WebSocketDisconnect:
            log.info("telnyx_to_realtime_disconnected")
        except Exception as e:
            log.exception("telnyx_to_realtime_error", error=str(e))

    async def realtime_to_telnyx() -> None:  # noqa: PLR0912, PLR0915
        """Forward audio from GPT Realtime to Telnyx."""
        nonlocal should_end_call
        greeting_fallback_task: asyncio.Task[None] | None = None

        try:
            if not realtime_session.connection:
                log.error("no_realtime_connection")
                return

            pending_end_call = False  # True when end_call requested but waiting for AI to finish

            async def _greeting_fallback() -> None:
                # Callee-speaks-first: only greet if the answerer stays silent.
                await asyncio.sleep(settings.REALTIME_GREETING_FALLBACK_SECONDS)
                if await realtime_session.trigger_initial_greeting():
                    log.info("initial_greeting_triggered_by_silence_fallback")

            async for event in realtime_session.connection:
                event_type = event.type

                # Callee-speaks-first: arm the silent-answerer fallback once the
                # session is configured; the caller's own speech disarms it.
                if event_type == "session.updated" and greeting_fallback_task is None:
                    greeting_fallback_task = asyncio.create_task(_greeting_fallback())
                    log.info("greeting_fallback_armed")

                elif event_type == "input_audio_buffer.speech_started":
                    if realtime_session.consume_pending_greeting():
                        log.info("caller_spoke_first_prompt_greeting_active")
                    if greeting_fallback_task and not greeting_fallback_task.done():
                        greeting_fallback_task.cancel()

                # Handle audio output (GA: response.output_audio.delta; beta: response.audio.delta)
                elif event_type in ("response.audio.delta", "response.output_audio.delta"):
                    if hasattr(event, "delta") and event.delta:
                        # event.delta is already base64 G.711 mu-law; forward as a
                        # Telnyx client media frame ({event, media:{payload}} — no stream_id).
                        await websocket.send_text(
                            json.dumps(
                                {
                                    "event": "media",
                                    "media": {"payload": event.delta},
                                }
                            )
                        )

                # Handle tool calls
                elif event_type == "response.function_call_arguments.done":
                    log.info(
                        "handling_function_call",
                        call_id=event.call_id,
                        name=event.name,
                    )
                    result = await realtime_session.handle_function_call_event(event)
                    # Check if this is an end_call action
                    if result.get("action") == "end_call":
                        log.info("end_call_action_received", reason=result.get("reason"))
                        pending_end_call = True

                # Capture transcript events
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    # Always expose the completed caller turn to booking state. The
                    # session separately applies the transcript-history toggle.
                    if hasattr(event, "transcript") and event.transcript:
                        realtime_session.observe_user_transcript(event.transcript)
                        log.debug("user_utterance_observed", length=len(event.transcript))

                elif enable_transcript and event_type in (
                    "response.audio_transcript.delta",
                    "response.output_audio_transcript.delta",
                ):
                    # Assistant speech transcript delta
                    if hasattr(event, "delta") and event.delta:
                        realtime_session.accumulate_assistant_text(event.delta)

                elif enable_transcript and event_type in (
                    "response.audio_transcript.done",
                    "response.output_audio_transcript.done",
                ):
                    # Assistant speech transcript complete
                    realtime_session.flush_assistant_text()

                # Handle response completion - check if we should end the call
                elif event_type == "response.done":
                    # First response.done is the greeting's — reopen the inbound gate
                    # so the caller is heard from here on. Idempotent after the first.
                    realtime_session.open_input_gate()
                    log.debug("realtime_event", event_type=event_type)
                    if pending_end_call:
                        log.info("ending_call_after_response_complete")
                        should_end_call = True
                        break

                # Surface Realtime API errors (previously silent) and fail-open the
                # gate so an errored greeting can never deadlock the caller's audio.
                elif event_type == "error":
                    log.warning("realtime_api_error", error=str(getattr(event, "error", event)))
                    realtime_session.open_input_gate()

                elif event_type in [
                    "response.audio.done",
                    "response.output_audio.done",
                    "input_audio_buffer.speech_stopped",
                ]:
                    log.debug("realtime_event", event_type=event_type)

        except Exception as e:
            log.exception("realtime_to_telnyx_error", error=str(e))
        finally:
            if greeting_fallback_task and not greeting_fallback_task.done():
                greeting_fallback_task.cancel()

    await _run_bridge_tasks(
        telnyx_to_realtime,
        realtime_to_telnyx,
        log,
        "telnyx",
    )

    # Close WebSocket to hang up the call if end_call was triggered
    if should_end_call:
        log.info("closing_websocket_for_end_call")
        with contextlib.suppress(Exception):
            await websocket.close(code=1000, reason="Call ended by agent")

    return call_control_id
