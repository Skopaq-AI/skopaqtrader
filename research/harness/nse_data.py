"""NSE end-of-day OHLCV loader built on NSE's public UDiFF Bhavcopy.

Why Bhavcopy and not yfinance:
    Bhavcopy is NSE's own authoritative EOD publication — free, public, and
    permitted for analysis/backtesting under NSE's Data Usage & Sharing Policy
    (that policy restricts *redistribution*, not consumption).  Yahoo's NSE
    coverage is a ToS-grey scrape with known split and volume defects that
    would silently corrupt a forecasting benchmark.

    Nothing here needs broker credentials, so it works even when the INDstocks
    token is expired.

Pipeline:
    1. download_range()   — one zip per trading day, resumable and polite
    2. load_long_frame()  — parse EQ-series rows into a tidy long DataFrame
    3. adjust_for_actions() — back-adjust splits/bonuses using NSE's own
                              PrvsClsgPric, which NSE restates on ex-dates
    4. build_panel()      — pivot into per-symbol OHLCV frames on disk

Cache lives under ``data_cache/bhavcopy`` which .gitignore already covers via
``**/data_cache/``.  Keep it that way: NSE permits you to *use* this data, not
to republish it.
"""

from __future__ import annotations

import io
import logging
import random
import ssl
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Iterator, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# NSE serves the UDiFF bhavcopy from this archive host.  The pre-2024 layout
# (content/historical/EQUITIES/...) is retired — it now 404s.
_BHAV_URL = (
    "https://nsearchives.nseindia.com/content/cm/"
    "BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
)

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}

# Columns we keep from the 34-column UDiFF schema.
_COLS = {
    "TradDt": "date",
    "TckrSymb": "symbol",
    "SctySrs": "series",
    "FinInstrmId": "security_id",   # matches INDstocks NSE_<id> scrip codes
    "OpnPric": "open",
    "HghPric": "high",
    "LwPric": "low",
    "ClsPric": "close",
    "PrvsClsgPric": "prev_close",
    "TtlTradgVol": "volume",
    "TtlTrfVal": "turnover",
    "TtlNbOfTxsExctd": "trades",
}

DEFAULT_CACHE = Path("data_cache/bhavcopy")


def _ssl_context() -> ssl.SSLContext:
    """Build a verifying SSL context that works on framework Python builds.

    Python installed from python.org does not read the macOS keychain, so
    ``urllib`` has no trusted roots and every https call dies with
    CERTIFICATE_VERIFY_FAILED.  certifi ships the Mozilla CA bundle and is
    already present transitively via httpx.  Verification stays ON — the fix is
    to give Python a trust store, never to stop checking.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:                       # pragma: no cover
        logger.warning("certifi missing; falling back to system trust store")
        return ssl.create_default_context()


_SSL_CTX = _ssl_context()


@dataclass
class DownloadStats:
    """Outcome of a download_range() sweep."""

    requested: int = 0
    downloaded: int = 0
    cached: int = 0
    holidays: int = 0
    failed: int = 0

    def __str__(self) -> str:
        return (
            f"{self.requested} weekdays: {self.downloaded} fetched, "
            f"{self.cached} cached, {self.holidays} non-trading, "
            f"{self.failed} failed"
        )


def _weekdays(start: date, end: date) -> Iterator[date]:
    """Yield Mon–Fri dates inclusive.  NSE holidays surface later as 404s."""
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def download_range(
    start: date,
    end: date,
    cache_dir: Path = DEFAULT_CACHE,
    delay: float = 0.6,
    timeout: int = 45,
) -> DownloadStats:
    """Fetch bhavcopy zips for every weekday in ``[start, end]``.

    Resumable: an existing ``.csv`` or ``.holiday`` marker short-circuits the
    request, so re-running after an interruption costs nothing.

    A 404 means NSE did not trade that day (holiday).  We record a marker file
    so subsequent runs never re-ask — that is the difference between a polite
    client and one NSE decides to rate-limit.

    Args:
        start: First calendar date (inclusive).
        end: Last calendar date (inclusive).
        cache_dir: Where to write ``YYYYMMDD.csv`` / ``YYYYMMDD.holiday``.
        delay: Base seconds between live requests.  Jittered ±25%.
        timeout: Per-request socket timeout in seconds.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    stats = DownloadStats()

    for d in _weekdays(start, end):
        stats.requested += 1
        stamp = d.strftime("%Y%m%d")
        csv_path = cache_dir / f"{stamp}.csv"
        holiday_path = cache_dir / f"{stamp}.holiday"

        if csv_path.exists():
            stats.cached += 1
            continue
        if holiday_path.exists():
            stats.holidays += 1
            continue

        url = _BHAV_URL.format(yyyymmdd=stamp)
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as resp:
                blob = resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                holiday_path.write_text("")           # non-trading day
                stats.holidays += 1
            else:
                logger.warning("%s -> HTTP %s", stamp, exc.code)
                stats.failed += 1
            time.sleep(delay)
            continue
        except Exception as exc:                       # noqa: BLE001 - network
            logger.warning("%s -> %s", stamp, exc)
            stats.failed += 1
            time.sleep(delay)
            continue

        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                inner = zf.namelist()[0]
                csv_path.write_bytes(zf.read(inner))
        except zipfile.BadZipFile:
            logger.warning("%s -> not a zip (%d bytes)", stamp, len(blob))
            stats.failed += 1
            time.sleep(delay)
            continue

        stats.downloaded += 1
        time.sleep(delay * random.uniform(0.75, 1.25))

    return stats


