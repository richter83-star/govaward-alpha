"""SQLite persistence layer."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from govaward_alpha.timeutil import utcnow
from typing import Generator

from govaward_alpha.config import get_settings
from govaward_alpha.models import AlertRecord, Award, Company, SourceRun

logger = logging.getLogger(__name__)


def connect(db_path: str) -> sqlite3.Connection:
    """Open a configured SQLite connection (WAL, Row factory, FK enforcement)."""
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


DDL = """
CREATE TABLE IF NOT EXISTS awards (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT    NOT NULL,
    source_award_id         TEXT    NOT NULL,
    source_url              TEXT    DEFAULT '',
    posted_date             TEXT,
    award_date              TEXT,
    awardee_name_raw        TEXT    DEFAULT '',
    awardee_name_normalized TEXT    DEFAULT '',
    parent_company_name     TEXT    DEFAULT '',
    ticker                  TEXT,
    agency                  TEXT    DEFAULT '',
    sub_agency              TEXT    DEFAULT '',
    naics                   TEXT    DEFAULT '',
    psc                     TEXT    DEFAULT '',
    award_amount            REAL,
    obligated_amount        REAL,
    description             TEXT    DEFAULT '',
    contract_number         TEXT    DEFAULT '',
    award_type              TEXT    DEFAULT '',
    is_idiq                 INTEGER DEFAULT 0,
    is_modification         INTEGER DEFAULT 0,
    is_option_exercise      INTEGER DEFAULT 0,
    is_ceiling_only         INTEGER DEFAULT 0,
    period_start            TEXT,
    period_end              TEXT,
    materiality_score       INTEGER,
    alert_level             TEXT,
    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL,
    content_hash            TEXT    DEFAULT '',
    UNIQUE(source, source_award_id)
);

CREATE INDEX IF NOT EXISTS idx_awards_ticker      ON awards(ticker);
CREATE INDEX IF NOT EXISTS idx_awards_posted_date ON awards(posted_date);
CREATE INDEX IF NOT EXISTS idx_awards_hash        ON awards(content_hash);

CREATE TABLE IF NOT EXISTS companies (
    ticker                  TEXT    PRIMARY KEY,
    company_name            TEXT    DEFAULT '',
    market_cap              REAL,
    trailing_revenue        REAL,
    avg_volume              REAL,
    price                   REAL,
    sector                  TEXT    DEFAULT '',
    industry                TEXT    DEFAULT '',
    exchange                TEXT    DEFAULT '',
    last_market_data_refresh TEXT
);

CREATE TABLE IF NOT EXISTS alerts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    award_id            INTEGER NOT NULL REFERENCES awards(id),
    ticker              TEXT    NOT NULL,
    materiality_score   INTEGER NOT NULL,
    alert_level         TEXT    NOT NULL,
    reasons_json        TEXT    DEFAULT '[]',
    sent_console        INTEGER DEFAULT 0,
    sent_email          INTEGER DEFAULT 0,
    sent_telegram       INTEGER DEFAULT 0,
    sent_discord        INTEGER DEFAULT 0,
    gemini_analysis_json TEXT   DEFAULT '',
    claude_analysis_json TEXT   DEFAULT '',
    created_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alerts_award_id ON alerts(award_id);
CREATE INDEX IF NOT EXISTS idx_alerts_ticker   ON alerts(ticker);

CREATE TABLE IF NOT EXISTS source_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    source           TEXT    NOT NULL,
    started_at       TEXT    NOT NULL,
    finished_at      TEXT,
    status           TEXT    DEFAULT 'running',
    records_seen     INTEGER DEFAULT 0,
    records_inserted INTEGER DEFAULT 0,
    records_updated  INTEGER DEFAULT 0,
    error_message    TEXT    DEFAULT ''
);

