"""Legal-consent policy for call recording.

US call-recording law is split: most states are "one-party consent" (the fact that
*we* are a party to the call is enough), but a minority require the consent of
*every* party on the line. Recording a two-party-consent state without an explicit
disclosure is a criminal exposure, not a product bug.

This module answers exactly one question, from the destination number alone:

    recording_allowed("+14155550123") -> bool

It is deliberately **fail-safe OFF**: unknown area code, non-US number, toll-free,
US territory, or anything unparseable all return ``False``. Not recording a call
costs us a nice-to-have artifact. Recording one we shouldn't have costs a lot more.

One override sits ABOVE the geography: ``RECORDING_CONSENT_NUMBERS``, an allowlist
of numbers whose owner has personally consented. Geography is only ever a proxy for
consent, so an actual consenting party is the stronger permission — that is the
supported way to record our own test handsets and listen to how the agent performs.

The caller is still responsible for ANDing this with the per-agent
``Agent.enable_recording`` toggle — this module only speaks to legality, never to
whether the operator actually wants a recording.
"""

from __future__ import annotations

from app.core.config import settings

# ---------------------------------------------------------------------------
# Consent classification
# ---------------------------------------------------------------------------

# States requiring ALL/two-party consent to record a call.
#
# OVER-INCLUSION BY DESIGN. A few of these (e.g. Nevada, Michigan, Oregon) are
# genuinely contested — courts and statutes disagree on whether the all-party rule
# reaches telephone calls, one-party participants, or only third-party eavesdropping.
# We resolve every ambiguity toward NOT recording: the cost of not recording is a
# missing audio file, the cost of illegal recording is statutory damages and, in
# several of these states, a criminal charge. If a state is arguable, it goes in
# the set.
ALL_PARTY_CONSENT_STATES: frozenset[str] = frozenset(
    {
        "CA",  # California - Cal. Penal Code 632
        "CT",  # Connecticut - Conn. Gen. Stat. 52-570d
        "DE",  # Delaware - 11 Del. C. 2402
        "FL",  # Florida - Fla. Stat. 934.03
        "IL",  # Illinois - 720 ILCS 5/14-2
        "MD",  # Maryland - Md. Code Cts. & Jud. Proc. 10-402
        "MA",  # Massachusetts - Mass. Gen. Laws ch. 272 s.99
        "MI",  # Michigan - MCL 750.539c (contested; treated as all-party)
        "MT",  # Montana - Mont. Code Ann. 45-8-213
        "NV",  # Nevada - NRS 200.620 (contested; treated as all-party)
        "NH",  # New Hampshire - RSA 570-A:2
        "OR",  # Oregon - ORS 165.540 (contested; treated as all-party)
        "PA",  # Pennsylvania - 18 Pa. C.S. 5703
        "WA",  # Washington - RCW 9.73.030
    }
)


# ---------------------------------------------------------------------------
# NANP area code -> US state / district
# ---------------------------------------------------------------------------

