"""LangGraph @tool definitions for crypto-specific analysts.

Each tool is a thin wrapper delegating to the corresponding dataflow module.
Tools must be sync functions (LangGraph ToolNode convention).
"""

from langchain_core.tools import tool

from tradingagents.dataflows.crypto_onchain import (
    get_blockchain_stats as _get_blockchain_stats,
    get_address_activity as _get_address_activity,
)
from tradingagents.dataflows.crypto_defi import (
    get_token_fundamentals as _get_token_fundamentals,
    get_defi_tvl as _get_defi_tvl,
    get_chain_tvl_overview as _get_chain_tvl_overview,
)
from tradingagents.dataflows.crypto_funding import (
    get_funding_rates as _get_funding_rates,
    get_open_interest as _get_open_interest,
    get_long_short_ratio as _get_long_short_ratio,
)


# ── On-Chain Tools ─────────────────────────────────────────────────────────


@tool
def get_blockchain_stats(coin: str) -> str:
    """Fetch blockchain network statistics for a cryptocurrency.

    Returns hashrate, difficulty, mempool, transaction count, fees,
    and market price data from Blockchair. For BTC, cross-validates
    with Blockchain.info.

    Args:
        coin: Coin symbol or Binance pair (e.g. "BTC", "BTCUSDT", "ETH")
    """
    return _get_blockchain_stats(coin)


@tool
def get_address_activity(coin: str) -> str:
    """Fetch on-chain address and transaction activity for a cryptocurrency.

    Returns hodling addresses, transaction count, largest transactions,
    and network activity trends. For BTC, includes additional metrics
    from Blockchain.info.

    Args:
        coin: Coin symbol or Binance pair (e.g. "BTC", "BTCUSDT", "SOL")
    """
    return _get_address_activity(coin)


# ── DeFi / Tokenomics Tools ───────────────────────────────────────────────


@tool
def get_token_fundamentals(coin: str) -> str:
    """Fetch token fundamentals from CoinGecko for a cryptocurrency.

    Returns market cap, fully diluted valuation, circulating/total/max supply,
    price changes (24h/7d/30d), ATH, ATL, and FDV/MCap dilution ratio.

    Args:
        coin: Coin symbol or Binance pair (e.g. "BTC", "ETHUSDT", "SOL")
    """
    return _get_token_fundamentals(coin)


@tool
def get_defi_tvl(protocol: str) -> str:
    """Fetch DeFi Total Value Locked (TVL) for a protocol from DeFiLlama.

    If a coin symbol is provided, maps to the dominant DeFi protocol
    for that chain. Returns current TVL, chain breakdown, and 30-day trend.

    Args:
        protocol: Protocol name or coin symbol (e.g. "uniswap", "ETH", "aave")
    """
    return _get_defi_tvl(protocol)


@tool
def get_chain_tvl_overview() -> str:
    """Fetch total DeFi TVL across all blockchain chains from DeFiLlama.

    Returns a ranked list of chains by TVL with the total crypto DeFi TVL.
    Useful for understanding where capital is allocated across the ecosystem.
    """
    return _get_chain_tvl_overview()


# ── Funding Rate / Derivatives Tools ──────────────────────────────────────


@tool
def get_funding_rates(symbol: str) -> str:
    """Fetch funding rate history from Binance Futures for a cryptocurrency.

    Funding rates are settled every 8 hours. Positive rate means longs pay
    shorts (bullish crowding), negative means shorts pay longs.
    Returns rate history, averages, and annualized rate.

    Args:
        symbol: Coin symbol or Binance pair (e.g. "BTC", "BTCUSDT", "ETHUSDT")
    """
    return _get_funding_rates(symbol)


@tool
def get_open_interest(symbol: str) -> str:
    """Fetch open interest from Binance Futures for a cryptocurrency.

    Open interest = total outstanding derivative contracts.
    Rising OI + rising price = strong trend (new money entering).
    Rising OI + falling price = bearish pressure.
    Includes 24h OI history trend.

    Args:
        symbol: Coin symbol or Binance pair (e.g. "BTC", "BTCUSDT")
    """
    return _get_open_interest(symbol)


@tool
def get_long_short_ratio(symbol: str) -> str:
    """Fetch global long/short account ratio from Binance Futures.

    Shows proportion of accounts net long vs short.
    Ratio > 1 means more longs (crowded long).
    Ratio < 1 means more shorts (crowded short).
    Extreme values can signal contrarian opportunities.

    Args:
        symbol: Coin symbol or Binance pair (e.g. "BTC", "BTCUSDT")
    """
    return _get_long_short_ratio(symbol)
