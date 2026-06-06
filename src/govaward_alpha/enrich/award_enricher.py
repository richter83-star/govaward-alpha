"""Award enrichment pipeline.

Takes a raw Award object and:
  1. Resolves the awardee to a ticker via company_resolver.
  2. Fetches market data for the resolved ticker.
  3. Returns an enriched Award and a Company (or None if unresolved).
"""

from __future__ import annotations

import logging
import sqlite3

from govaward_alpha import db
from govaward_alpha.enrich.company_resolver import get_watchlist, resolve
from govaward_alpha.models import Award, Company
from govaward_alpha.sources.market_data import fetch_market_data

logger = logging.getLogger(__name__)


def enrich_award(award: Award, conn: sqlite3.Connection) -> tuple[Award, Company | None]:
    """
    Enrich an award with ticker resolution and market data.

    Returns:
        (enriched_award, company_or_none)
    """
    # Resolve ticker
    resolution = resolve(award.awardee_name_raw)
    ticker = resolution["ticker"]

    enriched = award.model_copy(update={
        "ticker": ticker,
        "parent_company_name": resolution["parent_company_name"] or award.awardee_name_raw,
    })

    if not ticker:
        logger.debug("No ticker resolved for '%s'", award.awardee_name_raw)
        return enriched, None

    # Check watchlist — if ticker is on watchlist, always proceed regardless of market cap
    watchlist = get_watchlist()
    on_watchlist = ticker.upper() in watchlist

    # Fetch or use cached market data
    existing_row = db.get_company(conn, ticker)
    company: Company | None = None

    if existing_row:
        from datetime import datetime, timedelta
        refresh_str = existing_row["last_market_data_refresh"]
        if refresh_str:
            age = datetime.utcnow() - datetime.fromisoformat(refresh_str)
            if age < timedelta(hours=6):
                company = Company(
                    ticker=existing_row["ticker"],
                    company_name=existing_row["company_name"] or ticker,
                    market_cap=existing_row["market_cap"],
                    trailing_revenue=existing_row["trailing_revenue"],
                    avg_volume=existing_row["avg_volume"],
                    price=existing_row["price"],
                    sector=existing_row["sector"] or "",
                    industry=existing_row["industry"] or "",
                    exchange=existing_row["exchange"] or "",
                    last_market_data_refresh=datetime.fromisoformat(refresh_str),
                )

    if company is None:
        company = fetch_market_data(ticker)
        if company:
            db.upsert_company(conn, company)

    if company is None:
        logger.debug("No market data for %s", ticker)

    return enriched, company


def should_alert(
    award: Award,
    company: Company | None,
    score: int,
    min_score: int,
    market_cap_max: float,
) -> tuple[bool, str]:
    """
    Decide if this award should generate an alert.

    Returns (should_alert, reason_if_not).
    """
    watchlist = get_watchlist()
    on_watchlist = (award.ticker or "").upper() in watchlist

    if not award.ticker:
        return False, "No resolved ticker"

    # Mega-cap hard ignore (unless on watchlist)
    if company and company.market_cap and company.market_cap > market_cap_max and not on_watchlist:
        return False, f"Market cap ${company.market_cap/1e9:.1f}B exceeds max filter"

    # Missing amount — only alert if obligated amount meets threshold
    if award.award_amount is None:
        from govaward_alpha.config import get_settings
        settings = get_settings()
        if not award.obligated_amount or award.obligated_amount < settings.award_min_amount:
            return False, "Award amount missing and obligated amount below threshold"

    if score < min_score and not on_watchlist:
        return False, f"Score {score} below threshold {min_score}"

    return True, ""
