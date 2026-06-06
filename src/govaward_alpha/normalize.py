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

# Patterns that extract the leading numeric token from an amount phrase
_AMOUNT_RE = re.compile(
    r"""
    [\$]?                           # optional dollar sign
    \s*
    ([\d,]+(?:\.\d+)?)              # numeric part
    \s*
    (billion|billions|million|millions|thousand|thousands|[bBmMkK])?  # unit
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_dollar_amount(text: str) -> float | None:
    """
    Convert varied dollar-amount strings to a float of dollars.

    Examples:
        "$95,000,000"           → 95_000_000.0
        "$127.4 million"        → 127_400_000.0
        "$1.2 billion"          → 1_200_000_000.0
        "maximum $95,000,000"   → 95_000_000.0
        "not-to-exceed $75M"    → 75_000_000.0
        "obligated at the time of award: $18,000,000" → 18_000_000.0
    """
    if not text:
        return None
    # Strip prefixes like "maximum", "not-to-exceed", "obligated …:"
    cleaned = re.sub(r"[^$\d.,a-zA-Z\s]", " ", text)
    m = _AMOUNT_RE.search(cleaned)
    if not m:
        return None
    numeric_str = m.group(1).replace(",", "")
    try:
        value = float(numeric_str)
    except ValueError:
        return None
    unit = (m.group(2) or "").lower()
    multiplier = _UNIT_MAP.get(unit, 1.0)
    # If no unit but value looks like it might already be in millions
    # (common in some DoD pages), we do NOT auto-multiply — caller must pass full text.
    return value * multiplier


def extract_obligated_amount(text: str) -> float | None:
    """
    Extract the 'obligated at time of award' amount from prose text.
    Returns None if not found.
    """
    patterns = [
        r"obligated\s+at\s+(?:the\s+)?time\s+of\s+award[:\s]+(\$?[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|[mMbBkK]))?)",
        r"funded\s+(?:amount|at)[:\s]+(\$?[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|[mMbBkK]))?)",
        r"initial\s+(?:obligation|obligated)[:\s]+(\$?[\d,]+(?:\.\d+)?(?:\s*(?:million|billion|thousand|[mMbBkK]))?)",
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


def detect_flags(description: str, award_type: str = "") -> dict[str, bool]:
    text = f"{description} {award_type}"
    return {
        "is_idiq": bool(_IDIQ_KEYWORDS.search(text)),
        "is_modification": bool(_MODIFICATION_KEYWORDS.search(text)),
        "is_option_exercise": bool(_OPTION_KEYWORDS.search(text)),
        "is_ceiling_only": bool(_CEILING_KEYWORDS.search(text)),
        "has_defense_keywords": bool(_DEFENSE_KEYWORDS.search(text)),
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
