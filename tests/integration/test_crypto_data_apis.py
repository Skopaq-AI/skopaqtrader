"""Integration tests — Crypto data API connectivity.

Tests real API calls to free, public crypto endpoints (no authentication needed).
- Blockchair: 60 req/min
- DeFiLlama: No rate limit
- Binance Futures: 1200 weight/min
- CoinGecko: 10-50 req/min (free tier)
"""

import pytest

from tradingagents.dataflows.vendors.crypto_onchain import (
    get_blockchain_stats,
    get_address_activity,
)
from tradingagents.dataflows.vendors.crypto_defi import (
    get_token_fundamentals,
    get_defi_tvl,
    get_chain_tvl_overview,
)
from tradingagents.dataflows.vendors.crypto_funding import (
    get_funding_rates,
    get_open_interest,
    get_long_short_ratio,
)

pytestmark = pytest.mark.integration


# ── Blockchair (On-chain) ─────────────────────────────────────────────────────


class TestBlockchairLive:
    def test_btc_blockchain_stats(self):
        """Blockchair returns BTC network stats with positive values."""
        result = get_blockchain_stats("BTCUSDT")
        assert isinstance(result, str)
        assert len(result) > 100
        # Should contain network metrics
        assert "transaction" in result.lower() or "hash" in result.lower()

    def test_eth_blockchain_stats(self):
        """Blockchair returns ETH network stats."""
        result = get_blockchain_stats("ETH")
        assert isinstance(result, str)
        assert len(result) > 50

    def test_btc_address_activity(self):
        """Blockchair returns BTC address activity data."""
        result = get_address_activity("BTCUSDT", days=3)
        assert isinstance(result, str)
        assert len(result) > 50


# ── DeFiLlama + CoinGecko (DeFi/Tokenomics) ─────────────────────────────────


class TestDeFiLlamaLive:
    def test_chain_tvl_overview(self):
        """DeFiLlama returns TVL data for multiple chains."""
        result = get_chain_tvl_overview()
        assert isinstance(result, str)
        assert "Ethereum" in result
        assert len(result) > 100

    def test_lido_tvl(self):
        """DeFiLlama returns TVL for Lido protocol."""
        result = get_defi_tvl("lido")
        assert isinstance(result, str)
        assert len(result) > 50


class TestCoinGeckoLive:
    def test_btc_token_fundamentals(self):
        """CoinGecko returns BTC market data."""
        result = get_token_fundamentals("BTCUSDT")
        assert isinstance(result, str)
        assert len(result) > 100
        # Should contain supply or market cap data
        assert "supply" in result.lower() or "market" in result.lower()

    def test_eth_token_fundamentals(self):
        """CoinGecko returns ETH market data."""
        result = get_token_fundamentals("ETHUSDT")
        assert isinstance(result, str)
        assert len(result) > 50


# ── Binance Futures (Funding/OI) ─────────────────────────────────────────────


class TestBinanceFuturesLive:
    def test_btc_funding_rates(self):
        """Binance Futures returns BTC funding rate history."""
        result = get_funding_rates("BTCUSDT", limit=10)
        assert isinstance(result, str)
        assert len(result) > 50

    def test_btc_open_interest(self):
        """Binance Futures returns BTC open interest."""
        result = get_open_interest("BTCUSDT")
        assert isinstance(result, str)
        assert len(result) > 20

    def test_btc_long_short_ratio(self):
        """Binance Futures returns long/short ratio data."""
        result = get_long_short_ratio("BTCUSDT", period="1h", limit=5)
        assert isinstance(result, str)
        assert len(result) > 20
