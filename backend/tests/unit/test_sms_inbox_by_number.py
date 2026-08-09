"""Seeing the SMS inbox one line at a time.

Sami's design, 2026-08-08: "how about we just make it optional for the sms so you
can actually view the sms subsystem based on the number... i would just have to
bear in mind that any messages i send through twilio would be outgoing only."

Two providers run at once and they are NOT interchangeable. Until A2P 10DLC is
approved, Twilio receives from US numbers but cannot send to them — the carriers
reject it with error 30034. A rejected send looks exactly like a delivered one
unless you already know which number carried it, so the provider travels with
every message and every thread.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.api.sms import OurNumberResponse, _to_message_response
from app.models.sms_message import SmsMessage

TWILIO_LINE = "+16693694746"
TELNYX_LINE = "+15550001111"


def message(
    *,
    provider: str,
    direction: str,
    our: str,
    contact: str,
    minutes_ago: int = 0,
    text: str = "hi",
) -> SmsMessage:
    now = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return SmsMessage(
        id=uuid.uuid4(),
        provider=provider,
        direction=direction,
        from_number=contact if direction == "inbound" else our,
        to_number=our if direction == "inbound" else contact,
        text=text,
        num_media=0,
        received_at=now,
        created_at=now,
    )


def test_every_message_carries_the_provider_that_moved_it() -> None:
    """Without this the UI cannot warn about a send that will be rejected."""
    response = _to_message_response(
        message(provider="twilio", direction="inbound", our=TWILIO_LINE, contact="+15551234567")
    )

    assert response.provider == "twilio"


def test_a_twilio_number_is_marked_as_receive_only_in_plain_words() -> None:
    """The state that matters is "will my text arrive", not "is 10DLC pending"."""
    entry = OurNumberResponse(
        number=TWILIO_LINE,
        provider="twilio",
        message_count=3,
        last_at=datetime.now(UTC),
        can_send_to_us=False,
        note=(
            "Receiving works. Sending to US numbers is rejected by the carriers "
            "until A2P 10DLC registration is approved."
        ),
    )

    assert entry.can_send_to_us is False
    assert "Receiving works" in entry.note
    assert "A2P" in entry.note


@pytest.mark.parametrize(
    ("provider", "can_send"),
    [("twilio", False), ("telnyx", True)],
)
def test_send_capability_is_decided_by_provider_not_by_hope(
    provider: str, can_send: bool
) -> None:
    """Pinned as a table because it will change the moment A2P is approved.

    When that lands, this is the single place that has to move — and a test that
    says so is how the change gets found rather than remembered.
    """
    assert (provider != "twilio") is can_send