# Only US states + DC are mapped. Deliberately ABSENT (and therefore resolving to
# None -> recording denied):
#   * Canadian area codes (204, 416, 604, ...)
#   * Caribbean/other NANP countries (242 Bahamas, 876 Jamaica, ...)
#   * US territories (787/939 PR, 340 VI, 671 GU, 670 MP, 684 AS) - their consent
#     law differs from the mainland (Puerto Rico is all-party), so they fail safe.
#   * Non-geographic codes: toll-free (800/833/844/855/866/877/888), 900 premium,
#     500/521-529/533/544/566/577/588 personal-comm, 700, 710.
# An area code we don't know is an area code we don't record.
# fmt: off
AREA_CODE_TO_STATE: dict[str, str] = {
    # Alabama
    "205": "AL", "251": "AL", "256": "AL", "334": "AL", "483": "AL", "659": "AL",
    "938": "AL",
    # Alaska
    "907": "AK",
    # Arizona
    "480": "AZ", "520": "AZ", "602": "AZ", "623": "AZ", "928": "AZ",
    # Arkansas
    "327": "AR", "479": "AR", "501": "AR", "870": "AR",
    # California
    "209": "CA", "213": "CA", "279": "CA", "310": "CA", "323": "CA", "341": "CA",
    "350": "CA", "369": "CA", "408": "CA", "415": "CA", "424": "CA", "442": "CA",
    "510": "CA", "530": "CA", "559": "CA", "562": "CA", "619": "CA", "626": "CA",
    "628": "CA", "650": "CA", "657": "CA", "661": "CA", "669": "CA", "707": "CA",
    "714": "CA", "738": "CA", "747": "CA", "760": "CA", "764": "CA", "805": "CA",
    "818": "CA", "820": "CA", "831": "CA", "837": "CA", "840": "CA", "858": "CA",
    "909": "CA", "916": "CA", "925": "CA", "949": "CA", "951": "CA",
    # Colorado
    "303": "CO", "719": "CO", "720": "CO", "748": "CO", "970": "CO", "983": "CO",
    # Connecticut
    "203": "CT", "475": "CT", "860": "CT", "959": "CT",
    # Delaware
    "302": "DE",
    # District of Columbia
    "202": "DC",
    # Florida
    "239": "FL", "305": "FL", "321": "FL", "324": "FL", "352": "FL", "386": "FL",
    "407": "FL", "448": "FL", "561": "FL", "645": "FL", "656": "FL", "689": "FL",
    "727": "FL", "728": "FL", "754": "FL", "772": "FL", "786": "FL", "813": "FL",
    "850": "FL", "863": "FL", "904": "FL", "941": "FL", "954": "FL",
    # Georgia
    "229": "GA", "404": "GA", "470": "GA", "478": "GA", "678": "GA", "706": "GA",
    "762": "GA", "770": "GA", "912": "GA", "943": "GA",
    # Hawaii
    "808": "HI",
    # Idaho
    "208": "ID", "986": "ID",
    # Illinois
    "217": "IL", "224": "IL", "309": "IL", "312": "IL", "331": "IL", "447": "IL",
    "464": "IL", "618": "IL", "630": "IL", "708": "IL", "730": "IL", "773": "IL",
    "779": "IL", "815": "IL", "847": "IL", "861": "IL", "872": "IL",
    # Indiana
    "219": "IN", "260": "IN", "317": "IN", "463": "IN", "574": "IN", "765": "IN",
    "812": "IN", "930": "IN",
    # Iowa
    "319": "IA", "515": "IA", "563": "IA", "641": "IA", "712": "IA",
    # Kansas
    "316": "KS", "620": "KS", "785": "KS", "913": "KS",
    # Kentucky
    "270": "KY", "364": "KY", "502": "KY", "606": "KY", "859": "KY",
    # Louisiana
    "225": "LA", "318": "LA", "337": "LA", "504": "LA", "985": "LA",
    # Maine
    "207": "ME",
    # Maryland
    "227": "MD", "240": "MD", "301": "MD", "410": "MD", "443": "MD", "667": "MD",
    # Massachusetts
    "339": "MA", "351": "MA", "413": "MA", "508": "MA", "617": "MA", "774": "MA",
    "781": "MA", "857": "MA", "978": "MA",
    # Michigan
    "231": "MI", "248": "MI", "269": "MI", "313": "MI", "517": "MI", "586": "MI",
    "616": "MI", "679": "MI", "734": "MI", "810": "MI", "906": "MI", "947": "MI",
    "989": "MI",
    # Minnesota
    "218": "MN", "320": "MN", "507": "MN", "612": "MN", "651": "MN", "763": "MN",
    "924": "MN", "952": "MN",
    # Mississippi
    "228": "MS", "601": "MS", "662": "MS", "769": "MS",
    # Missouri
    "235": "MO", "314": "MO", "417": "MO", "557": "MO", "573": "MO", "636": "MO",
    "660": "MO", "816": "MO", "975": "MO",
    # Montana
    "406": "MT",
    # Nebraska
    "308": "NE", "402": "NE", "531": "NE",
    # Nevada
    "702": "NV", "725": "NV", "775": "NV",
    # New Hampshire
    "603": "NH",
    # New Jersey
    "201": "NJ", "551": "NJ", "609": "NJ", "640": "NJ", "732": "NJ", "848": "NJ",
    "856": "NJ", "862": "NJ", "908": "NJ", "973": "NJ",
    # New Mexico
    "505": "NM", "575": "NM",
    # New York
    "212": "NY", "315": "NY", "329": "NY", "332": "NY", "347": "NY", "363": "NY",
    "516": "NY", "518": "NY", "585": "NY", "607": "NY", "624": "NY", "631": "NY",
    "646": "NY", "680": "NY", "716": "NY", "718": "NY", "838": "NY", "845": "NY",
    "914": "NY", "917": "NY", "929": "NY", "934": "NY",
    # North Carolina
    "252": "NC", "336": "NC", "704": "NC", "743": "NC", "828": "NC", "910": "NC",
    "919": "NC", "980": "NC", "984": "NC",
    # North Dakota
    "701": "ND",
    # Ohio
    "216": "OH", "220": "OH", "234": "OH", "283": "OH", "326": "OH", "330": "OH",
    "380": "OH", "419": "OH", "436": "OH", "440": "OH", "513": "OH", "567": "OH",
    "614": "OH", "740": "OH", "937": "OH",
    # Oklahoma
    "405": "OK", "539": "OK", "572": "OK", "580": "OK", "918": "OK",
    # Oregon
    "458": "OR", "503": "OR", "541": "OR", "971": "OR",
    # Pennsylvania
    "215": "PA", "223": "PA", "267": "PA", "272": "PA", "412": "PA", "445": "PA",
    "484": "PA", "570": "PA", "582": "PA", "610": "PA", "717": "PA", "724": "PA",
    "814": "PA", "835": "PA", "878": "PA",
    # Rhode Island
    "401": "RI",
    # South Carolina
    "803": "SC", "821": "SC", "839": "SC", "843": "SC", "854": "SC", "864": "SC",
    # South Dakota
    "605": "SD",
    # Tennessee
    "423": "TN", "615": "TN", "629": "TN", "731": "TN", "865": "TN", "901": "TN",
    "931": "TN",
    # Texas
    "210": "TX", "214": "TX", "254": "TX", "281": "TX", "325": "TX", "346": "TX",
    "361": "TX", "409": "TX", "430": "TX", "432": "TX", "469": "TX", "512": "TX",
    "682": "TX", "713": "TX", "726": "TX", "737": "TX", "806": "TX", "817": "TX",
    "830": "TX", "832": "TX", "903": "TX", "915": "TX", "936": "TX", "940": "TX",
    "945": "TX", "956": "TX", "972": "TX", "979": "TX",
    # Utah
    "385": "UT", "435": "UT", "801": "UT",
    # Vermont
    "802": "VT",
    # Virginia
    "276": "VA", "434": "VA", "540": "VA", "571": "VA", "686": "VA", "703": "VA",
    "757": "VA", "804": "VA", "826": "VA", "948": "VA",
    # Washington
    "206": "WA", "253": "WA", "360": "WA", "425": "WA", "509": "WA", "564": "WA",
    # West Virginia
    "304": "WV", "681": "WV",
    # Wisconsin
    "262": "WI", "274": "WI", "353": "WI", "414": "WI", "534": "WI", "608": "WI",
    "715": "WI", "920": "WI",
    # Wyoming
    "307": "WY",
}
# fmt: on


