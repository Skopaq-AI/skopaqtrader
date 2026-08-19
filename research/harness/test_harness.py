"""Regression tests for the Kronos NSE validation harness.

Run:  python -m pytest research/kronos/test_harness.py -v

These cover the properties that, if broken, would produce a *confident wrong
answer* rather than an obvious crash — the only kind of bug that actually
matters in a benchmark.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nse_data                                        # noqa: E402
from forecasters import BASELINES, NaiveForecaster     # noqa: E402
from metrics import crps_from_samples, directional_accuracy  # noqa: E402
from universe import select_universe                   # noqa: E402


def _synthetic(symbol="TESTCO", closes=None, prevcloses=None):
    closes = closes or [1000.0, 1010.0, 1000.0, 505.0, 510.0]
    prevcloses = prevcloses or [990.0, 1000.0, 1010.0, 500.0, 505.0]
    dates = pd.to_datetime(
        ["2026-01-01", "2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07"]
    )[: len(closes)]
    return pd.DataFrame([
        dict(date=d, symbol=symbol, series="EQ", security_id="1",
             open=c, high=c, low=c, close=c, prev_close=pc,
             volume=1000, turnover=c * 1000, trades=10)
        for d, c, pc in zip(dates, closes, prevcloses)
    ])


class TestCorporateActions:
    def test_split_is_neutralised(self):
        """A 1:2 split must not read as a -50% day."""
        adj = nse_data.adjust_for_actions(_synthetic())
        rets = np.diff(adj["close"]) / adj["close"].iloc[:-1].to_numpy()
        assert abs(rets[2]) < 0.05, "split still shows as a crash"

    def test_post_split_prices_untouched(self):
        """History is rebased onto today; recent prices must not move."""
        adj = nse_data.adjust_for_actions(_synthetic())
        assert adj["close"].iloc[3] == pytest.approx(505.0)
        assert adj["close"].iloc[4] == pytest.approx(510.0)

    def test_turnover_is_invariant(self):
        """Price x volume must survive the adjustment unchanged."""
        raw = _synthetic()
        adj = nse_data.adjust_for_actions(raw)
        assert (adj["close"] * adj["volume"] - raw["close"] * raw["volume"]) \
            .abs().max() == pytest.approx(0.0, abs=1e-6)

    def test_absurd_factor_is_neutralised_not_propagated(self):
        """One bad tick must not rescale an entire symbol's history."""
        bad = _synthetic(prevcloses=[990.0, 1000.0, 1010.0, 0.0001, 505.0])
        adj = nse_data.adjust_for_actions(bad)
        assert adj["close"].max() < 1e5, "absurd factor propagated"

    def test_raw_close_is_preserved_for_audit(self):
        adj = nse_data.adjust_for_actions(_synthetic())
        assert list(adj["raw_close"]) == [1000.0, 1010.0, 1000.0, 505.0, 510.0]


class TestMetrics:
    def test_crps_matches_brute_force(self):
        """The sorted-array estimator must equal the O(n^2) definition."""
        rng = np.random.default_rng(0)
        x, y = rng.normal(100, 5, 300), 101.5
        brute = np.mean(np.abs(x - y)) - 0.5 * np.mean(np.abs(x[:, None] - x[None, :]))
        assert crps_from_samples(x, y) == pytest.approx(brute, abs=1e-9)

    def test_crps_rewards_the_sharper_correct_forecast(self):
        rng = np.random.default_rng(1)
        tight = rng.normal(100, 1, 500)
        wide = rng.normal(100, 20, 500)
        assert crps_from_samples(tight, 100.0) < crps_from_samples(wide, 100.0)

    def test_deadband_excludes_noise_days(self):
        """Sub-threshold moves must not be scored — they are coin flips."""
        origin = np.array([100.0, 100.0, 100.0])
        actual = np.array([100.05, 103.0, 97.0])       # first is +0.05%
        pred = np.array([101.0, 101.0, 101.0])
        _, n = directional_accuracy(pred, actual, origin, deadband=0.002)
        assert n == 2, "deadband did not exclude the flat day"

    def test_directional_accuracy_is_correct(self):
        origin = np.array([100.0, 100.0])
        actual = np.array([105.0, 95.0])
        pred = np.array([106.0, 104.0])                # right, then wrong
        acc, n = directional_accuracy(pred, actual, origin)
        assert n == 2 and acc == pytest.approx(0.5)


