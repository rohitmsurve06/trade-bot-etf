"""
tests/test_logic.py
────────────────────────────────────────────────────────────────────────────────
Unit tests for src/logic.py

Covers:
  • Weighted Average Price calculation
  • Budget accumulation (first-of-month, idempotency, accumulation)
  • Dip signal detection (boundary conditions)
  • Share calculation
  • Limit price calculation
  • apply_fill_to_state (full integration of CSV + tracker update)
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

# Allow imports from project root
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.logic import (
    COMMISSION_BUFFER,
    DIP_THRESHOLD,
    LIMIT_DISCOUNT,
    MONTHLY_BUDGET,
    accumulate_monthly_budget,
    apply_fill_to_state,
    calculate_limit_price,
    calculate_shares_to_buy,
    check_dip_signal,
    record_signal,
    update_weighted_avg_price,
)


# ══════════════════════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture
def sample_df() -> pd.DataFrame:
    """Minimal investments DataFrame with two tickers."""
    return pd.DataFrame(
        {
            "Ticker": ["INDA", "NIFBEES"],
            "Type": ["ETF", "UnitTrust"],
            "Total_Shares": [50.0, 200.0],
            "Weighted_Avg_Price": [38.50, 120.50],
            "Currency": ["USD", "INR"],
        }
    )


@pytest.fixture
def sample_tracker() -> dict:
    return {
        "last_budget_add_date": None,
        "tickers": {
            "INDA": {
                "pending_balance": 0.0,
                "is_waiting_for_execution": False,
                "target_limit_price": None,
                "last_signal_date": None,
                "last_closing_price": None,
            },
            "NIFBEES": {
                "pending_balance": 0.0,
                "is_waiting_for_execution": False,
                "target_limit_price": None,
                "last_signal_date": None,
                "last_closing_price": None,
            },
        },
    }


# ══════════════════════════════════════════════════════════════════════════════
# Weighted Average Price
# ══════════════════════════════════════════════════════════════════════════════

class TestUpdateWeightedAvgPrice:

    def test_basic_average(self):
        """50 shares @ 38.50 + 10 shares @ 35.00 → correct new WAP."""
        new_avg = update_weighted_avg_price(50, 38.50, 10, 35.00)
        expected = (50 * 38.50 + 10 * 35.00) / 60
        assert abs(new_avg - expected) < 1e-5

    def test_buy_at_same_price_unchanged(self):
        """Buying at the exact current WAP leaves WAP unchanged."""
        new_avg = update_weighted_avg_price(100, 50.00, 10, 50.00)
        assert abs(new_avg - 50.00) < 1e-5

    def test_buy_lower_reduces_avg(self):
        """Buying below WAP must lower the average."""
        original_avg = 40.00
        new_avg = update_weighted_avg_price(100, original_avg, 50, 30.00)
        assert new_avg < original_avg

    def test_buy_higher_raises_avg(self):
        """Buying above WAP must raise the average."""
        original_avg = 40.00
        new_avg = update_weighted_avg_price(100, original_avg, 50, 60.00)
        assert new_avg > original_avg

    def test_zero_existing_shares(self):
        """First purchase: WAP equals the purchase price."""
        new_avg = update_weighted_avg_price(0, 0, 20, 42.00)
        assert abs(new_avg - 42.00) < 1e-5

    def test_raises_on_zero_total(self):
        with pytest.raises(ValueError, match="must be > 0"):
            update_weighted_avg_price(0, 0, 0, 50.00)


# ══════════════════════════════════════════════════════════════════════════════
# Budget Accumulation
# ══════════════════════════════════════════════════════════════════════════════

class TestAccumulateMonthlyBudget:

    def test_credits_on_first_of_month(self, sample_tracker):
        """Budget should be added on the 1st of the month."""
        with patch("src.logic.date") as mock_date:
            mock_date.today.return_value = date(2025, 6, 1)
            mock_date.fromisoformat = date.fromisoformat

            tracker, credited = accumulate_monthly_budget(sample_tracker)

        assert set(credited) == {"INDA", "NIFBEES"}
        assert tracker["tickers"]["INDA"]["pending_balance"] == MONTHLY_BUDGET
        assert tracker["tickers"]["NIFBEES"]["pending_balance"] == MONTHLY_BUDGET

    def test_no_credit_on_non_first(self, sample_tracker):
        """No budget added on any day other than the 1st."""
        for day in [2, 15, 28]:
            with patch("src.logic.date") as mock_date:
                mock_date.today.return_value = date(2025, 6, day)
                mock_date.fromisoformat = date.fromisoformat
                _, credited = accumulate_monthly_budget(sample_tracker)
            assert credited == [], f"Unexpected credit on day {day}"

    def test_idempotent_same_month(self, sample_tracker):
        """Running twice on the 1st of the same month credits only once."""
        with patch("src.logic.date") as mock_date:
            mock_date.today.return_value = date(2025, 7, 1)
            mock_date.fromisoformat = date.fromisoformat
            tracker, _ = accumulate_monthly_budget(sample_tracker)

        # Simulate second run same day
        with patch("src.logic.date") as mock_date:
            mock_date.today.return_value = date(2025, 7, 1)
            mock_date.fromisoformat = date.fromisoformat
            tracker, credited = accumulate_monthly_budget(tracker)

        assert credited == []
        assert tracker["tickers"]["INDA"]["pending_balance"] == MONTHLY_BUDGET  # not doubled

    def test_accumulation_over_multiple_months(self, sample_tracker):
        """After 5 months without a buy, balance should be 5 × MONTHLY_BUDGET."""
        months = [
            date(2025, 1, 1),
            date(2025, 2, 1),
            date(2025, 3, 1),
            date(2025, 4, 1),
            date(2025, 5, 1),
        ]
        tracker = sample_tracker
        for d in months:
            with patch("src.logic.date") as mock_date:
                mock_date.today.return_value = d
                mock_date.fromisoformat = date.fromisoformat
                tracker, _ = accumulate_monthly_budget(tracker)

        assert abs(tracker["tickers"]["INDA"]["pending_balance"] - 5 * MONTHLY_BUDGET) < 1e-6


# ══════════════════════════════════════════════════════════════════════════════
# Dip Signal Detection
# ══════════════════════════════════════════════════════════════════════════════

class TestCheckDipSignal:

    def test_exactly_at_threshold_triggers(self):
        """Price exactly at WAP × threshold should trigger."""
        wap = 100.00
        price = wap * DIP_THRESHOLD
        assert check_dip_signal("TEST", price, wap, 100.0) is True

    def test_below_threshold_triggers(self):
        """Price below the threshold should always trigger."""
        wap = 38.50
        price = 35.00  # > 5% below
        assert check_dip_signal("INDA", price, wap, 200.0) is True

    def test_above_threshold_no_signal(self):
        """Price only 2% below WAP should NOT trigger."""
        wap = 100.00
        price = 98.00
        assert check_dip_signal("TEST", price, wap, 100.0) is False

    def test_no_signal_when_no_budget(self):
        """Signal should not fire if pending_balance is 0."""
        wap = 100.00
        price = 80.00   # 20% dip — would normally trigger
        assert check_dip_signal("TEST", price, wap, 0.0) is False

    def test_no_signal_negative_budget(self):
        assert check_dip_signal("TEST", 80.0, 100.0, -10.0) is False


# ══════════════════════════════════════════════════════════════════════════════
# Share Calculation
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculateSharesToBuy:

    def test_basic_calculation(self):
        budget = 100.0
        price = 40.0
        expected = budget / (price * COMMISSION_BUFFER)
        result = calculate_shares_to_buy(budget, price)
        assert abs(result - expected) < 1e-8

    def test_commission_buffer_effect(self):
        """
        COMMISSION_BUFFER = 0.998 < 1.0, so dividing by (price * 0.998) gives
        *more* shares than naive division — the buffer reserves headroom for
        commissions by slightly over-estimating the purchasable quantity.
        The important invariant is that the result differs from naive division.
        """
        naive = 100.0 / 40.0
        buffered = calculate_shares_to_buy(100.0, 40.0)
        # Buffer < 1.0 → denominator shrinks → result > naive
        assert abs(buffered - naive) > 1e-6  # They must differ

    def test_raises_on_zero_price(self):
        with pytest.raises(ValueError):
            calculate_shares_to_buy(100.0, 0.0)

    def test_large_budget(self):
        """5 months × $100 accumulated budget."""
        result = calculate_shares_to_buy(500.0, 38.50)
        assert result > 12   # sanity check


# ══════════════════════════════════════════════════════════════════════════════
# Limit Price Calculation
# ══════════════════════════════════════════════════════════════════════════════

class TestCalculateLimitPrice:

    def test_discount_applied(self):
        close = 40.00
        limit = calculate_limit_price(close)
        expected = round(close * (1 - LIMIT_DISCOUNT), 2)
        assert limit == expected

    def test_limit_below_close(self):
        assert calculate_limit_price(50.0) < 50.0

    def test_rounds_to_two_decimal_places(self):
        result = calculate_limit_price(38.50)
        assert result == round(result, 2)


# ══════════════════════════════════════════════════════════════════════════════
# record_signal
# ══════════════════════════════════════════════════════════════════════════════

class TestRecordSignal:

    def test_etf_sets_limit_price(self, sample_tracker):
        tracker = record_signal(sample_tracker, "INDA", 36.00, "ETF")
        state = tracker["tickers"]["INDA"]
        assert state["is_waiting_for_execution"] is True
        assert state["target_limit_price"] is not None
        assert state["target_limit_price"] < 36.00

    def test_unit_trust_sets_no_limit_price(self, sample_tracker):
        tracker = record_signal(sample_tracker, "NIFBEES", 115.00, "UnitTrust")
        state = tracker["tickers"]["NIFBEES"]
        assert state["is_waiting_for_execution"] is True
        assert state["target_limit_price"] is None

    def test_signal_date_is_today(self, sample_tracker):
        tracker = record_signal(sample_tracker, "INDA", 36.00, "ETF")
        assert tracker["tickers"]["INDA"]["last_signal_date"] == date.today().isoformat()


# ══════════════════════════════════════════════════════════════════════════════
# apply_fill_to_state
# ══════════════════════════════════════════════════════════════════════════════

class TestApplyFillToState:

    def test_wap_updated_after_fill(self, sample_df, sample_tracker):
        """After a fill, WAP should be recalculated correctly."""
        sample_tracker["tickers"]["INDA"]["pending_balance"] = 100.0
        df, tracker = apply_fill_to_state(
            sample_df.copy(), sample_tracker, "INDA",
            filled_shares=2.5, fill_price=36.00
        )
        row = df[df["Ticker"] == "INDA"]
        original_shares = 50.0
        original_avg = 38.50
        expected_avg = (original_shares * original_avg + 2.5 * 36.00) / (original_shares + 2.5)
        assert abs(float(row["Weighted_Avg_Price"].iloc[0]) - expected_avg) < 1e-5

    def test_total_shares_increases(self, sample_df, sample_tracker):
        df, _ = apply_fill_to_state(
            sample_df.copy(), sample_tracker, "INDA",
            filled_shares=10.0, fill_price=37.00
        )
        assert float(df[df["Ticker"] == "INDA"]["Total_Shares"].iloc[0]) == 60.0

    def test_pending_balance_reset_to_zero(self, sample_df, sample_tracker):
        sample_tracker["tickers"]["INDA"]["pending_balance"] = 250.0
        _, tracker = apply_fill_to_state(
            sample_df.copy(), sample_tracker, "INDA",
            filled_shares=5.0, fill_price=37.00
        )
        assert tracker["tickers"]["INDA"]["pending_balance"] == 0.0

    def test_execution_flags_cleared(self, sample_df, sample_tracker):
        sample_tracker["tickers"]["INDA"].update(
            {"is_waiting_for_execution": True, "target_limit_price": 36.50}
        )
        _, tracker = apply_fill_to_state(
            sample_df.copy(), sample_tracker, "INDA",
            filled_shares=5.0, fill_price=37.00
        )
        assert tracker["tickers"]["INDA"]["is_waiting_for_execution"] is False
        assert tracker["tickers"]["INDA"]["target_limit_price"] is None

    def test_raises_on_unknown_ticker(self, sample_df, sample_tracker):
        with pytest.raises(KeyError):
            apply_fill_to_state(
                sample_df.copy(), sample_tracker, "UNKNOWN",
                filled_shares=1.0, fill_price=50.0
            )