# ---------------------------------------------------------------------------
# Decision reasons (stable strings - safe to log and assert on)
# ---------------------------------------------------------------------------

REASON_ONE_PARTY = "one_party_consent_state"
REASON_ALL_PARTY = "all_party_consent_state"
REASON_UNKNOWN_AREA_CODE = "unknown_area_code"
REASON_UNPARSEABLE = "unparseable_number"
REASON_EXPLICIT_CONSENT = "explicit_consent_allowlist"

# A consent entry must carry at least a full national number, so a short or
# malformed entry can never match a broad set of real phones by suffix.
_MIN_CONSENT_MATCH_DIGITS = 9

# Cosmetic separators we tolerate inside a number. Anything else (a letter, a
# stray symbol) makes the input unparseable rather than silently salvageable.
_SEPARATORS = frozenset(" \t\r\n-().\u00a0\u202f\u2011\u2013")
_NANP_DIGITS = 10


def _strip_separators(raw: str) -> str:
    return "".join(char for char in raw if char not in _SEPARATORS)


def _national_digits(raw: str, *, explicit_plus: bool) -> str | None:
    """Drop the country code, returning the 10 national digits (or None)."""
    if explicit_plus:
        # A fully-qualified number that is not +1 carries an explicit non-NANP
        # country code. Reject it outright -- otherwise "+4420712345" would be
        # truncated into the California area code 442.
        if not raw.startswith("1") or len(raw) != _NANP_DIGITS + 1:
            return None
        return raw[1:]
    if len(raw) == _NANP_DIGITS + 1 and raw.startswith("1"):
        return raw[1:]
    return raw if len(raw) == _NANP_DIGITS else None


