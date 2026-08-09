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
from app.core.public_id import SHARE_TOKEN_LENGTH, generate_public_id
from app.db.session import get_db
from app.models.agent import Agent
from app.models.call_record import CallRecord, CallStatus
from app.models.workspace import AgentWorkspace
from app.services.amd import MACHINE_VERDICTS, classify_greeting
from app.services.call_events import stage_media_finalized_call_event
from app.services.gpt_realtime import GPTRealtimeSession
from app.services.telephony.media_grant import consume_twilio_media_grant
from app.services.tools.crm_tools import wait_for_calendar_writes

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

# Length of the base62 secret behind a public transcript link (B2).


def _response_id(event: Any) -> str | None:
    """The id of the response an event belongs to, when the payload carries one."""
    direct = getattr(event, "response_id", None)
    if direct:
        return str(direct)
    response = getattr(event, "response", None)
    identifier = getattr(response, "id", None) if response is not None else None
    return str(identifier) if identifier else None


# How many silent responses we will sit through after `end_call` before hanging up
# anyway. The line is billed and the caller is waiting, so this is small — it
# exists to survive one empty response, not to wait out a model that has decided
# to say nothing.
MAX_SILENT_RESPONSES_BEFORE_HANGUP = 2


def _response_spoke(event: Any) -> bool:
    """Did the response that just finished actually put words on the line?

    A response carrying only function calls is silent. So is an empty one — and
    an empty one is exactly what arrived 180ms after `end_call` on 2026-08-08.
    It satisfied a guard that was counting RESPONSES, so the line dropped on a
    caller who had just been booked in and never told. Counting responses is not
    the same as knowing they heard something, so this counts speech instead.
    """
    response = getattr(event, "response", None)
    output = getattr(response, "output", None) if response is not None else None
    for item in output or []:
        if str(getattr(item, "type", "") or "") != "message":
            continue
        for part in getattr(item, "content", None) or []:
            if str(getattr(part, "transcript", "") or "").strip():
                return True
            if str(getattr(part, "text", "") or "").strip():
                return True
            if str(getattr(part, "type", "") or "") in ("audio", "output_audio"):
                return True
    return False


