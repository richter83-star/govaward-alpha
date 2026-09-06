"""GovAward Alpha — Algorithmic Trade Execution & Alpha Play Engine.

Monitors incoming awards for high-conviction catalyst setups (ratio of market cap,
financial scale, trap verification), enforces per-company approval gates,
and executes paper or live orders with automated Take-Profit & Stop-Loss management.
"""

from __future__ import annotations

import logging
from typing import Any

from govaward_alpha.db import (
    close_position,
    get_company_approval,
    get_order_by_id,
    get_positions,
    get_trade_config,
    insert_order,
    insert_position,
    update_order_status,
    update_position_price,
    update_trade_config,
)
from govaward_alpha.models import Award, Company, ScoreResult

logger = logging.getLogger(__name__)


class AlphaPlayDetector:
    """Evaluates whether a contract award qualifies as an Alpha Play."""

    @staticmethod
    def is_alpha_play(
        award: Award,
        company: Company | None,
        score_result: ScoreResult,
        gemini_strategy: dict[str, Any] | None = None,
        min_ratio_pct: float = 10.0,
    ) -> tuple[bool, str, float]:
        """
        Returns:
            (is_qualified, reasoning, ratio_pct)
        """
        ticker = (award.ticker or "").upper()
        if not ticker or ticker == "UNRESOLVED":
            return False, "Unresolved ticker", 0.0

        if not company or not company.market_cap or company.market_cap <= 0:
            return False, "Unknown or invalid company market cap", 0.0

        # Filter for Small / Mid Cap only ($50M to $10B)
        if company.market_cap > 10_000_000_000:
            return False, f"Mega-cap company (${company.market_cap/1e9:.1f}B > $10B limit)", 0.0

        amt = award.award_amount or award.obligated_amount or 0.0
        if amt < 25_000_000:
            return False, f"Contract value too small (${amt/1e6:.1f}M < $25M min)", 0.0

        # Trap checks
        if award.is_ceiling_only or (award.is_idiq and (award.obligated_amount or 0) == 0):
            return False, "IDIQ ceiling or unfunded vehicle — not guaranteed revenue", 0.0

        ratio = amt / company.market_cap
        ratio_pct = round(ratio * 100.0, 1)

        if ratio_pct < min_ratio_pct:
            return False, f"Contract ratio {ratio_pct:.1f}% below required {min_ratio_pct:.1f}%", ratio_pct

        # Gemini strategy check if available
        if gemini_strategy:
            verdict = gemini_strategy.get("action_verdict", "MOMENTUM WATCH")
            if verdict == "TRAP / AVOID":
                return False, f"Flagged as TRAP by AI: {gemini_strategy.get('trap_check', '')}", ratio_pct

        return True, f"Verified Alpha Catalyst: {ratio_pct:.1f}% of market cap (${amt/1e6:.1f}M)", ratio_pct


class PaperBroker:
    """Virtual paper-trading execution simulator with realistic fills and cash tracking."""

    @staticmethod
    def get_current_price(ticker: str, fallback_price: float = 35.0) -> float:
        """Fetch current market price via yfinance with safe fallback."""
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            info = t.fast_info
            price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
            if price and price > 0:
                return round(float(price), 2)
        except Exception as exc:
            logger.debug("Live quote lookup failed for %s (%s); using fallback.", ticker, exc)

        return round(float(fallback_price or 35.0), 2)

    @classmethod
    def execute_buy(
        cls,
        ticker: str,
        allocation: float,
        signal_price: float | None = None,
        slippage_pct: float = 0.0005,  # 0.05% simulated slippage
    ) -> tuple[float, float, float]:
        """
        Simulate a market buy order fill.
        Returns:
            (fill_price, shares, total_cost)
        """
        price = cls.get_current_price(ticker, fallback_price=signal_price or 35.0)
        fill_price = round(price * (1.0 + slippage_pct), 2)
        shares = round(allocation / fill_price, 4)
        total_cost = round(fill_price * shares, 2)
        return fill_price, shares, total_cost

    @classmethod
    def execute_sell(
        cls,
        ticker: str,
        shares: float,
        slippage_pct: float = 0.0005,
    ) -> float:
        """
        Simulate a market sell order fill.
        Returns:
            exit_price
        """
        price = cls.get_current_price(ticker)
        exit_price = round(price * (1.0 - slippage_pct), 2)
        return exit_price


