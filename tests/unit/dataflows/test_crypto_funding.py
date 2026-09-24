"""Tests for funding rate data fetching (mocked HTTP)."""

import pytest
from unittest.mock import patch, MagicMock

from tradingagents.dataflows.vendors.crypto_funding import (
    get_funding_rates,
    get_open_interest,
    get_long_short_ratio,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

MOCK_FUNDING_RATES = [
    {"symbol": "BTCUSDT", "fundingRate": "0.00010000", "fundingTime": 1700000000000},
    {"symbol": "BTCUSDT", "fundingRate": "0.00015000", "fundingTime": 1700028800000},
    {"symbol": "BTCUSDT", "fundingRate": "-0.00005000", "fundingTime": 1700057600000},
    {"symbol": "BTCUSDT", "fundingRate": "0.00020000", "fundingTime": 1700086400000},
    {"symbol": "BTCUSDT", "fundingRate": "0.00012000", "fundingTime": 1700115200000},
]

MOCK_OPEN_INTEREST = {
    "symbol": "BTCUSDT",
    "openInterest": "85000.123",
    "time": 1700000000000,
}

MOCK_OI_HISTORY = [
    {"sumOpenInterest": "84000.000", "sumOpenInterestValue": "5670000000.00", "timestamp": 1699913600000},
    {"sumOpenInterest": "85000.123", "sumOpenInterestValue": "5737500000.00", "timestamp": 1699917200000},
]

MOCK_LONG_SHORT_RATIO = [
    {"symbol": "BTCUSDT", "longShortRatio": "1.2500", "longAccount": "0.5556", "shortAccount": "0.4444", "timestamp": 1700000000000},
    {"symbol": "BTCUSDT", "longShortRatio": "1.1000", "longAccount": "0.5238", "shortAccount": "0.4762", "timestamp": 1700003600000},
    {"symbol": "BTCUSDT", "longShortRatio": "0.8500", "longAccount": "0.4595", "shortAccount": "0.5405", "timestamp": 1700007200000},
]


def _mock_get_success(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestGetFundingRates:
    @patch("tradingagents.dataflows.vendors.crypto_funding.httpx.get")
    def test_funding_rates_success(self, mock_get):
        mock_get.return_value = _mock_get_success(MOCK_FUNDING_RATES)

        result = get_funding_rates("BTCUSDT", limit=5)

        assert isinstance(result, str)
        assert len(result) > 20
        # Should contain funding rate data
        assert "0.0001" in result or "funding" in result.lower()

    @patch("tradingagents.dataflows.vendors.crypto_funding.httpx.get")
    def test_api_error_returns_error_string(self, mock_get):
        mock_get.side_effect = Exception("Connection timeout")

        result = get_funding_rates("BTCUSDT")

        assert isinstance(result, str)
        assert "failed" in result.lower() or "error" in result.lower()


class TestGetOpenInterest:
    @patch("tradingagents.dataflows.vendors.crypto_funding.httpx.get")
    def test_open_interest_success(self, mock_get):
        # get_open_interest makes 2 calls: current OI + OI history
        mock_get.side_effect = [
            _mock_get_success(MOCK_OPEN_INTEREST),
            _mock_get_success(MOCK_OI_HISTORY),
        ]

        result = get_open_interest("BTCUSDT")

        assert isinstance(result, str)
        assert "85000" in result or "85,000" in result
        assert len(result) > 20

    @patch("tradingagents.dataflows.vendors.crypto_funding.httpx.get")
    def test_api_error_returns_error_string(self, mock_get):
        mock_get.side_effect = Exception("Rate limited")

        result = get_open_interest("BTCUSDT")

        assert isinstance(result, str)


class TestGetLongShortRatio:
    @patch("tradingagents.dataflows.vendors.crypto_funding.httpx.get")
    def test_ratio_success(self, mock_get):
        mock_get.return_value = _mock_get_success(MOCK_LONG_SHORT_RATIO)

        result = get_long_short_ratio("BTCUSDT", period="1h", limit=3)

        assert isinstance(result, str)
        assert len(result) > 20

    @patch("tradingagents.dataflows.vendors.crypto_funding.httpx.get")
    def test_api_error_returns_error_string(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")

        result = get_long_short_ratio("BTCUSDT")

        assert isinstance(result, str)
