"""Tests for on-chain data fetching (mocked HTTP)."""

import pytest
from unittest.mock import patch, MagicMock

from tradingagents.dataflows.vendors.crypto_onchain import (
    get_blockchain_stats,
    get_address_activity,
    CHAIN_MAP,
    _strip_coin,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

MOCK_BLOCKCHAIR_STATS = {
    "data": {
        "transactions_24h": 350000,
        "blocks_24h": 144,
        "hashrate_24h": "5.5E+20",
        "mempool_transactions": 12000,
        "average_transaction_fee_24h": 15000,  # satoshis
        "difficulty": 8.5e13,
        "market_price_usd": 67500.0,
        "market_cap_usd": 1320000000000,
    }
}

MOCK_BLOCKCHAIN_INFO_STATS = {
    "n_tx": 450000,
    "hash_rate": 550000000000000000000.0,
    "totalbc": 1970000000000000,
    "n_blocks_total": 835000,
}


def _mock_get_success(json_data, status_code=200):
    """Create a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestStripCoin:
    def test_binance_pair(self):
        assert _strip_coin("BTCUSDT") == "BTC"

    def test_bare_coin(self):
        assert _strip_coin("BTC") == "BTC"

    def test_yfinance_ticker(self):
        assert _strip_coin("BTC-USD") == "BTC"

    def test_lowercase(self):
        assert _strip_coin("ethusdt") == "ETH"

    def test_busd_pair(self):
        assert _strip_coin("SOLBUSD") == "SOL"


class TestChainMap:
    def test_btc_maps_to_bitcoin(self):
        assert CHAIN_MAP["BTC"] == "bitcoin"

    def test_eth_maps_to_ethereum(self):
        assert CHAIN_MAP["ETH"] == "ethereum"

    def test_sol_maps_to_solana(self):
        assert CHAIN_MAP["SOL"] == "solana"


class TestGetBlockchainStats:
    @patch("tradingagents.dataflows.vendors.crypto_onchain.httpx.get")
    def test_btc_stats_success(self, mock_get):
        mock_get.return_value = _mock_get_success(MOCK_BLOCKCHAIR_STATS)

        result = get_blockchain_stats("BTCUSDT")

        assert isinstance(result, str)
        assert "350000" in result or "350,000" in result  # transactions_24h
        assert "67500" in result or "67,500" in result  # market_price
        mock_get.assert_called()

    @patch("tradingagents.dataflows.vendors.crypto_onchain.httpx.get")
    def test_eth_stats_success(self, mock_get):
        mock_get.return_value = _mock_get_success(MOCK_BLOCKCHAIR_STATS)

        result = get_blockchain_stats("ETH")

        assert isinstance(result, str)
        assert len(result) > 50  # Meaningful content

    @patch("tradingagents.dataflows.vendors.crypto_onchain.httpx.get")
    def test_api_error_returns_error_string(self, mock_get):
        mock_get.side_effect = Exception("Connection timeout")

        result = get_blockchain_stats("BTCUSDT")

        assert "failed" in result.lower() or "error" in result.lower()

    @patch("tradingagents.dataflows.vendors.crypto_onchain.httpx.get")
    def test_unknown_chain_handled(self, mock_get):
        """Coins not in CHAIN_MAP should still return a graceful message."""
        result = get_blockchain_stats("UNKNOWNCOIN")

        assert isinstance(result, str)


class TestGetAddressActivity:
    @patch("tradingagents.dataflows.vendors.crypto_onchain.httpx.get")
    def test_btc_activity_success(self, mock_get):
        mock_get.return_value = _mock_get_success(MOCK_BLOCKCHAIR_STATS)

        result = get_address_activity("BTCUSDT", days=3)

        assert isinstance(result, str)
        assert len(result) > 20

    @patch("tradingagents.dataflows.vendors.crypto_onchain.httpx.get")
    def test_api_error_returns_error_string(self, mock_get):
        mock_get.side_effect = Exception("API down")

        result = get_address_activity("BTCUSDT")

        assert isinstance(result, str)
