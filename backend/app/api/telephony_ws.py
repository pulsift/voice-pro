"""Telephony WebSocket endpoints for Twilio and Telnyx media streaming.

These WebSocket endpoints handle the audio streams from Twilio and Telnyx,
connecting them to our AI voice agent pipeline.
"""

import asyncio
import base64
import contextlib
import json
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.models.agent import Agent
from app.models.call_record import CallRecord
from app.models.workspace import AgentWorkspace
from app.services.gpt_realtime import GPTRealtimeSession

router = APIRouter(prefix="/ws/telephony", tags=["telephony-ws"])
logger = structlog.get_logger()

# Constants for event logging
EVENT_LOG_THRESHOLD = 20  # Log first N events, then every 100th


async def get_agent_workspace_id(agent_id: uuid.UUID, db: AsyncSession) -> uuid.UUID | None:
    """Get workspace ID for an agent."""
    result = await db.execute(
        select(AgentWorkspace.workspace_id).where(AgentWorkspace.agent_id == agent_id).limit(1)
    )
    row = result.scalar_one_or_none()
    return row


async def save_transcript_to_call_record(
    call_sid: str,
    transcript: str,
    db: AsyncSession,
    log: Any,
) -> None:
    """Save transcript to the call record.

    Args:
        call_sid: Provider call ID (CallSid for Twilio, call_control_id for Telnyx)
        transcript: Formatted transcript text
        db: Database session
        log: Logger instance
    """
    if not transcript.strip():
        log.debug("empty_transcript_skipped")
        return

    result = await db.execute(select(CallRecord).where(CallRecord.provider_call_id == call_sid))
    call_record = result.scalar_one_or_none()

    if call_record:
        call_record.transcript = transcript
        await db.commit()
        log.info("transcript_saved", record_id=str(call_record.id), length=len(transcript))
    else:
        log.warning("call_record_not_found_for_transcript", call_sid=call_sid)


@router.websocket("/twilio/{agent_id}")
async def twilio_media_stream(
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

        # Get workspace for the agent
        workspace_id = await get_agent_workspace_id(agent.id, db)

        # Build agent config
        agent_config = {
            "system_prompt": agent.system_prompt,
            "enabled_tools": agent.enabled_tools,
            "language": agent.language,
            "voice": agent.voice or "shimmer",
            "enable_transcript": agent.enable_transcript,
            "initial_greeting": agent.initial_greeting,
        }

        # Initialize GPT Realtime session
        async with GPTRealtimeSession(
            db=db,
            user_id=user_id_int,
            agent_config=agent_config,
            session_id=session_id,
            workspace_id=workspace_id,
        ) as realtime_session:
            # Handle Twilio media stream and capture call_sid
            call_sid = await _handle_twilio_stream(
                websocket=websocket,
                realtime_session=realtime_session,
                log=log,
                enable_transcript=agent.enable_transcript,
            )

            # Save transcript to call record if enabled
            if agent.enable_transcript and call_sid:
                transcript = realtime_session.get_transcript()
                await save_transcript_to_call_record(call_sid, transcript, db, log)

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
) -> str:
    """Handle Twilio Media Stream messages.

    Args:
        websocket: WebSocket connection from Twilio
        realtime_session: GPT Realtime session
        log: Logger instance
        enable_transcript: Whether to capture transcript

    Returns:
        The call_sid for transcript saving
    """
    stream_sid = ""
    call_sid = ""
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

        try:
            if not realtime_session.connection:
                log.error("no_realtime_connection")
                return

            log.info("realtime_to_twilio_started", waiting_for_events=True)
            event_count = 0
            pending_end_call = False  # True when end_call requested but waiting for AI to finish
            greeting_triggered = False  # Track if we've triggered the greeting

            async for event in realtime_session.connection:
                event_type = event.type
                event_count += 1

                # Log all events for debugging
                if event_count <= EVENT_LOG_THRESHOLD or event_count % 100 == 0:
                    log.info("realtime_event_received", event_type=event_type, count=event_count)

                # Trigger initial greeting after session is configured
                # This avoids race condition where audio events arrive before listener is ready
                if event_type == "session.updated" and not greeting_triggered:
                    greeting_triggered = True
                    triggered = await realtime_session.trigger_initial_greeting()
                    if triggered:
                        log.info("initial_greeting_triggered_after_session_update")

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
                elif (
                    enable_transcript
                    and event_type == "conversation.item.input_audio_transcription.completed"
                ):
                    # User speech transcription
                    if hasattr(event, "transcript") and event.transcript:
                        realtime_session.add_user_transcript(event.transcript)
                        log.debug("user_transcript_captured", length=len(event.transcript))

                elif enable_transcript and event_type in ("response.audio_transcript.delta", "response.output_audio_transcript.delta"):
                    # Assistant speech transcript delta
                    if hasattr(event, "delta") and event.delta:
                        realtime_session.accumulate_assistant_text(event.delta)

                elif enable_transcript and event_type in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
                    # Assistant speech transcript complete
                    realtime_session.flush_assistant_text()

                # Handle response completion - check if we should end the call
                elif event_type == "response.done":
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

                # Log other events
                elif event_type in [
                    "response.audio.done",
                    "response.output_audio.done",
                    "input_audio_buffer.speech_started",
                    "input_audio_buffer.speech_stopped",
                ]:
                    log.debug("realtime_event", event_type=event_type)

        except Exception as e:
            log.exception("realtime_to_twilio_error", error=str(e))

    # Run both directions concurrently with timeout to prevent hung tasks
    try:
        await asyncio.wait_for(
            asyncio.gather(
                twilio_to_realtime(),
                realtime_to_twilio(),
                return_exceptions=True,
            ),
            timeout=300.0,  # 5 minute max call duration before forced cleanup
        )
    except TimeoutError:
        log.warning("twilio_bridge_timeout", message="Call exceeded max duration, forcing cleanup")

    # Close WebSocket to hang up the call if end_call was triggered
    if should_end_call:
        log.info("closing_websocket_for_end_call")
        with contextlib.suppress(Exception):
            await websocket.close(code=1000, reason="Call ended by agent")

    return call_sid


