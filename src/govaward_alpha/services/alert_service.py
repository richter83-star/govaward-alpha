"""Alert creation and notification dispatch."""

from __future__ import annotations

import json
import logging
import sqlite3

from govaward_alpha import db
from govaward_alpha.enrich.claude_enricher import ClaudeAnalysis, enrich
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
    Optionally enrich with Claude, persist an alert record, and dispatch to all configured channels.
    Returns True if the alert was created.
    """
    # Claude enrichment — runs before persistence so analysis is included in output
    claude_analysis: ClaudeAnalysis | None = None
    try:
        claude_analysis = enrich(award, company, result)
        if claude_analysis:
            logger.info(
                "Claude enrichment: resolver=%s quality=%s",
                claude_analysis.resolver_confidence,
                claude_analysis.contract_quality,
            )
            # Low-confidence resolver match — demote but don't suppress entirely
            if claude_analysis.resolver_confidence == "low":
                logger.warning(
                    "Claude flagged low resolver confidence for %s ← %s: %s",
                    award.ticker, award.awardee_name_raw, claude_analysis.resolver_note,
                )
    except Exception as exc:
        logger.warning("Claude enrichment error: %s", exc)

    claude_json = json.dumps(claude_analysis.to_dict()) if claude_analysis else ""

    alert = AlertRecord(
        award_id=award_id,
        ticker=award.ticker or "UNRESOLVED",
        materiality_score=result.score,
        alert_level=result.level,
        reasons_json=json.dumps(result.reasons),
        claude_analysis_json=claude_json,
    )

    alert_id = db.insert_alert(conn, alert)
    conn.commit()

    # Console (always on)
    try:
        console.print_alert(award, company, result, claude_analysis)
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
