"""
notifier.py
────────────────────────────────────────────────────────────────────────────────
SMTP email notifications for the trading bot.

Two main notification types:
  • Pre-Order Alert  — sent when a 5% dip signal fires (Day T).
  • Post-Order Alert — sent after an order fills or fails (Day T+1).
  • Balance Alert    — sent when account equity is insufficient.
  • Error Alert      — sent on unexpected runtime errors.
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import logging
import os
import smtplib
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# ── SMTP Configuration ────────────────────────────────────────────────────────
SMTP_HOST: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT: int = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER: str = os.getenv("SMTP_USER", "")
SMTP_PASS: str = os.getenv("SMTP_PASS", "")
NOTIFY_TO: str = os.getenv("NOTIFY_TO", "")
NOTIFY_FROM: str = os.getenv("NOTIFY_FROM", SMTP_USER)


# ══════════════════════════════════════════════════════════════════════════════
# Low-level send helper
# ══════════════════════════════════════════════════════════════════════════════

def _send_email(subject: str, html_body: str) -> bool:
    """
    Sends an HTML email via SMTP TLS.

    Returns True on success, False on failure (logs the error — never raises).
    """
    if not all([SMTP_USER, SMTP_PASS, NOTIFY_TO]):
        logger.warning("Email not configured — skipping notification: %s", subject)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = NOTIFY_FROM
    msg["To"] = NOTIFY_TO
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.ehlo()
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(NOTIFY_FROM, [NOTIFY_TO], msg.as_string())
        logger.info("Email sent: %s", subject)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send email '%s': %s", subject, exc)
        return False


# ══════════════════════════════════════════════════════════════════════════════
# HTML template helpers
# ══════════════════════════════════════════════════════════════════════════════

_STYLE = """
<style>
  body { font-family: Arial, sans-serif; background: #f4f4f4; margin: 0; padding: 20px; }
  .card { background: #fff; border-radius: 8px; padding: 24px 32px; max-width: 560px;
          margin: 0 auto; box-shadow: 0 2px 8px rgba(0,0,0,.1); }
  h2 { margin-top: 0; }
  table { width: 100%; border-collapse: collapse; margin-top: 12px; }
  td { padding: 8px 12px; border-bottom: 1px solid #eee; }
  td:first-child { color: #666; font-size: 13px; width: 48%; }
  td:last-child { font-weight: bold; text-align: right; }
  .tag { display: inline-block; padding: 3px 10px; border-radius: 12px;
         font-size: 12px; font-weight: bold; }
  .buy  { background: #d4edda; color: #155724; }
  .warn { background: #fff3cd; color: #856404; }
  .fail { background: #f8d7da; color: #721c24; }
  .foot { margin-top: 20px; font-size: 11px; color: #999; text-align: center; }
</style>
"""


def _html_wrapper(title: str, tag_class: str, tag_label: str, rows: list[tuple[str, str]], note: str = "") -> str:
    row_html = "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows)
    note_html = f"<p style='margin-top:16px;font-size:13px;color:#555;'>{note}</p>" if note else ""
    from datetime import timezone as _tz
    timestamp = datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"""
<!DOCTYPE html><html><head>{_STYLE}</head><body>
<div class="card">
  <h2>{title} &nbsp;<span class="tag {tag_class}">{tag_label}</span></h2>
  <table>{row_html}</table>
  {note_html}
  <div class="foot">Value Averaging Bot &bull; {timestamp}</div>
</div>
</body></html>"""


# ══════════════════════════════════════════════════════════════════════════════
# Public notification functions
# ══════════════════════════════════════════════════════════════════════════════

def send_pre_order_alert(
    ticker: str,
    instrument_type: str,
    current_price: float,
    weighted_avg_price: float,
    accumulated_budget: float,
    planned_limit_price: Optional[float],
    currency: str = "USD",
) -> bool:
    """
    Email 1 — Sent when the 5% dip condition is met.
    Includes: Ticker, Current vs Avg Price, Budget, Planned T+1 Order Details.
    """
    dip_pct = ((weighted_avg_price - current_price) / weighted_avg_price) * 100
    order_type = "Market Order (Unit Trust)" if instrument_type.upper() == "UNITTRUST" else "Limit Order"
    limit_display = f"{currency} {planned_limit_price:.4f}" if planned_limit_price else "At NAV (Market)"

    rows = [
        ("Ticker", ticker),
        ("Instrument Type", instrument_type),
        ("Current Market Price", f"{currency} {current_price:.4f}"),
        ("Weighted Avg Price (WAP)", f"{currency} {weighted_avg_price:.4f}"),
        ("Dip from WAP", f"▼ {dip_pct:.2f}%"),
        ("Accumulated Budget", f"{currency} {accumulated_budget:.2f}"),
        ("Tomorrow's Order Type", order_type),
        ("Planned Limit Price (T+1)", limit_display),
    ]
    note = (
        "⚠️ A limit order will be placed tomorrow at market open at the price shown above. "
        "Please ensure sufficient funds are available in your IBKR account."
    )
    body = _html_wrapper(
        title=f"📉 Dip Signal: {ticker}",
        tag_class="buy",
        tag_label="BUY SIGNAL",
        rows=rows,
        note=note,
    )
    return _send_email(
        subject=f"[TradingBot] 📉 Dip Signal Triggered — {ticker}",
        html_body=body,
    )


def send_post_order_alert(
    ticker: str,
    instrument_type: str,
    status: str,             # "FILLED" | "PARTIAL" | "FAILED" | "CANCELLED"
    shares_filled: float,
    fill_price: float,
    new_weighted_avg: float,
    remaining_cash_balance: float,
    currency: str = "USD",
    error_message: str = "",
) -> bool:
    """
    Email 2 — Sent after a trade order resolves (fill, partial, or failure).
    Includes: Fill details, new WAP, and IBKR cash balance.
    """
    status_class = "buy" if status == "FILLED" else ("warn" if status == "PARTIAL" else "fail")
    rows = [
        ("Ticker", ticker),
        ("Instrument Type", instrument_type),
        ("Order Status", status),
        ("Shares Filled", f"{shares_filled:.4f}"),
        ("Final Fill Price", f"{currency} {fill_price:.4f}"),
        ("New Weighted Avg Price", f"{currency} {new_weighted_avg:.6f}"),
        ("IBKR Remaining Cash", f"{currency} {remaining_cash_balance:,.2f}"),
    ]
    note = f"Error detail: {error_message}" if error_message else ""
    body = _html_wrapper(
        title=f"📋 Order Update: {ticker}",
        tag_class=status_class,
        tag_label=status,
        rows=rows,
        note=note,
    )
    return _send_email(
        subject=f"[TradingBot] 📋 Order {status} — {ticker}",
        html_body=body,
    )


def send_balance_alert(
    ticker: str,
    required_balance: float,
    account_equity: float,
    currency: str = "USD",
) -> bool:
    """
    Sent when Account_Equity < pending_balance for a ticker.
    Trade is skipped and user is alerted.
    """
    rows = [
        ("Ticker", ticker),
        ("Required Budget", f"{currency} {required_balance:.2f}"),
        ("Account Equity", f"{currency} {account_equity:,.2f}"),
        ("Shortfall", f"{currency} {required_balance - account_equity:.2f}"),
        ("Action Taken", "Trade SKIPPED — insufficient funds"),
    ]
    note = (
        "⛔ The trade was skipped because your account equity is below the required budget. "
        "Please top up your IBKR account to resume automated buying."
    )
    body = _html_wrapper(
        title=f"⛔ Insufficient Funds: {ticker}",
        tag_class="fail",
        tag_label="SKIPPED",
        rows=rows,
        note=note,
    )
    return _send_email(
        subject=f"[TradingBot] ⛔ Trade Skipped — Insufficient Funds ({ticker})",
        html_body=body,
    )


def send_error_alert(context: str, exc: Exception) -> bool:
    """
    Generic error notification for unexpected runtime failures.
    """
    tb = traceback.format_exc()
    rows = [
        ("Context", context),
        ("Error Type", type(exc).__name__),
        ("Message", str(exc)),
    ]
    note = f"<pre style='font-size:11px;background:#f8f8f8;padding:8px;border-radius:4px;overflow:auto'>{tb[:2000]}</pre>"
    body = _html_wrapper(
        title="🚨 Bot Runtime Error",
        tag_class="fail",
        tag_label="ERROR",
        rows=rows,
        note=note,
    )
    return _send_email(
        subject=f"[TradingBot] 🚨 Runtime Error in {context}",
        html_body=body,
    )
