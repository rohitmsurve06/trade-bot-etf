"""
main.py
────────────────────────────────────────────────────────────────────────────────
Daily orchestrator for the Value Averaging Dip-Buying strategy.

Run schedule: Daily at 07:00 UTC (15:00 SGT) via GitHub Actions.

Two-phase execution per run:

  PHASE 1 — Execute pending orders (Day T+1)
    For every ticker where is_waiting_for_execution == True:
      • Validate account equity ≥ pending_balance
      • Place Limit Order (ETF) or Market Order (Unit Trust)
      • Await fill / timeout
      • Update WAP + Total_Shares in investments.csv
      • Send Post-Order email
      • Reset tracker state

  PHASE 2 — Check for new dip signals (Day T)
    For every ticker in investments.csv:
      • Fetch current market price from IBKR
      • Accumulate monthly budget if it's the 1st of the month
      • Run 5% dip check against Weighted Avg Price
      • If signal fires: record signal, calculate T+1 limit price, send Pre-Order email
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import date

from dotenv import load_dotenv

# Allow running from project root or from src/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.ib_manager import IBConnectionError, IBManager, IBOrderError, IBPriceFetchError
from src.logic import (
    accumulate_monthly_budget,
    apply_fill_to_state,
    calculate_shares_to_buy,
    check_dip_signal,
    load_investments,
    load_tracker,
    record_signal,
    save_investments,
    save_tracker,
)
from src.notifier import (
    send_balance_alert,
    send_error_alert,
    send_post_order_alert,
    send_pre_order_alert,
)

load_dotenv()

# ── Logging setup ──────────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Execute pending T+1 orders
# ══════════════════════════════════════════════════════════════════════════════

async def phase1_execute_pending_orders(
    mgr: IBManager,
    df,
    tracker: dict,
) -> tuple:
    """
    Place orders for any ticker where is_waiting_for_execution == True.
    Mutates df and tracker in-place; returns them for further use.
    """
    logger.info("═══ PHASE 1: Executing pending T+1 orders ═══")
    pending_tickers = [
        t for t, s in tracker["tickers"].items()
        if s.get("is_waiting_for_execution")
    ]

    if not pending_tickers:
        logger.info("No pending orders to execute today.")
        return df, tracker

    # Fetch account equity once
    try:
        account_equity = await mgr.get_account_equity()
    except Exception as exc:
        logger.error("Could not fetch account equity: %s", exc)
        send_error_alert("Phase 1 — account equity fetch", exc)
        account_equity = 0.0

    for ticker in pending_tickers:
        state = tracker["tickers"][ticker]
        pending_balance: float = state["pending_balance"]
        limit_price: float | None = state["target_limit_price"]
        row = df[df["Ticker"] == ticker]
        if row.empty:
            logger.error("Ticker %s in tracker but not in investments.csv — skipping.", ticker)
            continue

        instrument_type: str = str(row["Type"].iloc[0])
        currency: str = str(row["Currency"].iloc[0])
        current_avg: float = float(row["Weighted_Avg_Price"].iloc[0])
        current_shares: float = float(row["Total_Shares"].iloc[0])
        last_close: float = float(state.get("last_closing_price") or 0)

        logger.info(
            "Processing pending order: %s | type=%s | budget=%.2f | limit=%.4f",
            ticker, instrument_type, pending_balance, limit_price or 0,
        )

        # ── Balance check ────────────────────────────────────────────────────
        if account_equity < pending_balance:
            logger.warning(
                "%s SKIPPED — equity %.2f < required %.2f",
                ticker, account_equity, pending_balance,
            )
            send_balance_alert(ticker, pending_balance, account_equity, currency)
            continue

        # ── Calculate shares ─────────────────────────────────────────────────
        reference_price = limit_price or last_close or current_avg
        try:
            shares_to_buy = calculate_shares_to_buy(pending_balance, reference_price)
        except ValueError as exc:
            logger.error("%s share calc failed: %s", ticker, exc)
            send_error_alert(f"share calculation for {ticker}", exc)
            continue

        # ── Place order ───────────────────────────────────────────────────────
        trade = None
        try:
            if instrument_type.upper() == "UNITTRUST":
                trade = await mgr.place_market_order(
                    ticker=ticker,
                    shares=shares_to_buy,
                    currency=currency,
                )
            else:
                if not limit_price:
                    logger.error("%s: no limit price recorded — cannot place limit order.", ticker)
                    send_error_alert(
                        f"Phase 1 — {ticker}",
                        ValueError("limit price is None for ETF order"),
                    )
                    continue
                trade = await mgr.place_limit_order(
                    ticker=ticker,
                    shares=shares_to_buy,
                    limit_price=limit_price,
                    currency=currency,
                )
        except IBOrderError as exc:
            logger.error("%s order placement failed: %s", ticker, exc)
            send_post_order_alert(
                ticker=ticker,
                instrument_type=instrument_type,
                status="FAILED",
                shares_filled=0,
                fill_price=0,
                new_weighted_avg=current_avg,
                remaining_cash_balance=account_equity,
                currency=currency,
                error_message=str(exc),
            )
            continue

        # ── Await fill ───────────────────────────────────────────────────────
        result = await mgr.await_fill(trade)
        cash_after = await mgr.get_cash_balance()

        if result["shares_filled"] > 0:
            fill_price = result["avg_fill_price"]
            filled_shares = result["shares_filled"]
            df, tracker = apply_fill_to_state(
                df, tracker, ticker, filled_shares, fill_price
            )
            new_avg = float(df.loc[df["Ticker"] == ticker, "Weighted_Avg_Price"].iloc[0])

            send_post_order_alert(
                ticker=ticker,
                instrument_type=instrument_type,
                status=result["status"],
                shares_filled=filled_shares,
                fill_price=fill_price,
                new_weighted_avg=new_avg,
                remaining_cash_balance=cash_after,
                currency=currency,
            )
        else:
            # Order not filled at all — keep state so it retries tomorrow?
            # Design choice: clear execution flag to avoid re-ordering indefinitely.
            tracker["tickers"][ticker]["is_waiting_for_execution"] = False
            send_post_order_alert(
                ticker=ticker,
                instrument_type=instrument_type,
                status=result["status"],
                shares_filled=0,
                fill_price=0,
                new_weighted_avg=current_avg,
                remaining_cash_balance=cash_after,
                currency=currency,
                error_message="Order was not filled within timeout window.",
            )

    return df, tracker


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Dip signal detection
# ══════════════════════════════════════════════════════════════════════════════

async def phase2_check_dip_signals(
    mgr: IBManager,
    df,
    tracker: dict,
) -> tuple:
    """
    For each ticker, fetch market price and check the 5% dip condition.
    Records signals for T+1 execution.
    """
    logger.info("═══ PHASE 2: Checking dip signals ═══")

    # Monthly budget accumulation (runs only on the 1st of each month)
    tracker, credited = accumulate_monthly_budget(tracker)
    if credited:
        logger.info("Monthly budget added for: %s", credited)

    today_str = date.today().isoformat()

    for _, row in df.iterrows():
        ticker: str = str(row["Ticker"])
        instrument_type: str = str(row["Type"])
        weighted_avg: float = float(row["Weighted_Avg_Price"])
        currency: str = str(row["Currency"])
        state = tracker["tickers"].get(ticker)

        if state is None:
            logger.warning("No tracker state for %s — skipping.", ticker)
            continue

        # Skip if already waiting — don't stack up duplicate signals
        if state.get("is_waiting_for_execution"):
            logger.info("%s: already waiting for execution — skip dip check.", ticker)
            continue

        pending_balance: float = state["pending_balance"]

        # ── Fetch price ───────────────────────────────────────────────────────
        try:
            current_price = await mgr.get_last_price(ticker=ticker, currency=currency)
        except IBPriceFetchError as exc:
            logger.error("Price fetch failed for %s: %s", ticker, exc)
            send_error_alert(f"Phase 2 — price fetch for {ticker}", exc)
            continue

        # ── Dip check ─────────────────────────────────────────────────────────
        signal = check_dip_signal(
            ticker=ticker,
            current_price=current_price,
            weighted_avg_price=weighted_avg,
            pending_balance=pending_balance,
        )

        if signal:
            from src.logic import calculate_limit_price  # local import to avoid circular

            limit_price = (
                None
                if instrument_type.upper() == "UNITTRUST"
                else calculate_limit_price(current_price)
            )

            tracker = record_signal(
                tracker=tracker,
                ticker=ticker,
                closing_price=current_price,
                instrument_type=instrument_type,
            )

            send_pre_order_alert(
                ticker=ticker,
                instrument_type=instrument_type,
                current_price=current_price,
                weighted_avg_price=weighted_avg,
                accumulated_budget=pending_balance,
                planned_limit_price=limit_price,
                currency=currency,
            )

    return df, tracker


# ══════════════════════════════════════════════════════════════════════════════
# Main entry point
# ══════════════════════════════════════════════════════════════════════════════

async def run_daily_job() -> None:
    logger.info("▶▶▶ Value Averaging Bot starting — %s", date.today().isoformat())

    # Load state
    df = load_investments()
    tracker = load_tracker()

    try:
        async with IBManager() as mgr:
            # Phase 1: execute any pending orders from yesterday's signal
            df, tracker = await phase1_execute_pending_orders(mgr, df, tracker)

            # Phase 2: check today's prices for new dip signals
            df, tracker = await phase2_check_dip_signals(mgr, df, tracker)

    except IBConnectionError as exc:
        logger.critical("Cannot connect to IB Gateway: %s", exc)
        send_error_alert("IB Gateway connection", exc)
        sys.exit(1)
    except Exception as exc:
        logger.critical("Unexpected error in daily job: %s", exc, exc_info=True)
        send_error_alert("run_daily_job", exc)
        sys.exit(1)
    finally:
        # Always persist state even on partial failure
        save_investments(df)
        save_tracker(tracker)
        logger.info("◀◀◀ State saved. Bot run complete.")


def main() -> None:
    asyncio.run(run_daily_job())


if __name__ == "__main__":
    main()
