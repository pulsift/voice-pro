"""Prepare, apply, and verify the reviewed Voice Pro prompt patch without storing JWTs."""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from pathlib import Path

from ops_common import AGENT_ID, OpsError, admin_request


def normalized(text: str) -> str:
    return text.replace("\r\n", "\n").rstrip("\n")


def sha256(text: str) -> str:
    return hashlib.sha256(normalized(text).encode("utf-8")).hexdigest()


EDITS = [
    (
        '- Warm, sincere, natural contractions. A little texture ("honestly", "sure thing") is human; perfectly smooth is robotic.',
        "- Warm, sincere, and direct. Use plain words. No filler, hedging, or rambling.",
    ),
    (
        '- Email on file, if any: {{leadEmail}}. Before booking you must confirm this OUT LOUD once - if it looks present, read it back ("I\'ve got your email as {{leadEmail}} - sound right?"); if it looks blank or they correct you, just ask for the right one naturally.',
        "- Email on file, if any: {{leadEmail}}. Use it silently. Ask for an email only if none is available or the lead volunteers a correction; never read an address back.",
    ),
    (
        '- check_availability: call it ONCE, right after the lead gives their timezone, passing their timezone. It returns TWO ready openings - each has a spoken "when" (already in the lead\'s timezone, inside our hours, on an upcoming weekday) and a "start". Offer the two "when"s; never invent a time. When they pick one, pass that option\'s exact "start" to book_appointment.',
        '- check_availability: call it after the lead gives their timezone. It returns up to TWO openings with a spoken "when", a "slot_id", and a "start". Offer only those openings; never invent a time.',
    ),
    (
        "- book_appointment: books it. The lead's NAME is filled in automatically - never ask for it. You MUST also pass the confirmed email and a quick icp object (see step 4f) - the tool will reject the booking and tell you what's missing if you skip either, so confirm/ask first. Pass the chosen \"start\" plus your notes. Call ONLY after the lead has clearly picked one specific time AND you've confirmed their email and captured the fit-check.",
        '- select_slot: after the lead clearly chooses one offered time, call this with that opening\'s slot_id. If it says the answer was unclear, ask "the first time or the second?" and wait for a new answer.\n- book_appointment: books only the slot that select_slot accepted. The lead\'s name and email on file are filled automatically. Pass a corrected email only if the lead volunteers one. Pass the selected "start", the icp object, and your notes.',
    ),
    (
        "   f) Once they've picked a time (before you call book_appointment): confirm their email in one short line (see WHAT YOU KNOW), then ask 2-3 quick fit questions back-to-back, casually - e.g. \"Two quick ones so the team's ready for you: what do you mainly install - rooftop, carports, storage, or financed deals? And roughly what's the smallest project size you'll take on, and which states or areas do you cover?\" Keep it brief and natural, not an interrogation - loose answers are fine, just capture what they say.",
        '   f) After select_slot accepts their choice, ask no more than these TWO short fit questions, one per turn: "What kind of solar work do you mainly take on, and what\'s the smallest project you\'ll consider?" Then: "Which states or areas do you cover?" Loose answers are fine.',
    ),
    (
        '5) CONFIRM THE PICK, THEN BOOK - only once they clearly name one of the times AND you\'ve confirmed their email and captured the fit-check (step 4f). A vague "yeah", "sure", "still there", or silence is NOT a pick; if unsure, ask "which works - the [day] or the [day]?". Then call book_appointment with that option\'s "start", the confirmed email, and the icp you captured, plus your notes - and WAIT for its result before you say ANYTHING about it being booked. If it comes back asking you to collect the email or fit-check because one was missing, just ask for it naturally and call book_appointment again - never mention the tool or an error.',
        '5) SELECT, THEN BOOK - a vague "yeah", "sure", "still there", unrelated words, or silence is NOT a pick. After a clear answer, call select_slot with that option\'s slot_id. Only after select_slot succeeds and the two fit questions are answered, call book_appointment with the selected "start", the icp, and your notes. If email is missing, ask once. WAIT for the result before saying it is booked. On slot_conflict, offer the fresh returned times and repeat the selection step; never substitute a time yourself.',
    ),
    (
        '4. Book ONLY after they clearly pick one of the two times. "Yeah" / "still there" / silence is not a pick - ask which one.',
        '4. Book ONLY after a new clear answer lets select_slot accept one offered time. "Yeah" / "still there" / unrelated words / silence are not a pick - ask "the first time or the second?".',
    ),
    (
        '7. Call book_appointment with the chosen "start" and WAIT for its result. Say "booked" / "you\'re set" ONLY after it returns success; if it errors or nothing returns, use the hiccup line. Read the time back ONCE. NEVER hang up before booking has finished.',
        '7. Call select_slot before book_appointment. Pass only its accepted start and WAIT for the booking result. Say "booked" / "you\'re set" ONLY after success; if it errors after its retry or nothing returns, use the hiccup line. Read the time back ONCE. NEVER hang up before booking has finished.',
    ),
    (
        "9. Before calling book_appointment: confirm their email out loud once, and capture the 2-3 quick fit-check answers (what they install, minimum project size, target states) - the tool needs both and will tell you if either's missing.",
        "9. Before calling book_appointment: select_slot must have accepted their clear choice, and capture the TWO fit-check answers (solar work + minimum size, then target states). Use the email on file silently unless it is missing.",
    ),
]

