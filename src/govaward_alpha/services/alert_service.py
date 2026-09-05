"""Alert creation and notification dispatch."""

from __future__ import annotations

import json
import logging
import sqlite3

from govaward_alpha import db
from govaward_alpha.enrich.gemini_enricher import GeminiAnalysis, enrich
from govaward_alpha.models import AlertRecord, Award, Company, ScoreResult
from govaward_alpha.notify import console, discord_notifier, email_notifier, telegram_notifier

logger = logging.getLogger(__name__)


def process_alert(
    conn: sqlite3.Connection,
    award_id: int,
    award: Award,
    company: Company | None,
    result: ScoreResult,
) -> bool:
    """
    Optionally enrich with Google Gemini, persist an alert record, and dispatch to all configured channels.
    Returns True if the alert was created.
    """
    # Gemini enrichment — runs before persistence so analysis is included in output
    gemini_analysis: GeminiAnalysis | None = None
    try:
        gemini_analysis = enrich(award, company, result)
        if gemini_analysis:
            logger.info(
                "Gemini enrichment: resolver=%s quality=%s",
                gemini_analysis.resolver_confidence,
                gemini_analysis.contract_quality,
            )
            # Low-confidence resolver match — demote but don't suppress entirely
            if gemini_analysis.resolver_confidence == "low":
                logger.warning(
                    "Gemini flagged low resolver confidence for %s ← %s: %s",
                    award.ticker, award.awardee_name_raw, gemini_analysis.resolver_note,
                )
    except Exception as exc:
        logger.warning("Gemini enrichment error: %s", exc)

    gemini_json = json.dumps(gemini_analysis.to_dict()) if gemini_analysis else ""

    alert = AlertRecord(
        award_id=award_id,
        ticker=award.ticker or "UNRESOLVED",
        materiality_score=result.score,
        alert_level=result.level,
        reasons_json=json.dumps(result.reasons),
        gemini_analysis_json=gemini_json,
        claude_analysis_json=gemini_json,
    )

    alert_id = db.insert_alert(conn, alert)
    conn.commit()

    # Console (always on)
    try:
        console.print_alert(award, company, result, gemini_analysis)
        db.mark_alert_sent(conn, alert_id, "console")
        conn.commit()
    except Exception as exc:
        logger.warning("Console print failed: %s", exc)

    # Email
    try:
        if email_notifier.send_alert(award, company, result):
            db.mark_alert_sent(conn, alert_id, "email")
            conn.commit()
    except Exception as exc:
        logger.warning("Email notify failed: %s", exc)

    # Telegram
    try:
        if telegram_notifier.send_alert(award, company, result):
            db.mark_alert_sent(conn, alert_id, "telegram")
            conn.commit()
    except Exception as exc:
        logger.warning("Telegram notify failed: %s", exc)

    # Discord
    try:
        if discord_notifier.send_alert(award, company, result):
            db.mark_alert_sent(conn, alert_id, "discord")
            conn.commit()
    except Exception as exc:
        logger.warning("Discord notify failed: %s", exc)

    logger.info(
        "Alert #%d created — %s score=%d level=%s",
        alert_id, award.ticker, result.score, result.level,
    )
    return True
