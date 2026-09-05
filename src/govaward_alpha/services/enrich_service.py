"""Backfill Google Gemini analysis on existing alerts.

The normal alert pipeline only runs Gemini enrichment when a NEW alert fires
(see ``services.alert_service.process_alert``). Alerts that were created
before ``GEMINI_API_KEY`` was configured therefore have an empty
``gemini_analysis_json`` column. This module backfills them.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from dataclasses import dataclass

from govaward_alpha import db
from govaward_alpha.config import get_settings
from govaward_alpha.enrich.gemini_enricher import enrich as gemini_enrich
from govaward_alpha.models import Award, Company, ScoreResult

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnrichStats:
    candidates: int
    enriched: int
    failed: int
    skipped_no_key: bool


def _row_to_award(r: sqlite3.Row) -> Award:
    """Materialize an ``Award`` from a joined alert+award row."""
    from datetime import date

    def _d(s: str | None) -> date | None:
        if not s:
            return None
        try:
            return date.fromisoformat(s[:10])
        except ValueError:
            return None

    return Award(
        source=r["source"],
        source_award_id=r["source_award_id"],
        source_url=r["source_url"] or "",
        posted_date=_d(r["posted_date"]),
        award_date=_d(r["award_date"]),
        awardee_name_raw=r["awardee_name_raw"] or "",
        awardee_name_normalized=r["awardee_name_normalized"] or "",
        parent_company_name=r["parent_company_name"] or "",
        ticker=r["ticker"],
        agency=r["agency"] or "",
        sub_agency=r["sub_agency"] or "",
        naics=r["naics"] or "",
        psc=r["psc"] or "",
        award_amount=r["award_amount"],
        obligated_amount=r["obligated_amount"],
        description=r["description"] or "",
        contract_number=r["contract_number"] or "",
        award_type=r["award_type"] or "",
        is_idiq=bool(r["is_idiq"]),
        is_modification=bool(r["is_modification"]),
        is_option_exercise=bool(r["is_option_exercise"]),
        is_ceiling_only=bool(r["is_ceiling_only"]),
    )


def _row_to_company(conn: sqlite3.Connection, ticker: str) -> Company | None:
    if not ticker or ticker == "UNRESOLVED":
        return None
    r = db.get_company(conn, ticker)
    if not r:
        return None
    return Company(
        ticker=r["ticker"],
        company_name=r["company_name"] or "",
        market_cap=r["market_cap"],
        trailing_revenue=r["trailing_revenue"],
        avg_volume=r["avg_volume"],
        price=r["price"],
        sector=r["sector"] or "",
        industry=r["industry"] or "",
        exchange=r["exchange"] or "",
    )


def _row_to_score_result(r: sqlite3.Row) -> ScoreResult:
    reasons = json.loads(r["reasons_json"] or "[]")
    return ScoreResult(
        score=r["materiality_score"],
        level=r["alert_level"],
        reasons=reasons,
        breakdown={},
    )


def backfill_gemini_analysis(
    limit: int | None = None,
    sleep_seconds: float = 0.5,
) -> EnrichStats:
    """Run Google Gemini enrichment on alerts that don't already have it.

    Args:
        limit: Maximum number of alerts to enrich in this run (None = all).
        sleep_seconds: Delay between API calls to stay friendly with rate limits.

    Returns:
        EnrichStats describing what happened.
    """
    settings = get_settings()
    api_key = settings.gemini_api_key or settings.anthropic_api_key
    if not api_key:
        logger.warning(
            "GEMINI_API_KEY not configured — skipping backfill. "
            "Set it in .env and try again."
        )
        return EnrichStats(candidates=0, enriched=0, failed=0, skipped_no_key=True)

    sql = """
        SELECT al.id as alert_id, al.materiality_score, al.alert_level,
               al.reasons_json, aw.*
        FROM alerts al
        JOIN awards aw ON al.award_id = aw.id
        WHERE ((al.gemini_analysis_json IS NULL OR al.gemini_analysis_json = '')
               AND (al.claude_analysis_json IS NULL OR al.claude_analysis_json = ''))
        ORDER BY al.created_at DESC
    """
    if limit and limit > 0:
        sql += f" LIMIT {int(limit)}"

    with db.get_connection() as conn:
        rows = conn.execute(sql).fetchall()
        candidates = len(rows)
        if not candidates:
            logger.info("No alerts need Gemini enrichment — already up to date.")
            return EnrichStats(candidates=0, enriched=0, failed=0, skipped_no_key=False)

        logger.info(
            "Backfilling Gemini analysis for %d alert(s) (sleep=%.1fs between calls)",
            candidates, sleep_seconds,
        )

        enriched = 0
        failed = 0
        for i, r in enumerate(rows, 1):
            try:
                award = _row_to_award(r)
                company = _row_to_company(conn, r["ticker"])
                result = _row_to_score_result(r)
                analysis = gemini_enrich(award, company, result)
                if analysis is None:
                    failed += 1
                    logger.warning(
                        "Alert #%d: enrich returned None (likely API failure or unparseable response)",
                        r["alert_id"],
                    )
                else:
                    payload = json.dumps(analysis.to_dict())
                    conn.execute(
                        "UPDATE alerts SET gemini_analysis_json=?, claude_analysis_json=? WHERE id=?",
                        (payload, payload, r["alert_id"]),
                    )
                    conn.commit()
                    enriched += 1
                    logger.info(
                        "Alert #%d (%s): enriched — resolver=%s quality=%s",
                        r["alert_id"], r["ticker"],
                        analysis.resolver_confidence, analysis.contract_quality,
                    )
            except Exception as exc:
                failed += 1
                logger.error("Alert #%d enrichment failed: %s", r["alert_id"], exc)

            if i < candidates and sleep_seconds > 0:
                time.sleep(sleep_seconds)

        logger.info(
            "Backfill done — candidates=%d enriched=%d failed=%d",
            candidates, enriched, failed,
        )
        return EnrichStats(
            candidates=candidates,
            enriched=enriched,
            failed=failed,
            skipped_no_key=False,
        )


# Backward-compatible alias
backfill_claude_analysis = backfill_gemini_analysis
