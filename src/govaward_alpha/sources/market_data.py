"""Market data enrichment via yfinance.

Pulls: ticker, company_name, market_cap, trailing_revenue,
       avg_volume, price, sector, industry, exchange.

Results are cached per-ticker for a configurable TTL to avoid hammering Yahoo.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from govaward_alpha.config import get_settings
from govaward_alpha.models import Company
from govaward_alpha.timeutil import utcfromtimestamp, utcnow

logger = logging.getLogger(__name__)

_CACHE_TTL_HOURS = 6


def _cache_path(ticker: str) -> Path:
    return get_settings().cache_path / f"mktdata_{ticker.upper()}.json"


def _load_cache(ticker: str) -> dict | None:
    p = _cache_path(ticker)
    if not p.exists():
        return None
    age = utcnow() - utcfromtimestamp(p.stat().st_mtime)
    if age > timedelta(hours=_CACHE_TTL_HOURS):
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_cache(ticker: str, data: dict) -> None:
    p = _cache_path(ticker)
    try:
        p.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        logger.debug("Market data cache write failed for %s: %s", ticker, exc)


def _safe_float(val: Any) -> float | None:
    if val is None:
        return None
    try:
        f = float(val)
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def fetch_market_data(ticker: str) -> Company | None:
    """
    Fetch market data for a ticker symbol. Returns None if data unavailable.
    Caches results to .cache/mktdata_<TICKER>.json.
    """
    ticker = ticker.upper().strip()
    if not ticker:
        return None

    cached = _load_cache(ticker)
    if cached:
        logger.debug("Market data cache hit for %s", ticker)
        return _dict_to_company(cached)

    try:
        import yfinance as yf
    except ImportError:
        logger.error("yfinance not installed — run: pip install yfinance")
        return None

    try:
        yfobj = yf.Ticker(ticker)
        info = yfobj.info or {}
    except Exception as exc:
        logger.warning("yfinance fetch failed for %s: %s", ticker, exc)
        return None

    if not info:
        return None

    data = {
        "ticker": ticker,
        "company_name": info.get("longName") or info.get("shortName") or ticker,
        "market_cap": _safe_float(info.get("marketCap")),
        "trailing_revenue": _safe_float(info.get("totalRevenue")),
        "avg_volume": _safe_float(info.get("averageVolume") or info.get("averageDailyVolume10Day")),
        "price": _safe_float(info.get("currentPrice") or info.get("regularMarketPrice")),
        "sector": info.get("sector") or "",
        "industry": info.get("industry") or "",
        "exchange": info.get("exchange") or "",
        "last_market_data_refresh": utcnow().isoformat(),
    }

    _save_cache(ticker, data)
    logger.debug("Market data fetched for %s: mktcap=%s", ticker, data.get("market_cap"))
    return _dict_to_company(data)


def _dict_to_company(d: dict) -> Company:
    refresh = d.get("last_market_data_refresh")
    refresh_dt = datetime.fromisoformat(refresh) if refresh else None
    return Company(
        ticker=d["ticker"],
        company_name=d.get("company_name") or d["ticker"],
        market_cap=d.get("market_cap"),
        trailing_revenue=d.get("trailing_revenue"),
        avg_volume=d.get("avg_volume"),
        price=d.get("price"),
        sector=d.get("sector") or "",
        industry=d.get("industry") or "",
        exchange=d.get("exchange") or "",
        last_market_data_refresh=refresh_dt,
    )
