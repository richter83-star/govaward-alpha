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


def in_evening_watch(dt: datetime | None = None) -> bool:
    """Weekdays 4:45–7:30 PM ET: when the cron-driven sniper is allowed to touch the network."""
    et = get_eastern_time(dt)
    return et.weekday() < 5 and (16, 45) <= (et.hour, et.minute) <= (19, 30)


logger = logging.getLogger(__name__)


class PentagonSniper:
    """High-frequency sniper for Pentagon daily contract announcements."""

    def __init__(self, drop_dir: Path | None = None) -> None:
        settings = get_settings()
        self.drop_dir = drop_dir or (settings.data_dir / "pentagon_drops")
        self.drop_dir.mkdir(parents=True, exist_ok=True)

    # A drop older than this is stale: it was either processed already or is not
    # today's page. (Processed drops are also moved into ``processed/``.)
    MAX_DROP_AGE_HOURS = 20

    def find_latest_local_drop(self) -> Path | None:
        """The newest *fresh* unprocessed file in the pentagon_drops directory."""
        candidates = list(self.drop_dir.glob("*.html")) + list(self.drop_dir.glob("*.htm")) + list(self.drop_dir.glob("*.txt"))
        cutoff = time.time() - self.MAX_DROP_AGE_HOURS * 3600
        candidates = [p for p in candidates if p.stat().st_mtime >= cutoff]
        if not candidates:
            return None
        return max(candidates, key=lambda p: p.stat().st_mtime)

    # -- article state: which RSS articles were fetched, blocked, or covered by a drop
    @property
    def state_path(self) -> Path:
        return self.drop_dir / "_state.json"

    def load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save_state(self, state: dict[str, Any]) -> None:
        # keep the last ~30 articles
        items = sorted(state.items(), key=lambda kv: kv[1].get("date", ""), reverse=True)[:30]
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(items), indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def awaiting_drops(self) -> list[dict[str, Any]]:
        """Blocked articles nobody has pasted yet (newest first) — the dashboard shows these."""
        out = [{"url": u, **v} for u, v in self.load_state().items() if v.get("status") == "blocked"]
        return sorted(out, key=lambda r: r.get("date", ""), reverse=True)

    def _mark_dates_done(self, article_date: date, how: str) -> None:
        state = self.load_state()
        for v in state.values():
            if v.get("date") == str(article_date) and v.get("status") == "blocked":
                v.update(status="done", via=how, at=datetime.now().isoformat(timespec="seconds"))
        self._save_state(state)

    def _fetch_network(self, lookback_days: int, results: dict[str, Any]) -> list[Award]:
        """RSS -> unfetched articles. One 403 stops the pass; a blocked article is never re-requested."""
        state = self.load_state()
        awards: list[Award] = []
        for url, art_date in defense_daily._discover_article_urls(lookback_days):
            if state.get(url, {}).get("status") in ("done", "blocked"):
                continue
            now = datetime.now().isoformat(timespec="seconds")
            try:
                got = defense_daily.fetch_article_awards(url, art_date)
            except defense_daily.DefenseSiteBlocked:
                state[url] = {"status": "blocked", "date": str(art_date), "at": now}
                results["blocked_now"] = url
                logger.warning("war.gov blocked %s (dated %s). %s", url, art_date, defense_daily.DROP_HINT)
                break
            state[url] = {"status": "done", "date": str(art_date), "at": now, "via": "network", "awards": len(got)}
            results["articles_fetched"] = results.get("articles_fetched", 0) + 1
            awards.extend(got)
        self._save_state(state)
        results["awaiting_drops"] = self.awaiting_drops()
        return awards

    def ingest_drop_text(self, conn: Any, text: str, dry_run: bool = False) -> dict[str, Any]:
        """Save a pasted/uploaded contracts page into the drop folder and snipe it now."""
        is_html = "<" in text[:2000] and ">" in text[:2000]
        name = datetime.now().strftime("manual-%Y%m%d-%H%M%S") + (".html" if is_html else ".txt")
        path = self.drop_dir / name
        path.write_text(text, encoding="utf-8")
        return self.snipe(conn, file_path=path, dry_run=dry_run)

    def _finish_drop(self, path: Path, awards: list[Award]) -> None:
        """Mark the drop's article date as covered and move the file out of the inbox."""
        if awards:
            self._mark_dates_done(awards[0].posted_date, "drop")
        try:
            if path.resolve().parent == self.drop_dir.resolve():
                done = self.drop_dir / "processed"
                done.mkdir(exist_ok=True)
                path.replace(done / path.name)
        except OSError as exc:
            logger.warning("Could not move processed drop %s: %s", path.name, exc)

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
            "awaiting_drops": [],
        }

        # 1. Fetch DoD contract awards: a fresh operator drop wins, else the RSS feed.
        from_drop = bool(target_file and target_file.exists())
        try:
            if from_drop:
                awards = defense_daily.fetch_awards(file_path=target_file)
            else:
                awards = self._fetch_network(lookback_days, results)
        except Exception as exc:
            err = f"Failed fetching DoD awards: {exc}"
            logger.error(err)
            results["errors"].append(err)
            self._record_run(conn, "error", 0, 0, err)
            return results

        if from_drop:
            self._finish_drop(target_file, awards)
            results["awaiting_drops"] = self.awaiting_drops()

        results["awards_detected"] = len(awards)
        if not awards:
            logger.info("No DoD awards returned in this pass.")
            if results.get("blocked_now"):
                self._record_run(conn, "error", 0, 0, f"war.gov 403 on {results['blocked_now']} - paste needed")
            elif from_drop or results.get("articles_fetched"):
                self._record_run(conn, "success", 0, 0)
            return results

        # 2. Process each award through enrichment, scoring, and alpha detection
        for award in awards:
            try:
                award_id, status = db.upsert_award(conn, award)
                is_new = status == db.INSERTED
                if is_new:
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

                # Only a newly seen award can trigger: re-reading the same page must
                # never stage the same trade twice.
                if is_alpha and enriched_award.ticker and is_new:
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
        self._record_run(conn, "success", results["awards_detected"], results["inserted_awards"])
        return results

    @staticmethod
    def _record_run(conn: Any, status: str, seen: int, inserted: int, error: str = "") -> None:
        """One source_runs row per pass that actually read something (feeds the dashboard's health)."""
        try:
            run_id = db.start_run(conn, "defense_daily")
            db.finish_run(conn, run_id, status, seen, inserted, 0, error[:500])
        except Exception as exc:  # health bookkeeping must never break a snipe
            logger.debug("source_runs bookkeeping failed: %s", exc)

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
