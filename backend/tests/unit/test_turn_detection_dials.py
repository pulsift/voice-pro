"""How the agent decides the caller has stopped talking.

Fable's read after comparing our transcripts with the Retell demo Sami liked:
read as TEXT, the Retell script is more robotic than ours. What sold him was the
voice and the timing. So the audio dials are a larger share of what still sounds
wrong than the words are — and they had never been tested at all.

The trap these pin: server VAD and semantic VAD take DIFFERENT parameters, and
the Realtime API ignores unknown ones silently. Sending server VAD's threshold
and silence_duration to semantic VAD would look exactly like a working change
while changing nothing.
"""

import pytest

from app.core.config import settings
from app.services.gpt_realtime import _turn_detection_config


def test_semantic_turn_detection_sends_eagerness_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "REALTIME_TURN_DETECTION", "semantic")
    monkeypatch.setattr(settings, "REALTIME_SEMANTIC_EAGERNESS", "auto")

    config = _turn_detection_config()

    assert config == {"type": "semantic_vad", "eagerness": "auto"}
    # Silence thresholds belong to the other mode. Sending them here is the
    # silent no-op that would make this change look applied when it was not.
    assert "silence_duration_ms" not in config
    assert "threshold" not in config


def test_server_turn_detection_still_carries_its_own_dials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fallback has to stay real — it is the revert path if semantic sounds worse."""
    monkeypatch.setattr(settings, "REALTIME_TURN_DETECTION", "server")
    monkeypatch.setattr(settings, "REALTIME_VAD_SILENCE_DURATION_MS", 450)
    monkeypatch.setattr(settings, "REALTIME_VAD_THRESHOLD", 0.6)
    monkeypatch.setattr(settings, "REALTIME_VAD_PREFIX_PADDING_MS", 300)

    config = _turn_detection_config()

    assert config["type"] == "server_vad"
    assert config["silence_duration_ms"] == 450
    assert config["threshold"] == 0.6
    assert "eagerness" not in config


def test_anything_unrecognised_falls_back_to_server_vad(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A typo in an env var must not send an invalid turn_detection block.

    The session update would be rejected, and the whole opening — the hello AND
    the dead-air hangup — hangs off that one event.
    """
    monkeypatch.setattr(settings, "REALTIME_TURN_DETECTION", "sematnic")

    assert _turn_detection_config()["type"] == "server_vad"


def test_the_shipped_dials_are_the_ones_that_were_decided() -> None:
    """The three values Sami's ear is about to judge, in one place.

    Each is independently revertible by env var, so a call that sounds worse can
    be bisected in one command instead of a redeploy.
    """
    assert settings.REALTIME_TURN_DETECTION == "semantic"
    assert settings.REALTIME_OUTPUT_SPEED == 1.0
    assert settings.REALTIME_VAD_SILENCE_DURATION_MS == 450
