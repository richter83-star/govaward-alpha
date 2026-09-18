"""End-to-end pipeline tests with a stubbed source adapter — no network.

Covers the fetch -> dedupe -> enrich -> score -> alert loop in
``services.scan_service``, including the run-audit counters.
"""

from __future__ import annotations

from datetime import date

import pytest

from govaward_alpha import db
from govaward_alpha.models import Award, Company
from govaward_alpha.services import scan_service


def _award(source_award_id="A1", amount=95_000_000.0, content_hash="hash-1"):
    return Award(
        source="stub",
        source_award_id=source_award_id,
        awardee_name_raw="Kratos Defense & Security Solutions, Inc.",
        agency="Department of Defense",
        posted_date=date(2026, 9, 2),
        award_amount=amount,
        obligated_amount=amount / 2,
        description="Firm fixed price contract for unmanned drone systems.",
        content_hash=content_hash,
    )


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Resolve to a fixed small-cap company; never touch SEC/yfinance/Claude."""
    company = Company(
        ticker="KTOS",
        company_name="Kratos Defense",
        market_cap=900_000_000.0,
        trailing_revenue=400_000_000.0,
        avg_volume=800_000.0,
        price=12.5,
    )
    monkeypatch.setattr(
        "govaward_alpha.enrich.award_enricher.resolve",
        lambda name: {
            "ticker": "KTOS",
            "parent_company_name": "Kratos Defense",
            "resolution_method": "stub",
        },
    )
    monkeypatch.setattr(
        "govaward_alpha.enrich.award_enricher.fetch_market_data", lambda ticker: company
    )
    monkeypatch.setattr("govaward_alpha.enrich.claude_enricher.enrich", lambda *a, **k: None)
    monkeypatch.setattr("govaward_alpha.notify.console.print_alert", lambda *a, **k: None)
    return company


@pytest.fixture
def initialised_db():
    db.init_db()


class TestRunSource:
    def test_first_run_inserts_and_alerts(self, initialised_db, stub_pipeline):
        stats = scan_service.run_source("stub", lambda: [_award()])
        assert stats["seen"] == 1
        assert stats["inserted"] == 1
        assert stats["updated"] == 0
        assert stats["unchanged"] == 0
        assert stats["alerted"] == 1
        assert stats["errors"] == 0

    def test_rescanning_the_same_award_is_unchanged_and_does_not_realert(
        self, initialised_db, stub_pipeline
    ):
        scan_service.run_source("stub", lambda: [_award()])
        stats = scan_service.run_source("stub", lambda: [_award()])
        assert stats["inserted"] == 0
        assert stats["updated"] == 0
        assert stats["unchanged"] == 1, "identical content hash is a no-op, not an update"
        assert stats["alerted"] == 0, "a known award must never re-alert"

        with db.get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1

    def test_changed_award_counts_as_updated(self, initialised_db, stub_pipeline):
        scan_service.run_source("stub", lambda: [_award()])
        stats = scan_service.run_source(
            "stub", lambda: [_award(amount=150_000_000.0, content_hash="hash-2")]
        )
        assert stats["updated"] == 1
        assert stats["unchanged"] == 0

    def test_resolved_ticker_is_persisted_back_to_the_award_row(
        self, initialised_db, stub_pipeline
    ):
        scan_service.run_source("stub", lambda: [_award()])
        with db.get_connection() as conn:
            row = conn.execute("SELECT ticker, parent_company_name, ticker_confidence FROM awards").fetchone()
        assert row["ticker"] == "KTOS"
        assert row["parent_company_name"] == "Kratos Defense"
        assert row["ticker_confidence"] in ("HIGH_CONFIDENCE", "MANUAL_OVERRIDE")

    def test_alert_idempotency_at_database_level(self, initialised_db):
        from govaward_alpha.models import AlertRecord
        with db.get_connection() as conn:
            award = _award("A-UNIQUE")
            award_id, _ = db.upsert_award(conn, award)
            alert = AlertRecord(
                award_id=award_id,
                ticker="KTOS",
                materiality_score=85,
                alert_level="RED",
            )
            id1 = db.insert_alert(conn, alert)
            id2 = db.insert_alert(conn, alert)
            assert id1 == id2
            count = conn.execute("SELECT COUNT(*) FROM alerts WHERE award_id=?", (award_id,)).fetchone()[0]
            assert count == 1

    def test_market_data_is_cached_in_companies(self, initialised_db, stub_pipeline):
        scan_service.run_source("stub", lambda: [_award()])
        with db.get_connection() as conn:
            row = db.get_company(conn, "KTOS")
        assert row is not None and row["market_cap"] == 900_000_000.0

    def test_fetch_failure_is_recorded_and_does_not_raise(self, initialised_db, stub_pipeline):
        def _boom():
            raise RuntimeError("upstream is down")

        stats = scan_service.run_source("stub", _boom)
        assert stats == {
            "seen": 0, "inserted": 0, "updated": 0,
            "unchanged": 0, "alerted": 0, "errors": 0,
        }
        with db.get_connection() as conn:
            run = conn.execute("SELECT * FROM source_runs ORDER BY id DESC").fetchone()
        assert run["status"] == "error"
        assert "upstream is down" in run["error_message"]

    def test_one_bad_award_does_not_abort_the_batch(self, initialised_db, stub_pipeline, monkeypatch):
        good = _award("GOOD", content_hash="h-good")
        bad = _award("BAD", content_hash="h-bad")

        real_score = scan_service.score_award

        def _flaky(award, company):
            if award.source_award_id == "BAD":
                raise ValueError("scoring blew up")
            return real_score(award, company)

        monkeypatch.setattr(scan_service, "score_award", _flaky)
        stats = scan_service.run_source("stub", lambda: [bad, good])
        assert stats["errors"] == 1
        assert stats["alerted"] == 1, "the good award still went through"

    def test_run_audit_row_is_written(self, initialised_db, stub_pipeline):
        scan_service.run_source("stub", lambda: [_award()])
        with db.get_connection() as conn:
            run = conn.execute("SELECT * FROM source_runs ORDER BY id DESC").fetchone()
        assert run["status"] == "success"
        assert run["source"] == "stub"
        assert run["records_seen"] == 1
        assert run["finished_at"] is not None

    def test_lookback_is_forwarded_when_the_adapter_accepts_it(self, initialised_db, stub_pipeline):
        seen = {}

        def _fetch(lookback_days=None):
            seen["lookback_days"] = lookback_days
            return []

        scan_service.run_source("stub", _fetch, lookback_days=7)
        assert seen["lookback_days"] == 7

    def test_lookback_is_omitted_for_adapters_without_the_parameter(
        self, initialised_db, stub_pipeline
    ):
        def _fetch():
            return []

        stats = scan_service.run_source("stub", _fetch, lookback_days=7)
        assert stats["errors"] == 0


class TestAlertGating:
    def test_megacap_award_is_not_alerted(self, initialised_db, monkeypatch):
        mega = Company(ticker="LMT", company_name="Lockheed", market_cap=120_000_000_000.0)
        monkeypatch.setattr(
            "govaward_alpha.enrich.award_enricher.resolve",
            lambda name: {"ticker": "LMT", "parent_company_name": "Lockheed", "resolution_method": "stub"},
        )
        monkeypatch.setattr(
            "govaward_alpha.enrich.award_enricher.fetch_market_data", lambda t: mega
        )
        stats = scan_service.run_source("stub", lambda: [_award()])
        assert stats["inserted"] == 1
        assert stats["alerted"] == 0

    def test_unresolved_award_is_stored_but_not_alerted(self, initialised_db, monkeypatch):
        monkeypatch.setattr(
            "govaward_alpha.enrich.award_enricher.resolve",
            lambda name: {"ticker": None, "parent_company_name": name, "resolution_method": "unresolved"},
        )
        stats = scan_service.run_source("stub", lambda: [_award()])
        assert stats["inserted"] == 1
        assert stats["alerted"] == 0
        with db.get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM awards").fetchone()[0] == 1