class OpeningSequence:
    """Hello-first opening, and the watchdog that keeps a silent line from hanging.

    Sami's design, in his words: "Make him say 'hello' first, just 'hello' and then
    either the receiver hears it and responds and the VA proceeds with his opener /
    or it kicks off too early and they dont hear it and in that case the VA is quiet
    after he said 'hello' and hes waiting for them to speak first."

    So the whole opening is three facts:
      1. On answer, one word: the hello. Forced, identical every call.
      2. Then silence. The caller's first sound is what triggers the opener — which
         is the same code path whether they heard the hello or not.
      3. The opener, once it starts, is PROTECTED: caller audio is held (not
         dropped) until it finishes, so it always reaches "caught you at an okay
         time?" instead of dying on a "yeah?".

    Plus the case a phone call has and a chat does not: nobody there at all. One
    "can you hear me?", then hang up, rather than holding a paid line open.
    """

    def __init__(
        self,
        session: GPTRealtimeSession,
        log: Any,
        *,
        on_giveup: Callable[[], None],
    ) -> None:
        self._session = session
        self._log = log
        self._on_giveup = on_giveup
        self._silence_task: asyncio.Task[None] | None = None
        self._hold_task: asyncio.Task[None] | None = None
        self._fallback_task: asyncio.Task[None] | None = None
        self.started = False

    async def start(self) -> None:
        """Say the hello and arm the dead-air watchdog (idempotent)."""
        if self.started:
            return
        self.started = True
        await self._session.send_hello()
        self._silence_task = asyncio.create_task(self._watch_silence())

    def arm_fallback_start(self) -> None:
        """Start the opening anyway if `session.updated` never arrives.

        The whole opening — hello AND the dead-air hangup — normally hangs off that
        one event. If the session config is rejected the server sends `error`
        instead, and without this the callee would hear pure silence and we would
        hold the (billed) leg open until the 300-second bridge timeout. Late is
        better than never: worst case the hello is spoken against a default session.
        """
        self._fallback_task = asyncio.create_task(self._fallback_start())

    async def _fallback_start(self) -> None:
        await asyncio.sleep(settings.REALTIME_SESSION_READY_TIMEOUT_SECONDS)
        if not self.started:
            self._log.warning("session_updated_never_arrived_starting_opening_anyway")
            await self.start()

    async def _watch_silence(self) -> None:
        await asyncio.sleep(settings.REALTIME_POST_HELLO_NUDGE_SECONDS)
        if self._session.caller_has_spoken:
            return
        await self._session.send_presence_check()
        await asyncio.sleep(settings.REALTIME_POST_HELLO_GIVEUP_SECONDS)
        if self._session.caller_has_spoken:
            return
        self._log.info("dead_air_after_hello_hanging_up")
        self._on_giveup()

    def caller_spoke(self) -> None:
        """The far end made a sound — disarm the watchdog for good."""
        self._session.note_caller_spoke()
        if self._silence_task and not self._silence_task.done():
            self._silence_task.cancel()

    def response_created(self, response_id: str | None = None) -> None:
        """Note the assistant turn; arm the hold ceiling if a hold just engaged.

        Re-armable: a turn that carried only a function call does not consume the
        opener's protection, so the next turn can be held and needs its own ceiling.
        """
        self._session.note_response_created(response_id)
        if self._session.input_held and (self._hold_task is None or self._hold_task.done()):
            self._hold_task = asyncio.create_task(self._hold_ceiling())

    async def _hold_ceiling(self) -> None:
        # Belt and braces: if the opener's response.done never arrives (error,
        # disconnect, model stall), the caller must still be heard.
        #
        # It releases WITHOUT flushing, deliberately. The protected response may
        # still be generating, and appending audio is exactly what makes the server
        # cancel it — so flushing here would let the protection cut off the very
        # sentence it exists to protect. Live audio flows normally from this moment,
        # so the caller can still interrupt for real; only the stale buffer is
        # dropped.
        await asyncio.sleep(settings.REALTIME_OPENER_HOLD_MAX_SECONDS)
        await self._session.release_input_hold(reason="hold_ceiling", flush=False)

    async def response_finished(self, response_id: str | None = None) -> None:
        """A response completed — release the hold if it was THIS response's.

        Pass no id to release unconditionally (an error, or teardown), which is the
        fail-open direction: a caller must never be left unheard.
        """
        await self._session.release_input_hold(
            reason="response_complete", response_id=response_id
        )

    def cancel(self) -> None:
        for task in (self._silence_task, self._hold_task, self._fallback_task):
            if task and not task.done():
                task.cancel()


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


