"""FastAPI dashboard for GovAward Alpha."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from govaward_alpha.config import get_settings
from govaward_alpha.db import connect

app = FastAPI(title="GovAward Alpha Dashboard", docs_url=None, redoc_url=None)

from govaward_alpha.dashboard.security import LIVE_LOCKED_MESSAGE, PasswordMiddleware, live_allowed  # noqa: E402

app.add_middleware(PasswordMiddleware)

_HTML = (Path(__file__).parent / "index.html").read_text(encoding="utf-8")


def _db() -> sqlite3.Connection:
    settings = get_settings()
    return connect(settings.db_path)


@app.get("/", response_class=HTMLResponse)
def root() -> HTMLResponse:
    path = Path(__file__).parent / "index.html"
    return HTMLResponse(path.read_text(encoding="utf-8"))


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

        gemini_enriched = conn.execute(
            """SELECT COUNT(*) FROM alerts
               WHERE (gemini_analysis_json != '' AND gemini_analysis_json IS NOT NULL)
                  OR (claude_analysis_json != '' AND claude_analysis_json IS NOT NULL)"""
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
            "gemini_enriched": gemini_enriched,
            "claude_enriched": gemini_enriched,
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
        al.gemini_analysis_json,
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
    ai_raw = row.get("gemini_analysis_json") or row.get("claude_analysis_json") or ""
    ai_parsed = json.loads(ai_raw) if ai_raw else None
    row["gemini"] = ai_parsed
    row["claude"] = ai_parsed
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
    has_gemini: str = "",     # "1" = only enriched, "0" = only un-enriched, "" = all
    has_claude: str = "",     # legacy alias
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

        ai_filter = has_gemini if has_gemini != "" else has_claude
        if ai_filter == "1":
            where.append("((al.gemini_analysis_json != '' AND al.gemini_analysis_json IS NOT NULL) OR (al.claude_analysis_json != '' AND al.claude_analysis_json IS NOT NULL))")
        elif ai_filter == "0":
            where.append("((al.gemini_analysis_json = '' OR al.gemini_analysis_json IS NULL) AND (al.claude_analysis_json = '' OR al.claude_analysis_json IS NULL))")

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


@app.get("/api/catalysts")
def get_retail_catalysts(
    min_amount: float = Query(50_000_000, description="Minimum contract award amount"),
    min_market_cap: float = Query(50_000_000, description="Minimum company market cap"),
    max_market_cap: float = Query(10_000_000_000, description="Maximum company market cap (small/mid cap cap)"),
    freshness: str = Query("all", description="today, 3d, 7d, all"),
    limit: int = Query(50, ge=1, le=200),
) -> dict[str, Any]:
    """Retail Catalyst endpoint: filters exclusively for Small/Mid-Cap $50M+ awards
    with actionable Gemini trade strategies and front-running advice."""
    from govaward_alpha.enrich.gemini_enricher import synthesize_retail_strategy
    from govaward_alpha.models import Award, Company

    conn = _db()
    try:
        where = [
            "aw.ticker IS NOT NULL",
            "aw.ticker != ''",
            "aw.ticker != 'UNRESOLVED'",
            "COALESCE(aw.award_amount, aw.obligated_amount, 0) >= ?",
            "co.market_cap IS NOT NULL",
            "co.market_cap BETWEEN ? AND ?",
        ]
        params: list[Any] = [min_amount, min_market_cap, max_market_cap]

        if freshness == "today":
            where.append("aw.posted_date >= date('now', '-1 day')")
        elif freshness == "3d":
            where.append("aw.posted_date >= date('now', '-3 days')")
        elif freshness == "7d":
            where.append("aw.posted_date >= date('now', '-7 days')")

        sql = f"""
            SELECT
                aw.id as award_id,
                aw.ticker,
                aw.awardee_name_raw,
                aw.award_amount,
                aw.obligated_amount,
                aw.agency,
                aw.posted_date,
                aw.award_date,
                aw.description,
                aw.source,
                aw.source_url,
                aw.is_idiq,
                aw.is_modification,
                aw.is_ceiling_only,
                aw.materiality_score,
                aw.alert_level,
                co.company_name,
                co.market_cap,
                co.trailing_revenue,
                co.sector,
                al.id as alert_id,
                al.gemini_analysis_json,
                al.claude_analysis_json
            FROM awards aw
            JOIN companies co ON aw.ticker = co.ticker
            LEFT JOIN alerts al ON aw.id = al.award_id
            WHERE {" AND ".join(where)}
            ORDER BY (COALESCE(aw.award_amount, aw.obligated_amount, 0) / co.market_cap) DESC, aw.award_amount DESC
            LIMIT ?
        """
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()

        catalysts = []
        buy_signals = 0
        top_ratio = 0.0

        for r in rows:
            row_dict = dict(r)
            amt = row_dict["award_amount"] or row_dict["obligated_amount"] or 0
            cap = row_dict["market_cap"] or 1
            ratio = amt / cap
            ratio_pct = round(ratio * 100.0, 1)
            row_dict["ratio_pct"] = ratio_pct
            if ratio_pct > top_ratio:
                top_ratio = ratio_pct

            # Check if Gemini analysis is already attached
            ai_raw = row_dict.get("gemini_analysis_json") or row_dict.get("claude_analysis_json") or ""
            ai_data = json.loads(ai_raw) if ai_raw else None

            # If missing or lacking action_verdict, synthesize actionable strategy
            if not ai_data or "action_verdict" not in ai_data:
                dummy_aw = Award(
                    source=row_dict["source"] or "defense_daily",
                    source_award_id=str(row_dict["award_id"]),
                    ticker=row_dict["ticker"],
                    awardee_name_raw=row_dict["awardee_name_raw"] or "",
                    award_amount=amt,
                    obligated_amount=row_dict["obligated_amount"],
                    agency=row_dict["agency"] or "",
                    posted_date=None,
                    description=row_dict["description"] or "",
                    is_idiq=bool(row_dict["is_idiq"]),
                    is_ceiling_only=bool(row_dict["is_ceiling_only"]),
                    is_modification=bool(row_dict["is_modification"]),
                )
                dummy_co = Company(
                    ticker=row_dict["ticker"],
                    company_name=row_dict["company_name"] or "",
                    market_cap=row_dict["market_cap"],
                    trailing_revenue=row_dict["trailing_revenue"],
                    sector=row_dict["sector"] or "",
                )
                ai_data = synthesize_retail_strategy(dummy_aw, dummy_co)

            row_dict["gemini_strategy"] = ai_data
            if ai_data.get("action_verdict") == "HIGH CONVICTION BUY":
                buy_signals += 1

            catalysts.append(row_dict)

        return {
            "catalysts": catalysts,
            "summary": {
                "total_catalysts": len(catalysts),
                "top_ratio_pct": top_ratio,
                "top_ticker": catalysts[0]["ticker"] if catalysts else None,
                "buy_signals": buy_signals,
                "freshness": freshness,
            }
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Auto-Trader & Alpha Play Execution API
# ---------------------------------------------------------------------------

@app.get("/api/trader/status")
def get_trader_status() -> dict[str, Any]:
    from govaward_alpha import db
    conn = _db()
    try:
        return db.get_trader_stats(conn)
    finally:
        conn.close()


@app.post("/api/trader/config")
async def update_trader_config(
    request: Request,
    engine_mode: str | None = Query(None, description="OFF | PAPER | LIVE"),
    default_allocation: float | None = Query(None, ge=100.0, le=100000.0),
    min_ratio_pct: float | None = Query(None, ge=1.0, le=100.0),
    take_profit_pct: float | None = Query(None, ge=1.0, le=100.0),
    stop_loss_pct: float | None = Query(None, ge=0.5, le=50.0),
    trailing_stop: int | None = Query(None, ge=0, le=1),
) -> dict[str, Any]:
    from govaward_alpha import db
    body: dict[str, Any] = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}

    em = body.get("engine_mode", engine_mode)
    da = body.get("default_allocation", default_allocation)
    mr = body.get("min_ratio_pct", min_ratio_pct)
    tp = body.get("take_profit_pct", take_profit_pct)
    sl = body.get("stop_loss_pct", stop_loss_pct)
    ts = body.get("trailing_stop", trailing_stop)
    if em is not None and str(em).strip().upper() == "LIVE" and not live_allowed():
        raise HTTPException(status_code=403, detail=LIVE_LOCKED_MESSAGE)

    conn = _db()
    try:
        db.update_trade_config(
            conn,
            engine_mode=em,
            default_allocation=da,
            min_ratio_pct=mr,
            take_profit_pct=tp,
            stop_loss_pct=sl,
            trailing_stop=ts,
        )
        return db.get_trader_stats(conn)
    finally:
        conn.close()


@app.get("/api/trader/approvals")
def list_company_approvals() -> list[dict[str, Any]]:
    from govaward_alpha import db
    conn = _db()
    try:
        return db.get_company_approvals(conn)
    finally:
        conn.close()


@app.post("/api/trader/approvals/bulk")
async def bulk_set_company_approvals(
    request: Request,
    is_approved: int = Query(1),
    auto_trade_enabled: int = Query(1),
) -> dict[str, Any]:
    from govaward_alpha import db
    body: dict[str, Any] = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}

    appr_val = body.get("is_approved", is_approved)
    auto_val = body.get("auto_trade_enabled", auto_trade_enabled)
    alloc_val = body.get("max_allocation")

    conn = _db()
    try:
        count = db.bulk_set_company_approvals(
            conn,
            is_approved=int(appr_val),
            auto_trade_enabled=int(auto_val),
            max_allocation=float(alloc_val) if alloc_val is not None else None,
        )
        return {"ok": True, "updated_count": count}
    finally:
        conn.close()


@app.post("/api/trader/bankroll/reset")
async def reset_paper_bankroll(
    request: Request,
    amount: float = Query(100000.0, ge=5.0),
) -> dict[str, Any]:
    from govaward_alpha import db
    body: dict[str, Any] = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}

    target_amount = float(body.get("amount", amount))
    conn = _db()
    try:
        new_balance = db.set_paper_cash_balance(conn, target_amount)
        return {"ok": True, "paper_cash_balance": new_balance}
    finally:
        conn.close()


@app.get("/api/trader/alpaca/account")
def get_alpaca_account_status() -> dict[str, Any]:
    """Retrieve live Alpaca connection and account metrics."""
    from govaward_alpha.services.alpaca_broker import AlpacaBroker
    broker = AlpacaBroker()
    if not broker.is_configured:
        return {
            "ok": False,
            "configured": False,
            "error": "Alpaca API credentials are not set in .env",
            "is_paper": broker.is_paper,
        }
    status = broker.verify_credentials()
    status["configured"] = True
    clock = broker.get_clock()
    status["market_open"] = bool(clock.get("is_open", False))
    status["clock"] = clock
    return status


@app.post("/api/trader/approvals/{ticker}")
async def set_company_approval(
    ticker: str,
    request: Request,
    is_approved: int | None = Query(None, ge=0, le=1),
    auto_trade_enabled: int | None = Query(None, ge=0, le=1),
    max_allocation: float | None = Query(None, ge=1.0),
    notes: str | None = Query(None),
) -> dict[str, Any]:
    from govaward_alpha import db
    body: dict[str, Any] = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            body = {}

    appr_raw = body.get("is_approved", is_approved)
    appr_val = 1 if (appr_raw is True or appr_raw == 1 or appr_raw == "1") else (0 if appr_raw is not None else 1)

    auto_raw = body.get("auto_trade_enabled", auto_trade_enabled)
    auto_val = 1 if (auto_raw is True or auto_raw == 1 or auto_raw == "1") else 0

    alloc_raw = body.get("max_allocation", max_allocation)
    alloc_val = float(alloc_raw or 1000.0)

    notes_raw = body.get("notes", notes)
    notes_val = str(notes_raw or "")

    conn = _db()
    try:
        res = db.set_company_approval(
            conn,
            ticker=ticker,
            is_approved=appr_val,
            auto_trade_enabled=auto_val,
            max_allocation=alloc_val,
            notes=notes_val,
        )
        return {"ok": True, **res}
    finally:
        conn.close()


@app.get("/api/trader/orders")
def list_trader_orders(
    limit: int = Query(100, ge=1, le=500),
    status: str = Query(None),
) -> list[dict[str, Any]]:
    from govaward_alpha import db
    conn = _db()
    try:
        return db.get_orders(conn, limit=limit, status=status)
    finally:
        conn.close()


@app.post("/api/trader/orders/{order_id}/approve")
def approve_trader_order(order_id: int) -> dict[str, Any]:
    from govaward_alpha.services.trade_engine import trade_engine
    conn = _db()
    try:
        res = trade_engine.approve_and_execute_order(conn, order_id)
        if not res:
            raise HTTPException(status_code=400, detail="Order not found or not in PENDING_APPROVAL state")
        return res
    finally:
        conn.close()


@app.post("/api/trader/orders/{order_id}/reject")
def reject_trader_order(order_id: int) -> dict[str, Any]:
    from govaward_alpha.services.trade_engine import trade_engine
    conn = _db()
    try:
        res = trade_engine.reject_order(conn, order_id)
        if not res:
            raise HTTPException(status_code=404, detail="Order not found")
        return res
    finally:
        conn.close()


@app.get("/api/trader/positions")
def list_trader_positions(status: str = Query("OPEN")) -> list[dict[str, Any]]:
    from govaward_alpha import db
    from govaward_alpha.services.trade_engine import trade_engine
    conn = _db()
    try:
        # Update open positions with live quotes if requested
        if status == "OPEN":
            trade_engine.monitor_and_update_positions(conn)
        return db.get_positions(conn, status=status)
    finally:
        conn.close()


@app.post("/api/trader/positions/{position_id}/close")
def close_trader_position(position_id: int) -> dict[str, Any]:
    from govaward_alpha.services.trade_engine import trade_engine
    conn = _db()
    try:
        res = trade_engine.close_position_manually(conn, position_id)
        if not res:
            raise HTTPException(status_code=400, detail="Position not found or already closed")
        return res
    finally:
        conn.close()


@app.post("/api/trader/stage_catalyst/{award_id}")
def stage_catalyst_trade(award_id: int) -> dict[str, Any]:
    from govaward_alpha.models import Award, Company, ScoreResult
    from govaward_alpha.services.trade_engine import trade_engine
    from govaward_alpha.enrich.gemini_enricher import synthesize_retail_strategy

    conn = _db()
    try:
        row = conn.execute("""
            SELECT aw.*, co.company_name, co.market_cap, co.trailing_revenue, co.sector
            FROM awards aw
            LEFT JOIN companies co ON aw.ticker = co.ticker
            WHERE aw.id = ?
        """, (award_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Award not found")

        r = dict(row)
        if not r.get("ticker") or r["ticker"] == "UNRESOLVED":
            raise HTTPException(status_code=400, detail="Award ticker unresolved — cannot stage trade")

        aw = Award(
            source=r["source"] or "manual",
            source_award_id=str(r["source_award_id"]),
            ticker=r["ticker"],
            awardee_name_raw=r["awardee_name_raw"] or "",
            award_amount=r["award_amount"],
            obligated_amount=r["obligated_amount"],
            agency=r["agency"] or "",
            description=r["description"] or "",
            is_idiq=bool(r["is_idiq"]),
            is_ceiling_only=bool(r["is_ceiling_only"]),
            is_modification=bool(r["is_modification"]),
        )
        aw.id = award_id
        co = Company(
            ticker=r["ticker"],
            company_name=r["company_name"] or "",
            market_cap=r["market_cap"],
            trailing_revenue=r["trailing_revenue"],
            sector=r["sector"] or "",
        )
        sc = ScoreResult(score=r["materiality_score"] or 75, level=r["alert_level"] or "ORANGE", reasons=["Manual stage"])
        syn = synthesize_retail_strategy(aw, co, sc)
        order = trade_engine.evaluate_and_stage_award(conn, aw, co, sc, gemini_strategy=syn)
        if not order:
            raise HTTPException(status_code=400, detail="Award did not pass Alpha Play filters or trade already exists")
        return order
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


@app.get("/api/charts/levels")
def chart_levels() -> dict[str, Any]:
    """Urgency level breakdown across all awards."""
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT COALESCE(alert_level, 'WATCH') as lvl, COUNT(*) as cnt
               FROM awards
               GROUP BY alert_level"""
        ).fetchall()
        counts = {r[0]: r[1] for r in rows}
        total = sum(counts.values()) or 1
        return {
            "RED": counts.get("RED", 0),
            "ORANGE": counts.get("ORANGE", 0),
            "YELLOW": counts.get("YELLOW", 0),
            "WATCH": counts.get("WATCH", 0),
            "total": total,
        }
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


