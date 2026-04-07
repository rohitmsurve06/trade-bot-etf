"""
ib_manager.py
────────────────────────────────────────────────────────────────────────────────
Manages all IBKR Gateway / TWS interactions via ib_insync.

Responsibilities:
  • Async connection with retry logic and timeout handling
  • Fetching live market price (last trade or delayed close)
  • Fetching account equity / cash balance
  • Placing Limit Orders (ETFs) and Market Orders (Unit Trusts)
  • Monitoring order status until filled, partially filled, or timed out
  • Graceful disconnection
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Optional

import nest_asyncio
from dotenv import load_dotenv
from ib_insync import (
    IB,
    Contract,
    LimitOrder,
    MarketOrder,
    Order,
    Stock,
    Trade,
    util,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

load_dotenv()
nest_asyncio.apply()   # Allows nested event loops (needed in some CI environments)

logger = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────
IB_HOST: str = os.getenv("IB_HOST", "127.0.0.1")
IB_PORT: int = int(os.getenv("IB_PORT", "4002"))
IB_CLIENT_ID: int = int(os.getenv("IB_CLIENT_ID", "1"))
IB_ACCOUNT: str = os.getenv("IB_ACCOUNT_ID", "")

ORDER_TIMEOUT_SECONDS: int = int(os.getenv("ORDER_TIMEOUT_SECONDS", "300"))   # 5 min
PRICE_TIMEOUT_SECONDS: int = int(os.getenv("PRICE_TIMEOUT_SECONDS", "30"))
MAX_CONNECT_RETRIES: int = 5


class IBConnectionError(Exception):
    """Raised when IB Gateway/TWS connection cannot be established."""


class IBPriceFetchError(Exception):
    """Raised when a market data snapshot cannot be retrieved."""


class IBOrderError(Exception):
    """Raised when an order cannot be placed or confirmed."""


# ══════════════════════════════════════════════════════════════════════════════
# IBManager
# ══════════════════════════════════════════════════════════════════════════════

class IBManager:
    """
    Context-manager-friendly async wrapper around ib_insync.IB.

    Usage:
        async with IBManager() as mgr:
            price = await mgr.get_last_price("INDA", "SMART", "USD")
    """

    def __init__(self) -> None:
        self._ib = IB()
        self._connected = False

    # ── Context manager protocol ──────────────────────────────────────────────

    async def __aenter__(self) -> "IBManager":
        await self.connect()
        return self

    async def __aexit__(self, *_) -> None:
        await self.disconnect()

    # ── Connection management ─────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type(ConnectionRefusedError),
        wait=wait_exponential(multiplier=2, min=4, max=60),
        stop=stop_after_attempt(MAX_CONNECT_RETRIES),
        reraise=True,
    )
    async def connect(self) -> None:
        """
        Connect to IB Gateway/TWS with exponential back-off retry.
        Raises IBConnectionError if all attempts fail.
        """
        if self._connected:
            return
        try:
            logger.info(
                "Connecting to IB Gateway at %s:%d (clientId=%d)…",
                IB_HOST, IB_PORT, IB_CLIENT_ID,
            )
            await self._ib.connectAsync(
                host=IB_HOST,
                port=IB_PORT,
                clientId=IB_CLIENT_ID,
                timeout=20,
                readonly=False,
            )
            self._connected = True
            logger.info("IB Gateway connected. Server version: %s", self._ib.client.serverVersion())
        except Exception as exc:
            raise IBConnectionError(f"IB connect failed: {exc}") from exc

    async def disconnect(self) -> None:
        if self._connected:
            self._ib.disconnect()
            self._connected = False
            logger.info("IB Gateway disconnected.")

    def _ensure_connected(self) -> None:
        if not self._connected:
            raise IBConnectionError("IBManager is not connected. Call connect() first.")

    # ── Contract resolution ───────────────────────────────────────────────────

    async def _resolve_contract(
        self,
        ticker: str,
        exchange: str,
        currency: str,
        sec_type: str = "STK",
    ) -> Contract:
        """
        Qualify a contract via IB's contract details API.
        Falls back to an unqualified Stock() if details are unavailable.
        """
        contract = Stock(ticker, exchange, currency)
        try:
            qualified = await asyncio.wait_for(
                self._ib.qualifyContractsAsync(contract),
                timeout=PRICE_TIMEOUT_SECONDS,
            )
            if qualified:
                logger.debug("Qualified contract: %s", qualified[0])
                return qualified[0]
        except asyncio.TimeoutError:
            logger.warning("Contract qualification timed out for %s — using unqualified.", ticker)
        return contract

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_last_price(
        self,
        ticker: str,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> float:
        """
        Fetch the last traded price (or delayed close) for a ticker.

        Priority chain:
          1. last  (live last trade)
          2. close (end-of-day close — available after hours)
          3. bid / ask midpoint (fallback)

        Raises IBPriceFetchError if no price can be retrieved.
        """
        self._ensure_connected()
        contract = await self._resolve_contract(ticker, exchange, currency)

        logger.info("Requesting market data for %s…", ticker)
        ticker_data = self._ib.reqMktData(contract, "", snapshot=True)

        # Wait for data to arrive
        deadline = asyncio.get_event_loop().time() + PRICE_TIMEOUT_SECONDS
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(0.5)
            price = (
                ticker_data.last
                or ticker_data.close
                or _midpoint(ticker_data.bid, ticker_data.ask)
            )
            if price and price > 0:
                self._ib.cancelMktData(contract)
                logger.info("%s market price: %.4f %s", ticker, price, currency)
                return float(price)

        self._ib.cancelMktData(contract)
        raise IBPriceFetchError(
            f"Could not retrieve price for {ticker} within {PRICE_TIMEOUT_SECONDS}s."
        )

    # ── Account data ──────────────────────────────────────────────────────────

    async def get_account_summary(self) -> dict[str, float]:
        """
        Return key account metrics as a dict:
          • NetLiquidation  — total account equity
          • TotalCashValue  — available cash
          • BuyingPower     — IBKR calculated buying power
        """
        self._ensure_connected()
        account_id = IB_ACCOUNT or ""

        summary = await asyncio.wait_for(
            self._ib.accountSummaryAsync(account=account_id),
            timeout=30,
        )
        result: dict[str, float] = {}
        for item in summary:
            if item.tag in ("NetLiquidation", "TotalCashValue", "BuyingPower"):
                try:
                    result[item.tag] = float(item.value)
                except ValueError:
                    pass

        logger.info("Account summary: %s", result)
        return result

    async def get_account_equity(self) -> float:
        """Convenience: returns NetLiquidation value."""
        summary = await self.get_account_summary()
        equity = summary.get("NetLiquidation", 0.0)
        logger.info("Account equity (NetLiquidation): %.2f", equity)
        return equity

    async def get_cash_balance(self) -> float:
        """Convenience: returns TotalCashValue."""
        summary = await self.get_account_summary()
        return summary.get("TotalCashValue", 0.0)

    # ── Order placement ───────────────────────────────────────────────────────

    async def place_limit_order(
        self,
        ticker: str,
        shares: float,
        limit_price: float,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Trade:
        """
        Place a GTC Limit Buy order.

        Returns the Trade object (use await_fill to monitor it).
        """
        self._ensure_connected()
        contract = await self._resolve_contract(ticker, exchange, currency)

        # Round shares down to integer (most ETFs don't support fractional on IBKR)
        qty = int(shares)
        if qty < 1:
            raise IBOrderError(
                f"Calculated quantity {shares:.4f} rounds to 0 for {ticker}. "
                "Insufficient budget for even 1 share."
            )

        order = LimitOrder(
            action="BUY",
            totalQuantity=qty,
            lmtPrice=limit_price,
            tif="GTC",          # Good Till Cancelled
            outsideRth=False,   # Only during regular trading hours
        )
        if IB_ACCOUNT:
            order.account = IB_ACCOUNT

        trade = self._ib.placeOrder(contract, order)
        logger.info(
            "Limit order placed: BUY %d %s @ %.4f GTC", qty, ticker, limit_price
        )
        return trade

    async def place_market_order(
        self,
        ticker: str,
        shares: float,
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> Trade:
        """
        Place a Market Buy order (used for Unit Trusts which trade at NAV).
        """
        self._ensure_connected()
        contract = await self._resolve_contract(ticker, exchange, currency)

        qty = int(shares)
        if qty < 1:
            raise IBOrderError(
                f"Calculated quantity {shares:.4f} rounds to 0 for {ticker}."
            )

        order = MarketOrder(
            action="BUY",
            totalQuantity=qty,
        )
        if IB_ACCOUNT:
            order.account = IB_ACCOUNT

        trade = self._ib.placeOrder(contract, order)
        logger.info("Market order placed: BUY %d %s @ MARKET", qty, ticker)
        return trade

    # ── Order monitoring ──────────────────────────────────────────────────────

    async def await_fill(self, trade: Trade, timeout: int = ORDER_TIMEOUT_SECONDS) -> dict:
        """
        Poll the trade until it reaches a terminal state or the timeout expires.

        Returns a dict:
          {
            "status":         "FILLED" | "PARTIAL" | "FAILED" | "CANCELLED",
            "shares_filled":  float,
            "avg_fill_price": float,
            "order_id":       int,
          }
        """
        terminal_statuses = {"Filled", "Cancelled", "Inactive"}
        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(2)
            self._ib.sleep(0)   # Process IB events

            status = trade.orderStatus.status
            filled = trade.orderStatus.filled
            avg_price = trade.orderStatus.avgFillPrice

            logger.debug(
                "Order %d status=%s filled=%.4f avgPrice=%.4f",
                trade.order.orderId, status, filled, avg_price,
            )

            if status == "Filled":
                return {
                    "status": "FILLED",
                    "shares_filled": float(filled),
                    "avg_fill_price": float(avg_price),
                    "order_id": trade.order.orderId,
                }
            if status in ("Cancelled", "Inactive"):
                return {
                    "status": "CANCELLED",
                    "shares_filled": float(filled),
                    "avg_fill_price": float(avg_price or 0),
                    "order_id": trade.order.orderId,
                }

        # Timeout reached — treat as partial if any fill occurred
        filled = trade.orderStatus.filled
        avg_price = trade.orderStatus.avgFillPrice or 0.0
        logger.warning(
            "Order %d timed out after %ds. Filled: %.4f",
            trade.order.orderId, timeout, filled,
        )
        return {
            "status": "PARTIAL" if filled > 0 else "FAILED",
            "shares_filled": float(filled),
            "avg_fill_price": float(avg_price),
            "order_id": trade.order.orderId,
        }

    async def cancel_order(self, trade: Trade) -> None:
        """Cancel a pending order."""
        self._ib.cancelOrder(trade.order)
        logger.info("Cancel requested for order %d.", trade.order.orderId)


# ══════════════════════════════════════════════════════════════════════════════
# Utility
# ══════════════════════════════════════════════════════════════════════════════

def _midpoint(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    """Return bid/ask midpoint if both are valid positive numbers."""
    if bid and ask and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return None
