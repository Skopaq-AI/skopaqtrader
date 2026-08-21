"""INDstocks data vendor — Indian market data for the TradingAgents pipeline.

Bridges the async ``INDstocksClient`` to the synchronous interface expected
by upstream agent tools.  Output is formatted as CSV strings matching the
yfinance output format so agents can parse it without changes.

Falls back gracefully if the INDstocks token is missing or expired.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Annotated

logger = logging.getLogger(__name__)

# Cache instrument mappings (symbol → security_id) to avoid repeated CSV downloads
_instrument_cache: dict[str, str] = {}
_instrument_cache_ts: float = 0.0
_CACHE_TTL = 3600  # 1 hour


def _run_async(coro):
    """Run an async coroutine from synchronous code.

    Handles both cases:
    - No event loop running → use ``asyncio.run()``
    - Event loop already running (e.g., inside Jupyter/FastAPI) → use thread
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        return asyncio.run(coro)
    else:
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(asyncio.run, coro)
            return future.result(timeout=30)


def _get_client():
    """Create a one-shot INDstocks client for a single call."""
    from skopaq.config import SkopaqConfig
    from skopaq.broker.client import INDstocksClient
    from skopaq.broker.token_manager import TokenManager

    config = SkopaqConfig()
    token_mgr = TokenManager()
    return INDstocksClient(config, token_mgr)


def _normalize_symbol(symbol: str) -> str:
    """Strip exchange suffixes that LLM agents may attach (e.g., .NS, .BO).

    Upstream agents were trained on Yahoo Finance conventions where Indian
    stocks use ``RELIANCE.NS``.  INDstocks expects bare ``RELIANCE``.
    """
    for suffix in (".NS", ".BO"):
        if symbol.upper().endswith(suffix):
            return symbol[: -len(suffix)]
    return symbol


async def _resolve_scrip_code(symbol: str, exchange: str = "NSE") -> str:
    """Resolve a human-readable symbol to a scrip-code for historical API.

    The historical endpoint uses ``scrip-codes=NSE_3045`` format where 3045
    is the SECURITY_ID from the instruments CSV.

    Returns format: ``NSE_3045``
    """
    import time

    symbol = _normalize_symbol(symbol)

    global _instrument_cache, _instrument_cache_ts

    # Check cache freshness
    now = time.time()
    if _instrument_cache and (now - _instrument_cache_ts) < _CACHE_TTL:
        key = f"{exchange}:{symbol}"
        if key in _instrument_cache:
            return _instrument_cache[key]

    # Download instruments CSV
    client = _get_client()
    async with client:
        csv_text = await client.get_instruments(source="equity")

    # Parse CSV: columns include SECURITY_ID, TRADING_SYMBOL, EXCH
    reader = csv.DictReader(io.StringIO(csv_text))
    new_cache: dict[str, str] = {}
    for row in reader:
        exch = row.get("EXCH", "").strip()
        trading_symbol = row.get("TRADING_SYMBOL", "").strip()
        security_id = row.get("SECURITY_ID", "").strip()
        if exch and trading_symbol and security_id:
            new_cache[f"{exch}:{trading_symbol}"] = f"{exch}_{security_id}"

    _instrument_cache = new_cache
    _instrument_cache_ts = now

    key = f"{exchange}:{symbol}"
    if key in _instrument_cache:
        return _instrument_cache[key]

    raise ValueError(f"Symbol '{symbol}' not found in {exchange} instruments")


def _date_to_epoch_ms(date_str: str) -> int:
    """Convert ``YYYY-MM-DD`` to Unix epoch **milliseconds** (IST = UTC+5:30).

    INDstocks historical API expects ``start_time``/``end_time`` in
    epoch milliseconds.  Response candle ``ts`` values are in seconds.
    """
    IST = timezone(timedelta(hours=5, minutes=30))
    dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=IST)
    return int(dt.timestamp() * 1000)


async def _fetch_historical(symbol: str, start_date: str, end_date: str) -> str:
    """Async helper — fetch OHLCV and format as CSV."""

    # Step 1: Resolve symbol to scrip-code
    scrip_code = await _resolve_scrip_code(symbol, "NSE")

    # Step 2: Convert dates to epoch milliseconds (API requirement)
    start_ms = _date_to_epoch_ms(start_date)
    end_ms = _date_to_epoch_ms(end_date)

    # Step 3: Fetch candles
    client = _get_client()
    async with client:
        candles = await client.get_historical(
            scrip_code=scrip_code,
            interval="1day",
            start_time=start_ms,
            end_time=end_ms,
        )

    if not candles:
        return f"No data found for symbol '{symbol}' between {start_date} and {end_date}"

    # Format as CSV matching yfinance output
    header = f"# Stock data for {symbol.upper()} from {start_date} to {end_date}\n"
    header += f"# Total records: {len(candles)}\n"
    header += f"# Data source: INDstocks\n"
    header += f"# Data retrieved on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    csv_lines = ["Date,Open,High,Low,Close,Volume"]
    for c in candles:
        date_str = c.timestamp.strftime("%Y-%m-%d") if hasattr(c.timestamp, "strftime") else str(c.timestamp)
        csv_lines.append(
            f"{date_str},{c.open:.2f},{c.high:.2f},{c.low:.2f},{c.close:.2f},{c.volume}"
        )

    return header + "\n".join(csv_lines) + "\n"


async def _fetch_quote(symbol: str) -> str:
    """Async helper — fetch real-time quote and format as CSV.

    Resolves the human-readable symbol to a scrip-code, then fetches
    the full quote using ``scrip-codes`` parameter.
    """
    # Resolve symbol to scrip-code (e.g. RELIANCE → NSE_2885)
    scrip_code = await _resolve_scrip_code(symbol, "NSE")

    client = _get_client()
    async with client:
        quote = await client.get_quote(scrip_code=scrip_code, symbol=symbol)

    header = f"# Quote for {symbol.upper()}\n"
    header += f"# Data source: INDstocks\n"
    header += f"# Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"

    csv_out = "Symbol,LTP,Open,High,Low,Close,Volume,Change,ChangePct\n"
    csv_out += (
        f"{symbol},{quote.ltp:.2f},{quote.open:.2f},{quote.high:.2f},"
        f"{quote.low:.2f},{quote.close:.2f},{quote.volume},"
        f"{quote.change:.2f},{quote.change_pct:.2f}\n"
    )

    return header + csv_out


# ── Public vendor functions (match upstream signature) ────────────────


def get_stock_data_indstocks(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[str, "Start date in yyyy-mm-dd format"],
    end_date: Annotated[str, "End date in yyyy-mm-dd format"],
) -> str:
    """Fetch OHLCV data from INDstocks.

    Drop-in replacement for ``get_YFin_data_online`` — same signature,
    same CSV output format.
    """
    try:
        return _run_async(_fetch_historical(symbol, start_date, end_date))
    except Exception as exc:
        logger.warning("INDstocks historical fetch failed for %s: %s", symbol, exc)
        raise


def get_quote_indstocks(
    symbol: Annotated[str, "ticker symbol of the company"],
) -> str:
    """Fetch real-time quote from INDstocks."""
    try:
        return _run_async(_fetch_quote(symbol))
    except Exception as exc:
        logger.warning("INDstocks quote fetch failed for %s: %s", symbol, exc)
        raise
