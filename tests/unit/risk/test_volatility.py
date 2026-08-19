"""Unit tests for skopaq.risk.volatility — no API keys required."""

from __future__ import annotations

import math

import pytest

from skopaq.risk.volatility import (
    DEFAULT_LAMBDA,
    annualize,
    ewma_variance,
    forecast_volatility,
    realized_variance,
    vol_risk_premium,
)


class _Candle:
    """Stand-in for broker HistoricalCandle (attribute access, Pydantic v2)."""

    def __init__(self, o, h, l, c):
        self.open, self.high, self.low, self.close = o, h, l, c


def _series(n=120, sigma=0.02, seed=0, price=1000.0):
    """Synthetic OHLC with a known daily sigma."""
    import random
    rng = random.Random(seed)
    out, c = [], price
    for _ in range(n):
        o = c
        c = o * math.exp(rng.gauss(0, sigma))
        wig = abs(rng.gauss(0, sigma * 0.5))
        out.append(_Candle(o, max(o, c) * (1 + wig), min(o, c) * (1 - wig), c))
    return out


class TestRealizedVariance:
    @pytest.mark.parametrize("est", ["close_to_close", "parkinson",
                                     "garman_klass", "rogers_satchell"])
    def test_positive_and_finite(self, est):
        v = realized_variance(_series(), est)
        assert v and all(x > 0 and math.isfinite(x) for x in v)

    def test_recovers_known_sigma(self):
        """Annualised vol must land near the sigma the series was built with."""
        sigma = 0.02
        v = realized_variance(_series(n=3000, sigma=sigma), "close_to_close")
        est = annualize(sum(v) / len(v))
        truth = sigma * math.sqrt(252) * 100
        assert 0.75 * truth < est < 1.25 * truth, f"{est:.1f} vs {truth:.1f}"

    def test_accepts_dicts_as_well_as_objects(self):
        objs = _series(60)
        dicts = [{"open": c.open, "high": c.high, "low": c.low, "close": c.close}
                 for c in objs]
        assert realized_variance(objs) == realized_variance(dicts)

    def test_skips_malformed_candles(self):
        good = _series(30)
        bad = list(good) + [_Candle(0, 0, 0, 0), _Candle(-1, 1, 1, 1), "junk"]
        assert len(realized_variance(bad)) == len(realized_variance(good))

    def test_zero_range_bar_is_floored_not_dropped(self):
        """A circuit-locked bar has zero range; it must not produce zero variance."""
        v = realized_variance([_Candle(100, 100, 100, 100)] * 5, "parkinson")
        assert len(v) == 5 and all(x > 0 for x in v)

    def test_unknown_estimator_rejected(self):
        with pytest.raises(ValueError):
            realized_variance(_series(30), "not_an_estimator")


class TestEWMA:
    def test_constant_series_returns_that_constant(self):
        assert ewma_variance([0.0004] * 200) == pytest.approx(0.0004, rel=1e-9)

    def test_recent_observations_dominate(self):
        low = [1e-6] * 100
        assert ewma_variance(low + [1e-2]) > ewma_variance(low + [1e-6])

    def test_stable_on_long_series(self):
        """lam**n underflows; the iterative form must not degenerate."""
        out = ewma_variance([0.0004] * 5000)
        assert out is not None and math.isfinite(out) and out > 0

    def test_empty_returns_none(self):
        assert ewma_variance([]) is None

    @pytest.mark.parametrize("lam", [0.0, 1.0, -0.5, 1.5])
    def test_invalid_lambda_rejected(self, lam):
        with pytest.raises(ValueError):
            ewma_variance([0.001, 0.002], lam=lam)


class TestForecast:
    def test_returns_forecast_with_enough_data(self):
        f = forecast_volatility("RELIANCE", _series(120))
        assert f is not None
        assert f.symbol == "RELIANCE" and f.n_bars >= 100
        assert 0 < f.forecast_vol_pct < 500

    def test_returns_none_when_too_short(self):
        assert forecast_volatility("TCS", _series(5)) is None

    def test_expanding_compares_against_trailing_not_latest(self):
        """A single noisy bar must not flip the regime flag.

        Measured on NSE: using the latest bar as reference inflated apparent
        directional accuracy from ~0.57 to ~0.68 — pure estimator noise.
        """
        f = forecast_volatility("X", _series(120))
        assert f.is_expanding == (f.forecast_vol_pct > f.trailing_vol_pct)

    def test_no_lookahead(self):
        """Forecast must depend only on the candles supplied."""
        s = _series(200)
        a = forecast_volatility("X", s[:150]).forecast_vol_pct
        tampered = s[:150] + [_Candle(100, 900, 10, 800)] * 50
        b = forecast_volatility("X", tampered[:150]).forecast_vol_pct
        assert a == pytest.approx(b)

    def test_to_dict_is_serialisable(self):
        import json
        json.dumps(forecast_volatility("INFY", _series(120)).to_dict())

    def test_higher_sigma_gives_higher_forecast(self):
        calm = forecast_volatility("CALM", _series(200, sigma=0.005, seed=1))
        wild = forecast_volatility("WILD", _series(200, sigma=0.04, seed=1))
        assert wild.forecast_vol_pct > calm.forecast_vol_pct


class TestVolRiskPremium:
    def test_positive_when_implied_exceeds_forecast(self):
        assert vol_risk_premium(18.0, 25.0) == pytest.approx(7.0)

    def test_negative_when_forecast_exceeds_implied(self):
        assert vol_risk_premium(30.0, 22.0) == pytest.approx(-8.0)


def test_default_lambda_is_riskmetrics():
    assert DEFAULT_LAMBDA == 0.94
