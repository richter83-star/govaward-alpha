"""Persistence-layer tests: dedupe semantics, alert bookkeeping, migrations."""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from govaward_alpha import db
from govaward_alpha.models import AlertRecord, Award, Company


def _award(source_award_id="A1", amount=95_000_000.0, content_hash="hash-1", **extra):
    base = dict(
        source="usaspending",
        source_award_id=source_award_id,
        source_url="https://example.gov/a",
        posted_date=date(2026, 9, 2),
        awardee_name_raw="Acme Defense Corp",
        awardee_name_normalized="acme defense",
        agency="Department of Defense",
        award_amount=amount,
        obligated_amount=amount / 2,
        description="Radar systems",
        content_hash=content_hash,
    )
    base.update(extra)
    return Award(**base)


class TestInitDb:
    def test_creates_all_tables(self, db_conn):
        names = {
            r[0]
            for r in db_conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert {"awards", "companies", "alerts", "source_runs"} <= names

    def test_is_idempotent(self, db_conn):
        db.init_db()  # second call must not raise or drop data
        db.init_db()

    def test_migration_adds_claude_column_to_legacy_db(self, tmp_path, monkeypatch):
        """_migrate must be additive on a DB created before the column existed."""
        monkeypatch.setenv("DB_PATH", str(tmp_path / "legacy.db"))
        from govaward_alpha import config

        config._settings = None
        legacy_ddl = db.DDL.replace(
            "    claude_analysis_json TEXT   DEFAULT '',\n", ""
        )
        conn = sqlite3.connect(str(tmp_path / "legacy.db"))
        conn.executescript(legacy_ddl)
        conn.execute(
            "INSERT INTO alerts (award_id,ticker,materiality_score,alert_level,created_at)"
            " VALUES (1,'KTOS',72,'ORANGE','2026-09-02')"
        )
        conn.commit()
        conn.close()

        db.init_db()

        conn = sqlite3.connect(str(tmp_path / "legacy.db"))
        cols = {r[1] for r in conn.execute("PRAGMA table_info(alerts)")}
        rows = conn.execute("SELECT ticker FROM alerts").fetchall()
        conn.close()
        assert "claude_analysis_json" in cols
        assert rows == [("KTOS",)], "existing rows must survive the migration"


class TestUpsertAward:
    def test_new_award_is_inserted(self, db_conn):
        award_id, status = db.upsert_award(db_conn, _award())
        assert status == db.INSERTED
        assert award_id > 0

    def test_identical_hash_is_a_no_op(self, db_conn):
        first_id, _ = db.upsert_award(db_conn, _award())
        second_id, status = db.upsert_award(db_conn, _award())
        assert status == db.UNCHANGED
        assert second_id == first_id

    def test_changed_hash_updates_in_place(self, db_conn):
        first_id, _ = db.upsert_award(db_conn, _award(amount=95_000_000.0, content_hash="hash-1"))
        second_id, status = db.upsert_award(
            db_conn, _award(amount=120_000_000.0, content_hash="hash-2")
        )
        assert status == db.UPDATED
        assert second_id == first_id
        row = db.get_award_by_id(db_conn, first_id)
        assert row["award_amount"] == 120_000_000.0
        assert db_conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0] == 1

    def test_same_id_from_a_different_source_is_a_separate_row(self, db_conn):
        db.upsert_award(db_conn, _award(source_award_id="X"))
        _, status = db.upsert_award(db_conn, _award(source_award_id="X", source="sam_gov"))
        assert status == db.INSERTED
        assert db_conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0] == 2


class TestAlerts:
    def _seed(self, conn):
        award_id, _ = db.upsert_award(conn, _award())
        alert_id = db.insert_alert(
            conn,
            AlertRecord(award_id=award_id, ticker="KTOS", materiality_score=72, alert_level="ORANGE"),
        )
        return award_id, alert_id

    def test_alert_exists_for_award(self, db_conn):
        award_id, _ = self._seed(db_conn)
        assert db.alert_exists_for_award(db_conn, award_id) is True
        assert db.alert_exists_for_award(db_conn, award_id + 999) is False

    @pytest.mark.parametrize("channel", ["console", "email", "telegram", "discord"])
    def test_mark_alert_sent_sets_the_right_column(self, db_conn, channel):
        _, alert_id = self._seed(db_conn)
        db.mark_alert_sent(db_conn, alert_id, channel)
        row = db.get_alert_by_id(db_conn, alert_id)
        assert row[f"sent_{channel}"] == 1

    def test_mark_alert_sent_rejects_unknown_channel(self, db_conn):
        """The column name used to be interpolated straight from the argument."""
        _, alert_id = self._seed(db_conn)
        with pytest.raises(ValueError, match="Unknown notification channel"):
            db.mark_alert_sent(db_conn, alert_id, "console=1 WHERE 1=1; --")

    def test_get_alert_by_id_returns_alert_scoped_columns(self, db_conn):
        """alerts and awards share id/ticker/created_at; the alert's must win."""
        award_id, alert_id = self._seed(db_conn)
        db_conn.execute("UPDATE awards SET ticker='OTHER' WHERE id=?", (award_id,))
        row = db.get_alert_by_id(db_conn, alert_id)
        assert row["id"] == alert_id
        assert row["ticker"] == "KTOS"

    def test_list_alerts_joins_award_fields(self, db_conn):
        self._seed(db_conn)
        rows = db.list_alerts(db_conn, limit=10)
        assert len(rows) == 1
        assert rows[0]["awardee_name_raw"] == "Acme Defense Corp"
        assert rows[0]["award_amount"] == 95_000_000.0


class TestCompaniesAndRuns:
    def test_upsert_company_is_idempotent(self, db_conn):
        co = Company(ticker="KTOS", company_name="Kratos", market_cap=900_000_000.0)
        db.upsert_company(db_conn, co)
        db.upsert_company(db_conn, co.model_copy(update={"market_cap": 950_000_000.0}))
        row = db.get_company(db_conn, "KTOS")
        assert row["market_cap"] == 950_000_000.0
        assert db_conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 1

    def test_run_audit_roundtrip(self, db_conn):
        run_id = db.start_run(db_conn, "usaspending")
        db.finish_run(db_conn, run_id, "success", seen=10, inserted=3, updated=1)
        row = db_conn.execute("SELECT * FROM source_runs WHERE id=?", (run_id,)).fetchone()
        assert row["status"] == "success"
        assert (row["records_seen"], row["records_inserted"], row["records_updated"]) == (10, 3, 1)

    def test_list_unresolved_only_returns_untickered_awards(self, db_conn):
        db.upsert_award(db_conn, _award(source_award_id="NO_TICKER"))
        resolved_id, _ = db.upsert_award(db_conn, _award(source_award_id="HAS_TICKER"))
        db_conn.execute("UPDATE awards SET ticker='KTOS' WHERE id=?", (resolved_id,))
        rows = db.list_unresolved_awards(db_conn, limit=10)
        assert [r["source_award_id"] for r in rows] == ["NO_TICKER"]
