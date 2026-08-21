"""DeFi / tokenomics data fetching — DeFiLlama + CoinGecko.

All endpoints are **public** and require no API key.
- DeFiLlama: Completely free, no auth, no rate limit published
- CoinGecko: Free tier ~10-50 requests/minute
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── CoinGecko ID mapping ──────────────────────────────────────────────────
COINGECKO_ID_MAP: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "DOT": "polkadot-new",
    "AVAX": "avalanche-2",
    "MATIC": "matic-network",
    "LTC": "litecoin",
    "LINK": "chainlink",
    "ATOM": "cosmos",
    "UNI": "uniswap",
    "NEAR": "near",
    "TRX": "tron",
    "SHIB": "shiba-inu",
    "ARB": "arbitrum",
    "OP": "optimism",
    "APT": "aptos",
}

# DeFiLlama protocol slugs (approximate — DeFiLlama uses lowercase names)
DEFILLAMA_PROTOCOL_MAP: dict[str, str] = {
    "ETH": "lido",  # largest ETH DeFi protocol
    "SOL": "marinade-finance",
    "BNB": "venus",
    "AVAX": "aave",
    "MATIC": "aave",
    "UNI": "uniswap",
    "LINK": "aave",  # Chainlink itself doesn't have TVL — use Aave (major consumer)
    "ATOM": "stride",
    "ARB": "aave",
    "OP": "aave",
}

COINGECKO_BASE = "https://api.coingecko.com/api/v3"
DEFILLAMA_BASE = "https://api.llama.fi"

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def _strip_coin(symbol: str) -> str:
    """Extract base coin from a Binance-style pair (e.g. BTCUSDT → BTC)."""
    symbol = symbol.upper()
    for suffix in ("USDT", "BUSD", "USDC", "USD"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return symbol[: -len(suffix)]
    return symbol


def get_token_fundamentals(coin: str) -> str:
    """Fetch token fundamentals from CoinGecko for *coin*.

    Returns: market_cap, FDV, circulating/total/max supply, price changes,
    ATH, ATL, and market cap rank.
    """
    base = _strip_coin(coin)
    cg_id = COINGECKO_ID_MAP.get(base)

    if not cg_id:
        return (
            f"[DeFi] No CoinGecko mapping for {base}. "
            f"Supported: {', '.join(sorted(COINGECKO_ID_MAP.keys()))}"
        )

    try:
        url = f"{COINGECKO_BASE}/coins/{cg_id}"
        params = {
            "localization": "false",
            "tickers": "false",
            "community_data": "false",
            "developer_data": "false",
            "sparkline": "false",
        }
        resp = httpx.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("CoinGecko HTTP error for %s: %s", cg_id, exc)
        return f"[DeFi] CoinGecko returned HTTP {exc.response.status_code} for {base}."
    except Exception as exc:
        logger.warning("CoinGecko request failed for %s: %s", cg_id, exc)
        return f"[DeFi] Failed to fetch token fundamentals for {base}: {exc}"

    md = data.get("market_data", {})

    def _usd(field: str, fallback="N/A"):
        val = md.get(field)
        if isinstance(val, dict):
            return val.get("usd", fallback)
        return val if val is not None else fallback

    market_cap = _usd("market_cap")
    fdv = _usd("fully_diluted_valuation")
    current_price = _usd("current_price")
    ath = _usd("ath")
    atl = _usd("atl")
    pct_24h = _usd("price_change_percentage_24h_in_currency")
    pct_7d = _usd("price_change_percentage_7d_in_currency")
    pct_30d = _usd("price_change_percentage_30d_in_currency")

    circ_supply = md.get("circulating_supply", "N/A")
    total_supply = md.get("total_supply", "N/A")
    max_supply = md.get("max_supply", "N/A")
    rank = data.get("market_cap_rank", "N/A")

    def _fmt_num(v, prefix="$", decimals=0):
        if isinstance(v, (int, float)):
            if decimals == 0:
                return f"{prefix}{v:,.0f}"
            return f"{prefix}{v:,.{decimals}f}"
        return str(v)

    def _fmt_pct(v):
        if isinstance(v, (int, float)):
            return f"{v:+.2f}%"
        return str(v)

    lines = [
        f"=== Token Fundamentals: {base} (CoinGecko: {cg_id}) ===",
        f"Market Cap Rank: #{rank}",
        f"Current Price:     {_fmt_num(current_price, decimals=2)}",
        f"Market Cap:        {_fmt_num(market_cap)}",
        f"Fully Diluted Val: {_fmt_num(fdv)}",
        "",
        f"Circulating Supply: {_fmt_num(circ_supply, prefix='')}" if circ_supply != "N/A" else "",
        f"Total Supply:       {_fmt_num(total_supply, prefix='')}" if total_supply != "N/A" else "",
        f"Max Supply:         {_fmt_num(max_supply, prefix='')}" if max_supply != "N/A" else "",
        "",
        f"Price Change 24h: {_fmt_pct(pct_24h)}",
        f"Price Change 7d:  {_fmt_pct(pct_7d)}",
        f"Price Change 30d: {_fmt_pct(pct_30d)}",
        "",
        f"All-Time High: {_fmt_num(ath, decimals=2)}",
        f"All-Time Low:  {_fmt_num(atl, decimals=2)}",
    ]

    # FDV / Market Cap ratio (supply inflation risk indicator)
    if isinstance(fdv, (int, float)) and isinstance(market_cap, (int, float)) and market_cap > 0:
        ratio = fdv / market_cap
        lines.append(f"FDV/MCap Ratio: {ratio:.2f}x {'(high dilution risk)' if ratio > 2 else '(low dilution risk)' if ratio < 1.2 else '(moderate)'}")

    return "\n".join(line for line in lines if line)


def get_defi_tvl(protocol: str) -> str:
    """Fetch DeFi TVL for a specific *protocol* from DeFiLlama.

    If *protocol* looks like a coin symbol (e.g. "ETH"), maps to the
    dominant DeFi protocol for that chain.

    Returns current TVL, chain breakdown, and recent trend.
    """
    base = _strip_coin(protocol)

    # Map coin to a representative protocol if possible
    slug = DEFILLAMA_PROTOCOL_MAP.get(base, base.lower())

    try:
        url = f"{DEFILLAMA_BASE}/protocol/{slug}"
        resp = httpx.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("DeFiLlama HTTP error for %s: %s", slug, exc)
        return f"[DeFi] DeFiLlama returned HTTP {exc.response.status_code} for {slug}."
    except Exception as exc:
        logger.warning("DeFiLlama request failed for %s: %s", slug, exc)
        return f"[DeFi] Failed to fetch TVL for {slug}: {exc}"

    name = data.get("name", slug)
    current_tvl = data.get("currentChainTvls", {})
    total_tvl = sum(v for v in current_tvl.values() if isinstance(v, (int, float)))

    # Chain breakdown (top 5)
    chain_sorted = sorted(
        [(k, v) for k, v in current_tvl.items() if isinstance(v, (int, float))],
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    # Recent TVL history (last 30 data points)
    tvl_history = data.get("tvl", [])
    recent = tvl_history[-30:] if tvl_history else []

    lines = [
        f"=== DeFi TVL: {name} (slug: {slug}) ===",
        f"Total TVL: ${total_tvl:,.0f}" if total_tvl else "Total TVL: N/A",
        "",
        "Chain Breakdown (Top 5):",
    ]
    for chain_name, tvl_val in chain_sorted:
        pct = (tvl_val / total_tvl * 100) if total_tvl > 0 else 0
        lines.append(f"  {chain_name}: ${tvl_val:,.0f} ({pct:.1f}%)")

    # TVL trend
    if len(recent) >= 2:
        first_tvl = recent[0].get("totalLiquidityUSD", 0) if isinstance(recent[0], dict) else 0
        last_tvl = recent[-1].get("totalLiquidityUSD", 0) if isinstance(recent[-1], dict) else 0
        if first_tvl > 0:
            change_pct = (last_tvl - first_tvl) / first_tvl * 100
            lines.append("")
            lines.append(f"30-Day TVL Trend: {change_pct:+.1f}% (${first_tvl:,.0f} → ${last_tvl:,.0f})")

    return "\n".join(lines)


def get_chain_tvl_overview() -> str:
    """Fetch total TVL across all chains from DeFiLlama.

    Returns per-chain TVL ranking and total crypto TVL.
    """
    try:
        url = f"{DEFILLAMA_BASE}/v2/chains"
        resp = httpx.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        chains = resp.json()
    except Exception as exc:
        logger.warning("DeFiLlama chain overview failed: %s", exc)
        return f"[DeFi] Failed to fetch chain TVL overview: {exc}"

    if not isinstance(chains, list):
        return "[DeFi] Unexpected response format from DeFiLlama /v2/chains."

    # Sort by TVL descending, take top 15
    valid = [c for c in chains if isinstance(c.get("tvl"), (int, float)) and c["tvl"] > 0]
    valid.sort(key=lambda c: c["tvl"], reverse=True)
    top = valid[:15]

    total_tvl = sum(c["tvl"] for c in valid)

    lines = [
        "=== DeFi TVL by Chain (DeFiLlama) ===",
        f"Total Crypto DeFi TVL: ${total_tvl:,.0f}",
        "",
        "Top 15 Chains:",
    ]
    for i, c in enumerate(top, 1):
        name = c.get("name", "?")
        tvl = c["tvl"]
        pct = tvl / total_tvl * 100 if total_tvl > 0 else 0
        lines.append(f"  {i:2d}. {name:20s} ${tvl:>15,.0f}  ({pct:.1f}%)")

    return "\n".join(lines)
