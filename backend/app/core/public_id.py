"""Public ID generation utilities for embeddable agents."""

import secrets
import string

# Base62 alphabet (URL-safe, no ambiguous characters)
ALPHABET = string.ascii_letters + string.digits  # a-zA-Z0-9

# Validation constants
EXPECTED_PARTS_COUNT = 2
MIN_RANDOM_LENGTH = 6
MAX_RANDOM_LENGTH = 16

# Length of a call share token — the secret that stands in for auth on public
# transcript and recording links. Lives here because both the websocket bridge
# and the recording webhook mint one.
SHARE_TOKEN_LENGTH = 16


def generate_public_id(prefix: str = "ag", length: int = 8) -> str:
    """Generate a URL-safe, short public ID.

    Format: {prefix}_{random_chars}
    Example: ag_xK9mN2pQ

    Args:
        prefix: Prefix for the ID (default: "ag" for agent)
        length: Length of the random portion (default: 8)

    Returns:
        A URL-safe public ID like "ag_xK9mN2pQ"
    """
    random_part = "".join(secrets.choice(ALPHABET) for _ in range(length))
    return f"{prefix}_{random_part}"


def validate_public_id(public_id: str, prefix: str = "ag") -> bool:
    """Validate that a public ID has the correct format.

    Args:
        public_id: The public ID to validate
        prefix: Expected prefix (default: "ag")

    Returns:
        True if valid, False otherwise
    """
    if not public_id:
        return False

    parts = public_id.split("_", 1)
    if len(parts) != EXPECTED_PARTS_COUNT:
        return False

    id_prefix, random_part = parts

    # Check prefix matches
    if id_prefix != prefix:
        return False

    # Check random part length and characters
    if len(random_part) < MIN_RANDOM_LENGTH or len(random_part) > MAX_RANDOM_LENGTH:
        return False

    # Check all characters are in the alphabet
    return all(c in ALPHABET for c in random_part)
