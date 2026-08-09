"""The public embed surface, which was standing open on 2026-08-09.

Found by the repaired day-one-review skill, running its "prove use before you
call anything dead" step over the costed list. The previous review had rated the
embed API "Low-medium" risk and put it in the LEAVE pile — pricing a compliance
surface on traffic, which that skill's own rule forbids.

What was actually true on the production agent: `embed_enabled` was on and
`allowed_domains` was empty, and an empty allowlist meant ALLOW EVERYTHING. Proven
by probing the deployed system from an unrelated origin with no credential:

    GET  /api/public/embed/ag_.../config      -> 200, agent config
    POST /api/public/embed/ag_.../session     -> 200, a working session + ws URL
    POST /api/public/embed/ag_.../tool-call   -> 200, "Tool X is not enabled"
                                                 (i.e. an ENABLED one would run)

The live agent's enabled tools are crm + call_control — thirteen of them,
including book_appointment against Sami's real calendar.

Embedding is now off on that agent, and an empty allowlist denies. The safe state
has to be the one you get by doing nothing.
"""

import pytest

from app.api.embed import validate_origin

REAL = "https://pulsift.com"
STRANGER = "https://totally-unrelated-site.example"


def test_an_empty_allowlist_denies_everyone() -> None:
    """The whole finding, in one assertion.

    Nobody fills in an allowlist they did not know existed, so "empty" is the
    state a production agent arrives in by default. It must be the closed one.
    """
    assert validate_origin(REAL, []) is False
    assert validate_origin(STRANGER, []) is False
    assert validate_origin(None, []) is False


def test_a_configured_allowlist_still_works() -> None:
    assert validate_origin(REAL, ["pulsift.com"]) is True
    assert validate_origin(STRANGER, ["pulsift.com"]) is False


def test_wildcards_still_match_a_subdomain_and_nothing_else() -> None:
    assert validate_origin("https://app.pulsift.com", ["*.pulsift.com"]) is True
    assert validate_origin("https://pulsift.com.evil.example", ["*.pulsift.com"]) is False


def test_a_missing_origin_header_is_refused_even_when_domains_are_set() -> None:
    """No Origin header is not a pass. A caller controls its own headers."""
    assert validate_origin(None, ["pulsift.com"]) is False
    assert validate_origin("", ["pulsift.com"]) is False


@pytest.mark.parametrize("junk", ["not a url", "://", "javascript:alert(1)", "  "])
def test_an_unparseable_origin_is_refused(junk: str) -> None:
    assert validate_origin(junk, ["pulsift.com"]) is False
