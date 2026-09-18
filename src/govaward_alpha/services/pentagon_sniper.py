"""Pentagon 5:00 PM ET DoD Contract Digest Sniper.

Automates ingestion, analysis, and after-hours execution of Department of Defense
daily contract announcements published at ~5:00 PM ET on Defense.gov / War.gov.
Bypasses retail news lag by executing Extended-Hours limit orders before overnight
and morning press releases trigger retail gaps.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from govaward_alpha import db
from govaward_alpha.config import get_settings
from govaward_alpha.enrich.award_enricher import enrich_award, should_alert
from govaward_alpha.models import Award, Company, ScoreResult
from govaward_alpha.scoring import score_award
from govaward_alpha.services.alert_service import process_alert
from govaward_alpha.services.trade_engine import AlphaPlayDetector, trade_engine
from govaward_alpha.sources import defense_daily
from govaward_alpha.timeutil import get_eastern_time, get_market_session, is_extended_hours, is_pentagon_drop_window

logger = logging.getLogger(__name__)


class PentagonSniper:
    """High-frequency sniper for Pentagon daily contract announcements."""

    def __init__(self, drop_dir: Path | None = None) -> None:
        settings = get_settings()
        self.drop_dir = drop_dir or (settings.data_dir / "pentagon_drops")
        self.drop_dir.mkdir(parents=True, exist_ok=True)

    def find_latest_local_drop(self) -> Path | None:
        """Find the most recent file in the pentagon_drops directory."""
        candidates = list(self.drop_dir.glob("*.html")) + list(self.drop_dir.glob("*.htm")) + list(self.drop_dir.glob("*.txt"))
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    def snipe(
        self,
        conn: Any,
        file_path: str | Path | None = None,
        lookback_days: int = 1,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """
        Execute an immediate snipe pass against Defense.gov contract announcements.

        Steps:
          1. Ingest DoD contract awards from network or local file.
          2. Deduplicate against database.
          3. Enrich with parent company & subsidiary ticker resolution.
          4. Score materiality and check FAR 4.1603 net-new revenue rules.
          5. Detect Alpha Plays (catalyst scale >= min_ratio_pct of market cap).
          6. Submit Extended-Hours Limit Orders via TradeEngine.
        """
        settings = get_settings()
        target_file = Path(file_path) if file_path else self.find_latest_local_drop()
        session_name = get_market_session()
        et_now = get_eastern_time()

        logger.info(
            "Pentagon Sniper activated [ET: %s | Session: %s | Source: %s]",
            et_now.strftime("%Y-%m-%d %H:%M:%S"),
            session_name,
            target_file.name if target_file else "Defense.gov Network",
        )

        results: dict[str, Any] = {
            "session": session_name,
            "eastern_time": et_now.isoformat(),
            "source_file": str(target_file) if target_file else None,
            "awards_detected": 0,
            "inserted_awards": 0,
            "tickers_matched": [],
            "alpha_plays": [],
            "orders_placed": [],
            "errors": [],
        }

        # 1. Fetch DoD contract awards
        try:
            if target_file and target_file.exists():
                awards = defense_daily.fetch_awards(file_path=target_file)
            else:
                awards = defense_daily.fetch_awards(lookback_days=lookback_days)
        except Exception as exc:
            err = f"Failed fetching DoD awards: {exc}"
            logger.error(err)
            results["errors"].append(err)
            return results

        results["awards_detected"] = len(awards)
        if not awards:
            logger.info("No DoD awards returned in this pass.")
            return results

        # 2. Process each award through enrichment, scoring, and alpha detection
        for award in awards:
            try:
                award_id, status = db.upsert_award(conn, award)
                if status == db.INSERTED:
                    results["inserted_awards"] += 1

                # Enrich (resolves subsidiaries to parent tickers using manual_ticker_overrides.yaml)
                enriched_award, company = enrich_award(award, conn)
                enriched_award = enriched_award.model_copy(update={"id": award_id})

                # Score
                score_result = score_award(enriched_award, company)
                enriched_award.materiality_score = score_result.score
                enriched_award.alert_level = score_result.level

                # Update award row
                conn.execute(
                    "UPDATE awards SET ticker=?, ticker_confidence=?, parent_company_name=?, awardee_name_normalized=?, "
                    "materiality_score=?, alert_level=? WHERE id=?",
                    (
                        enriched_award.ticker,
                        enriched_award.ticker_confidence,
                        enriched_award.parent_company_name,
                        enriched_award.awardee_name_normalized,
                        score_result.score,
                        score_result.level,
                        award_id,
                    ),
                )

                if enriched_award.ticker:
                    results["tickers_matched"].append(f"{enriched_award.ticker} (${award.award_amount:,.0f})")

                # Check Alpha Play setup
                cfg = db.get_trade_config(conn)
                min_ratio = cfg.get("min_ratio_pct", 10.0)
                is_alpha, reason, ratio = AlphaPlayDetector.is_alpha_play(
                    enriched_award, company, score_result, min_ratio_pct=min_ratio
                )

                if is_alpha and enriched_award.ticker:
                    alpha_item = {
                        "award_id": award_id,
                        "ticker": enriched_award.ticker,
                        "amount": award.award_amount,
                        "ratio_pct": ratio,
                        "reason": reason,
                        "agency": award.sub_agency or award.agency,
                    }
                    results["alpha_plays"].append(alpha_item)
                    logger.info(
                        "🎯 ALPHA PLAY DETECTED: %s | $%.2fM (%.1f%% of market cap) | %s",
                        enriched_award.ticker,
                        award.award_amount / 1_000_000.0,
                        ratio,
                        reason,
                    )

                    # Trigger alert dispatch if not already alerted
                    if not db.alert_exists_for_award(conn, award_id):
                        process_alert(conn, award_id, enriched_award, company, score_result)

                    # Execute or stage trade
                    if not dry_run:
                        staged_order = trade_engine.evaluate_and_stage_award(
                            conn, enriched_award, company, score_result
                        )
                        if staged_order:
                            results["orders_placed"].append(staged_order)
                    else:
                        results["orders_placed"].append({
                            "ticker": enriched_award.ticker,
                            "status": "DRY_RUN_QUALIFIED",
                            "ratio_pct": ratio,
                        })

            except Exception as exc:
                err = f"Error evaluating award '{award.awardee_name_raw}': {exc}"
                logger.error(err, exc_info=True)
                results["errors"].append(err)

        conn.commit()
        return results

    def watch(
        self,
        conn: Any,
        poll_interval_seconds: int = 15,
        max_duration_minutes: int = 35,
        stop_on_trade: bool = False,
        dry_run: bool = False,
        file_path: str | Path | None = None,
        on_pulse: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Actively monitor during the 4:55–5:30 PM ET Pentagon drop window.
        Polls every poll_interval_seconds until window closes or a contract drop is sniped.
        """
        start_time = time.time()
        end_time = start_time + (max_duration_minutes * 60)
        history: list[dict[str, Any]] = []

        logger.info(
            "Starting Pentagon Sniper Watcher (Interval: %ds, Max Duration: %dm, Window check: active)",
            poll_interval_seconds,
            max_duration_minutes,
        )

        while time.time() < end_time:
            now_et = get_eastern_time()
            in_window = is_pentagon_drop_window()

            status_msg = f"[{now_et.strftime('%H:%M:%S')} ET] Window: {'OPEN' if in_window else 'STANDBY'} | Polling..."
            if on_pulse:
                on_pulse(status_msg)

            # Perform a snipe pass
            res = self.snipe(conn, file_path=file_path, lookback_days=1, dry_run=dry_run)
            if res.get("awards_detected", 0) > 0 or res.get("alpha_plays"):
                history.append(res)
                if stop_on_trade and (res.get("orders_placed") or res.get("alpha_plays")):
                    logger.info("Alpha play detected or order executed; terminating watch loop.")
                    break

            time.sleep(poll_interval_seconds)

        return history


pentagon_sniper = PentagonSniper()
