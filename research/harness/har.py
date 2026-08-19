"""Realized-volatility estimation and HAR forecasting for NSE daily bars.

Why volatility and not price
----------------------------
Three walk-forward runs established that NSE daily *price* direction sits at
chance (kronos-ft 0.4941, CI [0.4729, 0.5147]) and that nothing beats a naive
random walk on magnitude.  That is the expected result — price levels are close
to a martingale.  Volatility is not: it clusters, mean-reverts, and is the one
property of returns with genuine, long-documented predictability.

Estimating RV from daily bars
-----------------------------
"Realized volatility" normally means intraday-sampled.  With daily OHLC we use
range-based estimators instead, which extract far more information than the
close-to-close return does:

    close-to-close   variance of ln(C_t / C_{t-1}).  Uses 2 of 4 prices and
                     throws away the entire intraday path.
    Parkinson        uses the high-low range.  ~5x more efficient than
                     close-to-close, but assumes zero drift and misses gaps.
    Garman-Klass     uses all four prices.  ~7x more efficient; the default.
    Rogers-Satchell  drift-robust, which matters for trending Indian names
                     where Garman-Klass is biased by a strong directional move.

Efficiency here means: how much less noisy the estimate is for the same number
of bars.  With only 648 bars per symbol that multiplier is the difference
between a usable signal and noise.

Models
------
    HAR-RV      Corsi (2009): regress future RV on daily, weekly and monthly
                averages of past RV.  Three terms approximate the long-memory
                behaviour of volatility without a fractionally-integrated model.
    Log-HAR     Same on log(RV).  RV is right-skewed and strictly positive, so
                OLS on levels is heteroskedastic and can predict negatives.
                This is the benchmark the 2026 TSFM survey found hardest to beat.
    RW          RV_{t+1} = RV_t.  The naive control, as in the price harness.
    MA22        Trailing 22-day mean.
    EWMA        RiskMetrics, lambda = 0.94.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# HAR's three horizons: today, one week, one month of trading days.
HAR_LAGS = (1, 5, 22)
_TRADING_DAYS = 252


# ── realized variance estimators ────────────────────────────────────────────

def realized_variance(df: pd.DataFrame, estimator: str = "garman_klass") -> np.ndarray:
    """Per-bar variance estimate from daily OHLC.

    Returns variance (not volatility) so the HAR additivity assumption holds —
    variances of independent periods add, standard deviations do not.

    Args:
        df: Frame with open/high/low/close columns.
        estimator: one of ``close_to_close``, ``parkinson``, ``garman_klass``,
            ``rogers_satchell``.

    Returns:
        Array of length ``len(df)``.  Index 0 is NaN for close_to_close (needs
        a previous close); range estimators are defined from the first bar.
    """
    o = df["open"].to_numpy(dtype=float)
    h = df["high"].to_numpy(dtype=float)
    l = df["low"].to_numpy(dtype=float)
    c = df["close"].to_numpy(dtype=float)

    with np.errstate(divide="ignore", invalid="ignore"):
        if estimator == "close_to_close":
            r = np.full_like(c, np.nan)
            r[1:] = np.log(c[1:] / c[:-1])
            rv = r ** 2
        elif estimator == "parkinson":
            rv = (np.log(h / l) ** 2) / (4.0 * np.log(2.0))
        elif estimator == "garman_klass":
            rv = 0.5 * np.log(h / l) ** 2 - (2.0 * np.log(2.0) - 1.0) * np.log(c / o) ** 2
        elif estimator == "rogers_satchell":
            rv = (np.log(h / c) * np.log(h / o)) + (np.log(l / c) * np.log(l / o))
        else:
            raise ValueError(f"unknown estimator: {estimator}")

    # A zero-range bar (circuit-locked or untraded) gives RV=0, which log-HAR
    # cannot take. Floor at a tiny positive value rather than dropping the bar,
    # so the calendar stays contiguous for the lag structure.
    rv = np.where(np.isfinite(rv), rv, np.nan)
    rv = np.maximum(rv, 1e-12)
    return rv


def annualize(variance: np.ndarray | float) -> np.ndarray | float:
    """Daily variance -> annualised volatility in percent, for readability."""
    return np.sqrt(np.asarray(variance) * _TRADING_DAYS) * 100.0


# ── HAR design matrix ───────────────────────────────────────────────────────

def har_features(rv: np.ndarray, lags: tuple[int, ...] = HAR_LAGS) -> np.ndarray:
    """Build the HAR regressor matrix: trailing means over each lag horizon.

    Row t holds the averages computed from data up to and including t, so a
    model fitted on row t and used to predict t+1 never sees the future.
    """
    n = len(rv)
    out = np.full((n, len(lags)), np.nan)
    for j, L in enumerate(lags):
        # cumulative-sum trailing mean, O(n) rather than O(n*L)
        cs = np.cumsum(np.insert(rv, 0, 0.0))
        idx = np.arange(n)
        lo = np.maximum(idx - L + 1, 0)
        out[:, j] = (cs[idx + 1] - cs[lo]) / (idx - lo + 1)
    return out


# ── forecasters ─────────────────────────────────────────────────────────────

class RandomWalkVol:
    """RV_{t+h} = RV_t. The control every other model must beat."""

    name = "rw"

    def forecast(self, rv: np.ndarray, horizon: int) -> float:
        return float(rv[-1])


class MovingAverageVol:
    """Trailing mean over ``window`` bars."""

    name = "ma22"

    def __init__(self, window: int = 22) -> None:
        self.window = window

    def forecast(self, rv: np.ndarray, horizon: int) -> float:
        return float(np.mean(rv[-self.window:]))


class EWMAVol:
    """RiskMetrics exponentially-weighted variance, lambda = 0.94."""

    name = "ewma"

    def __init__(self, lam: float = 0.94) -> None:
        self.lam = lam

    def forecast(self, rv: np.ndarray, horizon: int) -> float:
        w = self.lam ** np.arange(len(rv) - 1, -1, -1)
        return float(np.sum(w * rv) / np.sum(w))


class HARVol:
    """Corsi HAR-RV, refit by OLS at every origin on past data only.

    Args:
        log: fit on log(RV) instead of levels.  Strongly preferred — RV is
            right-skewed and positive, so level-OLS is heteroskedastic and can
            emit negative variance forecasts.
        min_obs: refuse to fit below this many usable rows; falls back to the
            trailing mean, which is what HAR degenerates to anyway.
    """

    def __init__(self, log: bool = True, min_obs: int = 60,
                 lags: tuple[int, ...] = HAR_LAGS) -> None:
        self.log = log
        self.min_obs = min_obs
        self.lags = lags
        self.name = "log-har" if log else "har"

    def forecast(self, rv: np.ndarray, horizon: int) -> float:
        y_raw = np.log(rv) if self.log else rv
        X_all = har_features(np.log(rv) if self.log else rv, self.lags)

        # Target is the mean over the next `horizon` bars, so the last usable
        # training row is the one whose target window ends at the final bar.
        n = len(rv)
        last = n - horizon
        if last < self.min_obs:
            return float(np.mean(rv[-22:]))

        idx = np.arange(max(self.lags) - 1, last)
        if idx.size < self.min_obs:
            return float(np.mean(rv[-22:]))

        # y[i] = mean of y_raw over (i, i+horizon]
        cs = np.cumsum(np.insert(y_raw, 0, 0.0))
        y = (cs[idx + horizon + 1] - cs[idx + 1]) / horizon

        X = np.column_stack([np.ones(idx.size), X_all[idx]])
        ok = np.isfinite(y) & np.all(np.isfinite(X), axis=1)
        if ok.sum() < self.min_obs:
            return float(np.mean(rv[-22:]))

        try:
            beta, *_ = np.linalg.lstsq(X[ok], y[ok], rcond=None)
        except np.linalg.LinAlgError:
            return float(np.mean(rv[-22:]))

        x_now = np.concatenate([[1.0], X_all[n - 1]])
        if not np.all(np.isfinite(x_now)):
            return float(np.mean(rv[-22:]))
        pred = float(x_now @ beta)

        if self.log:
            # Exp of a log-forecast is the conditional median, not the mean.
            # The Duan smearing correction restores the mean using the fitted
            # residuals — without it every log model is biased low, which would
            # flatter QLIKE for the wrong reason.
            resid = y[ok] - X[ok] @ beta
            pred = float(np.exp(pred) * np.mean(np.exp(resid)))
        return max(pred, 1e-12)


VOL_MODELS = [RandomWalkVol(), MovingAverageVol(), EWMAVol(),
              HARVol(log=False), HARVol(log=True)]