CREATE TABLE IF NOT EXISTS trade_configs (
    id                  INTEGER PRIMARY KEY CHECK (id = 1),
    engine_mode         TEXT    NOT NULL DEFAULT 'PAPER', -- OFF | PAPER | LIVE
    default_allocation  REAL    NOT NULL DEFAULT 1000.0,
    min_ratio_pct       REAL    NOT NULL DEFAULT 10.0,
    min_score           INTEGER NOT NULL DEFAULT 70,
    take_profit_pct     REAL    NOT NULL DEFAULT 10.0,
    stop_loss_pct       REAL    NOT NULL DEFAULT 4.0,
    trailing_stop       INTEGER NOT NULL DEFAULT 0,
    paper_cash_balance  REAL    NOT NULL DEFAULT 50000.0,
    updated_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS company_trade_approvals (
    ticker              TEXT    PRIMARY KEY,
    company_name        TEXT    DEFAULT '',
    is_approved         INTEGER NOT NULL DEFAULT 0,
    auto_trade_enabled  INTEGER NOT NULL DEFAULT 0,
    max_allocation      REAL    DEFAULT 1000.0,
    notes               TEXT    DEFAULT '',
    updated_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    award_id            INTEGER REFERENCES awards(id),
    ticker              TEXT    NOT NULL,
    side                TEXT    NOT NULL DEFAULT 'BUY',
    status              TEXT    NOT NULL DEFAULT 'PENDING_APPROVAL', -- PENDING_APPROVAL | FILLED | REJECTED | CANCELLED
    mode                TEXT    NOT NULL DEFAULT 'PAPER',            -- PAPER | LIVE
    shares              REAL    NOT NULL,
    signal_price        REAL    NOT NULL,
    fill_price          REAL,
    total_cost          REAL,
    take_profit_price   REAL,
    stop_loss_price     REAL,
    trigger_ratio_pct   REAL,
    verdict             TEXT,
    headline            TEXT,
    strategy_note       TEXT,
    created_at          TEXT    NOT NULL,
    filled_at           TEXT,
    closed_at           TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_ticker ON orders(ticker);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS positions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id            INTEGER REFERENCES orders(id),
    ticker              TEXT    NOT NULL,
    mode                TEXT    NOT NULL DEFAULT 'PAPER',
    shares              REAL    NOT NULL,
    entry_price         REAL    NOT NULL,
    current_price       REAL    NOT NULL,
    take_profit_price   REAL,
    stop_loss_price     REAL,
    unrealized_pnl      REAL    DEFAULT 0.0,
    unrealized_pnl_pct  REAL    DEFAULT 0.0,
    status              TEXT    NOT NULL DEFAULT 'OPEN', -- OPEN | CLOSED
    exit_price          REAL,
    realized_pnl        REAL    DEFAULT 0.0,
    realized_pnl_pct    REAL    DEFAULT 0.0,
    exit_reason         TEXT    DEFAULT '',              -- TAKE_PROFIT | STOP_LOSS | MANUAL_CLOSE
    opened_at           TEXT    NOT NULL,
    closed_at           TEXT,
    updated_at          TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_positions_ticker ON positions(ticker);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive migrations for existing databases."""
    alert_cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    if "claude_analysis_json" not in alert_cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN claude_analysis_json TEXT DEFAULT ''")
        conn.commit()
        logger.info("Migration: added claude_analysis_json column to alerts")

    if "gemini_analysis_json" not in alert_cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN gemini_analysis_json TEXT DEFAULT ''")
        conn.commit()
        if "claude_analysis_json" in alert_cols:
            conn.execute("UPDATE alerts SET gemini_analysis_json = claude_analysis_json WHERE gemini_analysis_json = '' AND claude_analysis_json != ''")
            conn.commit()
        logger.info("Migration: added gemini_analysis_json column to alerts")

    award_cols = {row[1] for row in conn.execute("PRAGMA table_info(awards)")}
    if "materiality_score" not in award_cols:
        conn.execute("ALTER TABLE awards ADD COLUMN materiality_score INTEGER")
        conn.commit()
        logger.info("Migration: added materiality_score column to awards")
    if "alert_level" not in award_cols:
        conn.execute("ALTER TABLE awards ADD COLUMN alert_level TEXT")
        conn.commit()
        logger.info("Migration: added alert_level column to awards")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_awards_score ON awards(materiality_score)")
    conn.commit()

    # Ensure default trade_configs row exists
    conn.execute("""
        INSERT OR IGNORE INTO trade_configs (
            id, engine_mode, default_allocation, min_ratio_pct, min_score,
            take_profit_pct, stop_loss_pct, trailing_stop, paper_cash_balance, updated_at
        ) VALUES (1, 'PAPER', 1000.0, 10.0, 70, 10.0, 4.0, 0, 50000.0, datetime('now'))
    """)
    conn.commit()


def init_db() -> None:
    """Create all tables if they do not exist."""
    settings = get_settings()
    conn = connect(settings.db_path)
    try:
        conn.executescript(DDL)
        conn.commit()
        _migrate(conn)
        logger.info("Database initialised at %s", settings.db_path)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Award persistence
# ---------------------------------------------------------------------------

def _award_to_row(a: Award) -> dict:
    return {
        "source": a.source,
        "source_award_id": a.source_award_id,
        "source_url": a.source_url,
        "posted_date": a.posted_date.isoformat() if a.posted_date else None,
        "award_date": a.award_date.isoformat() if a.award_date else None,
        "awardee_name_raw": a.awardee_name_raw,
        "awardee_name_normalized": a.awardee_name_normalized,
        "parent_company_name": a.parent_company_name,
        "ticker": a.ticker,
        "agency": a.agency,
        "sub_agency": a.sub_agency,
        "naics": a.naics,
        "psc": a.psc,
        "award_amount": a.award_amount,
        "obligated_amount": a.obligated_amount,
        "description": a.description,
        "contract_number": a.contract_number,
        "award_type": a.award_type,
        "is_idiq": int(a.is_idiq),
        "is_modification": int(a.is_modification),
        "is_option_exercise": int(a.is_option_exercise),
        "is_ceiling_only": int(a.is_ceiling_only),
        "period_start": a.period_start.isoformat() if a.period_start else None,
        "period_end": a.period_end.isoformat() if a.period_end else None,
        "materiality_score": a.materiality_score,
        "alert_level": a.alert_level,
        "content_hash": a.content_hash,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


INSERTED = "inserted"
UPDATED = "updated"
UNCHANGED = "unchanged"


def upsert_award(conn: sqlite3.Connection, award: Award) -> tuple[int, str]:
    """Insert or update an award.

    Returns ``(row_id, status)`` where status is one of ``INSERTED``,
    ``UPDATED`` (row existed and the content hash changed), or ``UNCHANGED``
    (row existed with an identical hash — no write performed). Callers need
    the three-way distinction so run audits do not report every re-seen award
    as an update.
    """
    row = _award_to_row(award)
    cur = conn.execute(
        "SELECT id, content_hash FROM awards WHERE source=? AND source_award_id=?",
        (award.source, award.source_award_id),
    )
    existing = cur.fetchone()
    if existing is None:
        cur = conn.execute(
            """INSERT INTO awards
               (source,source_award_id,source_url,posted_date,award_date,
                awardee_name_raw,awardee_name_normalized,parent_company_name,ticker,
                agency,sub_agency,naics,psc,award_amount,obligated_amount,
                description,contract_number,award_type,is_idiq,is_modification,
                is_option_exercise,is_ceiling_only,period_start,period_end,
                materiality_score,alert_level,content_hash,created_at,updated_at)
               VALUES
               (:source,:source_award_id,:source_url,:posted_date,:award_date,
                :awardee_name_raw,:awardee_name_normalized,:parent_company_name,:ticker,
                :agency,:sub_agency,:naics,:psc,:award_amount,:obligated_amount,
                :description,:contract_number,:award_type,:is_idiq,:is_modification,
                :is_option_exercise,:is_ceiling_only,:period_start,:period_end,
                :materiality_score,:alert_level,:content_hash,:created_at,:updated_at)""",
            row,
        )
        return cur.lastrowid, INSERTED
    else:
        if existing["content_hash"] == award.content_hash:
            return existing["id"], UNCHANGED
        row["id"] = existing["id"]
        row["updated_at"] = utcnow().isoformat()
        conn.execute(
            """UPDATE awards SET
               source_url=:source_url, posted_date=:posted_date, award_date=:award_date,
               awardee_name_normalized=:awardee_name_normalized,
               parent_company_name=:parent_company_name, ticker=:ticker,
               agency=:agency, sub_agency=:sub_agency, naics=:naics, psc=:psc,
               award_amount=:award_amount, obligated_amount=:obligated_amount,
               description=:description, contract_number=:contract_number,
               award_type=:award_type, is_idiq=:is_idiq, is_modification=:is_modification,
               is_option_exercise=:is_option_exercise, is_ceiling_only=:is_ceiling_only,
               period_start=:period_start, period_end=:period_end,
               materiality_score=COALESCE(:materiality_score, materiality_score),
               alert_level=COALESCE(:alert_level, alert_level),
               content_hash=:content_hash, updated_at=:updated_at
               WHERE id=:id""",
            row,
        )
        return existing["id"], UPDATED


def update_award_score(
    conn: sqlite3.Connection,
    award_id: int,
    score: int,
    level: str,
) -> None:
    """Update calculated score and alert level for an award row."""
    conn.execute(
        "UPDATE awards SET materiality_score=?, alert_level=?, updated_at=? WHERE id=?",
        (score, level, utcnow().isoformat(), award_id),
    )


def list_all_awards(
    conn: sqlite3.Connection,
    limit: int = 100,
    min_score: int | None = None,
    level: str | None = None,
    ticker: str | None = None,
    agency: str | None = None,
    source: str | None = None,
    sort: str = "score_desc",
) -> list[sqlite3.Row]:
    """Query all scanned awards with joined company data, scored or unscored."""
    where = ["1=1"]
    params: list[Any] = []
    if min_score is not None:
        where.append("aw.materiality_score >= ?")
        params.append(min_score)
    if level:
        where.append("aw.alert_level = ?")
        params.append(level.upper())
    if ticker:
        where.append("aw.ticker LIKE ?")
        params.append(f"%{ticker.upper()}%")
    if agency:
        where.append("aw.agency LIKE ?")
        params.append(f"%{agency}%")
    if source:
        where.append("aw.source = ?")
        params.append(source)

    order_by = {
        "score_desc":  "aw.materiality_score DESC NULLS LAST, aw.award_amount DESC NULLS LAST",
        "score_asc":   "aw.materiality_score ASC NULLS LAST",
        "amount_desc": "aw.award_amount DESC NULLS LAST",
        "amount_asc":  "aw.award_amount ASC NULLS LAST",
        "posted_desc": "aw.posted_date DESC NULLS LAST, aw.materiality_score DESC NULLS LAST",
        "created_desc":"aw.created_at DESC",
    }.get(sort, "aw.materiality_score DESC NULLS LAST, aw.award_amount DESC NULLS LAST")

    sql = f"""
        SELECT aw.*, co.company_name, co.market_cap, co.trailing_revenue, co.sector
        FROM awards aw
        LEFT JOIN companies co ON aw.ticker = co.ticker
        WHERE {" AND ".join(where)}
        ORDER BY {order_by}
        LIMIT ?
    """
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def get_award_by_id(conn: sqlite3.Connection, award_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM awards WHERE id=?", (award_id,)).fetchone()


def list_unresolved_awards(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """Awards above threshold with no resolved ticker."""
    settings = get_settings()
    return conn.execute(
        """SELECT * FROM awards
           WHERE ticker IS NULL
             AND (award_amount >= ? OR obligated_amount >= ?)
           ORDER BY award_amount DESC NULLS LAST
           LIMIT ?""",
        (settings.award_min_amount, settings.award_min_amount, limit),
    ).fetchall()


# ---------------------------------------------------------------------------
# Company persistence
# ---------------------------------------------------------------------------

def upsert_company(conn: sqlite3.Connection, company: Company) -> None:
    conn.execute(
        """INSERT INTO companies
           (ticker,company_name,market_cap,trailing_revenue,avg_volume,price,
            sector,industry,exchange,last_market_data_refresh)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(ticker) DO UPDATE SET
             company_name=excluded.company_name,
             market_cap=excluded.market_cap,
             trailing_revenue=excluded.trailing_revenue,
             avg_volume=excluded.avg_volume,
             price=excluded.price,
             sector=excluded.sector,
             industry=excluded.industry,
             exchange=excluded.exchange,
             last_market_data_refresh=excluded.last_market_data_refresh""",
        (
            company.ticker,
            company.company_name,
            company.market_cap,
            company.trailing_revenue,
            company.avg_volume,
            company.price,
            company.sector,
            company.industry,
            company.exchange,
            company.last_market_data_refresh.isoformat() if company.last_market_data_refresh else None,
        ),
    )


def get_company(conn: sqlite3.Connection, ticker: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM companies WHERE ticker=?", (ticker,)).fetchone()


# ---------------------------------------------------------------------------
# Alert persistence
# ---------------------------------------------------------------------------

def insert_alert(conn: sqlite3.Connection, alert: AlertRecord) -> int:
    gemini_json = alert.gemini_analysis_json or alert.claude_analysis_json
    claude_json = alert.claude_analysis_json or alert.gemini_analysis_json
    cur = conn.execute(
        """INSERT INTO alerts
           (award_id,ticker,materiality_score,alert_level,reasons_json,
            sent_console,sent_email,sent_telegram,sent_discord,
            gemini_analysis_json,claude_analysis_json,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            alert.award_id,
            alert.ticker,
            alert.materiality_score,
            alert.alert_level,
            alert.reasons_json,
            int(alert.sent_console),
            int(alert.sent_email),
            int(alert.sent_telegram),
            int(alert.sent_discord),
            gemini_json,
            claude_json,
            alert.created_at.isoformat(),
        ),
    )
    return cur.lastrowid


def alert_exists_for_award(conn: sqlite3.Connection, award_id: int) -> bool:
    row = conn.execute("SELECT id FROM alerts WHERE award_id=?", (award_id,)).fetchone()
    return row is not None


_SENT_COLUMNS = frozenset({"console", "email", "telegram", "discord"})


def mark_alert_sent(
    conn: sqlite3.Connection,
    alert_id: int,
    channel: str,
) -> None:
    if channel not in _SENT_COLUMNS:
        raise ValueError(
            f"Unknown notification channel {channel!r}; expected one of "
            + ", ".join(sorted(_SENT_COLUMNS))
        )
    # Column name is validated against a fixed allow-list above, never
    # interpolated from caller input.
    conn.execute(f"UPDATE alerts SET sent_{channel}=1 WHERE id=?", (alert_id,))


def list_alerts(conn: sqlite3.Connection, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT al.*, aw.awardee_name_raw, aw.award_amount, aw.agency, aw.source
           FROM alerts al JOIN awards aw ON al.award_id=aw.id
           ORDER BY al.created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def get_alert_by_id(conn: sqlite3.Connection, alert_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT al.*, aw.*
           FROM alerts al JOIN awards aw ON al.award_id=aw.id
           WHERE al.id=?""",
        (alert_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Source run audit
# ---------------------------------------------------------------------------

def start_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO source_runs (source,started_at,status) VALUES (?,?,?)",
        (source, utcnow().isoformat(), "running"),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    seen: int,
    inserted: int,
    updated: int,
    error: str = "",
) -> None:
    conn.execute(
        """UPDATE source_runs SET
           finished_at=?,status=?,records_seen=?,records_inserted=?,
           records_updated=?,error_message=?
           WHERE id=?""",
        (utcnow().isoformat(), status, seen, inserted, updated, error, run_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Auto-Trading & Alpha Play Database Operations
# ---------------------------------------------------------------------------

def get_trade_config(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM trade_configs WHERE id=1").fetchone()
    if not row:
        conn.execute("""
            INSERT OR IGNORE INTO trade_configs (
                id, engine_mode, default_allocation, min_ratio_pct, min_score,
                take_profit_pct, stop_loss_pct, trailing_stop, paper_cash_balance, updated_at
            ) VALUES (1, 'PAPER', 1000.0, 10.0, 70, 10.0, 4.0, 0, 50000.0, ?)
        """, (utcnow().isoformat(),))
        conn.commit()
        row = conn.execute("SELECT * FROM trade_configs WHERE id=1").fetchone()
    return dict(row) if row else {}


def update_trade_config(conn: sqlite3.Connection, **kwargs) -> dict[str, Any]:
    valid_keys = {
        "engine_mode", "default_allocation", "min_ratio_pct", "min_score",
        "take_profit_pct", "stop_loss_pct", "trailing_stop", "paper_cash_balance"
    }
    updates = []
    params = []
    for k, v in kwargs.items():
        if k in valid_keys and v is not None:
            updates.append(f"{k} = ?")
            params.append(v)
    if updates:
        updates.append("updated_at = ?")
        params.append(utcnow().isoformat())
        params.append(1)
        sql = f"UPDATE trade_configs SET {', '.join(updates)} WHERE id = ?"
        conn.execute(sql, params)
        conn.commit()
    return get_trade_config(conn)


def get_company_approvals(conn: sqlite3.Connection, max_market_cap: float = 10_000_000_000.0) -> list[dict[str, Any]]:
    rows = conn.execute("""
        SELECT COALESCE(co.ticker, ca.ticker) as ticker,
               COALESCE(co.company_name, ca.company_name, '') as company_name,
               co.market_cap, co.sector,
               COALESCE(ca.is_approved, 0) as is_approved,
               COALESCE(ca.auto_trade_enabled, 0) as auto_trade_enabled,
               COALESCE(ca.max_allocation, 1000.0) as max_allocation,
               COALESCE(ca.notes, '') as notes,
               ca.updated_at
        FROM companies co
        LEFT JOIN company_trade_approvals ca ON co.ticker = ca.ticker
        WHERE (co.market_cap IS NULL OR co.market_cap <= ?)
        UNION
        SELECT ca.ticker,
               COALESCE(co.company_name, ca.company_name, '') as company_name,
               co.market_cap, co.sector,
               COALESCE(ca.is_approved, 0) as is_approved,
               COALESCE(ca.auto_trade_enabled, 0) as auto_trade_enabled,
               COALESCE(ca.max_allocation, 1000.0) as max_allocation,
               COALESCE(ca.notes, '') as notes,
               ca.updated_at
        FROM company_trade_approvals ca
        LEFT JOIN companies co ON ca.ticker = co.ticker
        WHERE (co.market_cap IS NULL OR co.market_cap <= ?)
        ORDER BY market_cap DESC NULLS LAST
    """, (max_market_cap, max_market_cap)).fetchall()
    return [dict(r) for r in rows]


def get_company_approval(conn: sqlite3.Connection, ticker: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM company_trade_approvals WHERE ticker=?", (ticker.upper(),)).fetchone()
    return dict(row) if row else None


def set_company_approval(
    conn: sqlite3.Connection,
    ticker: str,
    is_approved: int,
    auto_trade_enabled: int,
    max_allocation: float | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    ticker = ticker.upper()
    now = utcnow().isoformat()
    existing = get_company_approval(conn, ticker)
    if existing:
        max_alloc = max_allocation if max_allocation is not None else existing.get("max_allocation", 1000.0)
        n = notes if notes is not None else existing.get("notes", "")
        conn.execute("""
            UPDATE company_trade_approvals
            SET is_approved=?, auto_trade_enabled=?, max_allocation=?, notes=?, updated_at=?
            WHERE ticker=?
        """, (is_approved, auto_trade_enabled, max_alloc, n, now, ticker))
    else:
        # Look up company name
        co_row = conn.execute("SELECT company_name FROM companies WHERE ticker=?", (ticker,)).fetchone()
        co_name = co_row["company_name"] if co_row else ""
        max_alloc = max_allocation if max_allocation is not None else 1000.0
        n = notes or ""
        conn.execute("""
            INSERT INTO company_trade_approvals (
                ticker, company_name, is_approved, auto_trade_enabled, max_allocation, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (ticker, co_name, is_approved, auto_trade_enabled, max_alloc, n, now))
    conn.commit()
    return get_company_approval(conn, ticker) or {}


def insert_order(conn: sqlite3.Connection, order: dict[str, Any]) -> int:
    now = utcnow().isoformat()
    cur = conn.execute("""
        INSERT INTO orders (
            award_id, ticker, side, status, mode, shares, signal_price, fill_price,
            total_cost, take_profit_price, stop_loss_price, trigger_ratio_pct,
            verdict, headline, strategy_note, created_at, filled_at, closed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        order.get("award_id"),
        order["ticker"].upper(),
        order.get("side", "BUY"),
        order.get("status", "PENDING_APPROVAL"),
        order.get("mode", "PAPER"),
        order["shares"],
        order["signal_price"],
        order.get("fill_price"),
        order.get("total_cost"),
        order.get("take_profit_price"),
        order.get("stop_loss_price"),
        order.get("trigger_ratio_pct"),
        order.get("verdict"),
        order.get("headline"),
        order.get("strategy_note"),
        order.get("created_at", now),
        order.get("filled_at"),
        order.get("closed_at"),
    ))
    conn.commit()
    return cur.lastrowid


def get_orders(conn: sqlite3.Connection, limit: int = 100, status: str | None = None) -> list[dict[str, Any]]:
    if status:
        rows = conn.execute(
            "SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_order_by_id(conn: sqlite3.Connection, order_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return dict(row) if row else None


def update_order_status(
    conn: sqlite3.Connection,
    order_id: int,
    status: str,
    fill_price: float | None = None,
    total_cost: float | None = None,
) -> None:
    now = utcnow().isoformat()
    if status == "FILLED":
        conn.execute("""
            UPDATE orders SET status=?, fill_price=?, total_cost=?, filled_at=?
            WHERE id=?
        """, (status, fill_price, total_cost, now, order_id))
    elif status in ("REJECTED", "CANCELLED"):
        conn.execute("UPDATE orders SET status=?, closed_at=? WHERE id=?", (status, now, order_id))
    else:
        conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()


def insert_position(conn: sqlite3.Connection, pos: dict[str, Any]) -> int:
    now = utcnow().isoformat()
    cur = conn.execute("""
        INSERT INTO positions (
            order_id, ticker, mode, shares, entry_price, current_price,
            take_profit_price, stop_loss_price, unrealized_pnl, unrealized_pnl_pct,
            status, opened_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        pos.get("order_id"),
        pos["ticker"].upper(),
        pos.get("mode", "PAPER"),
        pos["shares"],
        pos["entry_price"],
        pos.get("current_price", pos["entry_price"]),
        pos.get("take_profit_price"),
        pos.get("stop_loss_price"),
        pos.get("unrealized_pnl", 0.0),
        pos.get("unrealized_pnl_pct", 0.0),
        pos.get("status", "OPEN"),
        pos.get("opened_at", now),
        now,
    ))
    conn.commit()
    return cur.lastrowid


def get_positions(conn: sqlite3.Connection, status: str = "OPEN") -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM positions WHERE status=? ORDER BY id DESC",
        (status,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_position_by_id(conn: sqlite3.Connection, position_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
    return dict(row) if row else None


def update_position_price(conn: sqlite3.Connection, position_id: int, current_price: float) -> None:
    row = conn.execute("SELECT shares, entry_price FROM positions WHERE id=?", (position_id,)).fetchone()
    if not row:
        return
    shares = row["shares"]
    entry_price = row["entry_price"]
    unrealized_pnl = (current_price - entry_price) * shares
    unrealized_pnl_pct = ((current_price - entry_price) / entry_price) * 100.0 if entry_price else 0.0
    now = utcnow().isoformat()
    conn.execute("""
        UPDATE positions
        SET current_price=?, unrealized_pnl=?, unrealized_pnl_pct=?, updated_at=?
        WHERE id=?
    """, (current_price, round(unrealized_pnl, 2), round(unrealized_pnl_pct, 2), now, position_id))
    conn.commit()


def close_position(
    conn: sqlite3.Connection,
    position_id: int,
    exit_price: float,
    exit_reason: str = "MANUAL_CLOSE",
) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM positions WHERE id=?", (position_id,)).fetchone()
    if not row or row["status"] == "CLOSED":
        return None
    shares = row["shares"]
    entry_price = row["entry_price"]
    realized_pnl = (exit_price - entry_price) * shares
    realized_pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0 if entry_price else 0.0
    now = utcnow().isoformat()

    conn.execute("""
        UPDATE positions
        SET status='CLOSED', exit_price=?, realized_pnl=?, realized_pnl_pct=?,
            exit_reason=?, closed_at=?, updated_at=?
        WHERE id=?
    """, (exit_price, round(realized_pnl, 2), round(realized_pnl_pct, 2), exit_reason, now, now, position_id))

    # If paper mode, credit cash back to paper_cash_balance + realized PnL
    if row["mode"] == "PAPER":
        total_proceeds = exit_price * shares
        conn.execute("UPDATE trade_configs SET paper_cash_balance = paper_cash_balance + ? WHERE id=1", (total_proceeds,))

    conn.commit()
    return get_position_by_id(conn, position_id)


def get_trader_stats(conn: sqlite3.Connection) -> dict[str, Any]:
    cfg = get_trade_config(conn)
    cash = cfg.get("paper_cash_balance", 50000.0)

    open_positions = get_positions(conn, status="OPEN")
    open_equity = sum(p["current_price"] * p["shares"] for p in open_positions)
    open_unrealized_pnl = sum(p.get("unrealized_pnl", 0.0) for p in open_positions)

    closed_positions = get_positions(conn, status="CLOSED")
    total_realized_pnl = sum(p.get("realized_pnl", 0.0) for p in closed_positions)
    winning_trades = sum(1 for p in closed_positions if (p.get("realized_pnl") or 0) > 0)
    losing_trades = sum(1 for p in closed_positions if (p.get("realized_pnl") or 0) < 0)
    total_closed = len(closed_positions)
    win_rate = round((winning_trades / total_closed) * 100.0, 1) if total_closed > 0 else 0.0

    pending_orders = conn.execute("SELECT count(*) FROM orders WHERE status='PENDING_APPROVAL'").fetchone()[0]

    return {
        "engine_mode": cfg.get("engine_mode", "PAPER"),
        "default_allocation": cfg.get("default_allocation", 1000.0),
        "min_ratio_pct": cfg.get("min_ratio_pct", 10.0),
        "take_profit_pct": cfg.get("take_profit_pct", 10.0),
        "stop_loss_pct": cfg.get("stop_loss_pct", 4.0),
        "trailing_stop": cfg.get("trailing_stop", 0),
        "paper_cash_balance": round(cash, 2),
        "portfolio_equity": round(cash + open_equity, 2),
        "open_positions_count": len(open_positions),
        "open_unrealized_pnl": round(open_unrealized_pnl, 2),
        "total_realized_pnl": round(total_realized_pnl, 2),
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "win_rate": win_rate,
        "pending_orders_count": pending_orders,
    }
