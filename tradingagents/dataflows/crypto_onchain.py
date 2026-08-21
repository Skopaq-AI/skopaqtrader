"""On-chain data fetching — Blockchair (multi-chain) + Blockchain.info (BTC).

All endpoints are **public** and require no API key.
- Blockchair: ~60 requests/minute free tier
- Blockchain.info: ~5 requests/second
"""

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# ── Chain name mapping (Blockchair uses lowercase full names) ───────────────
CHAIN_MAP: dict[str, str] = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "SOL": "solana",
    "BNB": "bnb",
    "XRP": "ripple",
    "ADA": "cardano",
    "DOGE": "dogecoin",
    "DOT": "polkadot",
    "AVAX": "avalanche",
    "MATIC": "polygon",
    "LTC": "litecoin",
    "LINK": "chainlink",  # Blockchair doesn't support LINK — handled gracefully
    "ATOM": "cosmos",
    "UNI": "uniswap",  # ERC-20 token — Blockchair doesn't have separate chain
    "NEAR": "near",
}

BLOCKCHAIR_BASE = "https://api.blockchair.com"
BLOCKCHAIN_INFO_BASE = "https://blockchain.info"

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def _strip_coin(symbol: str) -> str:
    """Extract base coin from a trading pair (e.g. BTCUSDT → BTC, BTC-USD → BTC)."""
    symbol = symbol.upper()
    # Handle yfinance-style dash pairs first (e.g. BTC-USD)
    if "-" in symbol:
        symbol = symbol.split("-")[0]
        return symbol
    for suffix in ("USDT", "BUSD", "USDC", "USD"):
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            return symbol[: -len(suffix)]
    return symbol


def get_blockchain_stats(coin: str) -> str:
    """Fetch blockchain network statistics for *coin*.

    Sources:
      - Blockchair ``/{chain}/stats`` — hashrate, difficulty, mempool, fees
      - Blockchain.info (BTC-only cross-validation)

    Returns a human-readable summary string.
    """
    base = _strip_coin(coin)
    chain = CHAIN_MAP.get(base)

    if not chain:
        return (
            f"[On-Chain] No blockchain data available for {base}. "
            f"Supported chains: {', '.join(sorted(CHAIN_MAP.keys()))}"
        )

    try:
        url = f"{BLOCKCHAIR_BASE}/{chain}/stats"
        resp = httpx.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("data", {})
    except httpx.HTTPStatusError as exc:
        logger.warning("Blockchair HTTP error for %s: %s", chain, exc)
        return f"[On-Chain] Blockchair returned HTTP {exc.response.status_code} for {chain}."
    except Exception as exc:
        logger.warning("Blockchair request failed for %s: %s", chain, exc)
        return f"[On-Chain] Failed to fetch blockchain stats for {base}: {exc}"

    # Extract key metrics (field availability varies by chain)
    txns_24h = data.get("transactions_24h", data.get("transactions", "N/A"))
    blocks_24h = data.get("blocks_24h", data.get("blocks", "N/A"))
    hashrate = data.get("hashrate_24h", data.get("hashrate", "N/A"))
    difficulty = data.get("difficulty", "N/A")
    mempool = data.get("mempool_transactions", data.get("mempool_count", "N/A"))
    avg_fee = data.get("average_transaction_fee_24h", data.get("average_fee_24h", "N/A"))
    market_price = data.get("market_price_usd", "N/A")
    market_cap = data.get("market_cap_usd", "N/A")
    dominance = data.get("market_dominance_percentage", "N/A")

    lines = [
        f"=== On-Chain Stats: {base} ({chain}) ===",
        f"Market Price (USD): ${market_price:,.2f}" if isinstance(market_price, (int, float)) else f"Market Price (USD): {market_price}",
        f"Market Cap (USD):   ${market_cap:,.0f}" if isinstance(market_cap, (int, float)) else f"Market Cap (USD):   {market_cap}",
        f"Dominance:          {dominance}%" if dominance != "N/A" else "",
        f"Transactions (24h): {txns_24h:,}" if isinstance(txns_24h, (int, float)) else f"Transactions (24h): {txns_24h}",
        f"Blocks (24h):       {blocks_24h}" if blocks_24h != "N/A" else "",
        f"Hashrate (24h):     {hashrate}" if hashrate != "N/A" else "",
        f"Difficulty:         {difficulty}" if difficulty != "N/A" else "",
        f"Mempool Txns:       {mempool}" if mempool != "N/A" else "",
        f"Avg Fee (24h):      {avg_fee}" if avg_fee != "N/A" else "",
    ]

    # BTC-only: cross-validate with blockchain.info
    if base == "BTC":
        btc_extra = _fetch_btc_blockchain_info()
        if btc_extra:
            lines.append("")
            lines.append("--- Blockchain.info Cross-Validation (BTC) ---")
            lines.extend(btc_extra)

    return "\n".join(line for line in lines if line)


