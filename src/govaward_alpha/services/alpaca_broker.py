"""GovAward Alpha — Alpaca Brokerage REST API v2 Integration.

Handles account synchronization, real-time market clock detection,
extended-hours limit protection, server-side bracket order creation (Take-Profit & Stop-Loss OCO),
and position management via Alpaca Markets.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from govaward_alpha.config import get_settings

logger = logging.getLogger(__name__)


class AlpacaError(Exception):
    """Base exception for Alpaca API errors."""

    def __init__(self, message: str, status_code: int | None = None, response_text: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_text = response_text


class AlpacaBroker:
    """REST API Client for Alpaca Markets (Paper and Live environments)."""

    def __init__(
        self,
        api_key: str | None = None,
        secret_key: str | None = None,
        endpoint: str | None = None,
        timeout: float = 10.0,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key if api_key is not None else settings.alpaca_api_key
        self.secret_key = secret_key if secret_key is not None else settings.alpaca_secret_key
        raw_endpoint = endpoint if endpoint is not None else settings.alpaca_endpoint
        self.endpoint = (raw_endpoint or "https://paper-api.alpaca.markets").rstrip("/")
        self.timeout = timeout

    @property
    def is_configured(self) -> bool:
        """Returns True if API credentials are non-empty."""
        return bool(self.api_key and self.secret_key)

    @property
    def is_paper(self) -> bool:
        """Returns True if the endpoint points to Alpaca paper trading."""
        return "paper" in self.endpoint.lower()

    def get_headers(self) -> dict[str, str]:
        """Headers required for Alpaca authentication."""
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, json_data: dict[str, Any] | None = None) -> dict[str, Any] | list[Any]:
        """Internal HTTP request dispatcher with error normalization."""
        if not self.is_configured:
            raise AlpacaError("Alpaca API credentials are not configured.")

        url = f"{self.endpoint}{path}"
        try:
            with httpx.Client(timeout=self.timeout) as client:
                resp = client.request(
                    method=method,
                    url=url,
                    headers=self.get_headers(),
                    json=json_data,
                )
                if resp.status_code == 401:
                    raise AlpacaError("Alpaca authentication failed (401 Unauthorized). Check API keys.", status_code=401, response_text=resp.text)
                if resp.status_code == 403:
                    raise AlpacaError("Alpaca access forbidden (403 Forbidden). Account may be restricted.", status_code=403, response_text=resp.text)
                if resp.status_code >= 400:
                    raise AlpacaError(
                        f"Alpaca API error HTTP {resp.status_code}: {resp.text}",
                        status_code=resp.status_code,
                        response_text=resp.text,
                    )
                if resp.status_code == 204:
                    return {}
                return resp.json()
        except httpx.RequestError as exc:
            raise AlpacaError(f"Alpaca network connectivity error: {exc}") from exc

    def get_account(self) -> dict[str, Any]:
        """
        Fetch account details: buying_power, cash, portfolio_value, pattern_day_trader, etc.
        """
        data = self._request("GET", "/v2/account")
        return data if isinstance(data, dict) else {}

    def verify_credentials(self) -> dict[str, Any]:
        """
        Tests API connection and returns status dict.
        """
        try:
            acct = self.get_account()
            return {
                "ok": True,
                "status": acct.get("status", "ACTIVE"),
                "account_number": acct.get("account_number", ""),
                "currency": acct.get("currency", "USD"),
                "buying_power": float(acct.get("buying_power", 0.0) or 0.0),
                "cash": float(acct.get("cash", 0.0) or 0.0),
                "portfolio_value": float(acct.get("portfolio_value", 0.0) or 0.0),
                "pattern_day_trader": bool(acct.get("pattern_day_trader", False)),
                "daytrade_count": int(acct.get("daytrade_count", 0) or 0),
                "is_paper": self.is_paper,
                "trading_blocked": bool(acct.get("trading_blocked", False)),
            }
        except AlpacaError as exc:
            return {
                "ok": False,
                "error": str(exc),
                "status_code": exc.status_code,
                "is_paper": self.is_paper,
            }

    def get_clock(self) -> dict[str, Any]:
        """
        Query the market clock: whether regular market hours are active.
        """
        try:
            data = self._request("GET", "/v2/clock")
            return data if isinstance(data, dict) else {"is_open": False}
        except Exception as exc:
            logger.warning("Failed to query market clock (%s); assuming market closed.", exc)
            return {"is_open": False}

    def get_current_price(self, ticker: str, fallback_price: float = 35.0) -> float:
        """
        Fetch real-time quote via Alpaca Market Data or yfinance fallback.
        """
        ticker = ticker.upper()
        # Try Alpaca data API first if configured
        if self.is_configured:
            try:
                data_url = "https://data.alpaca.markets/v2/stocks/bars/latest"
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(
                        data_url,
                        params={"symbols": ticker, "feed": "iex"},
                        headers=self.get_headers(),
                    )
                    if resp.status_code == 200:
                        res = resp.json()
                        bars = res.get("bars", {})
                        if ticker in bars and bars[ticker].get("c"):
                            return round(float(bars[ticker]["c"]), 2)
            except Exception as exc:
                logger.debug("Alpaca quote lookup failed for %s: %s", ticker, exc)

        # Fallback to yfinance
        try:
            import yfinance as yf
            t = yf.Ticker(ticker)
            info = t.fast_info
            price = getattr(info, "last_price", None) or getattr(info, "previous_close", None)
            if price and price > 0:
                return round(float(price), 2)
        except Exception as exc:
            logger.debug("yfinance quote lookup failed for %s: %s", ticker, exc)

        return round(float(fallback_price or 35.0), 2)

    def execute_buy(
        self,
        ticker: str,
        allocation: float,
        signal_price: float | None = None,
        take_profit_price: float | None = None,
        stop_loss_price: float | None = None,
        limit_collar_pct: float = 0.015,
        extended_hours: bool | None = None,
    ) -> tuple[str, float, float, float]:
        """
        Submits a buy order to Alpaca.
        
        - During regular market hours: places a bracket order (Take-Profit & Stop-Loss OCO).
        - Outside regular hours (extended hours / 5:00 PM ET drops): places a Limit Order
          with limit_collar_pct protection and extended_hours=True (bracket orders cannot
          be combined with extended_hours in the initial fill on Alpaca).

        Returns:
            (broker_order_id, fill_price, shares, total_cost)
        """
        ticker = ticker.upper()
        price = self.get_current_price(ticker, fallback_price=signal_price or 35.0)

        # Determine shares: round to 2 decimals for fractional or integer if price is high
        raw_shares = allocation / price
        shares = round(raw_shares, 2)
        if shares <= 0:
            shares = 1.0

        clock = self.get_clock()
        market_is_open = bool(clock.get("is_open", False))

        # Check extended hours requirement
        use_extended = extended_hours if extended_hours is not None else (not market_is_open)

        order_payload: dict[str, Any] = {
            "symbol": ticker,
            "side": "buy",
            "qty": str(shares),
        }

        if use_extended:
            # Extended-hours limit order with collar to prevent gap-up trap
            limit_price = round(price * (1.0 + limit_collar_pct), 2)
            order_payload.update({
                "type": "limit",
                "time_in_force": "day",
                "limit_price": str(limit_price),
                "extended_hours": True,
            })
            exec_price = limit_price
            logger.info(
                "Submitting Extended-Hours Limit Buy for %s: %s shares @ limit $%s (collar +%.1f%%)",
                ticker, shares, limit_price, limit_collar_pct * 100,
            )
        elif take_profit_price and stop_loss_price:
            # Regular hours: submit server-side Bracket Order (OCO TP/SL)
            tp_clean = round(take_profit_price, 2)
            sl_clean = round(stop_loss_price, 2)
            order_payload.update({
                "type": "market",
                "time_in_force": "day",
                "order_class": "bracket",
                "take_profit": {"limit_price": str(tp_clean)},
                "stop_loss": {"stop_price": str(sl_clean)},
            })
            exec_price = price
            logger.info(
                "Submitting Market Bracket Order for %s: %s shares (TP: $%s, SL: $%s)",
                ticker, shares, tp_clean, sl_clean,
            )
        else:
            # Regular hours standard market order
            order_payload.update({
                "type": "market",
                "time_in_force": "day",
            })
            exec_price = price

        resp = self._request("POST", "/v2/orders", json_data=order_payload)
        if not isinstance(resp, dict):
            raise AlpacaError(f"Unexpected response from order placement: {resp}")

        broker_order_id = str(resp.get("id", ""))
        filled_avg_price = resp.get("filled_avg_price")
        if filled_avg_price:
            exec_price = float(filled_avg_price)
        total_cost = round(exec_price * shares, 2)

        return broker_order_id, exec_price, shares, total_cost

    def execute_sell(
        self,
        ticker: str,
        shares: float | None = None,
    ) -> tuple[str, float]:
        """
        Closes out an open position on Alpaca (liquidating position or submitting sell).
        Returns:
            (broker_order_id, exit_price)
        """
        ticker = ticker.upper()
        current_price = self.get_current_price(ticker)

        # Use Alpaca's DELETE /v2/positions/{symbol} to cleanly close position and cancel brackets
        try:
            resp = self._request("DELETE", f"/v2/positions/{ticker}")
            broker_order_id = str(resp.get("id", "")) if isinstance(resp, dict) else ""
            return broker_order_id, current_price
        except AlpacaError as exc:
            # If position not found on broker, fall back to posting a market sell
            logger.warning("Direct position liquidation returned error (%s); trying market sell.", exc)
            qty_str = str(round(shares, 2)) if shares else "1"
            sell_payload = {
                "symbol": ticker,
                "qty": qty_str,
                "side": "sell",
                "type": "market",
                "time_in_force": "day",
            }
            resp = self._request("POST", "/v2/orders", json_data=sell_payload)
            broker_order_id = str(resp.get("id", "")) if isinstance(resp, dict) else ""
            return broker_order_id, current_price

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order on Alpaca."""
        try:
            self._request("DELETE", f"/v2/orders/{order_id}")
            return True
        except AlpacaError as exc:
            logger.warning("Failed to cancel Alpaca order %s: %s", order_id, exc)
            return False

    def get_positions(self) -> list[dict[str, Any]]:
        """List all open positions directly from Alpaca."""
        data = self._request("GET", "/v2/positions")
        return data if isinstance(data, list) else []

    def get_orders(self, status: str = "open") -> list[dict[str, Any]]:
        """List orders from Alpaca."""
        data = self._request("GET", f"/v2/orders?status={status}")
        return data if isinstance(data, list) else []
