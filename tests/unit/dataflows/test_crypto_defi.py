"""Tests for DeFi/tokenomics data fetching (mocked HTTP)."""

import pytest
from unittest.mock import patch, MagicMock

from tradingagents.dataflows.vendors.crypto_defi import (
    get_token_fundamentals,
    get_defi_tvl,
    get_chain_tvl_overview,
    COINGECKO_ID_MAP,
    DEFILLAMA_PROTOCOL_MAP,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

MOCK_COINGECKO_RESPONSE = {
    "market_data": {
        "current_price": {"usd": 67500.0},
        "market_cap": {"usd": 1320000000000},
        "fully_diluted_valuation": {"usd": 1418000000000},
        "circulating_supply": 19700000,
        "total_supply": 21000000,
        "max_supply": 21000000,
        "total_volume": {"usd": 32000000000},
        "price_change_percentage_24h": 2.5,
        "price_change_percentage_7d": -1.2,
        "price_change_percentage_30d": 8.3,
        "ath": {"usd": 73800},
        "atl": {"usd": 67.81},
        "market_cap_rank": 1,
    }
}

MOCK_DEFILLAMA_PROTOCOL = {
    "name": "Lido",
    "tvl": [
        {"date": "2024-01-01", "totalLiquidityUSD": 15000000000},
        {"date": "2024-01-15", "totalLiquidityUSD": 16000000000},
        {"date": "2024-02-01", "totalLiquidityUSD": 17000000000},
    ],
    "currentChainTvls": {
        "Ethereum": 15000000000,
        "Solana": 500000000,
        "Polygon": 200000000,
    },
}

MOCK_DEFILLAMA_CHAINS = [
    {"name": "Ethereum", "tvl": 60000000000},
    {"name": "BSC", "tvl": 5000000000},
    {"name": "Solana", "tvl": 4000000000},
]


def _mock_get_success(json_data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestCoinGeckoIdMap:
    def test_btc_mapped(self):
        assert COINGECKO_ID_MAP["BTC"] == "bitcoin"

    def test_eth_mapped(self):
        assert COINGECKO_ID_MAP["ETH"] == "ethereum"

    def test_sol_mapped(self):
        assert COINGECKO_ID_MAP["SOL"] == "solana"


class TestDefiLlamaProtocolMap:
    def test_eth_has_protocol(self):
        assert "ETH" in DEFILLAMA_PROTOCOL_MAP

    def test_sol_has_protocol(self):
        assert "SOL" in DEFILLAMA_PROTOCOL_MAP


class TestGetTokenFundamentals:
    @patch("tradingagents.dataflows.vendors.crypto_defi.httpx.get")
    def test_btc_fundamentals_success(self, mock_get):
        mock_get.return_value = _mock_get_success(MOCK_COINGECKO_RESPONSE)

        result = get_token_fundamentals("BTCUSDT")

        assert isinstance(result, str)
        assert "67500" in result or "67,500" in result
        assert len(result) > 50

    @patch("tradingagents.dataflows.vendors.crypto_defi.httpx.get")
    def test_api_error_returns_error_string(self, mock_get):
        mock_get.side_effect = Exception("Rate limited")

        result = get_token_fundamentals("BTCUSDT")

        assert isinstance(result, str)
        assert "failed" in result.lower() or "error" in result.lower()

    @patch("tradingagents.dataflows.vendors.crypto_defi.httpx.get")
    def test_unknown_coin_handled(self, mock_get):
        result = get_token_fundamentals("UNKNOWNCOIN")

        assert isinstance(result, str)


class TestGetDefiTvl:
    @patch("tradingagents.dataflows.vendors.crypto_defi.httpx.get")
    def test_protocol_tvl_success(self, mock_get):
        mock_get.return_value = _mock_get_success(MOCK_DEFILLAMA_PROTOCOL)

        result = get_defi_tvl("lido")

        assert isinstance(result, str)
        assert len(result) > 20

    @patch("tradingagents.dataflows.vendors.crypto_defi.httpx.get")
    def test_api_error_returns_error_string(self, mock_get):
        mock_get.side_effect = Exception("Not found")

        result = get_defi_tvl("nonexistent")

        assert isinstance(result, str)


class TestGetChainTvlOverview:
    @patch("tradingagents.dataflows.vendors.crypto_defi.httpx.get")
    def test_chain_overview_success(self, mock_get):
        mock_get.return_value = _mock_get_success(MOCK_DEFILLAMA_CHAINS)

        result = get_chain_tvl_overview()

        assert isinstance(result, str)
        assert "Ethereum" in result

    @patch("tradingagents.dataflows.vendors.crypto_defi.httpx.get")
    def test_api_error_returns_error_string(self, mock_get):
        mock_get.side_effect = Exception("Connection refused")

        result = get_chain_tvl_overview()

        assert isinstance(result, str)
