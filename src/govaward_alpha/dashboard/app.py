"""FastAPI dashboard for GovAward Alpha."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse

from govaward_alpha.config import get_settings
from govaward_alpha.db import connect

app = FastAPI(title="GovAward Alpha Dashboard", docs_url=None, redoc_url=None)

_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


def _db() -> sqlite3.Connection:
    settings = get_settings()
    return connect(settings.db_path)


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    return HTMLResponse(_HTML)


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    conn = _db()
    try:
        total_alerts = conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
        red    = conn.execute("SELECT COUNT(*) FROM alerts WHERE alert_level='RED'").fetchone()[0]
        orange = conn.execute("SELECT COUNT(*) FROM alerts WHERE alert_level='ORANGE'").fetchone()[0]
        yellow = conn.execute("SELECT COUNT(*) FROM alerts WHERE alert_level='YELLOW'").fetchone()[0]
        watch  = conn.execute("SELECT COUNT(*) FROM alerts WHERE alert_level='WATCH'").fetchone()[0]

        total_awards = conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0]
        total_value  = conn.execute(
            "SELECT COALESCE(SUM(award_amount),0) FROM awards WHERE award_amount IS NOT NULL"
        ).fetchone()[0]

        unique_tickers = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM alerts WHERE ticker != 'UNRESOLVED'"
        ).fetchone()[0]

        claude_enriched = conn.execute(
            "SELECT COUNT(*) FROM alerts WHERE claude_analysis_json != '' AND claude_analysis_json IS NOT NULL"
        ).fetchone()[0]

        award_levels_raw = conn.execute(
            "SELECT COALESCE(alert_level, 'WATCH'), COUNT(*) FROM awards GROUP BY alert_level"
        ).fetchall()
        award_levels = {r[0]: r[1] for r in award_levels_raw}

        unique_award_tickers = conn.execute(
            "SELECT COUNT(DISTINCT ticker) FROM awards WHERE ticker IS NOT NULL AND ticker != ''"
        ).fetchone()[0]

        avg_score = conn.execute(
            "SELECT AVG(materiality_score) FROM alerts"
        ).fetchone()[0] or 0

        avg_award_score = conn.execute(
            "SELECT AVG(materiality_score) FROM awards WHERE materiality_score IS NOT NULL"
        ).fetchone()[0] or 0

        return {
            "total_alerts": total_alerts,
            "by_level": {"RED": red, "ORANGE": orange, "YELLOW": yellow, "WATCH": watch},
            "by_award_level": {
                "RED": award_levels.get("RED", 0),
                "ORANGE": award_levels.get("ORANGE", 0),
                "YELLOW": award_levels.get("YELLOW", 0),
                "WATCH": award_levels.get("WATCH", 0),
            },
            "total_awards_scanned": total_awards,
            "total_contract_value": total_value,
            "unique_tickers": unique_tickers,
            "unique_award_tickers": unique_award_tickers,
            "claude_enriched": claude_enriched,
            "avg_score": round(avg_score, 1),
            "avg_award_score": round(avg_award_score, 1),
        }
    finally:
        conn.close()


_ALERTS_SELECT = """
    SELECT
        al.id,
        al.ticker,
        al.materiality_score,
        al.alert_level,
        al.reasons_json,
        al.claude_analysis_json,
        al.created_at,
        al.sent_console,
        al.sent_email,
        al.sent_telegram,
        al.sent_discord,
        aw.awardee_name_raw,
        aw.award_amount,
        aw.obligated_amount,
        aw.agency,
        aw.sub_agency,
        aw.source,
        aw.source_url,
        aw.award_type,
        aw.is_idiq,
        aw.is_modification,
        aw.is_option_exercise,
        aw.is_ceiling_only,
        aw.posted_date,
        aw.award_date,
        aw.description,
        aw.naics,
        aw.psc,
        co.company_name,
        co.market_cap,
        co.trailing_revenue,
        co.avg_volume,
        co.price,
        co.sector,
        co.industry,
        co.exchange,
        co.last_market_data_refresh
    FROM alerts al
    JOIN awards aw ON al.award_id = aw.id
    LEFT JOIN companies co ON al.ticker = co.ticker
