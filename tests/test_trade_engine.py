"""Tests for GovAward Alpha — Auto-Trader & Alpha Play Execution Engine."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from govaward_alpha import db
from govaward_alpha.models import Award, Company, ScoreResult
from govaward_alpha.services.trade_engine import AlphaPlayDetector, PaperBroker, TradeEngine


@pytest.fixture
def client(tmp_path):
    """A dashboard client bound to the per-test database."""
    db.init_db()
    from govaward_alpha.dashboard.app import app

    return TestClient(app)


class TestAlphaPlayDetector:
    def test_alpha_play_qualified(self):
        award = Award(
            source="sam_gov",
            source_award_id="A-100",
            awardee_name_raw="KBR Wyle Services",
            agency="Department of Defense",
            posted_date=date(2026, 9, 1),
            award_amount=1_900_000_000.0,
            obligated_amount=1_900_000_000.0,
            description="Base operations logistics support",
            ticker="KBR",
        )
        company = Company(
            ticker="KBR",
            company_name="KBR Inc",
            market_cap=8_500_000_000.0,
            trailing_revenue=7_000_000_000.0,
        )
        score = ScoreResult(
            score=88,
            level="RED",
            reasons=["Mega contract"],
            breakdown={"scale": 30},
        )
        is_alpha, reason, ratio = AlphaPlayDetector.is_alpha_play(award, company, score)
        assert is_alpha is True
        assert ratio == 22.4
        assert "Verified Alpha Catalyst" in reason

    def test_alpha_play_below_ratio_threshold(self):
        award = Award(
            source="sam_gov",
            source_award_id="A-101",
            awardee_name_raw="Kratos",
            agency="Air Force",
            posted_date=date(2026, 9, 1),
            award_amount=30_000_000.0,
            obligated_amount=30_000_000.0,
            ticker="KTOS",
        )
        company = Company(
            ticker="KTOS",
            company_name="Kratos Defense",
            market_cap=1_000_000_000.0,
        )
        score = ScoreResult(score=50, level="YELLOW", reasons=[], breakdown={})
        is_alpha, reason, ratio = AlphaPlayDetector.is_alpha_play(
            award, company, score, min_ratio_pct=10.0
        )
        assert is_alpha is False
        assert ratio == 3.0
        assert "below required" in reason

    def test_alpha_play_mega_cap_disqualified(self):
        award = Award(
            source="sam_gov",
            source_award_id="A-102",
            awardee_name_raw="Lockheed Martin",
            agency="Navy",
            posted_date=date(2026, 9, 1),
            award_amount=1_200_000_000.0,
            obligated_amount=1_200_000_000.0,
            ticker="LMT",
        )
        company = Company(
            ticker="LMT",
            company_name="Lockheed Martin Corp",
            market_cap=110_000_000_000.0,  # $110B > $10B limit
        )
        score = ScoreResult(score=60, level="ORANGE", reasons=[], breakdown={})
        is_alpha, reason, _ = AlphaPlayDetector.is_alpha_play(award, company, score)
        assert is_alpha is False
        assert "Mega-cap" in reason

    def test_alpha_play_idiq_ceiling_trap(self):
        award = Award(
            source="sam_gov",
            source_award_id="A-103",
            awardee_name_raw="Kratos",
            agency="Navy",
            posted_date=date(2026, 9, 1),
            award_amount=200_000_000.0,
            obligated_amount=0.0,
            is_idiq=True,
            is_ceiling_only=True,
            ticker="KTOS",
        )
        company = Company(
            ticker="KTOS",
            company_name="Kratos Defense",
            market_cap=900_000_000.0,
        )
        score = ScoreResult(score=40, level="WATCH", reasons=[], breakdown={})
        is_alpha, reason, _ = AlphaPlayDetector.is_alpha_play(award, company, score)
        assert is_alpha is False
        assert "ceiling or unfunded" in reason

    def test_alpha_play_gemini_trap_verdict(self):
        award = Award(
            source="sam_gov",
            source_award_id="A-104",
            awardee_name_raw="Small Corp",
            agency="Army",
            posted_date=date(2026, 9, 1),
            award_amount=100_000_000.0,
            obligated_amount=100_000_000.0,
            ticker="SML",
        )
        company = Company(
            ticker="SML",
            company_name="Small Corp",
            market_cap=200_000_000.0,
        )
        score = ScoreResult(score=85, level="RED", reasons=[], breakdown={})
        strategy = {
            "action_verdict": "TRAP / AVOID",
            "trap_check": "Non-exclusive shared multiple-award vehicle with 15 vendors",
        }
        is_alpha, reason, _ = AlphaPlayDetector.is_alpha_play(
            award, company, score, gemini_strategy=strategy
        )
        assert is_alpha is False
        assert "Flagged as TRAP" in reason


class TestTradeEngine:
    def test_stage_order_pending_approval(self):
        db.init_db()
        engine = TradeEngine()

        with db.get_connection() as conn:
            award = Award(
                source="sam_gov",
                source_award_id="A-200",
                awardee_name_raw="Kratos Defense",
                agency="Air Force",
                posted_date=date(2026, 9, 1),
                award_amount=250_000_000.0,
                obligated_amount=250_000_000.0,
                ticker="KTOS",
            )
            award_id, _ = db.upsert_award(conn, award)
            award.id = award_id

            company = Company(
                ticker="KTOS",
                company_name="Kratos Defense",
                market_cap=1_000_000_000.0,
            )
            db.upsert_company(conn, company)

            score = ScoreResult(score=80, level="RED", reasons=[], breakdown={})

            with patch.object(PaperBroker, "get_current_price", return_value=50.0):
                res = engine.evaluate_and_stage_award(conn, award, company, score)

            assert res is not None
            assert res["status"] == "PENDING_APPROVAL"
            assert res["ticker"] == "KTOS"
            assert res["signal_price"] == 50.0
            assert res["take_profit_price"] == 55.0  # +10%
            assert res["stop_loss_price"] == 48.0   # -4%

            # Ensure duplicate order staging is prevented
            res_dup = engine.evaluate_and_stage_award(conn, award, company, score)
            assert res_dup is None

    def test_auto_trade_whitelisted_company(self):
        db.init_db()
        engine = TradeEngine()

        with db.get_connection() as conn:
            # Set company whitelist approval with auto_trade_enabled=True
            db.set_company_approval(
                conn,
                ticker="KBR",
                is_approved=True,
                auto_trade_enabled=True,
                max_allocation=2500.0,
                notes="Whitelisted for full auto-execution",
            )

            award = Award(
                source="sam_gov",
                source_award_id="A-201",
                awardee_name_raw="KBR Inc",
                agency="Army",
                posted_date=date(2026, 9, 1),
                award_amount=1_500_000_000.0,
                obligated_amount=1_500_000_000.0,
                ticker="KBR",
            )
            award_id, _ = db.upsert_award(conn, award)
            award.id = award_id

            company = Company(
                ticker="KBR",
                company_name="KBR Inc",
                market_cap=7_000_000_000.0,
            )
            db.upsert_company(conn, company)
            score = ScoreResult(score=90, level="RED", reasons=[], breakdown={})

            with patch.object(PaperBroker, "get_current_price", return_value=60.0):
                res = engine.evaluate_and_stage_award(conn, award, company, score)

            assert res is not None
            assert res["status"] == "FILLED"
            assert res["ticker"] == "KBR"

            # Check open position was automatically created
            positions = db.get_positions(conn, status="OPEN")
            assert len(positions) == 1
            assert positions[0]["ticker"] == "KBR"

    def test_manual_approval_and_rejection(self):
        db.init_db()
        engine = TradeEngine()

        with db.get_connection() as conn:
            order_id = db.insert_order(
                conn,
                {
                    "ticker": "KTOS",
                    "side": "BUY",
                    "status": "PENDING_APPROVAL",
                    "mode": "PAPER",
                    "shares": 20.0,
                    "signal_price": 50.0,
                    "total_cost": 1000.0,
                    "take_profit_price": 55.0,
                    "stop_loss_price": 48.0,
                },
            )

            with patch.object(PaperBroker, "get_current_price", return_value=50.0):
                filled = engine.approve_and_execute_order(conn, order_id)

            assert filled["status"] == "FILLED"
            positions = db.get_positions(conn, status="OPEN")
            assert len(positions) == 1
            assert positions[0]["ticker"] == "KTOS"

            # Test rejection on a separate order
            order_id_2 = db.insert_order(
                conn,
                {
                    "ticker": "AVAV",
                    "side": "BUY",
                    "status": "PENDING_APPROVAL",
                    "mode": "PAPER",
                    "shares": 10.0,
                    "signal_price": 100.0,
                    "total_cost": 1000.0,
                },
            )
            rejected = engine.reject_order(conn, order_id_2)
            assert rejected["status"] == "REJECTED"

    def test_position_monitoring_take_profit_and_stop_loss(self):
        db.init_db()
        engine = TradeEngine()

        with db.get_connection() as conn:
            # Create two open positions: one will hit take-profit, one will hit stop-loss
            pos1_id = db.insert_position(
                conn,
                {
                    "ticker": "WIN1",
                    "mode": "PAPER",
                    "shares": 10.0,
                    "entry_price": 100.0,
                    "current_price": 100.0,
                    "take_profit_price": 110.0,
                    "stop_loss_price": 95.0,
                    "status": "OPEN",
                },
            )
            pos2_id = db.insert_position(
                conn,
                {
                    "ticker": "LOSS1",
                    "mode": "PAPER",
                    "shares": 10.0,
                    "entry_price": 100.0,
                    "current_price": 100.0,
                    "take_profit_price": 110.0,
                    "stop_loss_price": 95.0,
                    "status": "OPEN",
                },
            )

            # WIN1 rises to $112 (hits TP), LOSS1 falls to $92 (hits SL)
            def mock_price(ticker, fallback_price=35.0):
                if ticker == "WIN1":
                    return 112.0
                if ticker == "LOSS1":
                    return 92.0
                return fallback_price

            with patch.object(PaperBroker, "get_current_price", side_effect=mock_price):
                engine.monitor_and_update_positions(conn)

            pos1 = db.get_position_by_id(conn, pos1_id)
            assert pos1["status"] == "CLOSED"
            assert pos1["exit_reason"] == "TAKE_PROFIT"
            assert pos1["realized_pnl"] > 0

            pos2 = db.get_position_by_id(conn, pos2_id)
            assert pos2["status"] == "CLOSED"
            assert pos2["exit_reason"] == "STOP_LOSS"
            assert pos2["realized_pnl"] < 0

    def test_manual_close_position(self):
        db.init_db()
        engine = TradeEngine()

        with db.get_connection() as conn:
            pos_id = db.insert_position(
                conn,
                {
                    "ticker": "MAN1",
                    "mode": "PAPER",
                    "shares": 20.0,
                    "entry_price": 50.0,
                    "current_price": 52.0,
                    "status": "OPEN",
                },
            )

            with patch.object(PaperBroker, "get_current_price", return_value=53.0):
                closed = engine.close_position_manually(conn, pos_id)

            assert closed is not None
            assert closed["status"] == "CLOSED"
            assert closed["exit_reason"] == "MANUAL_CLOSE"
            assert closed["realized_pnl"] > 0


class TestTraderApiEndpoints:
    def test_get_status_and_update_config(self, client):
        res = client.get("/api/trader/status")
        assert res.status_code == 200
        data = res.json()
        assert data["engine_mode"] == "PAPER"
        assert data["paper_cash_balance"] == 50000.0

        # Update configuration
        update_res = client.post(
            "/api/trader/config",
            json={
                "engine_mode": "PAPER",
                "min_ratio_pct": 15.0,
                "default_allocation": 2000.0,
                "take_profit_pct": 12.0,
                "stop_loss_pct": 5.0,
            },
        )
        assert update_res.status_code == 200
        up_data = update_res.json()
        assert up_data["min_ratio_pct"] == 15.0
        assert up_data["default_allocation"] == 2000.0
        assert up_data["take_profit_pct"] == 12.0

    def test_company_approvals_api(self, client):
        # Set approval for KBR
        post_res = client.post(
            "/api/trader/approvals/KBR",
            json={
                "is_approved": True,
                "auto_trade_enabled": True,
                "max_allocation": 3000.0,
                "notes": "Approved for testing",
            },
        )
        assert post_res.status_code == 200
        assert post_res.json()["ok"] is True

        # List approvals
        list_res = client.get("/api/trader/approvals")
        assert list_res.status_code == 200
        approvals = list_res.json()
        assert any(a["ticker"] == "KBR" and a["auto_trade_enabled"] == 1 for a in approvals)

    def test_orders_and_positions_flow_api(self, client):
        # Seed an award and stage it
        with db.get_connection() as conn:
            award = Award(
                source="sam_gov",
                source_award_id="API-AWARD-1",
                awardee_name_raw="KBR Inc",
                agency="Army",
                posted_date=date(2026, 9, 1),
                award_amount=1_200_000_000.0,
                obligated_amount=1_200_000_000.0,
                ticker="KBR",
            )
            award_id, _ = db.upsert_award(conn, award)

            company = Company(
                ticker="KBR",
                company_name="KBR Inc",
                market_cap=6_000_000_000.0,
            )
            db.upsert_company(conn, company)

        with patch.object(PaperBroker, "get_current_price", return_value=60.0):
            stage_res = client.post(f"/api/trader/stage_catalyst/{award_id}")
            assert stage_res.status_code == 200
            order = stage_res.json()
            order_id = order["id"]
            assert order["status"] == "PENDING_APPROVAL"

            # List orders
            orders_res = client.get("/api/trader/orders?status=PENDING_APPROVAL")
            assert orders_res.status_code == 200
            assert any(o["id"] == order_id for o in orders_res.json())

            # Approve order via API
            appr_res = client.post(f"/api/trader/orders/{order_id}/approve")
            assert appr_res.status_code == 200
            assert appr_res.json()["status"] == "FILLED"

            # Check open positions via API
            pos_res = client.get("/api/trader/positions?status=OPEN")
            assert pos_res.status_code == 200
            positions = pos_res.json()
            assert len(positions) == 1
            pos_id = positions[0]["id"]
            assert positions[0]["ticker"] == "KBR"

            # Close position via API
            close_res = client.post(f"/api/trader/positions/{pos_id}/close")
            assert close_res.status_code == 200
            assert close_res.json()["status"] == "CLOSED"
