"""Realized-volatility estimation and forecasting for position sizing.

Complements :mod:`skopaq.risk.atr`.  ATR is a *trailing* dispersion measure —
it tells you how much the stock has moved.  This module forecasts how much it
is *about to* move, which is what position sizing, regime scaling and options
pricing actually depend on.

Empirically grounded, not assumed
---------------------------------
Every default here was measured on 50 NSE symbols over 648 daily bars with
non-overlapping walk-forward origins (497 independent date clusters).  Full
methodology and results in ``research/harness/``.

    Garman-Klass estimator   Uses all four prices rather than close-to-close.
        Verified less noisy on NSE data; with only daily bars available that
        efficiency gain is the difference between signal and noise.

    EWMA with lambda = 0.94  RiskMetrics.  Measured QLIKE 0.3176 against the
        random walk's 0.5704 — it roughly halves the loss.  A Log-HAR
        regression scored marginally better (0.3092) but the gap was
        +0.008 QLIKE, about 2.7%, for an OLS refit at every origin plus a
        failure mode where level-OLS can emit non-positive variance.  EWMA
        buys ~97% of the benefit with none of that.

    What this does NOT do   Predict direction of *price*.  The same harness
        measured price direction at 0.4941 (CI [0.4729, 0.5147]) — chance.
        Volatility is forecastable; price is not.  Do not repurpose this.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

logger = logging.getLogger(__name__)

TRADING_DAYS = 252

# RiskMetrics decay. Higher = longer memory. 0.94 is the daily-data standard
# and was the value validated against NSE bars.
DEFAULT_LAMBDA = 0.94

# Below this many bars an EWMA is dominated by its initialisation.
MIN_BARS = 20

# Variance floor. A circuit-locked or untraded bar has zero range, and zero
# variance breaks every downstream ratio.
_VAR_FLOOR = 1e-12

_ESTIMATORS = frozenset(
    {"close_to_close", "parkinson", "garman_klass", "rogers_satchell"}
)


@dataclass
class VolatilityForecast:
    """Forward volatility estimate for one symbol."""

    symbol: str
    forecast_vol_pct: float      # annualised, percent
    current_vol_pct: float       # annualised, percent, latest single bar
    trailing_vol_pct: float      # annualised, percent, trailing 22-bar mean
    n_bars: int
    estimator: str

    @property
    def is_expanding(self) -> bool:
        """True when forward vol exceeds the trailing average.

        Compared against the 22-bar mean rather than the latest bar: a single
        bar's variance is a noisy estimate, and comparing to it produces
        spurious regime flips. Measured on NSE, using the noisy reference
        inflated apparent directional accuracy from ~0.57 to ~0.68.
        """
        return self.forecast_vol_pct > self.trailing_vol_pct

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "forecast_vol_pct": round(self.forecast_vol_pct, 2),
            "current_vol_pct": round(self.current_vol_pct, 2),
            "trailing_vol_pct": round(self.trailing_vol_pct, 2),
            "is_expanding": self.is_expanding,
            "n_bars": self.n_bars,
            "estimator": self.estimator,
        }


def _ohlc(candle: Any) -> Optional[tuple[float, float, float, float]]:
    """Pull OHLC from a HistoricalCandle (attributes) or a dict."""
    try:
        if hasattr(candle, "open"):
            return (float(candle.open), float(candle.high),
                    float(candle.low), float(candle.close))
        return (float(candle["open"]), float(candle["high"]),
                float(candle["low"]), float(candle["close"]))
    except (AttributeError, KeyError, TypeError, ValueError):
        return None


def realized_variance(
    candles: Sequence[Any], estimator: str = "garman_klass"
) -> list[float]:
    """Per-bar variance estimates from OHLC candles.

    Returns *variance*, not volatility, because variances of independent
    periods add while standard deviations do not — every downstream
    aggregation depends on that.

    Args:
        candles: ``HistoricalCandle`` objects or dicts with open/high/low/close.
        estimator: ``garman_klass`` (default), ``parkinson``,
            ``rogers_satchell`` or ``close_to_close``.

    Returns:
        One variance per usable candle. Malformed candles are skipped, so the
        result may be shorter than the input.
    """
    # Validate once, up front. The per-candle handler below catches ValueError
    # to skip malformed bars — without this check a bad estimator name would be
    # swallowed by it and silently yield an empty series instead of raising.
    if estimator not in _ESTIMATORS:
        raise ValueError(
            f"unknown estimator {estimator!r}; expected one of "
            f"{', '.join(sorted(_ESTIMATORS))}"
        )

    out: list[float] = []
    prev_close: Optional[float] = None

    for candle in candles:
        parsed = _ohlc(candle)
        if parsed is None:
            continue
        o, h, l, c = parsed
        if min(o, h, l, c) <= 0 or h < l:
            continue

        try:
            if estimator == "close_to_close":
                if prev_close is None:
                    prev_close = c
                    continue
                var = math.log(c / prev_close) ** 2
            elif estimator == "parkinson":
                var = (math.log(h / l) ** 2) / (4.0 * math.log(2.0))
            elif estimator == "garman_klass":
                var = (0.5 * math.log(h / l) ** 2
                       - (2.0 * math.log(2.0) - 1.0) * math.log(c / o) ** 2)
            elif estimator == "rogers_satchell":
                var = (math.log(h / c) * math.log(h / o)
                       + math.log(l / c) * math.log(l / o))
        except (ValueError, ZeroDivisionError):
            continue

        prev_close = c
        if math.isfinite(var):
            out.append(max(var, _VAR_FLOOR))

    return out


def ewma_variance(
    variances: Sequence[float], lam: float = DEFAULT_LAMBDA
) -> Optional[float]:
    """Exponentially-weighted mean of a variance series (RiskMetrics).

    Computed iteratively rather than as a weighted sum so it stays numerically
    stable for long series — ``lam ** n`` underflows to zero well before a
    3-year daily history ends.

    Args:
        variances: Per-bar variances, oldest first.
        lam: Decay factor in (0, 1).

    Returns:
        Forecast variance for the next bar, or None if the input is too short.
    """
    if not variances:
        return None
    if not 0.0 < lam < 1.0:
        raise ValueError(f"lambda must be in (0, 1), got {lam}")

    ewma = float(variances[0])
    for v in variances[1:]:
        ewma = lam * ewma + (1.0 - lam) * float(v)
    return max(ewma, _VAR_FLOOR)


def annualize(variance: float) -> float:
    """Daily variance -> annualised volatility in percent."""
    return math.sqrt(max(variance, 0.0) * TRADING_DAYS) * 100.0


def forecast_volatility(
    symbol: str,
    candles: Sequence[Any],
    estimator: str = "garman_klass",
    lam: float = DEFAULT_LAMBDA,
    min_bars: int = MIN_BARS,
) -> Optional[VolatilityForecast]:
    """Forecast next-bar annualised volatility from daily candles.

    Pure function — it takes candles rather than fetching them, so callers
    supply data through whatever path they already use (MCP tools, the daemon's
    broker client) and this stays trivially testable.

    Args:
        symbol: For labelling only.
        candles: Daily OHLC candles, oldest first. 60+ bars recommended.
        estimator: Realized-variance estimator.
        lam: EWMA decay.
        min_bars: Refuse to forecast below this many usable bars.

    Returns:
        A :class:`VolatilityForecast`, or None if there is not enough data.
    """
    variances = realized_variance(candles, estimator)
    if len(variances) < min_bars:
        logger.warning(
            "%s: only %d usable bars (need %d) — no volatility forecast",
            symbol, len(variances), min_bars,
        )
        return None

    forecast = ewma_variance(variances, lam)
    if forecast is None:
        return None

    trailing = sum(variances[-22:]) / len(variances[-22:])
    return VolatilityForecast(
        symbol=symbol,
        forecast_vol_pct=annualize(forecast),
        current_vol_pct=annualize(variances[-1]),
        trailing_vol_pct=annualize(trailing),
        n_bars=len(variances),
        estimator=estimator,
    )


def vol_risk_premium(forecast_vol_pct: float, implied_vol_pct: float) -> float:
    """Implied minus forecast volatility — the options-selling edge, in points.

    Positive means options are pricing more movement than we forecast, which is
    the condition that favours selling premium. Negative means the opposite and
    is a reason to stand aside, not to reverse into buying: the forecast has a
    measured QLIKE edge over a random walk, not a calibrated distribution.

    Args:
        forecast_vol_pct: Annualised forecast volatility, percent.
        implied_vol_pct: Annualised implied volatility from the option chain.
    """
    return implied_vol_pct - forecast_vol_pct
