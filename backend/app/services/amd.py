"""Answering-machine detection from the callee's first utterance (C2).

Carrier AMD listens to audio and guesses; we already have something better — the
Realtime API hands us a *transcript* of the callee's first words. Classifying that
text with a cheap chat model (the LiveKit-style recipe) is faster, cheaper and far
more accurate than beep-detection, and it costs nothing when the call is a human.

Deliberate asymmetry: the prompt is biased toward "human". A false "machine"
hangs up on a real prospect — the expensive mistake. A false "human" only wastes
a few seconds pitching a voicemail. Anything short, ambiguous or unclear is a
human. Any API error, missing key or unparseable answer degrades to "uncertain",
which the caller treats as "keep talking".
"""

from typing import Final

import structlog
from openai import AsyncOpenAI

from app.core.config import settings

logger = structlog.get_logger()

HUMAN: Final = "human"
MACHINE_VOICEMAIL: Final = "machine-vm"
MACHINE_IVR: Final = "machine-ivr"
UNCERTAIN: Final = "uncertain"

VALID_VERDICTS: Final = frozenset({HUMAN, MACHINE_VOICEMAIL, MACHINE_IVR, UNCERTAIN})
# The verdicts that mean "nobody human is on this line" — the caller hangs up.
MACHINE_VERDICTS: Final = frozenset({MACHINE_VOICEMAIL, MACHINE_IVR})

_MAX_TOKENS: Final = 5
_TIMEOUT_SECONDS: Final = 6.0

SYSTEM_PROMPT: Final = """\
You classify the FIRST thing a callee says when they pick up an OUTBOUND sales call.
Answer with exactly one label, nothing else:
human | machine-vm | machine-ivr | uncertain

machine-vm = a voicemail/answering-machine greeting. Markers: "you've reached",
"is not available", "leave a message", "after the tone/beep", "record your message",
carrier scripts ("the person you are calling", "the mailbox is full").
machine-ivr = an automated menu or auto-attendant. Markers: "press 1", "for sales,
press", "your call is important to us", "please hold", "thank you for calling X,
if you know your party's extension".
human = a live person. Real people answer short and messy: "Hello?", "Yeah?",
"This is Mike", "Hello, who's this?", "Sorry, hang on".

BIAS TOWARD human. Hanging up on a real prospect is the expensive mistake; wasting
a few seconds on a machine is cheap. If the text is short, garbled, ambiguous, or
you are not clearly sure it is a recording, answer human.
Answer uncertain only when the text is empty or pure noise.

Examples:
"Hello?" -> human
"Hi, this is Sarah speaking" -> human
"Yeah hello, who is this" -> human
"Hi, you've reached Dave. Leave a message after the tone." -> machine-vm
"The person you are trying to reach is not available. Please record your message." -> machine-vm
"Thank you for calling Acme. For sales, press 1." -> machine-ivr
"""


async def classify_greeting(transcript: str, *, api_key: str | None = None) -> str:
    """Classify a callee's opening utterance as human, voicemail, IVR or uncertain.

    Args:
        transcript: The callee's first transcribed utterance.
        api_key: OpenAI key override; defaults to the platform key in settings.

    Returns:
        One of "human", "machine-vm", "machine-ivr", "uncertain". Never raises —
        every failure path returns "uncertain" so a classifier outage can never
        hang up on a live prospect.
    """
    text = (transcript or "").strip()
    if not text:
        return UNCERTAIN

    key = api_key or settings.OPENAI_API_KEY
    if not key:
        logger.warning("amd_no_api_key")
        return UNCERTAIN

    try:
        client = AsyncOpenAI(api_key=key, timeout=_TIMEOUT_SECONDS)
        response = await client.chat.completions.create(
            model=settings.AMD_MODEL,
            temperature=0,
            max_tokens=_MAX_TOKENS,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        raw = (response.choices[0].message.content or "").strip().lower()
    except Exception as exc:
        # Timeout, auth, rate limit, malformed response - all identical downstream.
        logger.warning("amd_classification_failed", error=str(exc))
        return UNCERTAIN

    verdict = raw.strip(".\"' ")
    if verdict not in VALID_VERDICTS:
        logger.warning("amd_unexpected_verdict", raw=raw[:40])
        return UNCERTAIN
    return verdict
