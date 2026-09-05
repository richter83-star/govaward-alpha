"""SEC public company/ticker mapping.

Downloads and caches https://www.sec.gov/files/company_tickers.json
(no authentication required).
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from govaward_alpha.config import get_settings
from govaward_alpha.normalize import normalize_company_name
from govaward_alpha.timeutil import utcfromtimestamp, utcnow

logger = logging.getLogger(__name__)

_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_CACHE_FILE = "sec_tickers.json"
_CACHE_TTL_HOURS = 24

_UA_PRODUCT = "GovAwardAlpha/0.1"


def _headers() -> dict[str, str]:
    """SEC fair-access headers.

    EDGAR asks automated clients to identify themselves with a contact
    address. We send whatever the operator configured via SEC_CONTACT_EMAIL
    and fall back to the bare product token rather than shipping a fake
    address, which is what SEC actually blocks on.
    """
    contact = get_settings().sec_contact_email.strip()
    ua = f"{_UA_PRODUCT} {contact}" if contact else _UA_PRODUCT
    return {"User-Agent": ua, "Accept-Encoding": "gzip"}


def _cache_path() -> Path:
    return get_settings().cache_path / _CACHE_FILE


def _is_stale() -> bool:
    p = _cache_path()
    if not p.exists():
        return True
    age = utcnow() - utcfromtimestamp(p.stat().st_mtime)
    return age > timedelta(hours=_CACHE_TTL_HOURS)


def _download_tickers() -> dict[str, Any]:
    logger.info("Downloading SEC company tickers from %s", _SEC_TICKERS_URL)
    with httpx.Client() as client:
        resp = client.get(_SEC_TICKERS_URL, headers=_headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()


def load_sec_tickers(force_refresh: bool = False) -> dict[str, dict]:
    """
    Return a dict mapping normalized_company_name → {ticker, company_name, cik}.
    Refreshes cache if stale or force_refresh=True.
    """
    cache = _cache_path()

    if force_refresh or _is_stale():
        try:
            raw = _download_tickers()
            cache.write_text(json.dumps(raw), encoding="utf-8")
        except Exception as exc:
            logger.warning("SEC ticker download failed: %s", exc)
            if cache.exists():
                logger.info("Using stale SEC ticker cache.")
                raw = json.loads(cache.read_text(encoding="utf-8"))
            else:
                logger.error("No SEC ticker cache available.")
                return {}
    else:
        raw = json.loads(cache.read_text(encoding="utf-8"))

    # raw is { "0": {cik_str, ticker, title}, "1": {...}, ... }
    mapping: dict[str, dict] = {}
    for entry in raw.values():
        ticker = str(entry.get("ticker") or "").upper()
        name = entry.get("title") or ""
        cik = entry.get("cik_str") or entry.get("cik") or ""
        if not ticker or not name:
            continue
        norm = normalize_company_name(name)
        if norm:
            mapping[norm] = {"ticker": ticker, "company_name": name, "cik": cik}

    logger.debug("SEC tickers loaded: %d entries", len(mapping))
    return mapping


# Module-level cache to avoid repeated file reads within one process
_ticker_map: dict[str, dict] | None = None


def get_ticker_map(force_refresh: bool = False) -> dict[str, dict]:
    global _ticker_map
    if _ticker_map is None or force_refresh:
        _ticker_map = load_sec_tickers(force_refresh)
    return _ticker_map