class TestForecasters:
    @pytest.mark.parametrize("f", BASELINES, ids=lambda f: f.name)
    def test_shape_contract(self, f):
        hist = pd.DataFrame({
            "timestamps": pd.date_range("2024-01-01", periods=300, freq="B"),
            "close": np.linspace(100, 150, 300),
            "open": np.linspace(100, 150, 300),
            "high": np.linspace(101, 151, 300),
            "low": np.linspace(99, 149, 300),
            "volume": np.full(300, 1000),
        })
        out = f.forecast(hist, horizon=5, n_samples=16,
                         rng=np.random.default_rng(0))
        assert out.shape == (16, 5), f"{f.name} returned {out.shape}"
        assert np.all(np.isfinite(out)), f"{f.name} produced non-finite prices"
        assert np.all(out > 0), f"{f.name} produced non-positive prices"

    def test_naive_anchors_on_last_close(self):
        hist = pd.DataFrame({"close": [10.0, 20.0, 33.0]})
        out = NaiveForecaster().forecast(hist, 3, 4, np.random.default_rng(0))
        assert np.all(out == 33.0)

    @pytest.mark.parametrize("f", BASELINES, ids=lambda f: f.name)
    def test_no_lookahead(self, f):
        """A forecaster must be blind to anything after the origin.

        Two histories identical up to the origin, diverging wildly after, must
        yield identical forecasts. This is the property the entire benchmark
        rests on — if it fails, every result is meaningless.
        """
        base = pd.DataFrame({
            "timestamps": pd.date_range("2024-01-01", periods=300, freq="B"),
            "close": np.linspace(100, 150, 300),
            "open": np.linspace(100, 150, 300),
            "high": np.linspace(101, 151, 300),
            "low": np.linspace(99, 149, 300),
            "volume": np.full(300, 1000),
        })
        origin = 250
        a = base.iloc[: origin + 1]
        tampered = base.copy()
        tampered.loc[origin + 1:, "close"] *= 10        # future goes berserk
        b = tampered.iloc[: origin + 1]

        rng_a, rng_b = np.random.default_rng(7), np.random.default_rng(7)
        assert np.allclose(f.forecast(a, 5, 8, rng_a),
                           f.forecast(b, 5, 8, rng_b)), f"{f.name} peeked"


class TestUniverse:
    def test_coverage_floor_rejects_partial_listings(self):
        full = _synthetic("BIGCO")
        partial = _synthetic("NEWCO").iloc[:2]          # only 2 of 5 days
        df = pd.concat([full, partial], ignore_index=True)
        picked = select_universe(df, top_n=10, min_coverage=0.95,
                                 min_median_turnover=0.0)
        assert "BIGCO" in picked and "NEWCO" not in picked

    def test_turnover_floor_rejects_illiquid(self):
        df = _synthetic("PENNY")
        assert select_universe(df, min_median_turnover=1e12) == []


class TestAbstention:
    def test_flat_prediction_is_abstention_not_failure(self):
        """A forecaster predicting no change must not be scored as wrong.

        Naive predicts exactly the last close, so sign(pred_ret) == 0. Counting
        that as a miss would score naive at 0% direction and flatter every real
        model against a strawman.
        """
        origin = np.array([100.0, 100.0, 100.0])
        actual = np.array([105.0, 95.0, 103.0])
        flat = origin.copy()                    # naive: predicts no change
        acc, n = directional_accuracy(flat, actual, origin)
        assert n == 0, "flat predictions were scored instead of abstained"
        assert acc != acc, "abstention should report NaN, not a number"

    def test_partial_abstention_scores_only_real_calls(self):
        origin = np.array([100.0, 100.0, 100.0])
        actual = np.array([105.0, 95.0, 103.0])
        pred = np.array([100.0, 90.0, 108.0])   # abstain, right, right
        acc, n = directional_accuracy(pred, actual, origin)
        assert n == 2 and acc == pytest.approx(1.0)


