"""Dashboard API tests via FastAPI's TestClient — in-process, no server, no port."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from govaward_alpha import db
from govaward_alpha.models import AlertRecord, Award, Company


@pytest.fixture
def client(tmp_path):
    """A dashboard client bound to the per-test database."""
    db.init_db()
    from govaward_alpha.dashboard.app import app

    return TestClient(app)


@pytest.fixture
def seeded(client):
    """Two alerts: a small-cap ORANGE and a mega-cap WATCH."""
    with db.get_connection() as conn:
        db.upsert_company(
            conn,
            Company(
                ticker="KTOS", company_name="Kratos Defense", market_cap=900_000_000.0,
                trailing_revenue=400_000_000.0, sector="Industrials", exchange="NMS",
            ),
        )
        db.upsert_company(
            conn,
            Company(ticker="LMT", company_name="Lockheed", market_cap=1.2e11, sector="Industrials"),
        )
        for sid, ticker, amount, score, level, posted in [
            ("A1", "KTOS", 95_000_000.0, 72, "ORANGE", date(2026, 9, 2)),
            ("A2", "LMT", 800_000_000.0, 40, "WATCH", date(2026, 8, 1)),
        ]:
            award_id, _ = db.upsert_award(
                conn,
                Award(
                    source="usaspending", source_award_id=sid,
                    awardee_name_raw=f"{ticker} Contractor", agency="Department of Defense",
                    posted_date=posted, award_amount=amount, obligated_amount=amount / 2,
                    description="Radar systems", ticker=ticker, content_hash=f"h-{sid}",
                ),
            )
            db.insert_alert(
                conn,
                AlertRecord(
                    award_id=award_id, ticker=ticker, materiality_score=score,
                    alert_level=level, reasons_json='["because"]',
                ),
            )
    return client


class TestEndpointsOnEmptyDatabase:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/stats", "/api/alerts", "/api/filter-options", "/api/companies",
            "/api/source-runs", "/api/charts/scores", "/api/charts/ticker-value",
            "/api/awards", "/api/unresolved",
        ],
    )
    def test_returns_200(self, client, path):
        assert client.get(path).status_code == 200

    def test_index_page_is_served(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_missing_alert_is_404(self, client):
        assert client.get("/api/alerts/999").status_code == 404


class TestStats:
    def test_counts_by_level(self, seeded):
        body = seeded.get("/api/stats").json()
        assert body["total_alerts"] == 2
        assert body["by_level"]["ORANGE"] == 1
        assert body["by_level"]["WATCH"] == 1
        assert body["unique_tickers"] == 2
        assert body["total_contract_value"] == pytest.approx(895_000_000.0)


class TestAlertFilters:
    def test_unfiltered_returns_all(self, seeded):
        assert len(seeded.get("/api/alerts").json()) == 2

    def test_filter_by_level(self, seeded):
        rows = seeded.get("/api/alerts", params={"level": "ORANGE"}).json()
        assert [r["ticker"] for r in rows] == ["KTOS"]

    def test_filter_by_score_range(self, seeded):
        rows = seeded.get("/api/alerts", params={"min_score": 60}).json()
        assert [r["ticker"] for r in rows] == ["KTOS"]

    def test_filter_by_ticker_substring(self, seeded):
        rows = seeded.get("/api/alerts", params={"ticker": "kto"}).json()
        assert [r["ticker"] for r in rows] == ["KTOS"]

    def test_filter_by_market_cap_ceiling(self, seeded):
        rows = seeded.get(
            "/api/alerts", params={"max_market_cap": 10_000_000_000, "require_market_cap": 1}
        ).json()
        assert [r["ticker"] for r in rows] == ["KTOS"]

    def test_filter_by_posted_date(self, seeded):
        rows = seeded.get("/api/alerts", params={"posted_after": "2026-09-01"}).json()
        assert [r["ticker"] for r in rows] == ["KTOS"]

    def test_sort_by_amount(self, seeded):
        rows = seeded.get("/api/alerts", params={"sort": "amount_desc"}).json()
        assert [r["ticker"] for r in rows] == ["LMT", "KTOS"]

    def test_reasons_json_is_decoded(self, seeded):
        rows = seeded.get("/api/alerts").json()
        assert rows[0]["reasons"] == ["because"]
        assert rows[0]["claude"] is None

    def test_company_columns_are_joined_in(self, seeded):
        rows = seeded.get("/api/alerts", params={"ticker": "KTOS"}).json()
        assert rows[0]["market_cap"] == 900_000_000.0
        assert rows[0]["sector"] == "Industrials"


class TestLimitBounds:
    """`limit` was unbounded; a huge value should be rejected, not honoured."""

    def test_limit_above_the_cap_is_rejected(self, seeded):
        assert seeded.get("/api/alerts", params={"limit": 10_000_000}).status_code == 422

    def test_zero_limit_is_rejected(self, seeded):
        assert seeded.get("/api/alerts", params={"limit": 0}).status_code == 422

    def test_limit_within_range_is_honoured(self, seeded):
        rows = seeded.get("/api/alerts", params={"limit": 1}).json()
        assert len(rows) == 1

    def test_source_runs_limit_is_bounded(self, seeded):
        assert seeded.get("/api/source-runs", params={"limit": 10_000}).status_code == 422


class TestSingleAlertAndOptions:
    def test_get_alert_by_id(self, seeded):
        alert_id = seeded.get("/api/alerts").json()[0]["id"]
        body = seeded.get(f"/api/alerts/{alert_id}").json()
        assert body["id"] == alert_id
        assert body["awardee_name_raw"].endswith("Contractor")

    def test_filter_options_are_populated(self, seeded):
        body = seeded.get("/api/filter-options").json()
        assert body["levels"] == ["RED", "ORANGE", "YELLOW", "WATCH"]
        assert "Department of Defense" in body["agencies"]
        assert "usaspending" in body["sources"]
        assert set(body["tickers"]) == {"KTOS", "LMT"}


class TestCharts:
    def test_score_histogram_has_ten_buckets(self, seeded):
        body = seeded.get("/api/charts/scores").json()
        assert len(body["labels"]) == 10 and len(body["counts"]) == 10
        assert sum(body["counts"]) == 2

    def test_ticker_value_chart(self, seeded):
        body = seeded.get("/api/charts/ticker-value").json()
        assert body["tickers"][0] == "LMT"  # highest total award value first
        assert body["totals"][0] == pytest.approx(800_000_000.0)


class TestAllAwardsPipeline:
    def test_list_all_awards(self, seeded):
        resp = seeded.get("/api/awards")
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 2
        assert {r["ticker"] for r in rows} == {"KTOS", "LMT"}

    def test_filter_by_source(self, seeded):
        resp = seeded.get("/api/awards", params={"source": "usaspending"})
        assert resp.status_code == 200
        assert len(resp.json()) == 2

        resp2 = seeded.get("/api/awards", params={"source": "defense_daily"})
        assert resp2.status_code == 200
        assert len(resp2.json()) == 0

    def test_filter_by_ticker(self, seeded):
        resp = seeded.get("/api/awards", params={"ticker": "KTOS"})
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["ticker"] == "KTOS"


class TestUnresolvedEndpoint:
    def test_unresolved_returns_unmapped_entities(self, seeded):
        # Insert an award with no ticker and large amount
        with db.get_connection() as conn:
            db.upsert_award(
                conn,
                Award(
                    source="defense_daily",
                    source_award_id="DEF-UNRESOLVED-1",
                    awardee_name_raw="Apex Unmapped Technologies LLC",
                    agency="Air Force",
                    award_amount=25_000_000.0,
                    ticker=None,
                    content_hash="h-unres-1",
                ),
            )

        resp = seeded.get("/api/unresolved", params={"min_amount": 5_000_000})
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) >= 1
        names = [r["awardee_name"] for r in rows]
        assert "Apex Unmapped Technologies LLC" in names
        apex = next(r for r in rows if r["awardee_name"] == "Apex Unmapped Technologies LLC")
        assert apex["total_value"] == 25_000_000.0
        assert apex["contract_count"] == 1


class TestNewEndpoints:
    def test_chart_agencies(self, seeded):
        resp = seeded.get("/api/charts/agencies")
        assert resp.status_code == 200
        data = resp.json()
        assert "agencies" in data and "totals" in data and "counts" in data
        assert len(data["agencies"]) >= 1
        assert "Department of Defense" in data["agencies"]

    def test_export_csv(self, seeded):
        resp = seeded.get("/api/export/csv")
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        text = resp.text
        assert "ID,Source,Source Award ID" in text
        assert "KTOS" in text
        assert "LMT" in text

    def test_add_override_endpoint(self, seeded):
        # Backup manual overrides file to avoid dirtying git state
        from pathlib import Path
        override_file = Path("data/manual_ticker_overrides.yaml")
        backup = override_file.read_text(encoding="utf-8") if override_file.exists() else None
        try:
            # Insert unmapped award
            with db.get_connection() as conn:
                db.upsert_award(
                    conn,
                    Award(
                        source="usaspending",
                        source_award_id="TEST-OVERRIDE-AWD",
                        awardee_name_raw="Orbital Flight Systems Inc",
                        agency="Space Force",
                        award_amount=15_000_000.0,
                        ticker=None,
                        content_hash="h-override-test",
                    ),
                )

            resp = seeded.post(
                "/api/overrides",
                json={"raw_name": "Orbital Flight Systems Inc", "ticker": "KTOS"},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            assert body["ticker"] == "KTOS"
            assert body["updated_awards"] >= 1

            # Verify database updated
            awards_resp = seeded.get("/api/awards", params={"ticker": "KTOS"})
            tickers = [a["ticker"] for a in awards_resp.json()]
            assert "KTOS" in tickers
        finally:
            if backup is not None:
                override_file.write_text(backup, encoding="utf-8")


class TestRetailCatalysts:
    def test_empty_db_returns_empty_list(self, client):
        resp = client.get("/api/catalysts")
        assert resp.status_code == 200
        body = resp.json()
        assert body["catalysts"] == []
        assert body["summary"]["total_catalysts"] == 0

    def test_seeded_filters_small_mid_cap_and_50m_plus(self, seeded):
        resp = seeded.get("/api/catalysts")
        assert resp.status_code == 200
        body = resp.json()
        catalysts = body["catalysts"]
        # KTOS should be included: $95M award (>= $50M) and $900M cap (between 50M and 10B)
        # LMT should be excluded: $120B cap (> 10B mid-cap ceiling)
        assert len(catalysts) == 1
        c = catalysts[0]
        assert c["ticker"] == "KTOS"
        assert c["award_amount"] == 95_000_000.0
        assert c["ratio_pct"] == pytest.approx(10.6, abs=0.2)
        assert "gemini_strategy" in c
        strat = c["gemini_strategy"]
        assert strat["action_verdict"] in ["HIGH CONVICTION BUY", "MOMENTUM WATCH", "TRAP / AVOID"]
        assert "headline" in strat
        assert "trade_strategy" in strat
        assert "retail_thesis" in strat
        assert "trap_check" in strat


class TestScanTriggerEndpoint:
    def test_scan_status_and_trigger(self, client, monkeypatch):
        # Test status endpoint
        resp = client.get("/api/scan/status")
        assert resp.status_code == 200
        assert "running" in resp.json()

        # Mock run_source to avoid live external network calls
        monkeypatch.setattr("govaward_alpha.services.scan_service.run_source", lambda *args, **kwargs: {"seen": 0})
        resp_trig = client.post("/api/scan/trigger")
        assert resp_trig.status_code == 200
        assert resp_trig.json()["status"] in ["started", "busy"]
