"""ATR (Average True Range) data fetcher.

Wraps the upstream ``route_to_vendor("get_indicators", ...)`` call to extract
a numeric ATR value for position sizing.  Falls back to a percentage-based
estimate (2% of price) if the vendor call fails.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

# Default ATR as fraction of price when vendor data is unavailable
_FALLBACK_ATR_PCT = 0.02


def fetch_atr(
    symbol: str,
    trade_date: str,
    atr_period: int = 14,
    look_back_days: int = 30,
) -> Optional[float]:
    """Fetch the latest ATR value for a symbol from upstream vendors.

    Calls ``route_to_vendor("get_indicators", symbol, "atr", ...)`` which
    routes to Alpha Vantage (primary) or yfinance (fallback).

    Args:
        symbol: Stock symbol (e.g., "RELIANCE").
        trade_date: Current trading date (YYYY-MM-DD).
        atr_period: ATR lookback period (default 14).
        look_back_days: Data window for the indicator call.

    Returns:
        Latest ATR value as a float, or None if unavailable.
    """
    try:
        from tradingagents.dataflows.router import route_to_vendor

        result = route_to_vendor(
            "get_indicators",
            symbol,
            "atr",
            trade_date,
            look_back_days,
        )

        if not result or not isinstance(result, str):
            logger.warning("ATR fetch returned empty result for %s", symbol)
            return None

        return _parse_atr_value(result)

    except Exception:
        logger.warning(
            "ATR fetch failed for %s — will use fallback estimate",
            symbol,
            exc_info=True,
        )
        return None


def estimate_atr(price: float, pct: float = _FALLBACK_ATR_PCT) -> float:
    """Estimate ATR as a percentage of the current price.

    Used as a fallback when vendor data is unavailable.  The default 2%
    approximation is conservative for Indian large-caps (typical ATR is
    1.5–3% for NIFTY 50 stocks).

    Args:
        price: Current stock price.
        pct: Fraction of price to use (default 0.02 = 2%).

    Returns:
        Estimated ATR value.
    """
    return abs(price * pct)


def _parse_atr_value(raw: str) -> Optional[float]:
    """Extract the most recent ATR numeric value from vendor response.

    The upstream vendors return a text report with lines like:
        "2026-03-01: ATR = 45.23"
    or CSV-style:
        "date,ATR\\n2026-03-01,45.23\\n..."

    We scan for the last numeric value associated with "ATR" or "atr".
    """
    # Strategy 1: Look for "ATR" followed by a number
    # Matches patterns like "ATR = 45.23", "ATR: 45.23", "atr,45.23"
    matches = re.findall(r"(?:ATR|atr)\s*[=:,]\s*([\d.]+)", raw)
    if matches:
        try:
            return float(matches[-1])  # Most recent value (last in the list)
        except ValueError:
            pass

    # Strategy 2: Look for any decimal number in the last few lines
    # (vendor response may have description text followed by data)
    lines = raw.strip().splitlines()
    for line in reversed(lines[-10:]):
        nums = re.findall(r"\b(\d+\.?\d*)\b", line)
        if nums:
            try:
                val = float(nums[-1])
                if 0.1 < val < 10000:  # Reasonable ATR range
                    return val
            except ValueError:
                continue

    logger.warning("Could not parse ATR value from vendor response")
    return None
