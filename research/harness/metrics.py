"""Scoring for sampled price forecasts, with NSE-specific diagnostics.

Four questions, four families of metric:

    Is it directionally useful?   -> directional_accuracy (with a deadband)
    Is it more accurate than the
      dumbest possible model?     -> MASE, scaled by the naive forecast
    Is its *uncertainty* honest?  -> CRPS (proper scoring rule) and
                                     interval coverage (calibration)
    Does it understand NSE?       -> circuit_violation_rate

That last one exists because Kronos was trained on global exchange data and
Indian equities have a structural feature most markets lack: price bands.  A
stock in a 10% band physically cannot print outside it, so a model that
forecasts a 15% single-day move is not making a bold call — it is revealing
that it has never seen this market's microstructure.  Measuring that separately
stops it from hiding inside an averaged error number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

# Quantiles used for interval coverage and pinball loss.
_QUANTILES = (0.1, 0.25, 0.5, 0.75, 0.9)


@dataclass
class ScoreCard:
    """Aggregated scores for one forecaster at one horizon."""

    forecaster: str
    horizon: int
    n_origins: int = 0
    directional_accuracy: float = float("nan")
    directional_n: int = 0
    mae: float = float("nan")
    mase: float = float("nan")
    crps: float = float("nan")
    coverage_80: float = float("nan")
    circuit_violation_rate: float = float("nan")
    per_symbol: dict[str, float] = field(default_factory=dict)

    def as_row(self) -> dict[str, object]:
        return {
            "forecaster": self.forecaster,
            "horizon": self.horizon,
            "origins": self.n_origins,
            "dir_acc": round(self.directional_accuracy, 4),
            "dir_n": self.directional_n,
            "mae": round(self.mae, 4),
            "mase": round(self.mase, 4),
            "crps": round(self.crps, 4),
            "cov80": round(self.coverage_80, 4),
            "circuit_viol": round(self.circuit_violation_rate, 4),
        }


def crps_from_samples(samples: np.ndarray, actual: float) -> float:
    """Continuous Ranked Probability Score, estimated from an ensemble.

    Uses the standard energy-form estimator::

        CRPS = E|X - y| - 0.5 * E|X - X'|

    where X, X' are independent draws from the forecast distribution.  Unlike
    pinball loss at a handful of quantiles, this uses the whole ensemble, and
    unlike MAE it *rewards* a model for being appropriately uncertain rather
    than punishing it for not being a point estimate.

    Args:
        samples: 1-D array of sampled values for a single future step.
        actual: The realised value.
    """
    x = np.asarray(samples, dtype=float).ravel()
    if x.size == 0:
        return float("nan")
    term1 = float(np.mean(np.abs(x - actual)))
    if x.size == 1:
        return term1
    # E|X - X'| via the sorted-array identity: avoids the O(n^2) pairwise matrix.
    xs = np.sort(x)
    n = xs.size
    weights = (2 * np.arange(1, n + 1) - n - 1)
    term2 = 2.0 * float(np.sum(weights * xs)) / (n * n)
    return term1 - 0.5 * term2


def directional_accuracy(
    predicted: np.ndarray,
    actual: np.ndarray,
    origin: np.ndarray,
    deadband: float = 0.002,
) -> tuple[float, int]:
    """Fraction of forecasts that got the sign of the move right.

    A deadband drops cases where the stock barely moved.  Without it you score
    a model on coin-flips: if the true move is +0.01%, calling it "up" is luck,
    not skill, and on daily NSE data enough such days accumulate to swamp the
    signal.

    Args:
        predicted: Point forecasts (use the ensemble median).
        actual: Realised prices.
        origin: Price at the forecast origin.
        deadband: Minimum |actual return| to be counted, as a fraction.

    Returns:
        ``(accuracy, n_scored)``.  Accuracy is NaN when nothing clears the band.
    """
    pred_ret = (predicted - origin) / origin
    true_ret = (actual - origin) / origin

    # Two independent exclusions:
    #   - the stock barely moved  -> scoring it rewards luck, not skill
    #   - the model predicted flat -> it abstained; it did not guess wrong.
    # The second matters more than it looks. A naive forecaster predicts
    # exactly the last close, so sign(pred_ret) is exactly 0 and would score
    # 0% by construction. Counting that as failure would flatter every real
    # model by ~50 points against a strawman.
    moved = np.abs(true_ret) >= deadband
    called = np.sign(pred_ret) != 0
    mask = moved & called

    n = int(mask.sum())
    if n == 0:
        return float("nan"), 0
    hits = np.sign(pred_ret[mask]) == np.sign(true_ret[mask])
    return float(np.mean(hits)), n


def interval_coverage(
    samples: np.ndarray, actual: np.ndarray, lo: float = 0.1, hi: float = 0.9
) -> float:
    """Fraction of realised values inside the forecast's central interval.

    A well-calibrated 80% interval contains the truth ~80% of the time.  Much
    below means the model is overconfident; much above means it is hedging so
    widely the interval carries no information.  Either way, accuracy metrics
    alone would not have told you.

    Args:
        samples: ``(n_origins, n_samples)`` sampled values.
        actual: ``(n_origins,)`` realised values.
    """
    if samples.size == 0:
        return float("nan")
    lower = np.quantile(samples, lo, axis=1)
    upper = np.quantile(samples, hi, axis=1)
    inside = (actual >= lower) & (actual <= upper)
    return float(np.mean(inside))


def circuit_violation_rate(
    samples: np.ndarray,
    origin: np.ndarray,
    max_observed_move: np.ndarray,
    slack: float = 1.5,
) -> float:
    """Fraction of sampled paths implying a move the stock has never made.

    We do not have each symbol's exact NSE price band in bhavcopy, so we use
    an empirical proxy: the largest single-day move that symbol actually
    printed in the training window, widened by ``slack``.  A forecast beyond
    that is not merely wrong, it is structurally impossible-ish — the signature
    of a model that has not internalised Indian price bands.

    Args:
        samples: ``(n_origins, n_samples)`` predicted prices for step 1.
        origin: ``(n_origins,)`` price at each forecast origin.
        max_observed_move: ``(n_origins,)`` historical max |daily return|.
        slack: Multiplier applied to the empirical maximum.
    """
    if samples.size == 0:
        return float("nan")
    moves = np.abs(samples - origin[:, None]) / origin[:, None]
    limit = (max_observed_move * slack)[:, None]
    return float(np.mean(moves > limit))