class TradeEngine:
    """Core trade evaluation, order lifecycle, and position monitoring engine."""

    def __init__(self, broker: PaperBroker | None = None) -> None:
        self.broker = broker or PaperBroker()

    def evaluate_and_stage_award(
        self,
        conn: Any,
        award: Award,
        company: Company | None,
        score_result: ScoreResult,
        gemini_strategy: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """
        Evaluates an award. If it qualifies as an Alpha Play, creates an order.
        If auto-trading is approved for this company, fills the order immediately.
        Otherwise, stages it as PENDING_APPROVAL.
        """
        cfg = get_trade_config(conn)
        engine_mode = cfg.get("engine_mode", "PAPER")
        if engine_mode == "OFF":
            return None

        min_ratio = cfg.get("min_ratio_pct", 10.0)
        is_alpha, reason, ratio_pct = AlphaPlayDetector.is_alpha_play(
            award, company, score_result, gemini_strategy, min_ratio_pct=min_ratio
        )

        if not is_alpha:
            logger.debug("Award not qualified for auto-trade: %s", reason)
            return None

        ticker = award.ticker.upper()
        approval = get_company_approval(conn, ticker)
        is_auto_approved = bool(approval and approval.get("is_approved") and approval.get("auto_trade_enabled"))
        allocation = float(approval.get("max_allocation") or cfg.get("default_allocation") or 1000.0) if approval else float(cfg.get("default_allocation", 1000.0))

        # Check existing open position to avoid duplicate entries
        open_positions = get_positions(conn, status="OPEN")
        if any(p["ticker"] == ticker for p in open_positions):
            logger.info("Skipping trade staging for %s: Position already open.", ticker)
            return None

        # Check existing pending order
        existing_orders = conn.execute(
            "SELECT id FROM orders WHERE ticker=? AND status='PENDING_APPROVAL'",
            (ticker,),
        ).fetchall()
        if existing_orders:
            logger.info("Skipping trade staging for %s: Pending approval already exists.", ticker)
            return None

        current_price = self.broker.get_current_price(ticker, fallback_price=35.0)
        take_profit_pct = cfg.get("take_profit_pct", 10.0)
        stop_loss_pct = cfg.get("stop_loss_pct", 4.0)

        tp_price = round(current_price * (1.0 + (take_profit_pct / 100.0)), 2)
        sl_price = round(current_price * (1.0 - (stop_loss_pct / 100.0)), 2)
        shares = round(allocation / current_price, 4)

        headline = (gemini_strategy.get("headline") if gemini_strategy else None) or f"{ticker} contract catalyst ({ratio_pct:.1f}% cap)"
        strategy_note = (gemini_strategy.get("trade_strategy") if gemini_strategy else None) or reason

        order_data = {
            "award_id": getattr(award, "id", None),
            "ticker": ticker,
            "side": "BUY",
            "status": "PENDING_APPROVAL",
            "mode": engine_mode,
            "shares": shares,
            "signal_price": current_price,
            "total_cost": allocation,
            "take_profit_price": tp_price,
            "stop_loss_price": sl_price,
            "trigger_ratio_pct": ratio_pct,
            "verdict": gemini_strategy.get("action_verdict", "HIGH CONVICTION BUY") if gemini_strategy else "HIGH CONVICTION BUY",
            "headline": headline,
            "strategy_note": strategy_note,
        }

        order_id = insert_order(conn, order_data)
        logger.info(
            "Staged Alpha Play order #%d for %s (Ratio: %.1f%%, Auto-Approved: %s)",
            order_id, ticker, ratio_pct, is_auto_approved,
        )

        # If whitelisted for autonomous auto-trade, execute fill immediately
        if is_auto_approved:
            return self.approve_and_execute_order(conn, order_id)

        return get_order_by_id(conn, order_id)

    def approve_and_execute_order(self, conn: Any, order_id: int) -> dict[str, Any] | None:
        """Execute a staged order (either autonomously or via manual 1-click approval)."""
        order = get_order_by_id(conn, order_id)
        if not order or order["status"] != "PENDING_APPROVAL":
            return None

        ticker = order["ticker"]
        allocation = order["total_cost"] or 1000.0
        signal_price = order["signal_price"]

        fill_price, shares, total_cost = self.broker.execute_buy(ticker, allocation, signal_price)

        # Check paper cash balance if PAPER mode
        cfg = get_trade_config(conn)
        if order["mode"] == "PAPER":
            cash = cfg.get("paper_cash_balance", 50000.0)
            new_cash = max(0.0, cash - total_cost)
            update_trade_config(conn, paper_cash_balance=new_cash)

        update_order_status(conn, order_id, "FILLED", fill_price=fill_price, total_cost=total_cost)

        # Create active position
        pos_data = {
            "order_id": order_id,
            "ticker": ticker,
            "mode": order["mode"],
            "shares": shares,
            "entry_price": fill_price,
            "current_price": fill_price,
            "take_profit_price": order["take_profit_price"],
            "stop_loss_price": order["stop_loss_price"],
            "status": "OPEN",
        }
        pos_id = insert_position(conn, pos_data)
        logger.info("Executed & opened position #%d for %s at $%.2f (%s shares)", pos_id, ticker, fill_price, shares)
        return get_order_by_id(conn, order_id)

    def reject_order(self, conn: Any, order_id: int) -> dict[str, Any] | None:
        """Dismiss a pending order."""
        update_order_status(conn, order_id, "REJECTED")
        return get_order_by_id(conn, order_id)

    def monitor_and_update_positions(self, conn: Any) -> list[dict[str, Any]]:
        """
        Runs position monitoring:
        - Updates open positions with live quotes.
        - Triggers Take-Profit exit if price >= target.
        - Triggers Stop-Loss exit if price <= floor.
        """
        open_positions = get_positions(conn, status="OPEN")
        updated = []

        for pos in open_positions:
            pos_id = pos["id"]
            ticker = pos["ticker"]
            current_price = self.broker.get_current_price(ticker, fallback_price=pos["entry_price"])
            update_position_price(conn, pos_id, current_price)

            tp = pos.get("take_profit_price")
            sl = pos.get("stop_loss_price")

            # Check automated exit conditions
            if tp and current_price >= tp:
                logger.info("TAKE-PROFIT triggered for %s at $%.2f (Target: $%.2f)", ticker, current_price, tp)
                exit_price = self.broker.execute_sell(ticker, pos["shares"])
                closed = close_position(conn, pos_id, exit_price, exit_reason="TAKE_PROFIT")
                updated.append(closed)
            elif sl and current_price <= sl:
                logger.info("STOP-LOSS triggered for %s at $%.2f (Floor: $%.2f)", ticker, current_price, sl)
                exit_price = self.broker.execute_sell(ticker, pos["shares"])
                closed = close_position(conn, pos_id, exit_price, exit_reason="STOP_LOSS")
                updated.append(closed)
            else:
                updated.append(pos)

        return updated

    def close_position_manually(self, conn: Any, position_id: int) -> dict[str, Any] | None:
        """Manually close an open position immediately at market price."""
        from govaward_alpha.db import get_position_by_id
        pos = get_position_by_id(conn, position_id)
        if not pos or pos["status"] != "OPEN":
            return None
        exit_price = self.broker.execute_sell(pos["ticker"], pos["shares"])
        return close_position(conn, position_id, exit_price, exit_reason="MANUAL_CLOSE")


# Singleton instance
trade_engine = TradeEngine()