def load_long_frame(
    cache_dir: Path = DEFAULT_CACHE,
    series: tuple[str, ...] = ("EQ",),
) -> pd.DataFrame:
    """Parse every cached bhavcopy CSV into one tidy long DataFrame.

    Args:
        cache_dir: Directory written by :func:`download_range`.
        series: NSE security series to keep.  ``EQ`` is the ordinary rolling
            -settlement equity series; ``BE`` is trade-for-trade (no intraday
            netting) and behaves differently enough that mixing the two would
            muddy a forecast benchmark.

    Returns:
        Long frame with one row per (date, symbol).
    """
    files = sorted(cache_dir.glob("*.csv"))
    if not files:
        raise FileNotFoundError(
            f"No bhavcopy CSVs in {cache_dir}. Run download_range() first."
        )

    keep = set(series)
    chunks: list[pd.DataFrame] = []
    for path in files:
        try:
            raw = pd.read_csv(path, usecols=list(_COLS), dtype=str)
        except Exception as exc:                       # noqa: BLE001
            logger.warning("skipping unreadable %s: %s", path.name, exc)
            continue
        raw = raw.rename(columns=_COLS)
        raw["series"] = raw["series"].str.strip()
        raw = raw[raw["series"].isin(keep)]
        chunks.append(raw)

    df = pd.concat(chunks, ignore_index=True)
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d")
    df["symbol"] = df["symbol"].str.strip()
    for col in ("open", "high", "low", "close", "prev_close", "turnover"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("volume", "trades"):
        df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0).astype("int64")

    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[df["close"] > 0]
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def adjust_for_actions(
    df: pd.DataFrame,
    tol: float = 0.002,
    max_factor: float = 100.0,
) -> pd.DataFrame:
    """Back-adjust prices for splits/bonuses using NSE's restated prev_close.

    On an ex-date NSE restates ``PrvsClsgPric`` onto the new basis while
    ``ClsPric`` of the prior row stays on the old one.  The ratio between them
    is therefore the exchange's own adjustment factor — authoritative, and
    available without a separate corporate-actions feed::

        factor[t] = prev_close[t] / close[t-1]

    A 1:2 split gives factor 0.5.  To put history on today's basis we multiply
    every price at or before ``t-1`` by the product of all later factors, and
    divide volume by the same amount so turnover stays invariant.

    Rows where the factor is wildly implausible are treated as data faults and
    neutralised to 1.0 rather than propagated — a single bad tick would
    otherwise rescale a symbol's entire history.

    Args:
        df: Long frame from :func:`load_long_frame`.
        tol: Relative deviation from 1.0 below which a factor is treated as
            float noise rather than a real corporate action.
        max_factor: Reject factors outside ``[1/max_factor, max_factor]``.

    Returns:
        Copy of ``df`` with adjusted ``open/high/low/close/volume`` plus
        ``adj_factor`` (the cumulative multiplier applied to that row) and
        ``raw_close`` (the untouched exchange close, kept for auditing).
    """
    out = df.sort_values(["symbol", "date"]).copy()
    out["raw_close"] = out["close"]

    g = out.groupby("symbol", sort=False)
    prior_close = g["close"].shift(1)

    factor = out["prev_close"] / prior_close
    factor = factor.where(prior_close.notna() & (prior_close > 0), 1.0)

    # Treat float noise as "no action", and absurd ratios as bad data.
    noise = (factor - 1.0).abs() < tol
    absurd = (factor < 1.0 / max_factor) | (factor > max_factor) | factor.isna()
    n_actions = int((~noise & ~absurd).sum())
    n_absurd = int(absurd.sum())
    factor = factor.mask(noise | absurd, 1.0)

    # cumulative product of *future* factors, per symbol:
    #   cum[t] = prod(factor[u] for u > t)
    # Computed as total / running-inclusive-product, which is exact here
    # because every factor is strictly positive after masking.
    out["_f"] = factor
    running = out.groupby("symbol", sort=False)["_f"].cumprod()
    total = out.groupby("symbol", sort=False)["_f"].transform("prod")
    out["adj_factor"] = total / running

    for col in ("open", "high", "low", "close", "prev_close"):
        out[col] = out[col] * out["adj_factor"]
    # Volume moves inversely so price x volume (turnover) is preserved.
    out["volume"] = (out["volume"] / out["adj_factor"]).round().astype("int64")

    out = out.drop(columns=["_f"])
    logger.info(
        "corporate actions: %d adjustments applied, %d implausible factors "
        "neutralised", n_actions, n_absurd,
    )
    return out.reset_index(drop=True)


