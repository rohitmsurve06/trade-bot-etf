"""
tests/test_notifier.py
────────────────────────────────────────────────────────────────────────────────
Unit tests for src/notifier.py

Tests that:
  • _send_email is called with correct subject/body fragments
  • All four public functions invoke _send_email
  • Missing SMTP config silently skips sending (no exception)
────────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.notifier import (
    send_balance_alert,
    send_error_alert,
    send_post_order_alert,
    send_pre_order_alert,
)


@pytest.fixture(autouse=True)
def mock_smtp_env(monkeypatch):
    """Set minimal SMTP env vars for all tests."""
    monkeypatch.setenv("SMTP_USER", "bot@example.com")
    monkeypatch.setenv("SMTP_PASS", "secret")
    monkeypatch.setenv("NOTIFY_TO", "investor@example.com")
    monkeypatch.setenv("NOTIFY_FROM", "bot@example.com")


class TestSendPreOrderAlert:

    def test_calls_send_email(self):
        with patch("src.notifier._send_email", return_value=True) as mock_send:
            result = send_pre_order_alert(
                ticker="INDA",
                instrument_type="ETF",
                current_price=36.00,
                weighted_avg_price=38.50,
                accumulated_budget=300.0,
                planned_limit_price=35.93,
                currency="USD",
            )
        mock_send.assert_called_once()
        args = mock_send.call_args
        subject = args[0][0] if args[0] else args[1].get("subject", "")
        body = args[0][1] if len(args[0]) > 1 else args[1].get("html_body", "")
        assert "INDA" in subject
        assert "INDA" in body

    def test_unit_trust_shows_market_order(self):
        with patch("src.notifier._send_email", return_value=True) as mock_send:
            send_pre_order_alert(
                ticker="NIFBEES",
                instrument_type="UnitTrust",
                current_price=115.00,
                weighted_avg_price=120.50,
                accumulated_budget=100.0,
                planned_limit_price=None,
                currency="INR",
            )
        args = mock_send.call_args
        body = args[0][1] if len(args[0]) > 1 else args[1].get("html_body", "")
        assert "Market" in body or "NAV" in body


def _get_email_args(mock_send):
    """Extract subject and html_body from a mocked _send_email call regardless
    of whether positional or keyword arguments were used."""
    ca = mock_send.call_args
    positional = ca[0]  # tuple of positional args
    keyword = ca[1]     # dict of keyword args
    subject = positional[0] if len(positional) > 0 else keyword.get("subject", "")
    body = positional[1] if len(positional) > 1 else keyword.get("html_body", "")
    return subject, body


class TestSendPostOrderAlert:

    def test_filled_status(self):
        with patch("src.notifier._send_email", return_value=True) as mock_send:
            send_post_order_alert(
                ticker="INDA",
                instrument_type="ETF",
                status="FILLED",
                shares_filled=2.7,
                fill_price=35.93,
                new_weighted_avg=38.35,
                remaining_cash_balance=9500.0,
                currency="USD",
            )
        subject, body = _get_email_args(mock_send)
        assert "FILLED" in subject or "FILLED" in body

    def test_failed_status_includes_error(self):
        with patch("src.notifier._send_email", return_value=True) as mock_send:
            send_post_order_alert(
                ticker="INDA",
                instrument_type="ETF",
                status="FAILED",
                shares_filled=0,
                fill_price=0,
                new_weighted_avg=38.50,
                remaining_cash_balance=9500.0,
                currency="USD",
                error_message="Connection timeout",
            )
        _, body = _get_email_args(mock_send)
        assert "Connection timeout" in body


class TestSendBalanceAlert:

    def test_includes_shortfall(self):
        with patch("src.notifier._send_email", return_value=True) as mock_send:
            send_balance_alert(
                ticker="INDA",
                required_balance=500.0,
                account_equity=200.0,
                currency="USD",
            )
        _, body = _get_email_args(mock_send)
        assert "300" in body   # shortfall = 500 - 200 = 300


class TestSendErrorAlert:

    def test_includes_traceback(self):
        exc = ValueError("Something broke")
        with patch("src.notifier._send_email", return_value=True) as mock_send:
            send_error_alert("test_context", exc)
        _, body = _get_email_args(mock_send)
        assert "ValueError" in body or "Something broke" in body


class TestMissingSmtpConfig:

    def test_returns_false_when_no_config(self, monkeypatch):
        """When SMTP env vars are absent, _send_email should return False without raising."""
        monkeypatch.delenv("SMTP_USER", raising=False)
        monkeypatch.delenv("SMTP_PASS", raising=False)
        monkeypatch.delenv("NOTIFY_TO", raising=False)

        # Re-import to pick up env changes
        import importlib
        import src.notifier as notifier_mod
        importlib.reload(notifier_mod)

        result = notifier_mod._send_email("Test subject", "<p>body</p>")
        assert result is False
