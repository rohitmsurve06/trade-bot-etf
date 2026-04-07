"""
tests/test_ib_manager.py
────────────────────────────────────────────────────────────────────────────────
Unit tests for src/ib_manager.py

All IBKR network calls are mocked via pytest-mock / unittest.mock.
Tests cover:
  • Connection retry logic
  • Price fetching (last, close, midpoint fallback)
  • Limit and Market order placement
  • Order fill / timeout monitoring
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ib_manager import IBConnectionError, IBManager, IBOrderError, IBPriceFetchError, _midpoint


# ══════════════════════════════════════════════════════════════════════════════
# Utility function tests
# ══════════════════════════════════════════════════════════════════════════════

class TestMidpoint:

    def test_valid_bid_ask(self):
        assert _midpoint(10.0, 12.0) == 11.0

    def test_zero_bid_returns_none(self):
        assert _midpoint(0.0, 12.0) is None

    def test_none_values_returns_none(self):
        assert _midpoint(None, None) is None

    def test_negative_values_returns_none(self):
        assert _midpoint(-1.0, 12.0) is None


# ══════════════════════════════════════════════════════════════════════════════
# IBManager — connection
# ══════════════════════════════════════════════════════════════════════════════

class TestIBManagerConnect:

    @pytest.mark.asyncio
    async def test_connect_success(self):
        mgr = IBManager()
        mock_ib = MagicMock()
        mock_ib.connectAsync = AsyncMock()
        mock_ib.client.serverVersion.return_value = "176"
        mgr._ib = mock_ib

        await mgr.connect()

        assert mgr._connected is True
        mock_ib.connectAsync.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_connect_raises_on_failure(self):
        mgr = IBManager()
        mock_ib = MagicMock()
        mock_ib.connectAsync = AsyncMock(side_effect=ConnectionRefusedError("refused"))
        mgr._ib = mock_ib

        with pytest.raises(IBConnectionError):
            # Override tenacity retries to avoid long test duration
            with patch("src.ib_manager.MAX_CONNECT_RETRIES", 1):
                await mgr.connect()

    @pytest.mark.asyncio
    async def test_disconnect_sets_flag(self):
        mgr = IBManager()
        mgr._connected = True
        mock_ib = MagicMock()
        mgr._ib = mock_ib

        await mgr.disconnect()

        assert mgr._connected is False
        mock_ib.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_connected_raises_when_not_connected(self):
        mgr = IBManager()
        with pytest.raises(IBConnectionError):
            mgr._ensure_connected()


# ══════════════════════════════════════════════════════════════════════════════
# IBManager — price fetching
# ══════════════════════════════════════════════════════════════════════════════

class TestGetLastPrice:

    def _make_mgr_with_mock_ib(self, last=None, close=None, bid=None, ask=None):
        mgr = IBManager()
        mgr._connected = True

        ticker_data = MagicMock()
        ticker_data.last = last
        ticker_data.close = close
        ticker_data.bid = bid
        ticker_data.ask = ask

        mock_ib = MagicMock()
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[MagicMock()])
        mock_ib.reqMktData = MagicMock(return_value=ticker_data)
        mock_ib.cancelMktData = MagicMock()
        mgr._ib = mock_ib

        return mgr, ticker_data

    @pytest.mark.asyncio
    async def test_returns_last_price(self):
        mgr, _ = self._make_mgr_with_mock_ib(last=38.50)
        price = await mgr.get_last_price("INDA")
        assert price == 38.50

    @pytest.mark.asyncio
    async def test_falls_back_to_close(self):
        mgr, _ = self._make_mgr_with_mock_ib(last=None, close=37.80)
        price = await mgr.get_last_price("INDA")
        assert price == 37.80

    @pytest.mark.asyncio
    async def test_falls_back_to_midpoint(self):
        mgr, _ = self._make_mgr_with_mock_ib(last=None, close=None, bid=36.0, ask=38.0)
        price = await mgr.get_last_price("INDA")
        assert price == 37.0

    @pytest.mark.asyncio
    async def test_raises_when_no_price(self):
        mgr, _ = self._make_mgr_with_mock_ib()
        with patch("src.ib_manager.PRICE_TIMEOUT_SECONDS", 1):
            with pytest.raises(IBPriceFetchError):
                await mgr.get_last_price("INDA")


# ══════════════════════════════════════════════════════════════════════════════
# IBManager — order placement
# ══════════════════════════════════════════════════════════════════════════════

class TestPlaceOrders:

    def _make_connected_mgr(self):
        mgr = IBManager()
        mgr._connected = True
        mock_ib = MagicMock()
        mock_ib.qualifyContractsAsync = AsyncMock(return_value=[MagicMock()])
        mock_trade = MagicMock()
        mock_ib.placeOrder = MagicMock(return_value=mock_trade)
        mgr._ib = mock_ib
        return mgr, mock_ib, mock_trade

    @pytest.mark.asyncio
    async def test_place_limit_order_calls_placeOrder(self):
        mgr, mock_ib, mock_trade = self._make_connected_mgr()
        trade = await mgr.place_limit_order("INDA", shares=2.5, limit_price=35.93)
        mock_ib.placeOrder.assert_called_once()
        assert trade is mock_trade

    @pytest.mark.asyncio
    async def test_place_limit_order_zero_qty_raises(self):
        mgr, _, _ = self._make_connected_mgr()
        with pytest.raises(IBOrderError, match="rounds to 0"):
            await mgr.place_limit_order("INDA", shares=0.5, limit_price=35.93)

    @pytest.mark.asyncio
    async def test_place_market_order(self):
        mgr, mock_ib, mock_trade = self._make_connected_mgr()
        trade = await mgr.place_market_order("NIFBEES", shares=3.0)
        mock_ib.placeOrder.assert_called_once()
        assert trade is mock_trade


# ══════════════════════════════════════════════════════════════════════════════
# IBManager — order fill monitoring
# ══════════════════════════════════════════════════════════════════════════════

class TestAwaitFill:

    def _make_trade(self, status: str, filled: float, avg_price: float) -> MagicMock:
        trade = MagicMock()
        trade.order.orderId = 42
        trade.orderStatus.status = status
        trade.orderStatus.filled = filled
        trade.orderStatus.avgFillPrice = avg_price
        return trade

    @pytest.mark.asyncio
    async def test_filled_trade(self):
        mgr = IBManager()
        mgr._connected = True
        mgr._ib = MagicMock()
        mgr._ib.sleep = MagicMock()

        trade = self._make_trade("Filled", 2.0, 35.93)
        result = await mgr.await_fill(trade, timeout=5)

        assert result["status"] == "FILLED"
        assert result["shares_filled"] == 2.0
        assert result["avg_fill_price"] == 35.93

    @pytest.mark.asyncio
    async def test_cancelled_trade(self):
        mgr = IBManager()
        mgr._connected = True
        mgr._ib = MagicMock()
        mgr._ib.sleep = MagicMock()

        trade = self._make_trade("Cancelled", 0.0, 0.0)
        result = await mgr.await_fill(trade, timeout=5)

        assert result["status"] == "CANCELLED"

    @pytest.mark.asyncio
    async def test_timeout_with_no_fill(self):
        mgr = IBManager()
        mgr._connected = True
        mgr._ib = MagicMock()
        mgr._ib.sleep = MagicMock()

        trade = self._make_trade("Submitted", 0.0, 0.0)
        result = await mgr.await_fill(trade, timeout=1)

        assert result["status"] == "FAILED"
        assert result["shares_filled"] == 0.0

    @pytest.mark.asyncio
    async def test_timeout_with_partial_fill(self):
        mgr = IBManager()
        mgr._connected = True
        mgr._ib = MagicMock()
        mgr._ib.sleep = MagicMock()

        trade = self._make_trade("Submitted", 1.0, 35.93)
        result = await mgr.await_fill(trade, timeout=1)

        assert result["status"] == "PARTIAL"
        assert result["shares_filled"] == 1.0