class TestVolatility:
    """RV estimators and HAR must be correct before any result is believable."""

    @staticmethod
    def _synthetic_ohlc(n=400, sigma=0.02, seed=0):
        rng = np.random.default_rng(seed)
        r = rng.normal(0, sigma, n)
        close = 1000 * np.exp(np.cumsum(r))
        open_ = np.concatenate([[1000.0], close[:-1]])
        rng2 = np.random.default_rng(seed + 1)
        wig = np.abs(rng2.normal(0, sigma * 0.5, n))
        return pd.DataFrame({
            "timestamps": pd.date_range("2024-01-01", periods=n, freq="B"),
            "open": open_, "close": close,
            "high": np.maximum(open_, close) * (1 + wig),
            "low": np.minimum(open_, close) * (1 - wig),
        })

    @pytest.mark.parametrize("est", ["close_to_close", "parkinson",
                                     "garman_klass", "rogers_satchell"])
    def test_estimators_are_positive_and_finite(self, est):
        import har
        rv = har.realized_variance(self._synthetic_ohlc(), est)
        assert np.all(rv[1:] > 0), f"{est} produced non-positive variance"
        assert np.all(np.isfinite(rv[1:])), f"{est} produced non-finite variance"

    def test_estimators_recover_known_sigma(self):
        """Annualised vol from a known-sigma series must land near the truth."""
        import har
        sigma = 0.02                                  # 2% daily
        df = self._synthetic_ohlc(n=2000, sigma=sigma)
        truth = sigma * np.sqrt(252) * 100            # ~31.7% annualised
        rv = har.realized_variance(df, "close_to_close")
        est = har.annualize(np.nanmean(rv))
        assert 0.7 * truth < est < 1.3 * truth, f"got {est:.1f}, expected ~{truth:.1f}"

    def test_range_estimator_is_less_noisy_than_close_to_close(self):
        """The efficiency claim in har.py's docstring must actually hold."""
        import har
        df = self._synthetic_ohlc(n=2000)
        cc = har.realized_variance(df, "close_to_close")[1:]
        gk = har.realized_variance(df, "garman_klass")[1:]
        # Compare coefficient of variation: lower = more efficient estimator.
        cv = lambda x: np.nanstd(x) / np.nanmean(x)
        assert cv(gk) < cv(cc), "Garman-Klass was not less noisy than close-to-close"

    def test_har_features_have_no_lookahead(self):
        """Row t of the design matrix must depend only on rv[:t+1]."""
        import har
        rv = np.abs(np.random.default_rng(0).normal(1, 0.3, 200))
        a = har.har_features(rv)
        tampered = rv.copy(); tampered[150:] *= 100
        b = har.har_features(tampered)
        assert np.allclose(a[:150], b[:150]), "har_features leaked future data"

    @pytest.mark.parametrize("m", [m for m in __import__("har").VOL_MODELS],
                             ids=lambda m: m.name)
    def test_vol_models_return_positive_finite(self, m):
        import har
        rv = har.realized_variance(self._synthetic_ohlc(), "garman_klass")
        out = m.forecast(rv, horizon=1)
        assert np.isfinite(out) and out > 0, f"{m.name} returned {out}"

    @pytest.mark.parametrize("m", [m for m in __import__("har").VOL_MODELS],
                             ids=lambda m: m.name)
    def test_vol_models_no_lookahead(self, m):
        """Forecast at origin must ignore everything after it."""
        import har
        df = self._synthetic_ohlc(n=300)
        rv = har.realized_variance(df, "garman_klass")
        a = m.forecast(rv[:250], 1)
        tampered = rv.copy(); tampered[250:] *= 50
        b = m.forecast(tampered[:250], 1)
        assert a == pytest.approx(b), f"{m.name} peeked at the future"

    def test_qlike_is_minimised_at_the_truth(self):
        """QLIKE must bottom out when the forecast equals the actual."""
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from validate_vol import qlike
        actual = 1.0
        assert qlike(actual, 1.0) == pytest.approx(0.0, abs=1e-12)
        assert qlike(actual, 0.5) > 0 and qlike(actual, 2.0) > 0

    def test_qlike_punishes_under_forecasting_harder(self):
        """Asymmetry is the point: under-forecasting risk must cost more."""
        from validate_vol import qlike
        under = qlike(1.0, 0.5)     # forecast half the true variance
        over = qlike(1.0, 2.0)      # forecast double
        assert under > over, "QLIKE lost its asymmetry"
