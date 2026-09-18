"""Company name → ticker resolver.

Resolution priority:
  1. Manual override YAML (exact normalized key match)
  2. Manual override YAML (substring match, override key longer than 6 chars)
  3. SEC company_tickers.json (exact normalized match)
  4. SEC company_tickers.json (rapidfuzz WRatio >= 92, normalized name >= 8 chars)
  5. yfinance search fallback (partial_ratio >= 90 AND token_set_ratio >= 80)
  6. Unresolved (stored without alert; surfaces in `govaward unresolved`)

Thresholds are deliberately tight: most federal awards go to private firms
and nonprofits, so a loose match invents a false alert on an unrelated public
ticker. Prefer returning None and letting the operator add an override.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz import fuzz, process

from govaward_alpha.config import get_settings
from govaward_alpha.models import ResolutionConfidence
from govaward_alpha.normalize import normalize_company_name
from govaward_alpha.sources.sec_tickers import get_ticker_map

logger = logging.getLogger(__name__)

_FUZZY_THRESHOLD = 92   # raised: false positives fire below this on short/generic names
_MAX_FUZZY_CANDIDATES = 5
_MIN_NORM_LEN = 8       # skip fuzzy match if normalized name is too short/generic


def _load_overrides() -> dict[str, dict]:
    """Load manual_ticker_overrides.yaml. Normalized key → {ticker, parent_company_name}."""
    settings = get_settings()
    override_file = settings.data_dir / "manual_ticker_overrides.yaml"
    if not override_file.exists():
        logger.debug("No manual override file at %s", override_file)
        return {}
    try:
        raw = yaml.safe_load(override_file.read_text(encoding="utf-8")) or {}
        overrides = raw.get("overrides", {})
        return {normalize_company_name(k): v for k, v in overrides.items()}
    except Exception as exc:
        logger.warning("Failed to load ticker overrides: %s", exc)
        return {}


# Module-level cache
_override_cache: dict[str, dict] | None = None


def get_overrides(force_refresh: bool = False) -> dict[str, dict]:
    global _override_cache
    if _override_cache is None or force_refresh:
        _override_cache = _load_overrides()
    return _override_cache


def _load_watchlist() -> set[str]:
    settings = get_settings()
    wl_file = settings.data_dir / "watchlist.yaml"
    if not wl_file.exists():
        return set()
    try:
        raw = yaml.safe_load(wl_file.read_text(encoding="utf-8")) or {}
        return set(t.upper() for t in (raw.get("tickers") or []))
    except Exception:
        return set()


_watchlist_cache: set[str] | None = None


def get_watchlist() -> set[str]:
    global _watchlist_cache
    if _watchlist_cache is None:
        _watchlist_cache = _load_watchlist()
    return _watchlist_cache


def resolve(awardee_name_raw: str) -> dict[str, str | None]:
    """
    Resolve an awardee name to a ticker.

    Returns:
        {
            "ticker": str | None,
            "parent_company_name": str,
            "resolution_method": str,
            "confidence": str,
        }
    """
    if not awardee_name_raw:
        return {
            "ticker": None,
            "parent_company_name": "",
            "resolution_method": "empty",
            "confidence": ResolutionConfidence.UNRESOLVED,
        }

    normalized = normalize_company_name(awardee_name_raw)

    # 1. Manual overrides
    overrides = get_overrides()
    if normalized in overrides:
        entry = overrides[normalized]
        logger.debug("Resolved '%s' via manual override → %s", awardee_name_raw, entry.get("ticker"))
        return {
            "ticker": entry.get("ticker"),
            "parent_company_name": entry.get("parent_company_name") or awardee_name_raw,
            "resolution_method": "manual_override",
            "confidence": ResolutionConfidence.MANUAL_OVERRIDE,
        }

    # Also try partial override key match (for subsidiaries with slightly different names)
    awardee_head = (normalized.split() or [""])[0]
    for override_key, entry in overrides.items():
        if override_key and normalized and len(override_key) > 6:
            override_head = (override_key.split() or [""])[0]
            # Guard: require leading token agreement to avoid mapping generic
            # suffixes ("federal services", "security solutions", "construction")
            # to unrelated public tickers.
            if awardee_head and override_head and awardee_head == override_head:
                if override_key in normalized or (normalized in override_key and len(normalized) >= 6):
                    logger.debug("Resolved '%s' via partial override '%s' → %s", awardee_name_raw, override_key, entry.get("ticker"))
                    return {
                        "ticker": entry.get("ticker"),
                        "parent_company_name": entry.get("parent_company_name") or awardee_name_raw,
                        "resolution_method": "manual_override_partial",
                        "confidence": ResolutionConfidence.MANUAL_OVERRIDE,
                    }

    # 2. SEC exact match
    ticker_map = get_ticker_map()
    if normalized in ticker_map:
        entry = ticker_map[normalized]
        logger.debug("Resolved '%s' via SEC exact → %s", awardee_name_raw, entry["ticker"])
        return {
            "ticker": entry["ticker"],
            "parent_company_name": entry["company_name"],
            "resolution_method": "sec_exact",
            "confidence": ResolutionConfidence.HIGH_CONFIDENCE,
        }

    # 3. SEC fuzzy match — only if normalized name is long enough to be discriminating
    if ticker_map and len(normalized) >= _MIN_NORM_LEN:
        candidates = process.extract(
            normalized,
            ticker_map.keys(),
            scorer=fuzz.WRatio,
            limit=_MAX_FUZZY_CANDIDATES,
        )
        awardee_head = (normalized.split() or [""])[0]
        for match_key, score, _ in candidates:
            if score >= _FUZZY_THRESHOLD:
                # Extra guard: reject if matched key is very short (generic word)
                if len(match_key) < _MIN_NORM_LEN:
                    continue
                # Head-token guard. WRatio contains a partial-ratio term, so a
                # single shared generic word clears the threshold on its own:
                # "LS TECHNOLOGIES LLC" normalizes to "ls technologies" and
                # scored 95 against "technologies" — the key left over after
                # "GLOBAL TECHNOLOGIES LTD" loses its suffix words — mapping a
                # private FAA contractor onto an unrelated penny stock.
                # Requiring the leading (most distinctive) token to agree is
                # the same guard the yfinance fallback already applies.
                if awardee_head != (match_key.split() or [""])[0]:
                    logger.debug(
                        "Rejected SEC fuzzy '%s' → '%s' (score=%d): head token differs",
                        normalized, match_key, score,
                    )
                    continue
                entry = ticker_map[match_key]
                conf = (
                    ResolutionConfidence.HIGH_CONFIDENCE
                    if score >= 98
                    else ResolutionConfidence.MEDIUM_CONFIDENCE
                )
                logger.debug(
                    "Resolved '%s' via SEC fuzzy (score=%d) → %s (%s)",
                    awardee_name_raw, score, entry["ticker"], conf,
                )
                return {
                    "ticker": entry["ticker"],
                    "parent_company_name": entry["company_name"],
                    "resolution_method": f"sec_fuzzy_{score}",
                    "confidence": conf,
                }

    # 4. yfinance ticker symbol guess (last resort)
    guessed = _guess_ticker_yfinance(awardee_name_raw, normalized)
    if guessed:
        return guessed

    # 5. Unresolved
    logger.debug("Could not resolve '%s' to a ticker.", awardee_name_raw)
    return {
        "ticker": None,
        "parent_company_name": awardee_name_raw,
        "resolution_method": "unresolved",
        "confidence": ResolutionConfidence.UNRESOLVED,
    }


def _guess_ticker_yfinance(raw_name: str, normalized: str) -> dict | None:
    """Try yfinance.search() to find a ticker for the company name.

    The threshold is tight on purpose. Government contracts overwhelmingly go
    to private companies and nonprofits (Sabel Systems, Hanford Tank Waste,
    St Jude, etc.); a loose match would manufacture false-positive alerts that
    map a real public ticker (STEM, RSSS, OPRA …) to an unrelated private
    contractor. Better to return ticker=None and let the operator add an
    explicit override via `govaward add-override`.
    """
    try:
        import yfinance as yf
        # yfinance >= 0.2.40 has Ticker.search or we can use the search function
        results = yf.Search(raw_name[:60], max_results=3).quotes
    except Exception:
        return None

    if not results:
        return None

    # Tighter thresholds — false-positive mapping is the common failure mode.
    YF_PARTIAL_RATIO_MIN = 90    # was 75 — too loose; "stem" matches "systems"
    YF_TOKEN_RATIO_MIN   = 80    # require token-set overlap as a second check
    YF_TICKER_MIN_LEN    = 2     # reject single-letter tickers as too generic

    for result in results:
        name = result.get("longname") or result.get("shortname") or ""
        symbol = (result.get("symbol") or "").upper()
        quote_type = (result.get("quoteType") or "").upper()  # "EQUITY", "ETF", "MUTUALFUND", "CRYPTOCURRENCY", …
        exchange  = (result.get("exchange") or "").upper()    # "NMS", "NYQ", "NCM", …

        if not symbol or len(symbol) < YF_TICKER_MIN_LEN:
            continue
        # Only accept actual common-stock equities — skip ETFs, mutual funds,
        # crypto, indices, etc.
        if quote_type and quote_type != "EQUITY":
            continue
        # Skip pink-sheet / OTC tickers (they have very low volume and are
        # extremely unlikely to be the awardee on a $50M+ federal contract).
        if exchange in {"PNK", "OTC", "OEM", "OQX"}:
            continue

        norm_result = normalize_company_name(name)
        if not norm_result:
            continue

        partial = fuzz.partial_ratio(normalized, norm_result)
        token   = fuzz.token_set_ratio(normalized, norm_result)
        # Require BOTH a high partial-ratio AND a high token-set overlap. This
        # rejects matches where one name is just a substring of the other but
        # the rest of the words are unrelated.
        if partial < YF_PARTIAL_RATIO_MIN or token < YF_TOKEN_RATIO_MIN:
            continue

        # Final guard: the matched ticker symbol must appear (case-insensitive)
        # somewhere in the resolved name OR the resolved name must share its
        # FIRST significant token with the awardee. This catches cases like
        # awardee="Sabel Systems" matching yf result "Stem, Inc." where the
        # symbol "STEM" doesn't appear in the awardee at all.
        first_awardee_token = (normalized.split() or [""])[0]
        first_result_token  = (norm_result.split() or [""])[0]
        if first_awardee_token != first_result_token and symbol.lower() not in normalized:
            continue

        logger.debug(
            "Resolved '%s' via yfinance search → %s (partial=%d token=%d)",
            raw_name, symbol, partial, token,
        )
        return {
            "ticker": symbol,
            "parent_company_name": name,
            "resolution_method": f"yfinance_search_p{partial}_t{token}",
            "confidence": ResolutionConfidence.LOW_CONFIDENCE,
        }
    return None


def add_override(awardee_name: str, ticker: str, parent_name: str) -> None:
    """Append a new override to manual_ticker_overrides.yaml."""
    settings = get_settings()
    override_file = settings.data_dir / "manual_ticker_overrides.yaml"

    if override_file.exists():
        raw = yaml.safe_load(override_file.read_text(encoding="utf-8")) or {}
    else:
        raw = {}

    overrides = raw.setdefault("overrides", {})
    overrides[awardee_name.lower()] = {
        "ticker": ticker.upper(),
        "parent_company_name": parent_name,
    }

    override_file.write_text(yaml.dump(raw, default_flow_style=False), encoding="utf-8")
    global _override_cache
    _override_cache = None  # invalidate cache
    logger.info("Added override: '%s' → %s", awardee_name, ticker)
