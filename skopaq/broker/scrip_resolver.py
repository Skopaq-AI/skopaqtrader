"""Symbol → scrip-code resolver using INDstocks instruments CSV.

The INDstocks API requires ``scrip-codes=NSE_2885`` format for all market
data endpoints.  This module downloads the instruments master and caches
the mapping in memory for the session.

Usage::

    from skopaq.broker.scrip_resolver import resolve_scrip_code

    async with INDstocksClient(config, token_mgr) as client:
        scrip = await resolve_scrip_code(client, "RELIANCE")
        # → "NSE_2885"
"""

from __future__ import annotations

import csv
import io
import logging
import time
from decimal import Decimal
from typing import Optional

from skopaq.broker.client import INDstocksClient
from skopaq.broker.order_status import to_decimal

logger = logging.getLogger(__name__)

# In-memory cache: {"NSE:RELIANCE": "NSE_2885", ...}
_cache: dict[str, str] = {}
_cache_ts: float = 0.0
_CACHE_TTL = 3600  # 1 hour
# Tick sizes from the same CSV: {"NSE:RELIANCE": Decimal("0.10"), ...} (only values > 0)
_tick_cache: dict[str, Decimal] = {}


async def resolve_scrip_code(
    client: INDstocksClient,
    symbol: str,
    exchange: str = "NSE",
) -> str:
    """Resolve a human-readable symbol to a scrip-code.

    Downloads the instruments CSV on first call (or after TTL expires),
    parses it, and returns ``{exchange}_{security_id}`` format.

    Args:
        client: An open INDstocksClient instance.
        symbol: Trading symbol like ``RELIANCE``, ``TCS``, ``INFY``.
        exchange: Exchange code (default ``NSE``).

    Returns:
        Scrip code like ``NSE_2885``.

    Raises:
        ValueError: If symbol cannot be resolved.
    """
    global _cache, _cache_ts, _tick_cache

    key = f"{exchange}:{symbol}"

    # Check cache
    if _cache and (time.time() - _cache_ts) < _CACHE_TTL:
        if key in _cache:
            return _cache[key]

    # Download instruments CSV
    logger.info("Downloading instruments CSV for scrip-code resolution...")
    csv_text = await client.get_instruments(source="equity")

    # Parse CSV
    reader = csv.DictReader(io.StringIO(csv_text))
    new_cache: dict[str, str] = {}
    new_ticks: dict[str, Decimal] = {}
    for row in reader:
        exch = (row.get("EXCH") or "").strip()
        trading_symbol = (row.get("TRADING_SYMBOL") or "").strip()
        security_id = (row.get("SECURITY_ID") or "").strip()
        if exch and trading_symbol and security_id:
            cache_key = f"{exch}:{trading_symbol}"
            new_cache[cache_key] = f"{exch}_{security_id}"
            tick = to_decimal(row.get("TICK_SIZE"))
            if tick is not None and tick > 0:
                new_ticks[cache_key] = tick

    _cache = new_cache
    _tick_cache = new_ticks
    _cache_ts = time.time()
    logger.info("Instruments cache loaded: %d symbols", len(_cache))

    if key not in _cache:
        raise ValueError(
            f"Symbol '{symbol}' not found on {exchange}. "
            f"Check symbol spelling (e.g., RELIANCE, TCS, INFY)."
        )

    return _cache[key]


async def resolve_security_id(
    client: INDstocksClient,
    symbol: str,
    exchange: str = "NSE",
) -> str:
    """Resolve a symbol to its raw security ID (e.g. ``"2885"``).

    Unlike :func:`resolve_scrip_code` which returns ``"NSE_2885"``,
    this returns just the numeric part needed for the order API's
    ``security_id`` field.
    """
    scrip = await resolve_scrip_code(client, symbol, exchange)
    # scrip format: "NSE_2885" → extract "2885"
    return scrip.split("_", 1)[1] if "_" in scrip else scrip


def cached_tick_size(symbol: str, exchange: str = "NSE") -> Optional[Decimal]:
    """The instrument's tick size from the last instruments CSV loaded, however old (tick
    sizes do not change intraday); None when none is cached. Never downloads: a protective
    exit being re-priced must not wait for the CSV."""
    return _tick_cache.get(f"{exchange}:{symbol}")


async def resolve_tick_size(
    client: INDstocksClient,
    symbol: str,
    exchange: str = "NSE",
) -> Optional[Decimal]:
    """The instrument's tick size from the instruments CSV ``TICK_SIZE`` column.

    Loads the CSV (through :func:`resolve_scrip_code`) when the cache is empty or
    stale. Returns None when the symbol or its tick is missing or unparseable, or on
    any error (logged) — callers then use a coarse tick that is valid for every
    price band. Never raises.
    """
    key = f"{exchange}:{symbol}"
    try:
        await resolve_scrip_code(client, symbol, exchange)   # cached; reloads the CSV if stale
    except ValueError:
        return None                                  # symbol not in the CSV
    except Exception as exc:
        logger.warning("Tick size for %s unavailable (%s)", key, exc)
        return None
    return _tick_cache.get(key)
