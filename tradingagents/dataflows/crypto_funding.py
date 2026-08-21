"""Funding rate / derivatives data — Binance Futures public API.

All endpoints are **public** and require no API key.
- Base URL: https://fapi.binance.com
- Rate limit: 1200 weight/minute
"""

import logging
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

FUTURES_BASE = "https://fapi.binance.com"

_TIMEOUT = httpx.Timeout(15.0, connect=10.0)


def _normalize_symbol(symbol: str) -> str:
    """Ensure symbol is in BTCUSDT format (Binance Futures convention)."""
    symbol = symbol.upper()
    # If already has a quote suffix, return as-is
    for suffix in ("USDT", "BUSD", "USDC"):
        if symbol.endswith(suffix):
            return symbol
    # Bare coin → append USDT
    return f"{symbol}USDT"


def get_funding_rates(symbol: str, limit: int = 100) -> str:
    """Fetch funding rate history for *symbol* from Binance Futures.

    Funding rates are settled every 8 hours (00:00, 08:00, 16:00 UTC).
    - Positive rate → longs pay shorts (bullish crowding)
    - Negative rate → shorts pay longs (bearish crowding)

    Returns the most recent *limit* data points with trend analysis.
    """
    sym = _normalize_symbol(symbol)

    try:
        url = f"{FUTURES_BASE}/fapi/v1/fundingRate"
        resp = httpx.get(url, params={"symbol": sym, "limit": limit}, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Binance Futures funding rate HTTP error for %s: %s", sym, exc)
        return f"[Funding] Binance Futures returned HTTP {exc.response.status_code} for {sym}."
    except Exception as exc:
        logger.warning("Binance Futures funding rate failed for %s: %s", sym, exc)
        return f"[Funding] Failed to fetch funding rates for {sym}: {exc}"

    if not data:
        return f"[Funding] No funding rate data returned for {sym}."

    # Parse rates
    rates = []
    for entry in data:
        rate = float(entry.get("fundingRate", 0))
        ts = int(entry.get("fundingTime", 0))
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else None
        rates.append((dt, rate))

    # Summary statistics
    all_rates = [r for _, r in rates]
    avg_rate = sum(all_rates) / len(all_rates) if all_rates else 0
    max_rate = max(all_rates) if all_rates else 0
    min_rate = min(all_rates) if all_rates else 0
    positive_count = sum(1 for r in all_rates if r > 0)
    negative_count = sum(1 for r in all_rates if r < 0)

    # Recent trend (last 10 data points = ~3.3 days)
    recent = all_rates[-10:] if len(all_rates) >= 10 else all_rates
    recent_avg = sum(recent) / len(recent) if recent else 0

    # Annualized rate (funding rate × 3 settlements/day × 365)
    annual_rate = avg_rate * 3 * 365 * 100  # as percentage

    latest_dt, latest_rate = rates[-1] if rates else (None, 0)
    first_dt = rates[0][0] if rates else None

    lines = [
        f"=== Funding Rate Analysis: {sym} ===",
        f"Data Points: {len(rates)} (8h intervals)",
        f"Period: {first_dt:%Y-%m-%d %H:%M} → {latest_dt:%Y-%m-%d %H:%M} UTC" if first_dt and latest_dt else "",
        "",
        f"Latest Rate:    {latest_rate:+.6f} ({'longs pay' if latest_rate > 0 else 'shorts pay'})",
        f"Average Rate:   {avg_rate:+.6f}",
        f"Max Rate:       {max_rate:+.6f}",
        f"Min Rate:       {min_rate:+.6f}",
        f"Annualized:     {annual_rate:+.2f}%",
        "",
        f"Positive (longs pay): {positive_count}/{len(all_rates)} ({positive_count/len(all_rates)*100:.0f}%)" if all_rates else "",
        f"Negative (shorts pay): {negative_count}/{len(all_rates)} ({negative_count/len(all_rates)*100:.0f}%)" if all_rates else "",
        "",
        f"Recent Trend (last 10): avg {recent_avg:+.6f} {'(bullish crowding)' if recent_avg > 0.0001 else '(bearish crowding)' if recent_avg < -0.0001 else '(neutral)'}",
    ]

    # Last 5 data points for detail
    lines.append("")
    lines.append("Recent History:")
    for dt, rate in rates[-5:]:
        ts_str = f"{dt:%Y-%m-%d %H:%M}" if dt else "?"
        lines.append(f"  {ts_str} UTC: {rate:+.6f}")

    return "\n".join(line for line in lines if line)


def get_open_interest(symbol: str) -> str:
    """Fetch open interest for *symbol* from Binance Futures.

    Open interest = total number of outstanding derivative contracts.
    Rising OI + rising price = strong trend (new money entering).
    Rising OI + falling price = bearish pressure.
    Falling OI = closing positions / reduced conviction.
    """
    sym = _normalize_symbol(symbol)

    try:
        url = f"{FUTURES_BASE}/fapi/v1/openInterest"
        resp = httpx.get(url, params={"symbol": sym}, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Binance Futures OI HTTP error for %s: %s", sym, exc)
        return f"[Funding] Binance Futures returned HTTP {exc.response.status_code} for {sym}."
    except Exception as exc:
        logger.warning("Binance Futures OI failed for %s: %s", sym, exc)
        return f"[Funding] Failed to fetch open interest for {sym}: {exc}"

    oi = float(data.get("openInterest", 0))
    ts = int(data.get("time", 0))
    dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else None

    # Also fetch OI history for trend (open interest statistics)
    oi_hist_lines = _fetch_oi_history(sym)

    lines = [
        f"=== Open Interest: {sym} ===",
        f"Current OI (contracts): {oi:,.4f}",
        f"Timestamp: {dt:%Y-%m-%d %H:%M:%S} UTC" if dt else "",
    ]

    if oi_hist_lines:
        lines.append("")
        lines.extend(oi_hist_lines)

    return "\n".join(line for line in lines if line)


def get_long_short_ratio(symbol: str, period: str = "1h", limit: int = 30) -> str:
    """Fetch global long/short account ratio from Binance Futures.

    Shows what proportion of accounts are net long vs short.
    - Ratio > 1: more longs than shorts (crowded long)
    - Ratio < 1: more shorts than longs (crowded short)
    - Extreme values can signal contrarian opportunities.

    Periods: 5m, 15m, 30m, 1h, 2h, 4h, 6h, 12h, 1d
    """
    sym = _normalize_symbol(symbol)

    try:
        url = f"{FUTURES_BASE}/futures/data/globalLongShortAccountRatio"
        params = {"symbol": sym, "period": period, "limit": limit}
        resp = httpx.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.warning("Binance Futures L/S ratio HTTP error for %s: %s", sym, exc)
        return f"[Funding] Binance Futures returned HTTP {exc.response.status_code} for {sym}."
    except Exception as exc:
        logger.warning("Binance Futures L/S ratio failed for %s: %s", sym, exc)
        return f"[Funding] Failed to fetch long/short ratio for {sym}: {exc}"

    if not data:
        return f"[Funding] No long/short ratio data for {sym}."

    # Parse entries
    entries = []
    for entry in data:
        ratio = float(entry.get("longShortRatio", 1))
        long_pct = float(entry.get("longAccount", 0.5))
        short_pct = float(entry.get("shortAccount", 0.5))
        ts = int(entry.get("timestamp", 0))
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else None
        entries.append((dt, ratio, long_pct, short_pct))

    # Latest
    latest_dt, latest_ratio, latest_long, latest_short = entries[-1] if entries else (None, 1, 0.5, 0.5)

    # Trend
    all_ratios = [r for _, r, _, _ in entries]
    avg_ratio = sum(all_ratios) / len(all_ratios) if all_ratios else 1
    max_ratio = max(all_ratios) if all_ratios else 1
    min_ratio = min(all_ratios) if all_ratios else 1

    # Direction analysis
    if latest_ratio > 1.5:
        sentiment = "Heavily crowded LONG — contrarian SHORT signal"
    elif latest_ratio > 1.1:
        sentiment = "Moderately long-biased"
    elif latest_ratio < 0.67:
        sentiment = "Heavily crowded SHORT — contrarian LONG signal"
    elif latest_ratio < 0.9:
        sentiment = "Moderately short-biased"
    else:
        sentiment = "Balanced positioning"

    lines = [
        f"=== Long/Short Account Ratio: {sym} (period: {period}) ===",
        f"Data Points: {len(entries)}",
        "",
        f"Latest Ratio: {latest_ratio:.4f}",
        f"Long Accounts:  {latest_long*100:.1f}%",
        f"Short Accounts: {latest_short*100:.1f}%",
        f"Sentiment: {sentiment}",
        "",
        f"Average Ratio: {avg_ratio:.4f}",
        f"Max Ratio:     {max_ratio:.4f}",
        f"Min Ratio:     {min_ratio:.4f}",
    ]

    # Recent 5 data points
    lines.append("")
    lines.append("Recent History:")
    for dt, ratio, long_pct, short_pct in entries[-5:]:
        ts_str = f"{dt:%Y-%m-%d %H:%M}" if dt else "?"
        lines.append(f"  {ts_str} UTC: ratio={ratio:.4f} (L:{long_pct*100:.1f}% S:{short_pct*100:.1f}%)")

    return "\n".join(line for line in lines if line)


# ── Helper: OI history ──────────────────────────────────────────────────


def _fetch_oi_history(symbol: str) -> list[str]:
    """Fetch open interest statistics (5m intervals) for trend analysis."""
    try:
        url = f"{FUTURES_BASE}/futures/data/openInterestHist"
        params = {"symbol": symbol, "period": "1h", "limit": 24}
        resp = httpx.get(url, params=params, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return []

    if not data:
        return []

    oi_values = []
    for entry in data:
        oi_val = float(entry.get("sumOpenInterest", 0))
        oi_usd = float(entry.get("sumOpenInterestValue", 0))
        ts = int(entry.get("timestamp", 0))
        dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc) if ts else None
        oi_values.append((dt, oi_val, oi_usd))

    if len(oi_values) < 2:
        return []

    first_oi = oi_values[0][2]
    last_oi = oi_values[-1][2]
    change_pct = ((last_oi - first_oi) / first_oi * 100) if first_oi > 0 else 0

    lines = [
        "OI History (24h, 1h intervals):",
        f"  24h ago OI (USD): ${first_oi:,.0f}",
        f"  Current OI (USD): ${last_oi:,.0f}",
        f"  24h OI Change:    {change_pct:+.1f}%",
    ]
    return lines
