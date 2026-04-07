"""
logic.py
────────────────────────────────────────────────────────────────────────────────
Core financial logic for the Value Averaging Dip-Buying strategy.

Responsibilities:
  • Monthly budget accumulation (adds $MONTHLY_BUDGET on the 1st of each month)
  • 5% dip detection against Weighted Average Price
  • Shares-to-buy calculation with commission buffer
  • Weighted Average Price recalculation after fills
  • Loading / persisting investments.csv and tracker.json
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional

import pandas as pd
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────
MONTHLY_BUDGET: float = float(os.getenv("MONTHLY_BUDGET_PER_TICKER", "100.0"))
DIP_THRESHOLD: float = float(os.getenv("DIP_THRESHOLD", "0.95"))       # 5% below WAP
LIMIT_DISCOUNT: float = float(os.getenv("LIMIT_ORDER_DISCOUNT", "0.002"))  # 0.20%
COMMISSION_BUFFER: float = float(os.getenv("COMMISSION_BUFFER", "0.998"))

INVESTMENTS_PATH = Path(os.getenv("INVESTMENTS_CSV_PATH", "data/investments.csv"))
TRACKER_PATH = Path(os.getenv("TRACKER_JSON_PATH", "data/tracker.json"))


# ══════════════════════════════════════════════════════════════════════════════
# Data I/O
# ══════════════════════════════════════════════════════════════════════════════

def load_investments() -> pd.DataFrame:
    """Return the investments DataFrame, indexed by Ticker."""
    df = pd.read_csv(INVESTMENTS_PATH, dtype=str)
    df["Total_Shares"] = df["Total_Shares"].astype(float)
    df["Weighted_Avg_Price"] = df["Weighted_Avg_Price"].astype(float)
    logger.debug("Loaded %d tickers from %s", len(df), INVESTMENTS_PATH)
    return df


def save_investments(df: pd.DataFrame) -> None:
    """Persist the investments DataFrame back to CSV."""
    df.to_csv(INVESTMENTS_PATH, index=False, float_format="%.6f")
    logger.debug("Saved investments to %s", INVESTMENTS_PATH)


def load_tracker() -> dict:
    """
    Load tracker.json, auto-bootstrapping missing tickers from investments.csv.
    Returns a dict with top-level keys: 'last_budget_add_date', 'tickers'.
    """
    if TRACKER_PATH.exists():
        with open(TRACKER_PATH, "r") as fh:
            tracker = json.load(fh)
    else:
        tracker = {"last_budget_add_date": None, "tickers": {}}
        logger.info("tracker.json not found — created empty tracker.")

    # Ensure every ticker in investments.csv has a state entry
    df = load_investments()
    for ticker in df["Ticker"].tolist():
        if ticker not in tracker["tickers"]:
            tracker["tickers"][ticker] = _default_ticker_state()
            logger.info("Bootstrap tracker entry for new ticker: %s", ticker)

    return tracker


def save_tracker(tracker: dict) -> None:
    """Persist tracker state to JSON."""
    TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TRACKER_PATH, "w") as fh:
        json.dump(tracker, fh, indent=2, default=str)
    logger.debug("Saved tracker to %s", TRACKER_PATH)


def _default_ticker_state() -> dict:
    return {
        "pending_balance": 0.0,
        "is_waiting_for_execution": False,
        "target_limit_price": None,
        "last_signal_date": None,
        "last_closing_price": None,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Budget Accumulator
# ══════════════════════════════════════════════════════════════════════════════

def accumulate_monthly_budget(tracker: dict) -> tuple[dict, list[str]]:
    """
    On the 1st of each calendar month, add MONTHLY_BUDGET to every ticker's
    pending_balance — but only once per month.

    Returns:
        (updated_tracker, list_of_tickers_that_received_budget)
    """
    today = date.today()
    last_add = tracker.get("last_budget_add_date")

    # Parse last add date
    if last_add:
        last_add_date = date.fromisoformat(last_add)
    else:
        last_add_date = None

    credited: list[str] = []

    # Credit only on the 1st, and not if we already did it this month
    if today.day == 1:
        if last_add_date is None or (today.year, today.month) != (last_add_date.year, last_add_date.month):
            for ticker, state in tracker["tickers"].items():
                state["pending_balance"] = round(
                    state["pending_balance"] + MONTHLY_BUDGET, 6
                )
                credited.append(ticker)
                logger.info(
                    "Budget credit: %s → pending_balance now %.2f",
                    ticker, state["pending_balance"]
                )
            tracker["last_budget_add_date"] = today.isoformat()

    return tracker, credited


# ══════════════════════════════════════════════════════════════════════════════
# Dip Detection
# ══════════════════════════════════════════════════════════════════════════════

def check_dip_signal(
    ticker: str,
    current_price: float,
    weighted_avg_price: float,
    pending_balance: float,
) -> bool:
    """
    Return True if ALL of these hold:
      1. current_price <= weighted_avg_price * DIP_THRESHOLD  (5% dip)
      2. pending_balance > 0
    """
    if pending_balance <= 0:
        logger.debug("%s: no pending balance — skip dip check.", ticker)
        return False

    threshold_price = weighted_avg_price * DIP_THRESHOLD
    triggered = current_price <= threshold_price

    logger.info(
        "%s dip check | current=%.4f  threshold=%.4f (WAP=%.4f × %.2f)  → %s",
        ticker, current_price, threshold_price, weighted_avg_price,
        DIP_THRESHOLD, "TRIGGERED" if triggered else "no signal",
    )
    return triggered


# ══════════════════════════════════════════════════════════════════════════════
# Order Sizing
# ══════════════════════════════════════════════════════════════════════════════

def calculate_shares_to_buy(
    pending_balance: float,
    current_price: float,
) -> float:
    """
    Shares = pending_balance / (current_price * COMMISSION_BUFFER)

    The COMMISSION_BUFFER (default 0.998) means we reserve a small fraction
    of the budget for broker commissions so the order doesn't get partially
    rejected due to insufficient funds.
    """
    if current_price <= 0:
        raise ValueError("current_price must be positive.")
    shares = pending_balance / (current_price * COMMISSION_BUFFER)
    logger.debug(
        "Shares calc | budget=%.2f  price=%.4f  buffer=%.4f  → %.4f shares",
        pending_balance, current_price, COMMISSION_BUFFER, shares,
    )
    return shares


def calculate_limit_price(closing_price: float) -> float:
    """
    Limit order price for T+1 execution:
        limit = closing_price * (1 - LIMIT_DISCOUNT)
    Rounds to 2 decimal places (standard for most ETF markets).
    """
    limit = round(closing_price * (1.0 - LIMIT_DISCOUNT), 2)
    logger.debug(
        "Limit price calc | close=%.4f  discount=%.4f  → %.2f",
        closing_price, LIMIT_DISCOUNT, limit,
    )
    return limit


# ══════════════════════════════════════════════════════════════════════════════
# Weighted Average Price Update
# ══════════════════════════════════════════════════════════════════════════════

def update_weighted_avg_price(
    current_shares: float,
    current_avg: float,
    new_shares: float,
    purchase_price: float,
) -> float:
    """
    New WAP = (current_shares × current_avg + new_shares × purchase_price)
              ───────────────────────────────────────────────────────────
                           current_shares + new_shares

    Raises ValueError if total shares would be zero.
    """
    total_shares = current_shares + new_shares
    if total_shares <= 0:
        raise ValueError("Total shares after purchase must be > 0.")

    new_avg = (
        (current_shares * current_avg) + (new_shares * purchase_price)
    ) / total_shares

    logger.info(
        "WAP update | %.4f shares @ %.4f avg + %.4f new @ %.4f → new avg %.6f",
        current_shares, current_avg, new_shares, purchase_price, new_avg,
    )
    return round(new_avg, 6)


# ══════════════════════════════════════════════════════════════════════════════
# Post-Trade State Update
# ══════════════════════════════════════════════════════════════════════════════

def apply_fill_to_state(
    df: pd.DataFrame,
    tracker: dict,
    ticker: str,
    filled_shares: float,
    fill_price: float,
) -> tuple[pd.DataFrame, dict]:
    """
    After a trade fill:
      1. Update Weighted_Avg_Price and Total_Shares in the investments DataFrame.
      2. Reset pending_balance → 0 and clear execution flags in tracker.

    Returns updated (df, tracker).
    """
    row_mask = df["Ticker"] == ticker
    if not row_mask.any():
        raise KeyError(f"Ticker '{ticker}' not found in investments DataFrame.")

    current_shares = float(df.loc[row_mask, "Total_Shares"].iloc[0])
    current_avg = float(df.loc[row_mask, "Weighted_Avg_Price"].iloc[0])

    new_avg = update_weighted_avg_price(
        current_shares, current_avg, filled_shares, fill_price
    )
    new_total = current_shares + filled_shares

    df.loc[row_mask, "Weighted_Avg_Price"] = new_avg
    df.loc[row_mask, "Total_Shares"] = new_total

    # Reset tracker state
    tracker["tickers"][ticker]["pending_balance"] = 0.0
    tracker["tickers"][ticker]["is_waiting_for_execution"] = False
    tracker["tickers"][ticker]["target_limit_price"] = None
    tracker["tickers"][ticker]["last_signal_date"] = None
    tracker["tickers"][ticker]["last_closing_price"] = None

    logger.info(
        "Fill applied: %s | +%.4f shares @ %.4f  |  new WAP=%.6f  total=%.4f",
        ticker, filled_shares, fill_price, new_avg, new_total,
    )
    return df, tracker


# ══════════════════════════════════════════════════════════════════════════════
# Signal Recording (Day T → set up Day T+1)
# ══════════════════════════════════════════════════════════════════════════════

def record_signal(
    tracker: dict,
    ticker: str,
    closing_price: float,
    instrument_type: str,
) -> dict:
    """
    Record that a buy signal was triggered today so that tomorrow the bot
    knows to place the order.

    For ETFs   → target_limit_price = closing_price × (1 - LIMIT_DISCOUNT)
    For UnitTrusts → target_limit_price = None (will use Market Order)
    """
    today = date.today().isoformat()

    limit_price: Optional[float] = None
    if instrument_type.upper() != "UNITTRUST":
        limit_price = calculate_limit_price(closing_price)

    tracker["tickers"][ticker].update(
        {
            "is_waiting_for_execution": True,
            "target_limit_price": limit_price,
            "last_signal_date": today,
            "last_closing_price": closing_price,
        }
    )
    logger.info(
        "Signal recorded for %s | close=%.4f  limit=%.4f  type=%s",
        ticker, closing_price,
        limit_price if limit_price else 0.0,
        instrument_type,
    )
    return tracker
