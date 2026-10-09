"""Unit and integration tests for AlpacaBroker and TradeEngine live execution routing."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from pytest_httpx import HTTPXMock

from govaward_alpha.services.alpaca_broker import AlpacaBroker, AlpacaError
from govaward_alpha.services.trade_engine import TradeEngine, PaperBroker
from govaward_alpha.models import Award, Company, ScoreResult
from govaward_alpha import db


@pytest.fixture
def alpaca_broker():
    return AlpacaBroker(
        api_key="TEST_KEY",
        secret_key="TEST_SECRET",
        endpoint="https://paper-api.alpaca.markets",
    )


class TestAlpacaBrokerConfig:
    def test_configuration_flags(self):
        b = AlpacaBroker(api_key="PK123", secret_key="SK123", endpoint="https://paper-api.alpaca.markets")
        assert b.is_configured is True
        assert b.is_paper is True

        b_live = AlpacaBroker(api_key="AK123", secret_key="SK123", endpoint="https://api.alpaca.markets")
        assert b_live.is_paper is False

        b_empty = AlpacaBroker(api_key="", secret_key="")
        assert b_empty.is_configured is False


class TestAlpacaBrokerAccount:
    def test_get_account_success(self, alpaca_broker: AlpacaBroker, httpx_mock: HTTPXMock):
        mock_acct = {
            "id": "acct_123",
            "account_number": "PA123456",
            "status": "ACTIVE",
            "currency": "USD",
            "buying_power": "40000.00",
            "cash": "10000.00",
            "portfolio_value": "12500.00",
            "pattern_day_trader": False,
            "daytrade_count": 1,
            "trading_blocked": False,
        }
        httpx_mock.add_response(
            url="https://paper-api.alpaca.markets/v2/account",
            status_code=200,
            json=mock_acct,
        )

        res = alpaca_broker.get_account()
        assert res["account_number"] == "PA123456"
        assert res["buying_power"] == "40000.00"

    def test_verify_credentials_success(self, alpaca_broker: AlpacaBroker, httpx_mock: HTTPXMock):
        mock_acct = {
            "id": "acct_123",
            "account_number": "PA123456",
            "status": "ACTIVE",
            "currency": "USD",
            "buying_power": "40000.00",
            "cash": "10000.00",
            "portfolio_value": "12500.00",
            "pattern_day_trader": False,
            "daytrade_count": 1,
            "trading_blocked": False,
        }
        httpx_mock.add_response(
            url="https://paper-api.alpaca.markets/v2/account",
            status_code=200,
            json=mock_acct,
        )

        verified = alpaca_broker.verify_credentials()
        assert verified["ok"] is True
        assert verified["buying_power"] == 40000.00
        assert verified["is_paper"] is True

    def test_get_account_unauthorized(self, alpaca_broker: AlpacaBroker, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://paper-api.alpaca.markets/v2/account",
            status_code=401,
            text='{"message": "unauthorized"}',
        )
        with pytest.raises(AlpacaError) as exc:
            alpaca_broker.get_account()
        assert exc.value.status_code == 401

    def test_verify_credentials_unauthorized(self, alpaca_broker: AlpacaBroker, httpx_mock: HTTPXMock):
        httpx_mock.add_response(
            url="https://paper-api.alpaca.markets/v2/account",
            status_code=401,
            text='{"message": "unauthorized"}',
        )
        res = alpaca_broker.verify_credentials()
        assert res["ok"] is False
        assert res["status_code"] == 401


class TestAlpacaOrders:
    def test_execute_buy_regular_hours_bracket(self, alpaca_broker: AlpacaBroker, httpx_mock: HTTPXMock):
        # 1. Market clock says market is open
        httpx_mock.add_response(
            url="https://paper-api.alpaca.markets/v2/clock",
            status_code=200,
            json={"is_open": True, "timestamp": "2026-09-16T14:00:00Z"},
        )
        # 2. Latest bar quote
        httpx_mock.add_response(
            url="https://data.alpaca.markets/v2/stocks/bars/latest?symbols=KTOS&feed=iex",
            status_code=200,
            json={"bars": {"KTOS": {"c": 50.0}}},
        )
        # 3. Post order response
        order_resp = {
            "id": "alpaca_ord_999",
            "symbol": "KTOS",
            "qty": "20.0",
            "side": "buy",
            "type": "market",
            "order_class": "bracket",
            "filled_avg_price": "50.05",
        }
        httpx_mock.add_response(
            url="https://paper-api.alpaca.markets/v2/orders",
            status_code=200,
            json=order_resp,
        )

        broker_id, fill_price, shares, total_cost = alpaca_broker.execute_buy(
            ticker="KTOS",
            allocation=1000.0,
            signal_price=50.0,
            take_profit_price=55.0,
            stop_loss_price=48.0,
        )

        assert broker_id == "alpaca_ord_999"
        assert fill_price == 50.05
        assert shares == 20.0
        assert total_cost == 1001.0

        # Verify sent payload contained bracket order specs
        sent_req = [r for r in httpx_mock.get_requests() if r.url.path == "/v2/orders"][0]
        payload = json.loads(sent_req.content)
        assert payload["order_class"] == "bracket"
        assert payload["take_profit"]["limit_price"] == "55.0"
        assert payload["stop_loss"]["stop_price"] == "48.0"

    def test_execute_buy_extended_hours_limit_collar(self, alpaca_broker: AlpacaBroker, httpx_mock: HTTPXMock):
        # 1. Market clock: closed (5:00 PM defense drop)
        httpx_mock.add_response(
            url="https://paper-api.alpaca.markets/v2/clock",
            status_code=200,
            json={"is_open": False, "timestamp": "2026-09-16T17:05:00Z"},
        )
        # 2. Quote
        httpx_mock.add_response(
            url="https://data.alpaca.markets/v2/stocks/bars/latest?symbols=KBR&feed=iex",
            status_code=200,
            json={"bars": {"KBR": {"c": 40.0}}},
        )
        # 3. Post order response
        order_resp = {
            "id": "alpaca_ext_111",
            "symbol": "KBR",
            "qty": "25.0",
            "side": "buy",
            "type": "limit",
            "limit_price": "40.6",
            "extended_hours": True,
        }
        httpx_mock.add_response(
            url="https://paper-api.alpaca.markets/v2/orders",
            status_code=200,
            json=order_resp,
        )

        broker_id, limit_price, shares, total_cost = alpaca_broker.execute_buy(
            ticker="KBR",
            allocation=1000.0,
            signal_price=40.0,
            limit_collar_pct=0.015,  # 1.5% collar -> $40.60
        )

        assert broker_id == "alpaca_ext_111"
        assert limit_price == 40.6
        assert shares == 25.0

        sent_req = [r for r in httpx_mock.get_requests() if r.url.path == "/v2/orders"][0]
        payload = json.loads(sent_req.content)
        assert payload["type"] == "limit"
        assert payload["extended_hours"] is True
        assert payload["limit_price"] == "40.6"

    def test_execute_sell_position_liquidation(self, alpaca_broker: AlpacaBroker, httpx_mock: HTTPXMock):
        # 1. Quote
        httpx_mock.add_response(
            url="https://data.alpaca.markets/v2/stocks/bars/latest?symbols=KTOS&feed=iex",
            status_code=200,
            json={"bars": {"KTOS": {"c": 52.5}}},
        )
        # 2. DELETE position
        httpx_mock.add_response(
            url="https://paper-api.alpaca.markets/v2/positions/KTOS",
            status_code=200,
            json={"id": "exit_ord_555", "symbol": "KTOS"},
        )

        broker_id, exit_price = alpaca_broker.execute_sell("KTOS", shares=20.0)
        assert broker_id == "exit_ord_555"
        assert exit_price == 52.5


class TestTradeEngineLiveRouting:
    def test_live_order_execution_with_mock_broker(self, db_conn, monkeypatch):
        """Verify TradeEngine correctly executes a LIVE order and persists broker_order_id."""
        # LIVE is locked unless the server opts in (security.live_allowed); this test opts in.
        monkeypatch.setenv("GOVAWARD_ALLOW_LIVE", "yes")
        conn = db_conn

        # Set engine mode to LIVE
        db.update_trade_config(conn, engine_mode="LIVE")

        # Mock Alpaca live broker
        mock_live_broker = MagicMock()
        mock_live_broker.is_configured = True
        mock_live_broker.get_account.return_value = {
            "status": "ACTIVE",
            "buying_power": "50000.00",
            "trading_blocked": False,
        }
        mock_live_broker.execute_buy.return_value = ("alpaca_live_ord_777", 45.0, 22.0, 990.0)
        mock_live_broker.get_current_price.return_value = 45.0

        engine = TradeEngine(live_broker=mock_live_broker)

        # Stage an order directly
        order_data = {
            "ticker": "KTOS",
            "side": "BUY",
            "status": "PENDING_APPROVAL",
            "mode": "LIVE",
            "shares": 22.0,
            "signal_price": 45.0,
            "total_cost": 990.0,
            "take_profit_price": 49.5,
            "stop_loss_price": 43.2,
            "trigger_ratio_pct": 18.5,
            "verdict": "HIGH CONVICTION BUY",
        }
        order_id = db.insert_order(conn, order_data)

        # Execute order
        filled_order = engine.approve_and_execute_order(conn, order_id)
        assert filled_order is not None
        assert filled_order["status"] == "FILLED"
        assert filled_order["broker_order_id"] == "alpaca_live_ord_777"
        assert filled_order["fill_price"] == 45.0

        # Verify position in DB has broker_order_id
        positions = db.get_positions(conn, status="OPEN")
        assert len(positions) == 1
        pos = positions[0]
        assert pos["ticker"] == "KTOS"
        assert pos["mode"] == "LIVE"
        assert pos["broker_order_id"] == "alpaca_live_ord_777"

        # Verify mock broker buy was invoked with TP and SL
        mock_live_broker.execute_buy.assert_called_once_with(
            ticker="KTOS",
            allocation=990.0,
            signal_price=45.0,
            take_profit_price=49.5,
            stop_loss_price=43.2,
        )
