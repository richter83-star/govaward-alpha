"""Tests for Pentagon 5:00 PM ET Digest Sniper and Extended-Hours Trading Logic."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from govaward_alpha import db
from govaward_alpha.enrich.company_resolver import resolve
from govaward_alpha.models import Award, Company, ScoreResult
from govaward_alpha.services.pentagon_sniper import PentagonSniper
from govaward_alpha.services.trade_engine import AlphaPlayDetector, PaperBroker, TradeEngine
from govaward_alpha.timeutil import (
    get_eastern_time,
    get_market_session,
    is_extended_hours,
    is_pentagon_drop_window,
)


class TestMarketSessions:
    """Test US market trading session classifications."""

    def test_regular_trading_hours(self):
        # Wednesday 2026-09-16 14:00 UTC = 10:00 AM ET
        dt_utc = datetime(2026, 9, 16, 14, 0, 0)
        assert get_market_session(dt_utc) == "REGULAR"
        assert is_extended_hours(dt_utc) is False
        assert is_pentagon_drop_window(dt_utc) is False

    def test_extended_after_hours_pentagon_window(self):
        # Wednesday 2026-09-16 21:05 UTC = 5:05 PM ET (17:05 ET)
        dt_utc = datetime(2026, 9, 16, 21, 5, 0)
        assert get_market_session(dt_utc) == "EXTENDED_AFTER_HOURS"
        assert is_extended_hours(dt_utc) is True
        assert is_pentagon_drop_window(dt_utc) is True

    def test_extended_pre_market(self):
        # Wednesday 2026-09-16 12:00 UTC = 8:00 AM ET
        dt_utc = datetime(2026, 9, 16, 12, 0, 0)
        assert get_market_session(dt_utc) == "EXTENDED_PRE_MARKET"
        assert is_extended_hours(dt_utc) is True
        assert is_pentagon_drop_window(dt_utc) is False

    def test_closed_weekend(self):
        # Saturday 2026-09-19 18:00 UTC
        dt_utc = datetime(2026, 9, 19, 18, 0, 0)
        assert get_market_session(dt_utc) == "CLOSED"
        assert is_extended_hours(dt_utc) is False
        assert is_pentagon_drop_window(dt_utc) is False

    def test_closed_overnight(self):
        # Wednesday 2026-09-16 03:00 UTC = 11:00 PM ET on Tuesday
        dt_utc = datetime(2026, 9, 16, 3, 0, 0)
        assert get_market_session(dt_utc) == "CLOSED"
        assert is_extended_hours(dt_utc) is False


class TestSubsidiaryProvenanceResolution:
    """Test resolution of defense subsidiaries and divisions to parent tickers."""

    @pytest.mark.parametrize(
        "awardee,expected_ticker",
        [
            ("Sikorsky Aircraft Corp.", "LMT"),
            ("Pratt & Whitney", "RTX"),
            ("Aerojet Rocketdyne, Inc.", "LHX"),
            ("Bell Textron Inc.", "TXT"),
            ("Newport News Shipbuilding", "HII"),
            ("Sierra Technical Services", "KTOS"),
            ("Collins Aerospace", "RTX"),
            ("General Dynamics Land Systems", "GD"),
        ],
    )
    def test_subsidiary_resolves_to_parent(self, awardee, expected_ticker):
        res = resolve(awardee)
        assert res["ticker"] == expected_ticker
        assert res["confidence"] == "MANUAL_OVERRIDE"



class TestExtendedHoursTradeExecution:
    """Test paper broker and trade engine extended-hours order handling."""

    def test_paper_broker_extended_hours_collar(self):
        with patch.object(PaperBroker, "get_current_price", return_value=40.0):
            fill_price, shares, cost = PaperBroker.execute_buy(
                ticker="KTOS",
                allocation=1000.0,
                signal_price=40.0,
                extended_hours=True,
                limit_collar_pct=0.015,
            )
            # In extended hours, price should not exceed collar limit ($40.0 * 1.015 = $40.60)
            assert fill_price <= 40.60
            assert fill_price >= 40.0
            assert shares > 0
            assert cost > 0


    def test_trade_engine_records_session(self, tmp_path):
        db.init_db()
        with db.get_connection() as conn:
            # Stage an award
            award = Award(
                source="defense_daily",
                source_award_id="DOD-TEST-99",
                awardee_name_raw="Sierra Technical Services",
                agency="Department of Defense",
                sub_agency="Air Force",
                posted_date=date(2026, 9, 16),
                award_amount=350_000_000.0,
                description="Mass manufacture of stealth target drones",
                ticker="KTOS",
                ticker_confidence="MANUAL_OVERRIDE",
            )
            company = Company(
                ticker="KTOS",
                company_name="Kratos Defense",
                market_cap=2_000_000_000.0,
                trailing_revenue=1_000_000_000.0,
            )
            score = ScoreResult(
                score=92,
                level="RED",
                reasons=["Pentagon announcement"],
                breakdown={"scale": 30},
            )

            # Force after-hours time
            with patch("govaward_alpha.services.trade_engine.get_market_session", return_value="EXTENDED_AFTER_HOURS"):
                engine = TradeEngine()
                order = engine.evaluate_and_stage_award(conn, award, company, score)
                assert order is not None
                assert order["session"] == "EXTENDED_HOURS"

                # Execute order
                pos = engine.approve_and_execute_order(conn, order["id"])
                assert pos is not None
                assert pos["status"] == "FILLED"

                # Check position table in DB
                positions = db.get_positions(conn, status="OPEN")
                ktos_pos = [p for p in positions if p["ticker"] == "KTOS"]
                assert len(ktos_pos) == 1
                assert ktos_pos[0]["session"] == "EXTENDED_HOURS"


class TestPentagonSniperIngest:
    """Test PentagonSniper ingest and alpha detection."""

    _SAMPLE_DOD_HTML = """
    <html>
      <body>
        <div class="article-body">
          <p>
            Sierra Technical Services, Tehachapi, California, is being awarded a $1,250,000,000
            cost-plus-fixed-fee contract for autonomous collaborative combat aircraft development.
            Work will be performed in Tehachapi, California, and is expected to be completed by
            September 2028. Fiscal 2026 research, development, test and evaluation funds in the
            amount of $45,000,000 are being obligated at the time of award. Air Force Life Cycle
            Management Center, Wright-Patterson Air Force Base, Ohio, is the contracting activity
            (FA8650-26-C-9301).
          </p>
        </div>
      </body>
    </html>
    """

    def test_snipe_local_file(self, tmp_path):
        db.init_db()
        sample_file = tmp_path / "dod_contracts_20260916.html"
        sample_file.write_text(self._SAMPLE_DOD_HTML, encoding="utf-8")

        sniper = PentagonSniper(drop_dir=tmp_path)
        with db.get_connection() as conn:
            res = sniper.snipe(conn, file_path=sample_file, dry_run=True)
            assert res["awards_detected"] >= 1
            assert len(res["alpha_plays"]) >= 1
            assert res["alpha_plays"][0]["ticker"] == "KTOS"
            assert res["alpha_plays"][0]["amount"] == 1_250_000_000.0
            assert res["alpha_plays"][0]["ratio_pct"] >= 10.0

