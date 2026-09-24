"""Tests for ATR data fetching and parsing.

Validates the fallback estimate and the regex-based parser that extracts
ATR values from vendor text responses.
"""

import pytest
from unittest.mock import MagicMock, patch

from skopaq.risk.atr import estimate_atr, _parse_atr_value, fetch_atr


# ── Estimate ATR ──────────────────────────────────────────────────────────────


class TestEstimateATR:
    def test_default_2pct(self):
        """Default: 2% of price."""
        assert estimate_atr(2500.0) == 50.0

    def test_custom_pct(self):
        assert estimate_atr(1000.0, pct=0.03) == 30.0

    def test_zero_price(self):
        assert estimate_atr(0) == 0.0

    def test_negative_price(self):
        """Negative price should return positive ATR (abs)."""
        assert estimate_atr(-100.0) == 2.0  # abs(-100 * 0.02)


# ── Parse ATR value ───────────────────────────────────────────────────────────


class TestParseATRValue:
    def test_standard_format(self):
        """'ATR = 45.23' pattern."""
        raw = "2026-03-01: ATR = 45.23"
        assert _parse_atr_value(raw) == 45.23

    def test_csv_format(self):
        """'date,ATR\\n2026-03-01,45.23' pattern."""
        raw = "date,ATR\n2026-02-28,42.10\n2026-03-01,45.23"
        assert _parse_atr_value(raw) == 45.23

    def test_colon_format(self):
        """'ATR: 45.23' pattern."""
        raw = "Latest ATR: 45.23"
        assert _parse_atr_value(raw) == 45.23

    def test_lowercase(self):
        raw = "atr = 33.5"
        assert _parse_atr_value(raw) == 33.5

    def test_multiple_values_takes_last(self):
        """Multiple ATR values → take the most recent (last in text)."""
        raw = "ATR = 40.0\nATR = 42.0\nATR = 45.0"
        assert _parse_atr_value(raw) == 45.0

    def test_fallback_to_numeric_extraction(self):
        """No explicit ATR label → extract number from last lines."""
        raw = "Technical Analysis Report\nVolatility: moderate\n2026-03-01, 45.23"
        result = _parse_atr_value(raw)
        assert result == 45.23

    def test_no_numbers_returns_none(self):
        raw = "No data available for this symbol"
        assert _parse_atr_value(raw) is None

    def test_empty_string_returns_none(self):
        assert _parse_atr_value("") is None

    def test_unreasonable_value_filtered(self):
        """Values outside 0.1–10000 range should be filtered out."""
        raw = "timestamp: 1709251200"  # Unix timestamp, not ATR
        # The number is way too large to be a valid ATR
        assert _parse_atr_value(raw) is None


class TestFetchATR:
    """Test fetch_atr which lazy-imports route_to_vendor inside a try/except."""

    def test_vendor_failure_returns_none(self):
        """When upstream vendor is unavailable, return None."""
        with patch.dict("sys.modules", {"tradingagents.dataflows.router": None}):
            result = fetch_atr("RELIANCE", "2026-03-01")
        assert result is None

    def test_vendor_returns_empty(self):
        mock_module = MagicMock()
        mock_module.route_to_vendor.return_value = ""
        with patch.dict("sys.modules", {"tradingagents.dataflows.router": mock_module}):
            result = fetch_atr("RELIANCE", "2026-03-01")
        assert result is None

    def test_vendor_returns_valid_data(self):
        mock_module = MagicMock()
        mock_module.route_to_vendor.return_value = "ATR = 55.0"
        with patch.dict("sys.modules", {"tradingagents.dataflows.router": mock_module}):
            result = fetch_atr("RELIANCE", "2026-03-01")
        assert result == 55.0