def get_address_activity(coin: str, days: int = 7) -> str:
    """Fetch address/transaction activity trends for *coin*.

    Uses Blockchair ``/{chain}/stats`` for the current snapshot.
    For BTC, supplements with Blockchain.info endpoints for richer data.

    Returns a human-readable summary string.
    """
    base = _strip_coin(coin)
    chain = CHAIN_MAP.get(base)

    if not chain:
        return (
            f"[On-Chain] No address activity data for {base}. "
            f"Supported: {', '.join(sorted(CHAIN_MAP.keys()))}"
        )

    try:
        url = f"{BLOCKCHAIR_BASE}/{chain}/stats"
        resp = httpx.get(url, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("data", {})
    except Exception as exc:
        logger.warning("Blockchair address activity fetch failed for %s: %s", chain, exc)
        return f"[On-Chain] Failed to fetch address activity for {base}: {exc}"

    # Current snapshot metrics
    hodling_addresses = data.get("hodling_addresses", "N/A")
    txns_24h = data.get("transactions_24h", data.get("transactions", "N/A"))
    outputs_24h = data.get("outputs_24h", "N/A")
    largest_tx_24h = data.get("largest_transaction_24h", {})
    largest_tx_value = largest_tx_24h.get("value_usd", "N/A") if isinstance(largest_tx_24h, dict) else "N/A"

    lines = [
        f"=== Address Activity: {base} ({chain}) ===",
        f"Hodling Addresses:      {hodling_addresses:,}" if isinstance(hodling_addresses, (int, float)) else f"Hodling Addresses: {hodling_addresses}",
        f"Transactions (24h):     {txns_24h:,}" if isinstance(txns_24h, (int, float)) else f"Transactions (24h): {txns_24h}",
        f"Outputs (24h):          {outputs_24h:,}" if isinstance(outputs_24h, (int, float)) else f"Outputs (24h): {outputs_24h}",
        f"Largest Tx (24h, USD):  ${largest_tx_value:,.0f}" if isinstance(largest_tx_value, (int, float)) else f"Largest Tx (24h, USD): {largest_tx_value}",
    ]

    # BTC: enrich with blockchain.info data
    if base == "BTC":
        btc_activity = _fetch_btc_activity()
        if btc_activity:
            lines.append("")
            lines.append("--- BTC Network Activity (Blockchain.info) ---")
            lines.extend(btc_activity)

    return "\n".join(line for line in lines if line)


# ── BTC-specific helpers (Blockchain.info) ────────────────────────────────


def _fetch_btc_blockchain_info() -> Optional[list[str]]:
    """Fetch supplementary BTC stats from blockchain.info/q/ endpoints."""
    try:
        endpoints = {
            "Unconfirmed Txns": f"{BLOCKCHAIN_INFO_BASE}/q/unconfirmedcount",
            "Hash Rate (GH/s)": f"{BLOCKCHAIN_INFO_BASE}/q/hashrate",
            "Block Count": f"{BLOCKCHAIN_INFO_BASE}/q/getblockcount",
        }
        lines = []
        for label, url in endpoints.items():
            resp = httpx.get(url, timeout=_TIMEOUT)
            if resp.status_code == 200:
                lines.append(f"{label}: {resp.text.strip()}")
        return lines if lines else None
    except Exception as exc:
        logger.debug("Blockchain.info BTC stats failed: %s", exc)
        return None


def _fetch_btc_activity() -> Optional[list[str]]:
    """Fetch BTC-specific activity metrics from blockchain.info."""
    try:
        endpoints = {
            "24h Tx Count": f"{BLOCKCHAIN_INFO_BASE}/q/24hrtransactioncount",
            "Avg Block Size (bytes)": f"{BLOCKCHAIN_INFO_BASE}/q/avgtxsize",
            "Total BTC Sent (24h, BTC)": f"{BLOCKCHAIN_INFO_BASE}/q/24hrbtcsent",
        }
        lines = []
        for label, url in endpoints.items():
            resp = httpx.get(url, timeout=_TIMEOUT)
            if resp.status_code == 200:
                value = resp.text.strip()
                # 24hrbtcsent returns satoshis — convert to BTC
                if "btcsent" in label.lower() and value.isdigit():
                    value = f"{int(value) / 1e8:,.2f}"
                lines.append(f"{label}: {value}")
        return lines if lines else None
    except Exception as exc:
        logger.debug("Blockchain.info BTC activity failed: %s", exc)
        return None