FORBIDDEN = (
    "confirm this OUT LOUD",
    "confirm their email",
    "confirmed email",
    "sound right?",
    "2-3 quick fit",
)
REQUIRED = (
    "select_slot",
    "Use it silently",
    "no more than these TWO short fit questions",
    "never substitute a time yourself",
    "never pressure",
    "I'm Pulsift's AI assistant",
)


def patch_prompt(source: str) -> str:
    candidate = normalized(source)
    for old, new in EDITS:
        count = candidate.count(old)
        if count != 1:
            raise OpsError(f"prompt anchor count was {count}, expected 1: {old[:60]}")
        candidate = candidate.replace(old, new, 1)
    assert_candidate(candidate)
    return candidate


def assert_candidate(candidate: str) -> None:
    lower = candidate.lower()
    for phrase in FORBIDDEN:
        if phrase.lower() in lower:
            raise OpsError(f"forbidden prompt behavior remains: {phrase}")
    for phrase in REQUIRED:
        if phrase.lower() not in lower:
            raise OpsError(f"required prompt behavior is missing: {phrase}")


def live_prompt() -> str:
    agent = admin_request(f"/api/v1/agents/{AGENT_ID}")
    if not isinstance(agent, dict):
        raise OpsError("agent GET returned an unexpected response")
    return str(agent.get("system_prompt") or "")


def require_hash(prompt: str, expected: str, label: str) -> None:
    observed = sha256(prompt)
    if observed != expected:
        raise OpsError(
            f"{label} hash mismatch: expected={expected} observed={observed}"
        )


def backup(source: str, candidate: str) -> Path:
    root = Path(tempfile.gettempdir()) / "voice-pro-ops" / sha256(source)[:12]
    root.mkdir(parents=True, exist_ok=True)
    (root / "prompt-before.txt").write_text(normalized(source), encoding="utf-8")
    (root / "prompt-candidate.txt").write_text(normalized(candidate), encoding="utf-8")
    return root


def replace_with_rollback(
    current: str,
    target: str,
    *,
    validate_target: bool,
) -> None:
    """PUT a prompt and automatically restore the hash-pinned prior value on failure."""
    current_hash = sha256(current)
    target_hash = sha256(target)
    mutation_attempted = False
    try:
        mutation_attempted = True
        updated = admin_request(
            f"/api/v1/agents/{AGENT_ID}",
            method="PUT",
            body={"system_prompt": normalized(target)},
        )
        if not isinstance(updated, dict):
            raise OpsError("agent PUT returned an unexpected response")
        reloaded = live_prompt()
        require_hash(reloaded, target_hash, "reloaded prompt")
        if validate_target:
            assert_candidate(reloaded)
    except Exception as apply_error:
        if (
            not mutation_attempted
        ):  # pragma: no cover - retained as a defensive invariant
            raise
        try:
            admin_request(
                f"/api/v1/agents/{AGENT_ID}",
                method="PUT",
                body={"system_prompt": normalized(current)},
            )
            restored = live_prompt()
            require_hash(restored, current_hash, "auto-rollback prompt")
        except Exception as rollback_error:
            raise OpsError(
                "FATAL: prompt update failed and hash-guarded auto-rollback also failed"
            ) from rollback_error
        raise OpsError(
            f"prompt update failed; auto-rollback restored sha256={current_hash}"
        ) from apply_error


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--expected-live-sha", required=True)
    apply = sub.add_parser("apply")
    apply.add_argument("--expected-live-sha", required=True)
    apply.add_argument("--expected-candidate-sha", required=True)
    apply.add_argument("--confirm", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("--expected-sha", required=True)
    restore = sub.add_parser("restore")
    restore.add_argument("--expected-live-sha", required=True)
    restore.add_argument("--from-file", type=Path, required=True)
    restore.add_argument("--backup-sha", required=True)
    restore.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    source = live_prompt()
    if args.command == "verify":
        require_hash(source, args.expected_sha, "live prompt")
        assert_candidate(source)
        print(
            f"verified=true prompt_len={len(normalized(source))} sha256={sha256(source)}"
        )
        return 0

    if args.command == "restore":
        require_hash(source, args.expected_live_sha, "live prompt")
        if not args.confirm:
            raise OpsError("prompt restore requires --confirm")
        restored_prompt = args.from_file.read_text(encoding="utf-8")
        require_hash(restored_prompt, args.backup_sha, "backup prompt")
        replace_with_rollback(source, restored_prompt, validate_target=False)
        print(
            f"restored=true verified=true prompt_len={len(normalized(restored_prompt))} "
            f"sha256={sha256(restored_prompt)}"
        )
        return 0

    require_hash(source, args.expected_live_sha, "live prompt")
    candidate = patch_prompt(source)
    candidate_hash = sha256(candidate)
    root = backup(source, candidate)
    print(
        f"live_unchanged=true source_len={len(normalized(source))} source_sha256={sha256(source)} "
        f"candidate_len={len(candidate)} candidate_sha256={candidate_hash} artifacts={root}"
    )
    if args.command == "prepare":
        return 0
    if not args.confirm:
        raise OpsError("prompt apply requires --confirm")
    if candidate_hash != args.expected_candidate_sha:
        raise OpsError(
            f"candidate hash mismatch: expected={args.expected_candidate_sha} observed={candidate_hash}"
        )
    replace_with_rollback(source, candidate, validate_target=True)
    print(
        f"applied=true verified=true prompt_len={len(normalized(candidate))} sha256={candidate_hash}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OpsError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
