"""SQLite persistence layer."""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Generator

from govaward_alpha.config import get_settings
from govaward_alpha.models import AlertRecord, Award, Company, SourceRun

logger = logging.getLogger(__name__)


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_connection() -> Generator[sqlite3.Connection, None, None]:
    settings = get_settings()
    conn = _conn(settings.db_path)
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
"""


def _migrate(conn: sqlite3.Connection) -> None:
    """Apply additive migrations for existing databases."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(alerts)")}
    if "claude_analysis_json" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN claude_analysis_json TEXT DEFAULT ''")
        conn.commit()
        logger.info("Migration: added claude_analysis_json column to alerts")


def init_db() -> None:
    """Create all tables if they do not exist."""
    settings = get_settings()
    conn = _conn(settings.db_path)
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
        "content_hash": a.content_hash,
        "created_at": a.created_at.isoformat(),
        "updated_at": a.updated_at.isoformat(),
    }


def upsert_award(conn: sqlite3.Connection, award: Award) -> tuple[int, bool]:
    """Insert or update an award. Returns (row_id, is_new)."""
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
                content_hash,created_at,updated_at)
               VALUES
               (:source,:source_award_id,:source_url,:posted_date,:award_date,
                :awardee_name_raw,:awardee_name_normalized,:parent_company_name,:ticker,
                :agency,:sub_agency,:naics,:psc,:award_amount,:obligated_amount,
                :description,:contract_number,:award_type,:is_idiq,:is_modification,
                :is_option_exercise,:is_ceiling_only,:period_start,:period_end,
                :content_hash,:created_at,:updated_at)""",
            row,
        )
        return cur.lastrowid, True
    else:
        if existing["content_hash"] == award.content_hash:
            return existing["id"], False
        row["id"] = existing["id"]
        row["updated_at"] = datetime.utcnow().isoformat()
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
               content_hash=:content_hash, updated_at=:updated_at
               WHERE id=:id""",
            row,
        )
        return existing["id"], False


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
    cur = conn.execute(
        """INSERT INTO alerts
           (award_id,ticker,materiality_score,alert_level,reasons_json,
            sent_console,sent_email,sent_telegram,sent_discord,
            claude_analysis_json,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
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
            alert.claude_analysis_json,
            alert.created_at.isoformat(),
        ),
    )
    return cur.lastrowid


def alert_exists_for_award(conn: sqlite3.Connection, award_id: int) -> bool:
    row = conn.execute("SELECT id FROM alerts WHERE award_id=?", (award_id,)).fetchone()
    return row is not None


def mark_alert_sent(
    conn: sqlite3.Connection,
    alert_id: int,
    channel: str,
) -> None:
    col = f"sent_{channel}"
    conn.execute(f"UPDATE alerts SET {col}=1 WHERE id=?", (alert_id,))


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
        (source, datetime.utcnow().isoformat(), "running"),
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
        (datetime.utcnow().isoformat(), status, seen, inserted, updated, error, run_id),
    )
    conn.commit()
