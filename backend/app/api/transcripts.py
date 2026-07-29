"""Public, unauthenticated transcript share pages + the 30-day retention sweep (B2).

A call transcript is worth nothing sitting in a database nobody opens. This router
turns each one into a link that can be dropped into Slack and opened on a phone,
logged out, in one tap: GET /api/public/transcripts/{share_token}.

The token IS the capability — a 16-char base62 secret minted per call when the
transcript is first saved. No auth, no Origin check, no enumeration surface (the
call's UUID never appears in the URL). Unknown or expired token => a plain 404
page, which is also what an expired link looks like: the retention sweep nulls the
transcript, the token and the recording URL together, so a leaked link goes dead
with the data it pointed at.

Retention: a daily background loop (started from main.py's lifespan, mirroring the
campaign worker) nulls those three columns on every call that ended more than
TRANSCRIPT_RETENTION_DAYS ago. The call record itself — timing, outcome, booking —
is kept forever; only the content that identifies a person is dropped.
"""

import asyncio
import contextlib
import html
import re
from datetime import UTC, datetime, timedelta
from typing import Final

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import AsyncSessionLocal, get_db
from app.models.call_record import CallRecord

router = APIRouter(prefix="/api/public/transcripts", tags=["public-transcripts"])
logger = structlog.get_logger()

# The public path this router serves; call_events builds share URLs from it.
TRANSCRIPT_PATH_PREFIX: Final = "/api/public/transcripts"

# Daily tick. The sweep is idempotent, so an extra run costs one cheap UPDATE.
RETENTION_INTERVAL_SECONDS: Final = 24 * 60 * 60

_SECONDS_PER_MINUTE: Final = 60
_PHONE_SUFFIX_LENGTH: Final = 4

# The stored transcript is flat text: "[User]: ..." / "[Assistant]: ..." paragraphs.
_SPEAKER_RE: Final = re.compile(r"^\[(user|assistant)\]\s*:\s*(.*)$", re.IGNORECASE)

_ACCENT: Final = "#ff5e00"
_FONT_STACK: Final = (
    '"Segoe UI", -apple-system, BlinkMacSystemFont, Roboto, "Helvetica Neue", Arial, sans-serif'
)

_PAGE_CSS: Final = f"""
*, *::before, *::after {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 0; background: #ffffff; color: #1a1a1a;
  font-family: {_FONT_STACK}; font-size: 16px; line-height: 1.5;
  -webkit-text-size-adjust: 100%;
}}
.wrap {{ max-width: 720px; margin: 0 auto; padding: 24px 16px 64px; }}
header {{ border-bottom: 3px solid {_ACCENT}; padding-bottom: 16px; margin-bottom: 24px; }}
h1 {{ font-size: 20px; margin: 0 0 10px; font-weight: 600; letter-spacing: -0.01em; }}
.meta {{ display: flex; flex-wrap: wrap; gap: 8px 20px; font-size: 14px; color: #5c5c5c; }}
.meta b {{ color: #1a1a1a; font-weight: 600; }}
.turn {{ display: flex; margin-bottom: 14px; }}
.turn.user {{ justify-content: flex-start; }}
.turn.assistant {{ justify-content: flex-end; }}
.turn.note {{ justify-content: center; }}
.bubble {{
  max-width: 82%; padding: 10px 14px; border-radius: 14px;
  white-space: pre-wrap; overflow-wrap: anywhere;
}}
.user .bubble {{ background: #f2f3f5; border-bottom-left-radius: 4px; }}
.assistant .bubble {{
  background: {_ACCENT}; color: #ffffff; border-bottom-right-radius: 4px;
}}
.note .bubble {{ background: #ffffff; border: 1px solid #e5e5e5; color: #5c5c5c; }}
.who {{
  display: block; font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
  opacity: 0.7; margin-bottom: 3px; font-weight: 600;
}}
.empty {{ color: #5c5c5c; font-style: italic; }}
footer {{ margin-top: 32px; font-size: 12px; color: #8a8a8a; text-align: center; }}
@media (max-width: 480px) {{
  .wrap {{ padding: 16px 12px 48px; }}
  .bubble {{ max-width: 90%; }}
}}
"""


def _page(title: str, body: str) -> str:
    """Wrap page body in the shared self-contained shell (inline CSS, no assets)."""
    return (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex, nofollow">'
        f"<title>{html.escape(title)}</title>"
        f"<style>{_PAGE_CSS}</style>"
        f'</head><body><div class="wrap">{body}</div></body></html>'
    )


def parse_transcript(raw: str | None) -> list[tuple[str, str]]:
    """Parse flat "[User]: ..." transcript text into (role, text) turns.

    Defensive by design: unlabelled lines before the first marker (or from a future
    format change) survive as neutral "note" turns instead of vanishing, and
    continuation lines are appended to the turn they belong to.
    """
    turns: list[tuple[str, str]] = []
    role: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if buffer:
            text = "\n".join(buffer).strip()
            if text:
                turns.append((role or "note", text))
        buffer.clear()

    for line in (raw or "").splitlines():
        match = _SPEAKER_RE.match(line.strip())
        if match:
            flush()
            role = match.group(1).lower()
            buffer.append(match.group(2))
        elif line.strip():
            buffer.append(line)
    flush()
    return turns