@router.websocket("/telnyx/{agent_id}")
async def telnyx_media_stream(
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

        # Get workspace for the agent
        workspace_id = await get_agent_workspace_id(agent.id, db)

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
                agent_config["initial_greeting"], call_variables
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
            # Handle Telnyx media stream and capture call_control_id
            call_control_id = await _handle_telnyx_stream(
                websocket=websocket,
                realtime_session=realtime_session,
                log=log,
                enable_transcript=agent.enable_transcript,
            )

            # Save transcript to call record if enabled
            if agent.enable_transcript and call_control_id:
                transcript = realtime_session.get_transcript()
                await save_transcript_to_call_record(call_control_id, transcript, db, log)

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
) -> str:
    """Handle Telnyx Media Stream messages.

    Args:
        websocket: WebSocket connection from Telnyx
        realtime_session: GPT Realtime session
        log: Logger instance
        enable_transcript: Whether to capture transcript

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

    async def realtime_to_telnyx() -> None:  # noqa: PLR0912
        """Forward audio from GPT Realtime to Telnyx."""
        nonlocal should_end_call

        try:
            if not realtime_session.connection:
                log.error("no_realtime_connection")
                return

            pending_end_call = False  # True when end_call requested but waiting for AI to finish
            greeting_triggered = False  # Track if we've triggered the greeting

            async for event in realtime_session.connection:
                event_type = event.type

                # Trigger initial greeting after session is configured
                # This avoids race condition where audio events arrive before listener is ready
                if event_type == "session.updated" and not greeting_triggered:
                    greeting_triggered = True
                    triggered = await realtime_session.trigger_initial_greeting()
                    if triggered:
                        log.info("initial_greeting_triggered_after_session_update")

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
                elif (
                    enable_transcript
                    and event_type == "conversation.item.input_audio_transcription.completed"
                ):
                    # User speech transcription
                    if hasattr(event, "transcript") and event.transcript:
                        realtime_session.add_user_transcript(event.transcript)
                        log.debug("user_transcript_captured", length=len(event.transcript))

                elif enable_transcript and event_type in ("response.audio_transcript.delta", "response.output_audio_transcript.delta"):
                    # Assistant speech transcript delta
                    if hasattr(event, "delta") and event.delta:
                        realtime_session.accumulate_assistant_text(event.delta)

                elif enable_transcript and event_type in ("response.audio_transcript.done", "response.output_audio_transcript.done"):
                    # Assistant speech transcript complete
                    realtime_session.flush_assistant_text()

                # Handle response completion - check if we should end the call
                elif event_type == "response.done":
                    log.debug("realtime_event", event_type=event_type)
                    if pending_end_call:
                        log.info("ending_call_after_response_complete")
                        should_end_call = True
                        break

                elif event_type in [
                    "response.audio.done",
                    "response.output_audio.done",
                    "input_audio_buffer.speech_started",
                    "input_audio_buffer.speech_stopped",
                ]:
                    log.debug("realtime_event", event_type=event_type)

        except Exception as e:
            log.exception("realtime_to_telnyx_error", error=str(e))

    # Run both directions concurrently with timeout to prevent hung tasks
    try:
        await asyncio.wait_for(
            asyncio.gather(
                telnyx_to_realtime(),
                realtime_to_telnyx(),
                return_exceptions=True,
            ),
            timeout=300.0,  # 5 minute max call duration before forced cleanup
        )
    except TimeoutError:
        log.warning("telnyx_bridge_timeout", message="Call exceeded max duration, forcing cleanup")

    # Close WebSocket to hang up the call if end_call was triggered
    if should_end_call:
        log.info("closing_websocket_for_end_call")
        with contextlib.suppress(Exception):
            await websocket.close(code=1000, reason="Call ended by agent")

    return call_control_id
