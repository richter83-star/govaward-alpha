"""Text normalization and amount parsing utilities."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata


# ---------------------------------------------------------------------------
# Dollar amount parser
# ---------------------------------------------------------------------------

_BILLION = 1_000_000_000.0
_MILLION = 1_000_000.0
_THOUSAND = 1_000.0

_UNIT_MAP = {
    "billion": _BILLION,
    "billions": _BILLION,
    "b": _BILLION,
    "million": _MILLION,
    "millions": _MILLION,
    "m": _MILLION,
    "thousand": _THOUSAND,
    "thousands": _THOUSAND,
    "k": _THOUSAND,
}

# Amount patterns.
#
# Prose is the hard case. A bare "first number wins" scan mis-reads
# "5-year contract valued at $95,000,000" as $5 and a contract number
# ("W91234-26-C-0001") as $91,234. So an amount in prose must be anchored by
# either a "$" sign or an explicit unit word; a bare numeric is only trusted
# when it is the *entire* input (structured API fields like "95000000").

_UNIT_GROUP = r"(billion|billions|million|millions|thousand|thousands|[bmk]\b)"

# The whole (stripped) input is just a number — a structured field value.
_BARE_NUMERIC_RE = re.compile(r"^\$?\s*([\d,]+(?:\.\d+)?)\s*$")

# Prose, "$" anchored. Unit optional: "$95,000,000", "$1.2 billion", "$75M".
_DOLLAR_ANCHORED_RE = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*" + _UNIT_GROUP + r"?",
    re.IGNORECASE,
)

# Prose, unit anchored — no "$" but an explicit scale word: "75 million".
_UNIT_ANCHORED_RE = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*" + _UNIT_GROUP,
    re.IGNORECASE,
)


def _to_float(numeric_str: str, unit: str | None) -> float | None:
    try:
        value = float(numeric_str.replace(",", ""))
    except ValueError:
        return None
    return value * _UNIT_MAP.get((unit or "").lower(), 1.0)


def parse_dollar_amount(text: str) -> float | None:
    """
    Convert varied dollar-amount strings to a float of dollars.

    A bare number is accepted only when it is the entire input (structured
    field values). Inside prose the amount must carry a "$" or a unit word,
    so incidental numbers ("5-year", "2000 units", contract numbers) are
    ignored rather than mistaken for the award value.

    Examples:
        "95000000"                          -> 95_000_000.0   (bare field)
        "$95,000,000"                       -> 95_000_000.0
        "$127.4 million"                    -> 127_400_000.0
        "$1.2 billion"                      -> 1_200_000_000.0
        "maximum $95,000,000"               -> 95_000_000.0
        "not-to-exceed $75M"                -> 75_000_000.0
        "5-year contract valued at $95,000,000" -> 95_000_000.0  (not 5.0)
        "Contract W91234-26-C-0001"         -> None
    """
    if not text:
        return None

    bare = _BARE_NUMERIC_RE.match(text.strip())
    if bare:
        return _to_float(bare.group(1), None)

    # Strip punctuation that would otherwise glue tokens together ("$95M/yr"),
    # keeping "$", digits, separators, letters and whitespace.
    cleaned = re.sub(r"[^$\d.,a-zA-Z\s]", " ", text)

    for pattern in (_DOLLAR_ANCHORED_RE, _UNIT_ANCHORED_RE):
        m = pattern.search(cleaned)
        if m:
            value = _to_float(m.group(1), m.group(2))
            if value is not None:
                return value
    return None


def extract_obligated_amount(text: str) -> float | None:
    """
    Extract the 'obligated at time of award' amount from prose text.
    Returns None if not found.
    """
    patterns = [
        r"obligated\s+at\s+(?:the\s+)?time\s+of\s+award[:\s]+(\$?[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|[mMbBkK]))?)",
        r"funded\s+(?:amount|at)[:\s]+(\$?[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|[mMbBkK]))?)",
        r"initial\s+(?:obligation|obligated)[:\s]+(\$?[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|[mMbBkK]))?)",
        r"funds\s+in\s+the\s+amount\s+of\s+(\$?[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|[mMbBkK]))?)\s+(?:are|is)\s+(?:being\s+)?obligated",
        r"(\$?[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|[mMbBkK]))?)\s+(?:are|is)\s+(?:being\s+)?obligated\s+at\s+(?:the\s+)?time\s+of\s+award",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = parse_dollar_amount(m.group(1))
            if val is not None:
                return val
    return None


# ---------------------------------------------------------------------------
# Contract flag detection
# ---------------------------------------------------------------------------

_IDIQ_KEYWORDS = re.compile(
    r"\b(idiq|indefinite[- ]delivery|indefinite[- ]quantity|task\s+order\s+contract)\b",
    re.IGNORECASE,
)

_MODIFICATION_KEYWORDS = re.compile(
    r"\b(modification|modification\s+no|mod\s+p|amended|amendment)\b",
    re.IGNORECASE,
)

_OPTION_KEYWORDS = re.compile(
    r"\b(option\s+exercise|exercising\s+(?:an\s+)?option|option\s+period)\b",
    re.IGNORECASE,
)

_CEILING_KEYWORDS = re.compile(
    r"\b(ceiling|maximum\s+value|not[- ]to[- ]exceed|nte)\b",
    re.IGNORECASE,
)

_DEFENSE_KEYWORDS = re.compile(
    r"\b(defense|military|navy|army|air\s+force|marine|missile|munition|weapon|"
    r"combat|warfare|surveillance|radar|drone|uav|uas|satellite|cyber|"
    r"intelligence|reconnaissance|SIGINT|ISR|logistics\s+support|"
    r"artificial\s+intelligence|AI|machine\s+learning|geospatial|space)\b",
    re.IGNORECASE,
)


def has_defense_keywords(text: str) -> bool:
    """True if the text mentions a defense / AI / cyber / space category."""
    return bool(_DEFENSE_KEYWORDS.search(text))


from datetime import date


def extract_piid_fiscal_year(contract_id: str) -> int | None:
    """Extract the 4-digit fiscal year from a Federal Procurement Instrument Identifier (PIID)
    pursuant to FAR 4.1603 (e.g. 80JSC017C0006 -> 2017, 75N93019C00069 -> 2019,
    FA8650-19-C-5212 -> 2019, W9124P-26-C-0001 -> 2026).
    """
    if not contract_id or len(contract_id) < 8:
        return None
    cleaned = contract_id.strip().upper()
    # 1. FAR standard: [Agency 2-8 chars][-?][2-digit FY][-?][1-char Type][-?][Serial 3+ chars]
    m = re.search(r"^[A-Z0-9]{2,8}(?:-[A-Z0-9]{2,6})?-?([0-9]{2})-?([A-Z])-?([A-Z0-9]{3,})", cleaned)
    if m:
        yy = int(m.group(1))
        return 2000 + yy if yy < 80 else 1900 + yy

    # 2. Embedded hyphenated standard PIID anywhere in text
    m2 = re.search(r"\b[A-Z0-9]{4,8}-([0-9]{2})-[A-Z]-[A-Z0-9]{4,}\b", cleaned)
    if m2:
        yy = int(m2.group(1))
        return 2000 + yy if yy < 80 else 1900 + yy

    return None


def detect_flags(
    description: str,
    award_type: str = "",
    contract_number: str = "",
    period_start: date | None = None,
    award_date: date | None = None,
) -> dict[str, bool]:
    text = f"{description} {award_type} {contract_number}"
    is_idiq = bool(_IDIQ_KEYWORDS.search(text))
    is_mod_keyword = bool(_MODIFICATION_KEYWORDS.search(text))
    is_option = bool(_OPTION_KEYWORDS.search(text))
    is_ceiling = bool(_CEILING_KEYWORDS.search(text))
    has_defense = bool(_DEFENSE_KEYWORDS.search(text))

    # Detect legacy contract vehicle via FAR 4.1603 PIID fiscal year or period of performance
    is_legacy = False
    piid_fy = extract_piid_fiscal_year(contract_number)
    if not piid_fy:
        piid_fy = extract_piid_fiscal_year(description)

    ref_year = award_date.year if award_date else 2026
    if piid_fy and piid_fy < (ref_year - 1):
        is_legacy = True

    if period_start and award_date:
        if (award_date - period_start).days > 90:
            is_legacy = True

    # Legacy contract transactions are de-facto modifications/renewals
    is_modification = is_mod_keyword or is_legacy

    return {
        "is_idiq": is_idiq,
        "is_modification": is_modification,
        "is_option_exercise": is_option,
        "is_ceiling_only": is_ceiling,
        "has_defense_keywords": has_defense,
        "is_legacy_contract": is_legacy,
    }


# ---------------------------------------------------------------------------
# Company name normalization
# ---------------------------------------------------------------------------

_LEGAL_SUFFIXES = re.compile(
    r"\b(incorporated|inc|corporation|corp|limited|ltd|llc|llp|lp|"
    r"company|co|group|holdings|holding|enterprises|"
    r"solutions|services|international|global|"
    r"associates|partners|consulting|division)\b\.?",
    re.IGNORECASE,
)

_WHITESPACE = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")


def normalize_company_name(name: str) -> str:
    """
    Return a canonical lowercase, stripped company name for fuzzy matching.
    Removes punctuation, legal suffix words, and collapses whitespace.
    """
    if not name:
        return ""
    # Unicode normalization
    name = unicodedata.normalize("NFKD", name)
    name = name.encode("ascii", "ignore").decode("ascii")
    name = name.lower()
    name = _PUNCT.sub(" ", name)
    name = _LEGAL_SUFFIXES.sub(" ", name)
    name = _WHITESPACE.sub(" ", name)
    return name.strip()


# ---------------------------------------------------------------------------
# Content hashing for dedup
# ---------------------------------------------------------------------------

def content_hash(data: dict) -> str:
    """Stable SHA-256 hash of a dict (sorted keys)."""
    serialized = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode()).hexdigest()[:16]