def build_panel(
    df: pd.DataFrame,
    symbols: list[str],
    out_dir: Path,
    min_rows: int = 300,
) -> dict[str, Path]:
    """Write one OHLCV CSV per symbol, in the column order Kronos expects.

    Kronos consumes a frame with ``timestamps, open, high, low, close, volume``
    (and optional ``amount``).  Emitting exactly that shape here means the
    forecaster stage needs zero adapter code — and, importantly, that stage can
    run in a *different* virtualenv, since a CSV crosses env boundaries where a
    live DataFrame cannot.

    Args:
        df: Adjusted long frame.
        symbols: Symbols to export.
        out_dir: Destination directory.
        min_rows: Skip symbols with fewer rows than this — too short to
            walk-forward test meaningfully.

    Returns:
        Mapping of symbol -> written path (symbols that were too short are
        absent from the mapping).
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    for sym in symbols:
        sub = df[df["symbol"] == sym]
        if len(sub) < min_rows:
            logger.debug("%s: only %d rows, skipped", sym, len(sub))
            continue
        frame = pd.DataFrame(
            {
                "timestamps": sub["date"].dt.strftime("%Y-%m-%d"),
                "open": sub["open"].round(4),
                "high": sub["high"].round(4),
                "low": sub["low"].round(4),
                "close": sub["close"].round(4),
                "volume": sub["volume"],
                "amount": sub["turnover"].round(2),
            }
        )
        path = out_dir / f"{sym}.csv"
        frame.to_csv(path, index=False)
        written[sym] = path

    logger.info("panel: wrote %d/%d symbols to %s",
                len(written), len(symbols), out_dir)
    return written
