"""Forecaster interface plus the baselines Kronos has to beat.

The central discipline of this harness: **every forecaster returns a
distribution, not a point.**  Kronos's whole pitch is that sampling it N times
gives you N plausible futures, so comparing its sampled spread against a
point-forecast baseline would be rigged in its favour.  Each baseline below
therefore emits samples too, and both sides are scored with the same
distributional metrics.

Baselines, in increasing order of how embarrassing it is to lose to them:

    NaiveForecaster   — tomorrow's close is today's close.  On daily equity
                        data this is brutally hard to beat; efficient-market
                        behaviour means most "successful" price models are
                        really just rediscovering it.
    DriftForecaster   — random walk plus the historical mean log return.
    BootstrapForecaster — resamples the symbol's own recent daily returns.
                        This is the real bar: it reproduces the fat tails and
                        volatility clustering of the actual series without
                        knowing anything at all.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd


@runtime_checkable
class Forecaster(Protocol):
    """Anything that can turn a history window into sampled future closes."""

    name: str

    def forecast(
        self,
        history: pd.DataFrame,
        horizon: int,
        n_samples: int,
        rng: np.random.Generator,
    ) -> np.ndarray:
        """Return an array of shape ``(n_samples, horizon)`` of close prices.

        Args:
            history: OHLCV rows up to and including the forecast origin.
                Never contains future data — the walk-forward driver guarantees
                this, and any forecaster that reaches outside it is cheating.
            horizon: Steps ahead to predict.
            n_samples: Number of independent future paths to draw.
            rng: Seeded generator, so a run is reproducible.
        """
        ...


def _log_returns(closes: np.ndarray) -> np.ndarray:
    """Daily log returns, guarding against non-positive prices."""
    closes = np.asarray(closes, dtype=float)
    closes = closes[closes > 0]
    if closes.size < 2:
        return np.zeros(1)
    return np.diff(np.log(closes))


class NaiveForecaster:
    """Random walk with zero drift — the benchmark that matters."""

    name = "naive"

    def forecast(self, history, horizon, n_samples, rng):
        last = float(history["close"].iloc[-1])
        return np.full((n_samples, horizon), last)


class DriftForecaster:
    """Random walk plus historical mean log return, with Gaussian noise."""

    name = "drift"

    def __init__(self, lookback: int = 250) -> None:
        self.lookback = lookback

    def forecast(self, history, horizon, n_samples, rng):
        closes = history["close"].to_numpy(dtype=float)[-self.lookback:]
        rets = _log_returns(closes)
        mu, sigma = float(np.mean(rets)), float(np.std(rets))
        last = float(closes[-1])

        shocks = rng.normal(mu, sigma, size=(n_samples, horizon))
        return last * np.exp(np.cumsum(shocks, axis=1))


class BootstrapForecaster:
    """Resamples the symbol's own recent returns — no model, real fat tails.

    Block bootstrapping (contiguous runs rather than single days) preserves
    short-horizon volatility clustering, which single-day resampling destroys.
    That clustering is most of what makes equity returns non-Gaussian, so a
    Gaussian baseline flatters any model that captures it.
    """

    name = "bootstrap"

    def __init__(self, lookback: int = 250, block: int = 5) -> None:
        self.lookback = lookback
        self.block = block

    def forecast(self, history, horizon, n_samples, rng):
        closes = history["close"].to_numpy(dtype=float)[-self.lookback:]
        rets = _log_returns(closes)
        last = float(closes[-1])
        if rets.size < self.block + 1:
            return np.full((n_samples, horizon), last)

        n_blocks = int(np.ceil(horizon / self.block))
        starts = rng.integers(
            0, rets.size - self.block, size=(n_samples, n_blocks)
        )
        offs = np.arange(self.block)
        drawn = rets[starts[..., None] + offs]              # (S, B, block)
        paths = drawn.reshape(n_samples, -1)[:, :horizon]
        return last * np.exp(np.cumsum(paths, axis=1))


BASELINES: list[Forecaster] = [
    NaiveForecaster(),
    DriftForecaster(),
    BootstrapForecaster(),
]