def _merge_call_artifacts(
    call_record: CallRecord,
    transcript: str,
    booking_attempts: list[dict[str, Any]] | None,
    amd_verdict: str | None,
    fit_answers: dict[str, Any] | None = None,
) -> bool:
    """Merge call artifacts onto the record in place; return whether anything changed.

    Also mints the transcript's share token (B2): a transcript with no share link
    is a transcript nobody reads. Minted once and never rotated — the link stays
    stable until the retention sweep nulls it along with the transcript itself.
    """
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
        # An empty incoming list on a record that never had attempts is NOT a
        # change — writing [] over NULL would dirty the row (and trigger a
        # commit) on every call that made no booking attempt.
        if merged_attempts != existing_attempts or (
            merged_attempts and call_record.booking_attempts != merged_attempts
        ):
            call_record.booking_attempts = merged_attempts
            changed = True

    if amd_verdict or fit_answers:
        # Rebind (never mutate) so SQLAlchemy sees the JSON column change.
        existing_variables = call_record.variables if isinstance(call_record.variables, dict) else {}
        updated_variables = dict(existing_variables)
        variables_changed = False
        if amd_verdict and existing_variables.get("amd") != amd_verdict:
            updated_variables["amd"] = amd_verdict
            variables_changed = True
        # fit_answers rides the same call-ended payload (it just echoes
        # record.variables) — this is what lets a call that never reaches
        # book_appointment still hand the team something real.
        if fit_answers and existing_variables.get("fit_answers") != fit_answers:
            updated_variables["fit_answers"] = fit_answers
            variables_changed = True
        if variables_changed:
            call_record.variables = updated_variables
            changed = True

    if (call_record.transcript or "").strip() and not call_record.share_token:
        call_record.share_token = generate_public_id(prefix="tr", length=SHARE_TOKEN_LENGTH)
        changed = True

    return changed


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
    amd_verdict: str | None = None,
    media_finalized: bool = False,
    fit_answers: dict[str, Any] | None = None,
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
        amd_verdict: Answering-machine verdict for this call (C2), stored under
            variables["amd"] so downstream consumers can see it was a machine
        media_finalized: Whether this save is the terminal media teardown
        fit_answers: ICP fit answers captured independent of booking, stored
            under variables["fit_answers"] so a call that never reaches
            book_appointment still hands the team something real

    Returns:
        The matched call record (artifacts now merged), or None when no
        unambiguous record was found.
    """
    if (
        not transcript.strip()
        and booking_attempts is None
        and not media_finalized
        and not fit_answers
    ):
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
        changed = _merge_call_artifacts(
            call_record, transcript, booking_attempts, amd_verdict, fit_answers
        )
        if media_finalized:
            await stage_media_finalized_call_event(db, call_record, observed_at=datetime.now(UTC))
        if changed or media_finalized:
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
        # Twilio strips query strings from <Stream> URLs, so the single-use grant and
        # bound call context arrive in the start event's customParameters. Consume
        # frames up to and including start BEFORE authorizing or building
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
                if not isinstance(raw_params, dict) or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in raw_params.items()
                ):
                    log.warning("twilio_media_parameters_invalid")
                    await websocket.close(code=4003, reason="Invalid media grant")
                    return
                custom_params = dict(raw_params)
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

        call_record = await consume_twilio_media_grant(
            db=db,
            token=custom_params.get("media_grant", ""),
            call_sid=call_sid,
            agent_id=agent_id,
            workspace_id=custom_params.get("workspace_id", ""),
            cv=custom_params.get("cv", ""),
        )
        if call_record is None:
            log.warning("twilio_media_grant_rejected", call_sid=call_sid)
            await websocket.close(code=4003, reason="Invalid media grant")
            return

        # Load agent configuration
        result = await db.execute(select(Agent).where(Agent.id == call_record.agent_id))
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

        workspace_id = call_record.workspace_id

        # Build agent config
        agent_config = {
            "system_prompt": agent.system_prompt,
            "enabled_tools": agent.enabled_tools,
            "language": agent.language,
            "voice": agent.voice or "shimmer",
            "enable_transcript": agent.enable_transcript,
            "initial_greeting": agent.initial_greeting,
        }

        # Use only canonical variables from the grant-bound call record.
        stored_variables = call_record.variables
        call_variables = dict(stored_variables) if isinstance(stored_variables, dict) else {}
        if call_variables:
            log.info("call_variables_loaded", keys=list(call_variables.keys()))

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
            amd_state: dict[str, str] = {}
            # AMD IS OUTBOUND-ONLY (Codex review blocker, 2026-07-30). Inbound
            # calls share this same bridge, and an inbound HUMAN who answers
            # with "Hi, you've reached Acme..." would be classified machine-vm
            # and hung up on mid-sentence. Our outbound dials always carry
            # per-call variables (that is how leadName reaches the greeting);
            # an inbound stream never does, so their presence is the gate.
            amd_allowed = bool(call_variables)
            if not amd_allowed:
                log.info("amd_skipped_no_call_variables_probably_inbound")
            call_sid = await _handle_twilio_stream(
                websocket=websocket,
                realtime_session=realtime_session,
                log=log,
                enable_transcript=agent.enable_transcript,
                stream_sid=stream_sid,
                call_sid=call_sid,
                amd_state=amd_state,
                amd_allowed=amd_allowed,
            )

            # The calendar write now runs behind the agent's confirmation, so give
            # it the few seconds it needs before the call record is written. It is
            # already durable and already alerts on failure — this is only the
            # difference between the booking id landing ON the record and landing
            # just after it.
            await wait_for_calendar_writes()

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
                    amd_verdict=amd_state.get("verdict"),
                    media_finalized=True,
                    fit_answers=realtime_session.get_fit_answers(),
                )

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
    amd_state: dict[str, str] | None = None,
    amd_allowed: bool = False,
) -> str:
    """Handle Twilio Media Stream messages.

    Args:
        websocket: WebSocket connection from Twilio
        realtime_session: GPT Realtime session
        log: Logger instance
        enable_transcript: Whether to capture transcript
        stream_sid: Stream SID when the start event was already consumed by the caller
        call_sid: Call SID when the start event was already consumed by the caller
        amd_state: Optional dict the answering-machine verdict is written into
            (key "verdict") so the caller can persist it after teardown
        amd_allowed: Whether this call is eligible for answering-machine
            detection. OUTBOUND ONLY — hanging up on an inbound human who
            answers "you've reached Acme" is the failure mode this gate exists
            to prevent. Defaults False: a caller must opt a call in.

    Returns:
        The call_sid for transcript saving
    """
    should_end_call = False  # Flag to signal call should end
    amd_result: dict[str, str] = amd_state if amd_state is not None else {}

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
        amd_task: asyncio.Task[None] | None = None

        def _giveup() -> None:
            nonlocal should_end_call
            should_end_call = True

        opening = OpeningSequence(realtime_session, log, on_giveup=_giveup)
        availability_task: asyncio.Task[bool] | None = None

        try:
            if not realtime_session.connection:
                log.error("no_realtime_connection")
                return

            log.info("realtime_to_twilio_started", waiting_for_events=True)
            opening.arm_fallback_start()
            event_count = 0
            pending_end_call = False  # True when end_call requested but waiting for AI to finish
            farewell_pending = False  # the closing line comes in the response AFTER end_call
            silent_responses = 0  # responses since end_call that said nothing aloud

            async def _classify_answerer(first_utterance: str) -> None:
                # C2: the callee's first words say whether a person picked up. On a
                # machine, hang up via the SAME flag the end_call tool sets — the
                # media loop sees it on its next frame (~20ms), which ends the
                # bridge and closes the socket. Never raises: classify_greeting
                # degrades to "uncertain", which keeps the call alive.
                nonlocal should_end_call
                verdict = await classify_greeting(first_utterance)
                amd_result["verdict"] = verdict
                if verdict in MACHINE_VERDICTS:
                    log.info("amd_machine_detected", verdict=verdict)
                    should_end_call = True
                else:
                    log.info("amd_human_verdict", verdict=verdict)

            async for event in realtime_session.connection:
                event_type = event.type
                event_count += 1

                # Log all events for debugging
                if event_count <= EVENT_LOG_THRESHOLD or event_count % 100 == 0:
                    log.info("realtime_event_received", event_type=event_type, count=event_count)

                # The session is live: say hello, and start pulling the calendar in
                # the background so the agent HAS the open times without ever
                # spending a conversational turn asking for them.
                if event_type == "session.updated" and not opening.started:
                    await opening.start()
                    availability_task = asyncio.create_task(
                        realtime_session.load_availability()
                    )

                elif event_type == "response.created":
                    opening.response_created(_response_id(event))

                elif event_type == "input_audio_buffer.speech_started":
                    opening.caller_spoke()
                    # BARGE-IN FLUSH (phase 2, 2026-07-29): when the caller
                    # starts speaking, OpenAI cancels its response server-side
                    # but every audio chunk already pushed into Twilio's playout
                    # buffer KEEPS PLAYING — the agent talks over the caller for
                    # the length of the buffer. Twilio's "clear" message flushes
                    # it (LiveKit's bridge does the equivalent; ours never did).
                    # Harmless no-op when nothing is buffered.
                    if stream_sid:
                        try:
                            await websocket.send_text(json.dumps(
                                {"event": "clear", "streamSid": stream_sid}))
                            log.info("twilio_buffer_cleared_on_barge_in",
                                     stream_sid=stream_sid)
                        except Exception as clear_err:
                            log.warning("twilio_clear_failed", error=str(clear_err))

                # Handle audio output (GA: response.output_audio.delta; beta: response.audio.delta)
                elif event_type in ("response.audio.delta", "response.output_audio.delta"):
                    # This turn is actually SPEAKING, which is how the opener is told
                    # apart from a turn that only carried a function call.
                    realtime_session.note_assistant_audio()
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
                        # Handling the call already asked for one more response,
                        # and that is where the goodbye lives now that tools are
                        # called before the agent speaks. Hanging up on the
                        # response that merely CARRIED end_call would end the
                        # call on silence.
                        farewell_pending = True

                # Capture transcript events
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    # Always expose the completed caller turn to booking state. The
                    # session separately applies the transcript-history toggle.
                    if hasattr(event, "transcript") and event.transcript:
                        realtime_session.observe_user_transcript(event.transcript)
                        log.debug("user_utterance_observed", length=len(event.transcript))
                        # C2: only the FIRST completed utterance is the AMD signal.
                        # Classified in the background so it never stalls the audio.
                        if settings.AMD_ENABLED and amd_allowed and amd_task is None:
                            amd_task = asyncio.create_task(_classify_answerer(event.transcript))

                # Always accumulated: the booking state needs what the agent said in
                # order to read the caller's reply in context. flush_assistant_text
                # applies the transcript-persistence toggle itself.
                elif event_type in (
                    "response.audio_transcript.delta",
                    "response.output_audio_transcript.delta",
                ):
                    # Assistant speech transcript delta
                    if hasattr(event, "delta") and event.delta:
                        realtime_session.accumulate_assistant_text(event.delta)

                elif event_type in (
                    "response.audio_transcript.done",
                    "response.output_audio_transcript.done",
                ):
                    # Assistant speech transcript complete
                    realtime_session.flush_assistant_text()

                # Handle response completion - check if we should end the call
                elif event_type == "response.done":
                    # Release any caller audio held while this response played
                    # (the opener). Held audio is forwarded, never discarded.
                    await opening.response_finished(_response_id(event))
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
                        if farewell_pending and not _response_spoke(event):
                            silent_responses += 1
                            if silent_responses <= MAX_SILENT_RESPONSES_BEFORE_HANGUP:
                                log.info(
                                    "waiting_for_farewell_before_hangup",
                                    silent_responses=silent_responses,
                                )
                                continue
                            log.warning(
                                "hanging_up_without_a_spoken_goodbye",
                                silent_responses=silent_responses,
                            )
                        log.info("ending_call_after_response_complete")
                        should_end_call = True
                        break

                # Surface Realtime API errors (previously silent) and fail-open both
                # the gate and the opener hold — an errored turn must never leave
                # the caller unheard.
                elif event_type == "error":
                    log.warning("realtime_api_error", error=str(getattr(event, "error", event)))
                    await opening.response_finished()

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
            opening.cancel()
            for pending_task in (amd_task, availability_task):
                if pending_task and not pending_task.done():
                    pending_task.cancel()

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
async def telnyx_media_stream(  # noqa: PLR0915
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

            # The calendar write now runs behind the agent's confirmation, so give
            # it the few seconds it needs before the call record is written. It is
            # already durable and already alerts on failure — this is only the
            # difference between the booking id landing ON the record and landing
            # just after it.
            await wait_for_calendar_writes()

            # Persist booking diagnostics on every call; transcript text remains opt-in.
            if call_control_id:
                await update_media_lifecycle_safely(call_control_id, ended=True)
                transcript = realtime_session.get_transcript() if agent.enable_transcript else ""
                await save_transcript_to_call_record(
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
                    media_finalized=True,
                    fit_answers=realtime_session.get_fit_answers(),
                )

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

        def _giveup() -> None:
            nonlocal should_end_call
            should_end_call = True

        opening = OpeningSequence(realtime_session, log, on_giveup=_giveup)
        availability_task: asyncio.Task[bool] | None = None

        try:
            if not realtime_session.connection:
                log.error("no_realtime_connection")
                return

            pending_end_call = False  # True when end_call requested but waiting for AI to finish
            farewell_pending = False  # the closing line comes in the response AFTER end_call
            silent_responses = 0  # responses since end_call that said nothing aloud
            opening.arm_fallback_start()

            async for event in realtime_session.connection:
                event_type = event.type

                # Hello-first opening + background calendar pre-load (see
                # OpeningSequence and GPTRealtimeSession.load_availability).
                if event_type == "session.updated" and not opening.started:
                    await opening.start()
                    availability_task = asyncio.create_task(
                        realtime_session.load_availability()
                    )

                elif event_type == "response.created":
                    opening.response_created(_response_id(event))

                elif event_type == "input_audio_buffer.speech_started":
                    opening.caller_spoke()

                # Handle audio output (GA: response.output_audio.delta; beta: response.audio.delta)
                elif event_type in ("response.audio.delta", "response.output_audio.delta"):
                    # This turn is actually SPEAKING, which is how the opener is told
                    # apart from a turn that only carried a function call.
                    realtime_session.note_assistant_audio()
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
                        # Handling the call already asked for one more response,
                        # and that is where the goodbye lives now that tools are
                        # called before the agent speaks. Hanging up on the
                        # response that merely CARRIED end_call would end the
                        # call on silence.
                        farewell_pending = True

                # Capture transcript events
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    # Always expose the completed caller turn to booking state. The
                    # session separately applies the transcript-history toggle.
                    if hasattr(event, "transcript") and event.transcript:
                        realtime_session.observe_user_transcript(event.transcript)
                        log.debug("user_utterance_observed", length=len(event.transcript))

                # Always accumulated: the booking state needs what the agent said in
                # order to read the caller's reply in context. flush_assistant_text
                # applies the transcript-persistence toggle itself.
                elif event_type in (
                    "response.audio_transcript.delta",
                    "response.output_audio_transcript.delta",
                ):
                    # Assistant speech transcript delta
                    if hasattr(event, "delta") and event.delta:
                        realtime_session.accumulate_assistant_text(event.delta)

                elif event_type in (
                    "response.audio_transcript.done",
                    "response.output_audio_transcript.done",
                ):
                    # Assistant speech transcript complete
                    realtime_session.flush_assistant_text()

                # Handle response completion - check if we should end the call
                elif event_type == "response.done":
                    await opening.response_finished(_response_id(event))
                    log.debug("realtime_event", event_type=event_type)
                    if pending_end_call:
                        if farewell_pending and not _response_spoke(event):
                            silent_responses += 1
                            if silent_responses <= MAX_SILENT_RESPONSES_BEFORE_HANGUP:
                                log.info(
                                    "waiting_for_farewell_before_hangup",
                                    silent_responses=silent_responses,
                                )
                                continue
                            log.warning(
                                "hanging_up_without_a_spoken_goodbye",
                                silent_responses=silent_responses,
                            )
                        log.info("ending_call_after_response_complete")
                        should_end_call = True
                        break

                # Surface Realtime API errors (previously silent) and fail-open the
                # gate + opener hold so an errored turn can never deadlock the
                # caller's audio.
                elif event_type == "error":
                    log.warning("realtime_api_error", error=str(getattr(event, "error", event)))
                    await opening.response_finished()

                elif event_type in [
                    "response.audio.done",
                    "response.output_audio.done",
                    "input_audio_buffer.speech_stopped",
                ]:
                    log.debug("realtime_event", event_type=event_type)

        except Exception as e:
            log.exception("realtime_to_telnyx_error", error=str(e))
        finally:
            opening.cancel()
            if availability_task and not availability_task.done():
                availability_task.cancel()

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
