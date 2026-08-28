"""Publish the in-repo agent prompt to the live agent, hash-guarded and reversible.

The prompt used to live only in a database row, which meant the thing that most
determines call quality had no diff, no review and no history. It now lives in
`backend/app/prompts/pulsift_booker.md` and this script is the only way it reaches
production:

    python scripts/ops/prompt_publish.py show
    python scripts/ops/prompt_publish.py diff
    python scripts/ops/prompt_publish.py publish --confirm

`publish` records the live prompt, applies the file, re-reads it, verifies the hash
and the behavioural assertions, and on ANY failure restores the exact prior text
(the rollback is itself hash-verified — see prompt_sync.replace_with_rollback).

The greeting is published alongside it, because the opening only makes sense as a
pair: the stored greeting is the bare "Hello?" the bridge speaks first, and the
prompt is what tells the agent its next turn is the whole opener.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from ops_common import AGENT_ID, OpsError, admin_request
from prompt_sync import live_prompt, normalized, replace_with_rollback, sha256

PROMPT_FILE = (
    Path(__file__).resolve().parents[2] / "backend" / "app" / "prompts" / "pulsift_booker.md"
)

# The bare first word. Kept here rather than in the prompt because the bridge speaks
# it directly (GPTRealtimeSession.send_hello), before the model takes a turn.
HELLO_LINE = "Hello?"

# Behaviours that must survive any future edit of the prompt file. Each one is a
# real incident or a ratified decision, not a style preference.
# Each entry is a BEHAVIOUR that must survive any rewrite, pinned to the words
# that carry it. When a rewrite changes the wording, move the row to the new
# words — never delete it, because the row IS the behaviour. The 2026-08-08
# rewrite cut the prompt by two thirds and this gate is what proved nothing
# load-bearing went with it.
REQUIRED = (
    "{{availability_block}}",  # the pre-loaded calendar must actually be rendered
    "answer that first",  # their words come before the agent's running order
    "Leave settled things settled",  # the anti-re-asking principle
    "That alright?",  # the opener runs to the end, agenda included
    "select_slot",  # selection gate before booking
    "Pulsift's AI assistant",  # stated in the FIRST line, every call
    "costs you nothing",  # consistent answer on the magnet
    "manufacture urgency",  # no fake scarcity — a halal standard, not a preference
    "Never deny being an AI",
    # Added 2026-08-08. Each is a fault Sami heard on a real call:
    "Never end a call in silence",  # it booked him in, then dropped the line
    "Never apologise",  # it apologised for OUR transcription lag
    "NOT all you have",  # it treated its two opening times as the whole calendar
    # Added 2026-08-09. The three tells that made it read as a machine:
    "Acknowledge only when they hand you something",  # every turn had the same shape
    "Spend their name on friction, not rhythm",  # "Got it, Sami" on a question
    "two or three of their own words",  # it read whole answers back like a menu
    "is a different question and gets your name",  # "who's this?" != "are you an AI?"
    "No second greeting",  # the second line was another opener
    # The earned-acknowledgement rule has one edge that must not be lost: a time
    # named early IS something handed to you, and answering it with only a
    # question reads as not having heard them.
    "never answer a named time with only a question",
)
# Phrases that must NOT come back: each one caused a bad call.
FORBIDDEN = (
    "the first time or the second",  # nobody talks like that
    "check_availability",  # superseded by the pre-loaded calendar
    "read it back",  # reading an email address aloud on a phone call
    # Added 2026-08-08: this instruction is what turned the caller's "San Jose"
    # into the agent's "Santa Clara County" — a normalisation the prompt asked
    # for, not a fabrication, and the reason the rule is now "THEIR words".
    "in your own words",
    # Added 2026-08-09, all three from the same finding: the prompt's own examples
    # taught the tic that its rules forbade. Every example turn opened with an
    # affirmative, so whatever the rule said, the few-shot data said otherwise.
    "start most turns with a short affirmative",  # replaced by earned acknowledgement
    "Give their answers back in THEIR words",  # read as echo-wholesale
    "the free list",  # "free" from a stranger is the telemarketer marker
    "put your hand up for",  # direct-response jargon, not speech
    "Heyy",  # a text affectation; TTS either flattens it or drags the vowel
)
# NOT forbidden, deliberately: "let me capture that" and "then we'll continue".
# The prompt names those phrases in order to BAN them, and a substring check
# cannot tell an instruction from a prohibition. The eval rig's
# check_no_narrated_tool_calls catches the behaviour itself, which is the thing
# that matters.


def file_prompt() -> str:
    if not PROMPT_FILE.exists():
        raise OpsError(f"prompt file is missing: {PROMPT_FILE}")
    return normalized(PROMPT_FILE.read_text(encoding="utf-8"))


def assert_prompt(text: str) -> None:
    # Match on the WORDS, not the line layout. A required phrase that happened to
    # straddle a line wrap failed this gate three times running (2026-08-21 twice,
    # 2026-08-28), each time sending someone to re-wrap a paragraph to please a
    # substring search. The gate cares whether the behaviour is in the prompt; how
    # the markdown is folded is none of its business.
    lowered = " ".join(text.lower().split())
    for phrase in REQUIRED:
        if " ".join(phrase.lower().split()) not in lowered:
            raise OpsError(f"prompt is missing required behaviour: {phrase}")
    for phrase in FORBIDDEN:
        if " ".join(phrase.lower().split()) in lowered:
            raise OpsError(f"prompt contains a forbidden phrase: {phrase}")


def show() -> int:
    candidate = file_prompt()
    assert_prompt(candidate)
    live = live_prompt()
    print(f"file_sha={sha256(candidate)} file_len={len(candidate)}")
    print(f"live_sha={sha256(live)} live_len={len(normalized(live))}")
    print(f"in_sync={sha256(candidate) == sha256(live)}")
    return 0


def diff() -> int:
    candidate = file_prompt()
    live = normalized(live_prompt())
    lines = list(
        difflib.unified_diff(
            live.splitlines(),
            candidate.splitlines(),
            fromfile="live",
            tofile="repo",
            lineterm="",
        )
    )
    print("\n".join(lines) if lines else "identical")
    return 0


def publish(*, confirm: bool) -> int:
    candidate = file_prompt()
    assert_prompt(candidate)
    live = live_prompt()
    if sha256(candidate) == sha256(live):
        print("already in sync; nothing to publish")
        return 0
    if not confirm:
        raise OpsError("publish requires --confirm")

    replace_with_rollback(live, candidate, validate_target=False)
    reloaded = live_prompt()
    assert_prompt(reloaded)

    # The greeting is idempotent, and applied only after the prompt is safely live.
    agent = admin_request(f"/api/v1/agents/{AGENT_ID}")
    if isinstance(agent, dict) and str(agent.get("initial_greeting") or "") != HELLO_LINE:
        admin_request(
            f"/api/v1/agents/{AGENT_ID}",
            method="PUT",
            body={"initial_greeting": HELLO_LINE},
        )
        confirmed = admin_request(f"/api/v1/agents/{AGENT_ID}")
        if not isinstance(confirmed, dict) or (
            str(confirmed.get("initial_greeting") or "") != HELLO_LINE
        ):
            raise OpsError("greeting did not persist; prompt IS live, greeting is not")
        print(f"greeting={HELLO_LINE!r} applied")

    print(f"published=true sha256={sha256(reloaded)} len={len(normalized(reloaded))}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("show")
    sub.add_parser("diff")
    published = sub.add_parser("publish")
    published.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    if args.command == "show":
        return show()
    if args.command == "diff":
        return diff()
    return publish(confirm=args.confirm)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OpsError as error:
        print(f"error: {error}", file=sys.stderr)
        sys.exit(1)
