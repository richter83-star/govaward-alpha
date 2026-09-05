"""Scan service — orchestrates source fetching, enrichment, scoring, and alert dispatch."""

from __future__ import annotations

import logging
from typing import Callable

from govaward_alpha import db
from govaward_alpha.config import get_settings
from govaward_alpha.enrich.award_enricher import enrich_award, should_alert
from govaward_alpha.models import Award
from govaward_alpha.scoring import score_award
from govaward_alpha.services.alert_service import process_alert

logger = logging.getLogger(__name__)


def run_source(
    source_name: str,
    fetch_fn: Callable[[], list[Award]],
    lookback_days: int | None = None,
) -> dict[str, int]:
    """
    Run a single source adapter end-to-end:
      fetch → dedupe → enrich → score → alert

    Returns stats dict with seen/inserted/updated counts.
    """
    settings = get_settings()
    stats = {"seen": 0, "inserted": 0, "updated": 0, "unchanged": 0, "alerted": 0, "errors": 0}

    with db.get_connection() as conn:
        run_id = db.start_run(conn, source_name)

    try:
        if lookback_days is not None:
            import inspect
            sig = inspect.signature(fetch_fn)
            if "lookback_days" in sig.parameters:
                awards = fetch_fn(lookback_days=lookback_days)  # type: ignore[call-arg]
            else:
                awards = fetch_fn()
        else:
            awards = fetch_fn()
    except Exception as exc:
        logger.error("Source %s fetch failed: %s", source_name, exc)
        with db.get_connection() as conn:
            db.finish_run(conn, run_id, "error", 0, 0, 0, str(exc))
        return stats

    stats["seen"] = len(awards)
    logger.info("%s: %d awards fetched", source_name, len(awards))

    with db.get_connection() as conn:
        for award in awards:
            try:
                award_id, status = db.upsert_award(conn, award)
                if status == db.INSERTED:
                    stats["inserted"] += 1
                elif status == db.UPDATED:
                    stats["updated"] += 1
                else:
                    stats["unchanged"] += 1
                is_new = status == db.INSERTED

                # Enrich with company/ticker/market data
                enriched_award, company = enrich_award(award, conn)
                # Score
                result = score_award(enriched_award, company)
                enriched_award.materiality_score = result.score
                enriched_award.alert_level = result.level

                # Persist enriched fields and score back to awards row
                conn.execute(
                    "UPDATE awards SET ticker=?, parent_company_name=?, awardee_name_normalized=?, "
                    "materiality_score=?, alert_level=? WHERE id=?",
                    (
                        enriched_award.ticker,
                        enriched_award.parent_company_name,
                        enriched_award.awardee_name_normalized,
                        result.score,
                        result.level,
                        award_id,
                    ),
                )

                # Alert decision
                fire, reason = should_alert(
                    enriched_award,
                    company,
                    result.score,
                    settings.alert_min_score,
                    settings.market_cap_max,
                )

                if fire and not db.alert_exists_for_award(conn, award_id):
                    alerted = process_alert(conn, award_id, enriched_award, company, result)
                    if alerted:
                        stats["alerted"] += 1
                elif not fire and is_new:
                    logger.debug(
                        "No alert for '%s': %s",
                        award.awardee_name_raw[:60],
                        reason,
                    )

            except Exception as exc:
                logger.error("Error processing award '%s': %s", award.awardee_name_raw, exc, exc_info=True)
                stats["errors"] += 1

        db.finish_run(
            conn, run_id, "success",
            stats["seen"], stats["inserted"], stats["updated"],
        )

    logger.info(
        "%s done — seen=%d inserted=%d updated=%d unchanged=%d alerted=%d errors=%d",
        source_name, stats["seen"], stats["inserted"], stats["updated"],
        stats["unchanged"], stats["alerted"], stats["errors"],
    )
    return stats


def run_all(lookback_days: int | None = None) -> dict[str, dict]:
    """Run all enabled source adapters."""
    from govaward_alpha.sources import sam_gov, defense_daily, usaspending

    results: dict[str, dict] = {}

    results["sam_gov"] = run_source("sam_gov", sam_gov.fetch_awards, lookback_days)
    results["defense_daily"] = run_source("defense_daily", defense_daily.fetch_awards, lookback_days)
    results["usaspending"] = run_source("usaspending", usaspending.fetch_recent_awards, lookback_days)

    return results