@app.get("/api/charts/agencies")
def chart_agencies() -> dict[str, Any]:
    """Top awarding agencies by total contract value."""
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT agency, SUM(award_amount) as total, COUNT(*) as count
               FROM awards
               WHERE agency IS NOT NULL AND agency != '' AND award_amount IS NOT NULL
               GROUP BY agency
               ORDER BY total DESC LIMIT 10"""
        ).fetchall()
        return {
            "agencies": [r[0] for r in rows],
            "totals":   [r[1] for r in rows],
            "counts":   [r[2] for r in rows],
        }
    finally:
        conn.close()


@app.get("/api/export/csv")
def export_csv(
    source: str | None = Query(None),
    min_score: int | None = Query(None, ge=0, le=100),
    level: str | None = Query(None),
    ticker: str | None = Query(None),
) -> Response:
    """Export awards catalog matching active filters as a CSV spreadsheet."""
    import csv
    import io
    from fastapi.responses import Response
    from govaward_alpha.db import list_all_awards

    conn = _db()
    try:
        rows = list_all_awards(
            conn,
            limit=5000,
            source=source,
            min_score=min_score,
            level=level,
            ticker=ticker,
            sort="amount_desc",
        )
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow([
            "ID", "Source", "Source Award ID", "Awardee", "Ticker", "Parent Company",
            "Award Amount", "Obligated Amount", "Agency", "Score", "Level",
            "Posted Date", "Award Date", "Description"
        ])
        for r in rows:
            writer.writerow([
                r["id"], r["source"], r["source_award_id"], r["awardee_name_raw"],
                r["ticker"] or "UNRESOLVED", r["parent_company_name"] or "",
                r["award_amount"], r["obligated_amount"], r["agency"],
                r["materiality_score"], r["alert_level"],
                r["posted_date"], r["award_date"], (r["description"] or "").replace("\n", " "),
            ])
        csv_bytes = out.getvalue().encode("utf-8")
        return Response(
            content=csv_bytes,
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=govaward_pipeline_export.csv"},
        )
    finally:
        conn.close()


from pydantic import BaseModel


class OverrideRequest(BaseModel):
    raw_name: str
    ticker: str | None = None
    parent_name: str | None = None


@app.post("/api/overrides")
def add_override_endpoint(req: OverrideRequest) -> dict[str, Any]:
    """Add or update an awardee mapping and immediately re-score matching awards."""
    import yaml
    from govaward_alpha.db import update_award_score
    from govaward_alpha.enrich.award_enricher import enrich_award
    from govaward_alpha.enrich.company_resolver import get_overrides
    from govaward_alpha.models import Award
    from govaward_alpha.normalize import normalize_company_name
    from govaward_alpha.scoring import score_award

    clean_raw = req.raw_name.strip()
    if not clean_raw:
        raise HTTPException(status_code=400, detail="Awardee name cannot be empty")

    clean_ticker = req.ticker.strip().upper() if req.ticker and req.ticker.strip() else None
    clean_parent = req.parent_name.strip() if req.parent_name and req.parent_name.strip() else None
    if clean_ticker and not clean_parent:
        clean_parent = clean_ticker

    settings = get_settings()
    yaml_path = settings.data_dir / "manual_ticker_overrides.yaml"
    data: dict[str, Any] = {"overrides": {}}
    if yaml_path.exists():
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {"overrides": {}}
        except Exception:
            data = {"overrides": {}}

    if "overrides" not in data or not isinstance(data["overrides"], dict):
        data["overrides"] = {}

    norm_key = normalize_company_name(clean_raw)
    data["overrides"][norm_key] = {
        "parent_company_name": clean_parent or f"{clean_raw} (private)",
        "ticker": clean_ticker,
    }

    yaml_path.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")
    get_overrides(force_refresh=True)

    # Re-resolve and update awards in database
    conn = _db()
    updated_count = 0
    try:
        rows = conn.execute(
            "SELECT id, awardee_name_raw FROM awards WHERE awardee_name_raw = ? OR ticker IS NULL",
            (clean_raw,),
        ).fetchall()
        for r in rows:
            if normalize_company_name(r["awardee_name_raw"]) == norm_key:
                conn.execute(
                    "UPDATE awards SET ticker = ?, parent_company_name = ? WHERE id = ?",
                    (clean_ticker, clean_parent, r["id"]),
                )
                updated_count += 1
                if clean_ticker:
                    full_row = conn.execute("SELECT * FROM awards WHERE id = ?", (r["id"],)).fetchone()
                    d = dict(full_row)
                    d["is_idiq"] = bool(d.get("is_idiq"))
                    d["is_modification"] = bool(d.get("is_modification"))
                    aw = Award(**d)
                    enr, co = enrich_award(aw, conn)
                    res = score_award(enr, co)
                    update_award_score(conn, r["id"], res.score, str(res.level))
        conn.commit()
    finally:
        conn.close()

    return {
        "status": "ok",
        "raw_name": clean_raw,
        "ticker": clean_ticker,
        "parent_name": clean_parent,
        "updated_awards": updated_count,
    }


import threading

_scan_lock = threading.Lock()
_scan_status = {"running": False, "last_status": "idle", "last_run": None, "message": ""}


@app.post("/api/scan/trigger")
def trigger_scan() -> dict[str, Any]:
    """Trigger a fast USAspending scan in the background."""
    from govaward_alpha.timeutil import utcnow

    global _scan_status
    if _scan_lock.locked():
        return {"status": "busy", "message": "A scan is currently running."}

    def _run_bg():
        global _scan_status
        with _scan_lock:
            _scan_status["running"] = True
            _scan_status["last_run"] = utcnow().isoformat()
            try:
                from govaward_alpha.services.scan_service import run_source
                from govaward_alpha.sources import usaspending

                run_source("usaspending", usaspending.fetch_recent_awards, lookback_days=1)
                _scan_status["last_status"] = "success"
                _scan_status["message"] = "Scan completed successfully."
            except Exception as exc:
                _scan_status["last_status"] = "error"
                _scan_status["message"] = str(exc)
            finally:
                _scan_status["running"] = False

    t = threading.Thread(target=_run_bg, daemon=True)
    t.start()
    return {"status": "started", "message": "Background scan initiated."}


@app.get("/api/scan/status")
def get_scan_status() -> dict[str, Any]:
    return _scan_status