"""


def _parse_alert_row(r: sqlite3.Row) -> dict[str, Any]:
    row = dict(r)
    row["reasons"] = json.loads(row.get("reasons_json") or "[]")
    claude_raw = row.get("claude_analysis_json") or ""
    row["claude"] = json.loads(claude_raw) if claude_raw else None
    return row


@app.get("/api/alerts")
def list_alerts(
    limit: int = Query(200, ge=1, le=2000),
    level: str = "",          # comma-separated RED,ORANGE,...
    ticker: str = "",         # partial match
    min_score: int = 0,
    max_score: int = 100,
    agency: str = "",         # partial match
    source: str = "",         # exact
    sector: str = "",         # exact
    has_claude: str = "",     # "1" = only enriched, "0" = only un-enriched, "" = all
    min_amount: float = 0,    # award_amount lower bound (dollars); 0 = no limit
    max_amount: float = 0,    # award_amount upper bound (dollars); 0 = no limit
    min_market_cap: float = 0,  # company market_cap lower bound; 0 = no limit
    max_market_cap: float = 0,  # company market_cap upper bound; 0 = no limit
    require_market_cap: int = 0,  # 1 = exclude rows with unknown market cap
    posted_after: str = "",   # YYYY-MM-DD inclusive; only awards posted on/after this date
    posted_before: str = "",  # YYYY-MM-DD inclusive; only awards posted on/before this date
    require_posted: int = 0,  # 1 = exclude rows with unknown posted_date (recommended for "fresh only")
    sort: str = "posted_desc",  # posted_desc | posted_asc | created_desc | score_desc | amount_desc
) -> list[dict[str, Any]]:
    conn = _db()
    try:
        where: list[str] = ["al.materiality_score >= ?", "al.materiality_score <= ?"]
        params: list[Any] = [min_score, max_score]

        if level:
            lvls = [l.strip().upper() for l in level.split(",") if l.strip()]
            if lvls:
                placeholders = ",".join("?" * len(lvls))
                where.append(f"al.alert_level IN ({placeholders})")
                params.extend(lvls)

        if ticker:
            where.append("al.ticker LIKE ?")
            params.append(f"%{ticker.upper()}%")

        if agency:
            where.append("aw.agency LIKE ?")
            params.append(f"%{agency}%")

        if source:
            where.append("aw.source = ?")
            params.append(source)

        if sector:
            where.append("co.sector = ?")
            params.append(sector)

        if has_claude == "1":
            where.append("al.claude_analysis_json != '' AND al.claude_analysis_json IS NOT NULL")
        elif has_claude == "0":
            where.append("(al.claude_analysis_json = '' OR al.claude_analysis_json IS NULL)")

        # Award-amount range. We compare against the larger of award_amount and
        # obligated_amount because some sources only populate one of the two.
        if min_amount > 0:
            where.append("COALESCE(aw.award_amount, aw.obligated_amount, 0) >= ?")
            params.append(min_amount)
        if max_amount > 0:
            where.append("COALESCE(aw.award_amount, aw.obligated_amount, 0) <= ?")
            params.append(max_amount)

        # Market-cap range. By default, rows with unknown market cap are kept
        # so the user can still see them; require_market_cap=1 enforces strict
        # small/mid-cap filtering by excluding unknowns.
        if require_market_cap or min_market_cap > 0 or max_market_cap > 0:
            if require_market_cap:
                where.append("co.market_cap IS NOT NULL")
            if min_market_cap > 0:
                where.append("(co.market_cap IS NULL OR co.market_cap >= ?)")
                params.append(min_market_cap)
            if max_market_cap > 0:
                where.append("(co.market_cap IS NULL OR co.market_cap <= ?)")
                params.append(max_market_cap)

        # Posted-date freshness filter (the most important filter for an alpha-gen
        # use case — historical backfill rows are noise).
        if require_posted:
            where.append("aw.posted_date IS NOT NULL")
        if posted_after:
            where.append("aw.posted_date >= ?")
            params.append(posted_after)
        if posted_before:
            where.append("aw.posted_date <= ?")
            params.append(posted_before)

        order_clauses = {
            "posted_desc":  "aw.posted_date DESC NULLS LAST, al.materiality_score DESC",
            "posted_asc":   "aw.posted_date ASC NULLS LAST, al.materiality_score DESC",
            "created_desc": "al.created_at DESC",
            "score_desc":   "al.materiality_score DESC, aw.posted_date DESC NULLS LAST",
            "amount_desc":  "aw.award_amount DESC NULLS LAST",
        }
        order_by = order_clauses.get(sort, order_clauses["posted_desc"])

        sql = _ALERTS_SELECT + " WHERE " + " AND ".join(where)
        sql += f" ORDER BY {order_by} LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [_parse_alert_row(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/alerts/{alert_id}")
def get_alert(alert_id: int) -> dict[str, Any]:
    conn = _db()
    try:
        row = conn.execute(
            _ALERTS_SELECT + " WHERE al.id = ?",
            (alert_id,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Alert not found")
        return _parse_alert_row(row)
    finally:
        conn.close()


@app.get("/api/filter-options")
def filter_options() -> dict[str, Any]:
    conn = _db()
    try:
        agencies = [r[0] for r in conn.execute(
            """SELECT DISTINCT aw.agency FROM alerts al
               JOIN awards aw ON al.award_id = aw.id
               WHERE aw.agency != '' ORDER BY aw.agency"""
        ).fetchall()]

        sources = [r[0] for r in conn.execute(
            """SELECT DISTINCT aw.source FROM alerts al
               JOIN awards aw ON al.award_id = aw.id
               ORDER BY aw.source"""
        ).fetchall()]

        sectors = [r[0] for r in conn.execute(
            """SELECT DISTINCT co.sector FROM alerts al
               LEFT JOIN companies co ON al.ticker = co.ticker
               WHERE co.sector IS NOT NULL AND co.sector != ''
               ORDER BY co.sector"""
        ).fetchall()]

        tickers = [r[0] for r in conn.execute(
            """SELECT DISTINCT ticker FROM alerts
               WHERE ticker != 'UNRESOLVED'
               ORDER BY ticker"""
        ).fetchall()]

        return {
            "levels": ["RED", "ORANGE", "YELLOW", "WATCH"],
            "agencies": agencies,
            "sources": sources,
            "sectors": sectors,
            "tickers": tickers,
        }
    finally:
        conn.close()


@app.get("/api/companies")
def list_companies() -> list[dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT co.*, COUNT(al.id) as alert_count,
                      MAX(al.materiality_score) as max_score,
                      SUM(aw.award_amount) as total_awarded
               FROM companies co
               LEFT JOIN alerts al ON co.ticker = al.ticker
               LEFT JOIN awards aw ON al.award_id = aw.id
               GROUP BY co.ticker
               ORDER BY total_awarded DESC NULLS LAST"""
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/awards")
def list_awards(
    limit: int = Query(100, ge=1, le=500),
    min_score: int | None = Query(None, ge=0, le=100),
    level: str | None = Query(None),
    ticker: str | None = Query(None),
    agency: str | None = Query(None),
    source: str | None = Query(None),
    sort: str = Query("score_desc"),
) -> list[dict[str, Any]]:
    """Query all scanned awards with joined company data, scored or unscored."""
    from govaward_alpha.db import list_all_awards
    conn = _db()
    try:
        rows = list_all_awards(
            conn,
            limit=limit,
            min_score=min_score,
            level=level,
            ticker=ticker,
            agency=agency,
            source=source,
            sort=sort,
        )
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/unresolved")
def list_unresolved(
    min_amount: float = Query(5_000_000.0, ge=0),
    limit: int = Query(100, ge=1, le=500),
) -> list[dict[str, Any]]:
    """List large awards above threshold that have no resolved ticker, aggregated by awardee."""
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT
                   awardee_name_raw AS awardee_name,
                   SUM(COALESCE(award_amount, 0)) AS total_value,
                   COUNT(*) AS contract_count,
                   MAX(agency) AS sample_agency,
                   MAX(source) AS sample_source
               FROM awards
               WHERE (ticker IS NULL OR ticker = '' OR ticker = 'UNRESOLVED')
               GROUP BY awardee_name_raw
               HAVING total_value >= ?
               ORDER BY total_value DESC
               LIMIT ?""",
            (min_amount, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/source-runs")
def source_runs(limit: int = Query(60, ge=1, le=1000)) -> list[dict[str, Any]]:
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT * FROM source_runs ORDER BY started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@app.get("/api/charts/scores")
def chart_scores() -> dict[str, Any]:
    """Score distribution across all scored awards and alerts."""
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT COALESCE(aw.materiality_score, al.materiality_score)
               FROM awards aw
               LEFT JOIN alerts al ON al.award_id = aw.id
               WHERE aw.materiality_score IS NOT NULL OR al.materiality_score IS NOT NULL
               ORDER BY aw.created_at DESC LIMIT 500"""
        ).fetchall()
        scores = [r[0] for r in rows if r[0] is not None]

        buckets = [0] * 10
        labels = [f"{i*10}–{i*10+9}" for i in range(10)]
        for s in scores:
            buckets[min(int(s) // 10, 9)] += 1

        return {"labels": labels, "counts": buckets}
    finally:
        conn.close()


@app.get("/api/charts/ticker-value")
def chart_ticker_value() -> dict[str, Any]:
    """Top tickers by total award value."""
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT ticker, SUM(award_amount) as total
               FROM awards
               WHERE ticker IS NOT NULL AND ticker != '' AND award_amount IS NOT NULL
               GROUP BY ticker
               ORDER BY total DESC LIMIT 15"""
        ).fetchall()
        return {
            "tickers": [r[0] for r in rows],
            "totals":  [r[1] for r in rows],
        }
    finally:
        conn.close()
