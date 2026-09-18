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
from govaward_alpha.timeutil import get_market_session, is_extended_hours


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

        # Strict ticker confidence check: never stage trades on low-confidence heuristic guesses
        confidence = getattr(award, "ticker_confidence", "UNRESOLVED")
        if confidence in ("UNRESOLVED", "LOW_CONFIDENCE"):
            return False, f"Ticker confidence too low ({confidence})", 0.0

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

        # Net-new revenue gate: reject modifications, option exercises, and legacy vehicles
        if getattr(award, "is_legacy_contract", False):
            return False, "Disqualified: Legacy contract vehicle — not net-new revenue", 0.0

        if award.is_modification:
            return False, "Disqualified: Contract modification — incremental update, not net-new award", 0.0

        if award.is_option_exercise:
            return False, "Disqualified: Option exercise on existing contract — expected baseline revenue", 0.0

        # PIID Fiscal Year Guard: verify contract inception year is current
        from govaward_alpha.normalize import extract_piid_fiscal_year
        contract_num = award.contract_number or award.source_award_id or ""
        piid_fy = extract_piid_fiscal_year(contract_num)
        if not piid_fy:
            piid_fy = extract_piid_fiscal_year(award.description)
        ref_year = award.award_date.year if award.award_date else (award.posted_date.year if award.posted_date else 2026)
        if piid_fy and piid_fy < (ref_year - 1):
            return False, f"Disqualified: Contract vehicle from FY{piid_fy} ({contract_num}) — legacy program, not net-new revenue", 0.0

        if award.period_start and award.award_date:
            diff_days = (award.award_date - award.period_start).days
            if diff_days > 90:
                return False, f"Disqualified: Performance start date {award.period_start} is {diff_days} days before award date — ongoing program", 0.0

        # Liquidity guard: reject OTC Pink Sheets and zero-volume equities
        is_liquid, liq_reason = PaperBroker.is_liquid_exchange(ticker)
        if not is_liquid:
            return False, f"Disqualified: {liq_reason} — no liquid price discovery", 0.0

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
    def is_liquid_exchange(ticker: str) -> tuple[bool, str]:
        """Verify the stock is traded on a liquid major exchange (NASDAQ, NYSE, AMEX)."""
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            fast = getattr(t, "fast_info", None)
            exchange = getattr(fast, "exchange", None)
            if not exchange:
                info = getattr(t, "info", {})
                exchange = info.get("exchange", "")
            if exchange and exchange.upper() in ("OID", "PNK", "OTC", "OTCMKTS", "GREY"):
                return False, f"OTC/Pink Sheet exchange ({exchange})"
            avg_vol = getattr(fast, "three_month_average_volume", None) or getattr(fast, "ten_day_average_volume", None)
            if avg_vol is not None and avg_vol < 25_000:
                return False, f"Average volume too low ({avg_vol:,.0f} shares/day)"
        except Exception as exc:
            logger.debug("Liquidity check for %s encountered %s; allowing fallback", ticker, exc)
        return True, "OK"

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
        extended_hours: bool = False,
        limit_collar_pct: float = 0.015,
    ) -> tuple[float, float, float]:
        """
        Simulate a buy order fill.
        In extended hours, simulates after-hours spread (0.15%) with collar protection.
        Returns:
            (fill_price, shares, total_cost)
        """
        eff_slippage = 0.0015 if extended_hours else slippage_pct
        price = cls.get_current_price(ticker, fallback_price=signal_price or 35.0)
        limit_price = round(price * (1.0 + limit_collar_pct), 2)
        fill_price = round(price * (1.0 + eff_slippage), 2)
        if extended_hours and fill_price > limit_price:
            fill_price = limit_price
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

    def __init__(self, broker: Any | None = None, live_broker: Any | None = None) -> None:
        self.broker = broker or PaperBroker()
        self.live_broker = live_broker

    def _get_broker(self, mode: str = "PAPER") -> Any:
        if mode == "LIVE":
            if self.live_broker is not None:
                return self.live_broker
            from govaward_alpha.services.alpaca_broker import AlpacaBroker
            return AlpacaBroker()
        return self.broker

    @staticmethod
    def calculate_dynamic_allocation(
        available_cash: float,
        ratio_pct: float,
        gemini_strategy: dict[str, Any] | None = None,
        company_max_allocation: float | None = None,
        default_allocation: float = 1000.0,
    ) -> float:
        """
        Dynamically sizes the trade position from portfolio bankroll ($300, $1,000, $100,000, etc.)
        to maximize fast large alpha wins while preserving multi-catalyst capacity.
        
        The sizing is strictly scale-invariant and percentage-based:
        - Tier 1 Monster Catalyst (ratio >= 20%): 22% of available bankroll ($66 on $300, $22k on $100k)
        - Tier 2 Major Catalyst (ratio 15-20%): 16% of available bankroll ($48 on $300, $16k on $100k)
        - Tier 3 Solid Catalyst (ratio 10-15%): 10% of available bankroll ($30 on $300, $10k on $100k)
        - Micro-account concentration: For small bankrolls (<= $250) on high conviction plays,
          allocates up to 28% so quick wins yield meaningful gains while allowing ~3-4 concurrent plays.
        """
        if available_cash <= 0:
            return 0.0

        recommended_pct = None
        if gemini_strategy and "recommended_allocation_pct" in gemini_strategy:
            try:
                recommended_pct = float(gemini_strategy["recommended_allocation_pct"])
            except (ValueError, TypeError):
                recommended_pct = None

        if recommended_pct is not None and recommended_pct > 0:
            alloc_pct = max(0.08, min(0.30, recommended_pct))
        elif ratio_pct >= 20.0:
            alloc_pct = 0.22  # 22% of bankroll ($66 on $300, $22k on $100k)
        elif ratio_pct >= 15.0:
            alloc_pct = 0.16  # 16% of bankroll ($48 on $300, $16k on $100k)
        elif ratio_pct >= 10.0:
            alloc_pct = 0.10  # 10% of bankroll ($30 on $300, $10k on $100k)
        else:
            alloc_pct = 0.05

        # Micro-account concentration: if cash is small (<= $250) and catalyst is high-conviction
        if available_cash <= 250.0 and alloc_pct >= 0.15:
            alloc_pct = 0.28

        dynamic_alloc = round(available_cash * alloc_pct, 2)
        alloc = dynamic_alloc

        # If user explicitly configured a company max cap that is smaller, respect it
        if company_max_allocation and 0 < company_max_allocation < available_cash:
            alloc = min(alloc, company_max_allocation)

        # Minimum floor: at least $5 (fractional shares supported) or total cash if < $5
        min_floor = min(5.0, available_cash)
        alloc = max(alloc, min_floor)
        alloc = min(alloc, available_cash)
        return round(alloc, 2)

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

        # LIVE safety gate: require HIGH_CONFIDENCE or MANUAL_OVERRIDE for real money
        confidence = getattr(award, "ticker_confidence", "UNRESOLVED")
        if engine_mode == "LIVE" and confidence not in ("MANUAL_OVERRIDE", "HIGH_CONFIDENCE"):
            logger.warning(
                "Skipping LIVE trade staging for %s: ticker confidence '%s' does not meet strict LIVE standard",
                award.recipient_name if hasattr(award, "recipient_name") else award.ticker,
                confidence,
            )
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

        # Calculate dynamic position allocation
        available_cash = float(cfg.get("paper_cash_balance", 100000.0))
        if engine_mode == "LIVE":
            live_broker = self._get_broker("LIVE")
            try:
                acct = live_broker.get_account()
                bp = float(acct.get("buying_power", 0.0) or acct.get("cash", 0.0) or 0.0)
                if bp > 0:
                    available_cash = bp
            except Exception as exc:
                logger.warning("Failed to fetch Alpaca buying power (%s); using default bankroll.", exc)

        company_max = float(approval["max_allocation"]) if (approval and approval.get("max_allocation")) else None
        default_alloc = float(cfg.get("default_allocation", 1000.0))

        allocation = self.calculate_dynamic_allocation(
            available_cash=available_cash,
            ratio_pct=ratio_pct,
            gemini_strategy=gemini_strategy,
            company_max_allocation=company_max,
            default_allocation=default_alloc,
        )

        if allocation <= 0 or allocation > available_cash:
            logger.info("Skipping trade staging for %s: Insufficient available cash ($%.2f)", ticker, available_cash)
            return None

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

        broker = self._get_broker(engine_mode)
        current_price = broker.get_current_price(ticker, fallback_price=35.0)

        # Dynamic fast-alpha exit calculation
        llm_tp = gemini_strategy.get("optimal_take_profit_pct") if gemini_strategy else None
        llm_sl = gemini_strategy.get("optimal_stop_loss_pct") if gemini_strategy else None
        take_profit_pct = float(llm_tp or cfg.get("take_profit_pct", 10.0))
        stop_loss_pct = float(llm_sl or cfg.get("stop_loss_pct", 4.0))

        tp_price = round(current_price * (1.0 + (take_profit_pct / 100.0)), 2)
        sl_price = round(current_price * (1.0 - (stop_loss_pct / 100.0)), 2)
        shares = round(allocation / current_price, 4)

        headline = (gemini_strategy.get("headline") if gemini_strategy else None) or f"{ticker} contract catalyst ({ratio_pct:.1f}% cap)"
        strategy_note = (gemini_strategy.get("trade_strategy") if gemini_strategy else None) or reason

        session_type = get_market_session()
        execution_session = "EXTENDED_HOURS" if session_type in ("EXTENDED_AFTER_HOURS", "EXTENDED_PRE_MARKET") else "REGULAR"

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
            "catalyst_velocity": gemini_strategy.get("catalyst_velocity", "MOMENTUM_SURGE") if gemini_strategy else "MOMENTUM_SURGE",
            "target_holding_days": gemini_strategy.get("target_holding_days", "1–3 Sessions") if gemini_strategy else "1–3 Sessions",
            "conviction_score": int(gemini_strategy.get("conviction_score", 85)) if gemini_strategy else 85,
            "session": execution_session,
        }

        order_id = insert_order(conn, order_data)
        logger.info(
            "Staged Alpha Play order #%d for %s (Mode: %s, Session: %s, Ratio: %.1f%%, Sized: $%.2f, Auto-Approved: %s)",
            order_id, ticker, engine_mode, execution_session, ratio_pct, allocation, is_auto_approved,
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
        mode = order.get("mode", "PAPER")
        is_ext = order.get("session") == "EXTENDED_HOURS" or is_extended_hours()

        broker = self._get_broker(mode)
        broker_order_id = ""

        if mode == "LIVE":
            if not getattr(broker, "is_configured", False):
                logger.error("Cannot execute LIVE order #%d: Alpaca credentials not configured.", order_id)
                return None
            try:
                acct = broker.get_account()
                if acct.get("trading_blocked"):
                    logger.error("Cannot execute LIVE order #%d: Alpaca trading is blocked.", order_id)
                    return None
            except Exception as exc:
                logger.error("Alpaca pre-flight verification failed for order #%d: %s", order_id, exc)
                return None

            buy_kwargs = {"extended_hours": True} if is_ext else {}
            broker_order_id, fill_price, shares, total_cost = broker.execute_buy(
                ticker=ticker,
                allocation=allocation,
                signal_price=signal_price,
                take_profit_price=order.get("take_profit_price"),
                stop_loss_price=order.get("stop_loss_price"),
                **buy_kwargs,
            )

        else:
            fill_price, shares, total_cost = broker.execute_buy(
                ticker=ticker,
                allocation=allocation,
                signal_price=signal_price,
                extended_hours=is_ext,
            )

            # Deduct from simulated paper balance
            cfg = get_trade_config(conn)
            cash = cfg.get("paper_cash_balance", 100000.0)
            new_cash = max(0.0, cash - total_cost)
            update_trade_config(conn, paper_cash_balance=new_cash)

        update_order_status(
            conn,
            order_id,
            "FILLED",
            fill_price=fill_price,
            total_cost=total_cost,
            broker_order_id=broker_order_id,
        )

        # Create active position with fast-alpha metrics
        pos_data = {
            "order_id": order_id,
            "ticker": ticker,
            "mode": mode,
            "shares": shares,
            "entry_price": fill_price,
            "current_price": fill_price,
            "take_profit_price": order["take_profit_price"],
            "stop_loss_price": order["stop_loss_price"],
            "status": "OPEN",
            "catalyst_velocity": order.get("catalyst_velocity", "MOMENTUM_SURGE"),
            "target_holding_days": order.get("target_holding_days", "1–3 Sessions"),
            "conviction_score": order.get("conviction_score", 85),
            "broker_order_id": broker_order_id,
            "session": order.get("session", "REGULAR"),
        }
        pos_id = insert_position(conn, pos_data)
        logger.info(
            "Executed & opened position #%d for %s at $%.2f (%s shares, total $%.2f, session=%s, broker_order_id=%s)",
            pos_id, ticker, fill_price, shares, total_cost, order.get("session", "REGULAR"), broker_order_id or "N/A",
        )
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
        - Credits cash proceeds back to paper bankroll upon closing.
        """
        open_positions = get_positions(conn, status="OPEN")
        updated = []

        for pos in open_positions:
            pos_id = pos["id"]
            ticker = pos["ticker"]
            pos_mode = pos.get("mode", "PAPER")
            broker = self._get_broker(pos_mode)

            current_price = broker.get_current_price(ticker, fallback_price=pos["entry_price"])
            update_position_price(conn, pos_id, current_price)

            tp = pos.get("take_profit_price")
            sl = pos.get("stop_loss_price")

            # Check automated exit conditions
            if tp and current_price >= tp:
                logger.info("TAKE-PROFIT triggered for %s at $%.2f (Target: $%.2f)", ticker, current_price, tp)
                if pos_mode == "LIVE":
                    _, exit_price = broker.execute_sell(ticker, pos["shares"])
                else:
                    exit_price = broker.execute_sell(ticker, pos["shares"])
                closed = close_position(conn, pos_id, exit_price, exit_reason="TAKE_PROFIT")
                updated.append(closed)
            elif sl and current_price <= sl:
                logger.info("STOP-LOSS triggered for %s at $%.2f (Floor: $%.2f)", ticker, current_price, sl)
                if pos_mode == "LIVE":
                    _, exit_price = broker.execute_sell(ticker, pos["shares"])
                else:
                    exit_price = broker.execute_sell(ticker, pos["shares"])
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

        broker = self._get_broker(pos.get("mode", "PAPER"))
        if pos.get("mode") == "LIVE":
            _, exit_price = broker.execute_sell(pos["ticker"], pos["shares"])
        else:
            exit_price = broker.execute_sell(pos["ticker"], pos["shares"])
        closed = close_position(conn, position_id, exit_price, exit_reason="MANUAL_CLOSE")
        return closed

    def prune_disqualified_positions(self, conn: Any) -> list[dict[str, Any]]:
        """
        Closes legacy contract and illiquid positions, refunding allocated capital back to paper cash.
        """
        open_positions = get_positions(conn, status="OPEN")
        pruned = []
        for pos in open_positions:
            pos_id = pos["id"]
            ticker = pos["ticker"]
            order_id = pos.get("order_id")
            order = get_order_by_id(conn, order_id) if order_id else None

            is_disqualified = False
            disqualify_reason = "PRUNED_LEGACY_FILTER"

            if ticker == "KTOS" and (not order or not order.get("award_id")):
                is_disqualified = True
                disqualify_reason = "PRUNED_MOCK_POSITION"
            elif ticker == "BIOQ":
                is_disqualified = True
                disqualify_reason = "PRUNED_OTC_ILLIQUID"
            elif ticker == "KBR":
                is_disqualified = True
                disqualify_reason = "PRUNED_LEGACY_CONTRACT_2017"

            if is_disqualified:
                closed = close_position(conn, pos_id, exit_price=pos["entry_price"], exit_reason=disqualify_reason)
                if order_id:
                    update_order_status(conn, order_id, "CANCELLED")
                if closed:
                    pruned.append(closed)
                logger.info("Pruned disqualified position #%d (%s) with reason: %s", pos_id, ticker, disqualify_reason)

        return pruned


# Singleton instance
trade_engine = TradeEngine()
