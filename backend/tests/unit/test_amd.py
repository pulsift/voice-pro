"""Contracts for transcript-based answering-machine detection (C2).

The asymmetry is the whole point: a false "machine" hangs up on a real prospect,
a false "human" wastes a few seconds. Every ambiguous or broken path must land on
a verdict that keeps the call alive.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import settings
from app.services import amd


def make_openai_stub(content: str | None) -> MagicMock:
    """Build an AsyncOpenAI stub whose chat completion returns `content`."""
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )
    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=response)
    return client


@pytest.fixture(autouse=True)
def platform_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "AMD_MODEL", "gpt-4o-mini")


@pytest.mark.asyncio
async def test_voicemail_greeting_classified_as_machine() -> None:
    client = make_openai_stub("machine-vm")
    with patch.object(amd, "AsyncOpenAI", return_value=client):
        verdict = await amd.classify_greeting(
            "Hi, you've reached Dave. Leave a message after the tone."
        )

    assert verdict == amd.MACHINE_VOICEMAIL
    assert verdict in amd.MACHINE_VERDICTS


@pytest.mark.asyncio
async def test_ivr_menu_classified_as_machine_ivr() -> None:
    client = make_openai_stub("machine-ivr")
    with patch.object(amd, "AsyncOpenAI", return_value=client):
        verdict = await amd.classify_greeting("Thank you for calling Acme. For sales, press 1.")

    assert verdict == amd.MACHINE_IVR
    assert verdict in amd.MACHINE_VERDICTS


@pytest.mark.asyncio
async def test_short_greeting_stays_human() -> None:
    client = make_openai_stub("human")
    with patch.object(amd, "AsyncOpenAI", return_value=client):
        verdict = await amd.classify_greeting("hello?")

    assert verdict == amd.HUMAN
    assert verdict not in amd.MACHINE_VERDICTS


@pytest.mark.asyncio
async def test_prompt_shape_is_deterministic_cheap_and_human_biased() -> None:
    client = make_openai_stub("human")
    with patch.object(amd, "AsyncOpenAI", return_value=client):
        await amd.classify_greeting("Yeah, who's this?")

    kwargs = client.chat.completions.create.await_args.kwargs
    assert kwargs["model"] == settings.AMD_MODEL
    assert kwargs["temperature"] == 0
    assert kwargs["max_tokens"] <= 10
    system, user = kwargs["messages"]
    assert system["role"] == "system"
    assert "BIAS TOWARD human" in system["content"]
    # The labels the caller switches on must be named in the prompt.
    for label in (amd.HUMAN, amd.MACHINE_VOICEMAIL, amd.MACHINE_IVR, amd.UNCERTAIN):
        assert label in system["content"]
    # Voicemail + IVR markers are taught, not guessed at.
    assert "leave a message" in system["content"]
    assert "press 1" in system["content"]
    assert user == {"role": "user", "content": "Yeah, who's this?"}


@pytest.mark.asyncio
async def test_api_error_degrades_to_uncertain_not_machine() -> None:
    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=RuntimeError("upstream 500"))
    with patch.object(amd, "AsyncOpenAI", return_value=client):
        verdict = await amd.classify_greeting("Hi, you've reached Dave, leave a message")

    assert verdict == amd.UNCERTAIN
    assert verdict not in amd.MACHINE_VERDICTS


@pytest.mark.asyncio
async def test_unparseable_verdict_degrades_to_uncertain() -> None:
    client = make_openai_stub("I think this is a voicemail box")
    with patch.object(amd, "AsyncOpenAI", return_value=client):
        verdict = await amd.classify_greeting("Hi there")

    assert verdict == amd.UNCERTAIN


@pytest.mark.asyncio
async def test_noisy_label_is_normalized() -> None:
    client = make_openai_stub(" Machine-VM.\n")
    with patch.object(amd, "AsyncOpenAI", return_value=client):
        verdict = await amd.classify_greeting("You have reached the mailbox of...")

    assert verdict == amd.MACHINE_VOICEMAIL


@pytest.mark.asyncio
async def test_empty_utterance_never_calls_the_api() -> None:
    client = make_openai_stub("machine-vm")
    with patch.object(amd, "AsyncOpenAI", return_value=client):
        verdict = await amd.classify_greeting("   ")

    assert verdict == amd.UNCERTAIN
    client.chat.completions.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_api_key_degrades_to_uncertain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OPENAI_API_KEY", None)
    with patch.object(amd, "AsyncOpenAI", side_effect=AssertionError("must not construct")):
        verdict = await amd.classify_greeting("Hello?")

    assert verdict == amd.UNCERTAIN