def _format_duration(seconds: int | None) -> str:
    total = max(int(seconds or 0), 0)
    return f"{total // _SECONDS_PER_MINUTE}m {total % _SECONDS_PER_MINUTE}s"


def _mask_number(number: str | None) -> str:
    digits = re.sub(r"\D", "", number or "")
    if len(digits) < _PHONE_SUFFIX_LENGTH:
        return "unknown"
    return f"•••• {digits[-_PHONE_SUFFIX_LENGTH:]}"


def _format_when(moment: datetime | None) -> str:
    if not moment:
        return "unknown"
    when = moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return when.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC")


def render_not_found() -> str:
    """The page a bad or expired link gets. Says nothing about why."""
    return _page(
        "Transcript unavailable",
        "<header><h1>Transcript unavailable</h1></header>"
        "<p class='empty'>This link is not valid, or the transcript has been deleted "
        "under the retention policy.</p>",
    )


def render_transcript_page(record: CallRecord) -> str:
    """Render one call's transcript as a self-contained chat-bubble page."""
    turns = parse_transcript(record.transcript)
    if turns:
        labels = {"user": "Caller", "assistant": "Agent", "note": "Note"}
        bubbles = "".join(
            f'<div class="turn {role}"><div class="bubble">'
            f'<span class="who">{html.escape(labels.get(role, "Note"))}</span>'
            f"{html.escape(text)}</div></div>"
            for role, text in turns
        )
    else:
        bubbles = "<p class='empty'>No conversation was captured on this call.</p>"

    meta = (
        f"<span><b>{html.escape(_format_when(record.started_at))}</b></span>"
        f"<span>Duration <b>{html.escape(_format_duration(record.duration_seconds))}</b></span>"
        f"<span>Number <b>{html.escape(_mask_number(record.to_number))}</b></span>"
    )
    return _page(
        "Call transcript",
        f'<header><h1>Call transcript</h1><div class="meta">{meta}</div></header>'
        f"{bubbles}"
        f"<footer>This link expires {settings.TRANSCRIPT_RETENTION_DAYS} days "
        "after the call.</footer>",
    )


@router.get("/{share_token}", response_class=HTMLResponse, include_in_schema=False)
async def public_transcript_page(
    share_token: str,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Serve one call transcript as a public read-only page. The token is the key."""
    result = await db.execute(select(CallRecord).where(CallRecord.share_token == share_token))
    record = result.scalar_one_or_none()

    if record is None:
        logger.info("public_transcript_not_found")
        return HTMLResponse(render_not_found(), status_code=404)

    logger.info("public_transcript_served", call_id=str(record.id))
    return HTMLResponse(render_transcript_page(record))


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------


async def purge_expired_transcripts(db: AsyncSession) -> int:
    """Null transcript, share token and recording URL on calls past retention.

    One UPDATE, no row loading. The call record itself is kept — this drops only
    the content that identifies a person. Returns the number of rows scrubbed.
    """
    cutoff = datetime.now(UTC) - timedelta(days=settings.TRANSCRIPT_RETENTION_DAYS)
    result = await db.execute(
        update(CallRecord)
        .where(
            CallRecord.ended_at.is_not(None),
            CallRecord.ended_at < cutoff,
            or_(
                CallRecord.transcript.is_not(None),
                CallRecord.share_token.is_not(None),
                CallRecord.recording_url.is_not(None),
            ),
        )
        .values(transcript=None, share_token=None, recording_url=None)
    )
    await db.commit()
    return int(result.rowcount or 0)


class TranscriptRetentionWorker:
    """Daily background sweep that enforces the transcript retention window."""

    def __init__(self) -> None:
        self.running = False
        self._task: asyncio.Task[None] | None = None
        self.logger = logger.bind(component="transcript_retention")

    async def start(self) -> None:
        """Start the retention loop (no-op if already running)."""
        if self.running:
            self.logger.warning("transcript_retention_already_running")
            return
        self.running = True
        self._task = asyncio.create_task(self._run_loop())
        self.logger.info(
            "transcript_retention_started",
            retention_days=settings.TRANSCRIPT_RETENTION_DAYS,
        )

    async def stop(self) -> None:
        """Stop the retention loop."""
        self.running = False
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self.logger.info("transcript_retention_stopped")

    async def _run_loop(self) -> None:
        while self.running:
            try:
                async with AsyncSessionLocal() as db:
                    purged = await purge_expired_transcripts(db)
                if purged:
                    self.logger.info("transcripts_purged", count=purged)
            except Exception:
                # A failed sweep must never take the app down; retry next tick.
                self.logger.exception("transcript_retention_sweep_failed")
            await asyncio.sleep(RETENTION_INTERVAL_SECONDS)


_retention_worker: TranscriptRetentionWorker | None = None


async def start_transcript_retention_worker() -> TranscriptRetentionWorker:
    """Start the global retention worker (called from the app lifespan)."""
    global _retention_worker
    if _retention_worker is None:
        _retention_worker = TranscriptRetentionWorker()
        await _retention_worker.start()
    return _retention_worker


async def stop_transcript_retention_worker() -> None:
    """Stop the global retention worker."""
    global _retention_worker
    if _retention_worker:
        await _retention_worker.stop()
        _retention_worker = None