def _normalize(to_number: str | None) -> str | None:
    """Reduce a US/NANP number to its bare 10 digits, or None if it is not one.

    Accepts ``+1XXXXXXXXXX``, ``1XXXXXXXXXX`` and bare ``XXXXXXXXXX``, with the
    usual cosmetic separators.
    """
    if not to_number or not isinstance(to_number, str):
        return None

    raw = _strip_separators(to_number.strip())
    explicit_plus = raw.startswith("+")
    if explicit_plus:
        raw = raw[1:]
    if not raw or not raw.isdigit():
        return None

    national = _national_digits(raw, explicit_plus=explicit_plus)
    if national is None:
        return None

    # Valid NANP: area code and exchange both start 2-9.
    if national[0] in "01" or national[3] in "01":
        return None

    return national


def area_code(to_number: str | None) -> str | None:
    """Return the NANP area code for a US number, or None if not parseable."""
    national = _normalize(to_number)
    return national[:3] if national else None


def state_for_number(to_number: str | None) -> str | None:
    """Return the US state/district code for a number, or None if unknown."""
    npa = area_code(to_number)
    return AREA_CODE_TO_STATE.get(npa) if npa else None


def has_explicit_consent(to_number: str | None) -> bool:
    """True when this number's owner has personally consented to being recorded.

    Consent is what the law actually asks for; the area-code map only ever
    APPROXIMATES it from geography, and cannot know it for an international or
    ported number. ``RECORDING_CONSENT_NUMBERS`` is the narrow, deliberate
    override for numbers whose owner has actually said yes — in practice our own
    test handsets, which is how the agent gets listened to and diagnosed.

    Compared on the LAST 9 digits — the subscriber part — because the same phone is
    written several ways and the prefixes are exactly what differ: +46700171894 and
    the Swedish national form 0700171894 must be recognised as one number (note the
    national trunk "0" REPLACES the country code, so aligning on longer suffixes
    fails). Nine digits is specific enough that two real numbers cannot collide, and
    the list itself is a handful of operator-entered entries.
    """
    target = "".join(char for char in (to_number or "") if char.isdigit())
    if len(target) < _MIN_CONSENT_MATCH_DIGITS:
        return False
    for entry in (settings.RECORDING_CONSENT_NUMBERS or "").split(","):
        allowed = "".join(char for char in entry if char.isdigit())
        if len(allowed) < _MIN_CONSENT_MATCH_DIGITS:
            continue
        if allowed[-_MIN_CONSENT_MATCH_DIGITS:] == target[-_MIN_CONSENT_MATCH_DIGITS:]:
            return True
    return False


def recording_decision(to_number: str | None) -> tuple[bool, str | None, str]:
    """Return ``(allowed, state, reason)`` for recording a call to ``to_number``.

    The tuple exists so the caller can log *why* a call was or wasn't recorded
    without re-deriving the state.
    """
    # An explicitly consenting party outranks the geographic approximation — it is
    # the stronger form of the same permission, not a loophole around it.
    if has_explicit_consent(to_number):
        return True, None, REASON_EXPLICIT_CONSENT

    npa = area_code(to_number)
    if npa is None:
        return False, None, REASON_UNPARSEABLE

    state = AREA_CODE_TO_STATE.get(npa)
    if state is None:
        return False, None, REASON_UNKNOWN_AREA_CODE

    if state in ALL_PARTY_CONSENT_STATES:
        return False, state, REASON_ALL_PARTY

    return True, state, REASON_ONE_PARTY


def recording_allowed(to_number: str) -> bool:
    """True only when the destination is a KNOWN one-party-consent US state.

    Fail-safe OFF: unknown area code, non-US number, US territory, toll-free or
    unparseable input all return False.
    """
    allowed, _state, _reason = recording_decision(to_number)
    return allowed
