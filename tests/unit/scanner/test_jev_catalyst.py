"""Jev catalyst scoring of scanner candidates (ScannerEngine._score_catalysts)."""

from __future__ import annotations

import json

import pytest

from skopaq.llm.jev import CatalystScore
from skopaq.scanner.engine import ScannerEngine
from skopaq.scanner.models import ScannerCandidate
from skopaq.scanner.watchlist import Watchlist


class FakeJev:
    """Stands in for skopaq.llm.jev.Jev: a fixed CatalystScore per symbol."""

    def __init__(self, scores: dict, min_catalyst_score: float = 1.0):
        self.scores = scores
        self.min_catalyst_score = min_catalyst_score
        self.asked: list[tuple[str, str]] = []

    async def catalyst(self, symbol: str, reason: str):
        self.asked.append((symbol, reason))
        result = self.scores.get(symbol)
        if isinstance(result, Exception):
            raise result
        return result


def _score(value: float, specific_news: float = 0.5) -> CatalystScore:
    return CatalystScore(value, 0.8, specific_news, "jev-1.13.0")


def _candidates(*symbols: str) -> list[ScannerCandidate]:
    return [ScannerCandidate(symbol=s, reason=f"{s} reason") for s in symbols]


def _symbols(candidates) -> list[str]:
    return [c.symbol for c in candidates]


@pytest.mark.asyncio
async def test_weak_catalysts_are_dropped_and_the_rest_ranked():
    jev = FakeJev({"A": _score(1.2), "B": _score(0.4), "C": _score(2.6)})
    engine = ScannerEngine(jev=jev)

    kept = await engine._score_catalysts(_candidates("A", "B", "C"))

    assert _symbols(kept) == ["C", "A"]
    assert kept[0].metrics["catalyst_score"] == 2.6
    assert kept[0].metrics["specific_news"] == 0.5
    assert jev.asked == [("A", "A reason"), ("B", "B reason"), ("C", "C reason")]


@pytest.mark.asyncio
async def test_threshold_zero_only_ranks():
    jev = FakeJev({"A": _score(0.2), "B": _score(2.9)}, min_catalyst_score=0.0)
    kept = await ScannerEngine(jev=jev)._score_catalysts(_candidates("A", "B"))
    assert _symbols(kept) == ["B", "A"]


@pytest.mark.asyncio
async def test_drops_are_logged_as_a_warning(caplog):
    jev = FakeJev({"A": _score(0.2), "B": _score(2.9)})
    with caplog.at_level("WARNING", logger="skopaq.scanner.engine"):
        await ScannerEngine(jev=jev)._score_catalysts(_candidates("A", "B"))
    assert "dropped 1 of 2" in caplog.text


@pytest.mark.asyncio
async def test_named_company_news_breaks_ties():
    jev = FakeJev({"A": _score(2.0, specific_news=0.1), "B": _score(2.0, specific_news=0.9)})
    kept = await ScannerEngine(jev=jev)._score_catalysts(_candidates("A", "B"))
    assert _symbols(kept) == ["B", "A"]


@pytest.mark.asyncio
async def test_multi_source_confluence_still_ranks_first():
    jev = FakeJev({"A": _score(1.1), "B": _score(3.0)})
    candidates = _candidates("A", "B")
    candidates[0].metrics["source_count"] = 2

    kept = await ScannerEngine(jev=jev)._score_catalysts(candidates)

    assert _symbols(kept) == ["A", "B"]


@pytest.mark.asyncio
async def test_unscored_candidates_are_kept_at_the_threshold():
    jev = FakeJev({"A": _score(1.1), "B": None, "C": RuntimeError("boom"), "D": _score(2.5)})
    kept = await ScannerEngine(jev=jev)._score_catalysts(_candidates("A", "B", "C", "D"))

    assert _symbols(kept) == ["D", "A", "B", "C"]
    assert "catalyst_score" not in kept[2].metrics


@pytest.mark.asyncio
async def test_jev_outage_changes_nothing():
    jev = FakeJev({})  # every answer is None
    candidates = _candidates("C", "A", "B")
    kept = await ScannerEngine(jev=jev)._score_catalysts(candidates)
    assert _symbols(kept) == ["C", "A", "B"]


@pytest.mark.asyncio
async def test_without_jev_candidates_pass_through():
    candidates = _candidates("A", "B")
    assert await ScannerEngine()._score_catalysts(candidates) is candidates


@pytest.mark.asyncio
async def test_scan_cycle_filters_before_returning():
    async def quotes(symbols):
        return [{"symbol": s, "ltp": 100, "close": 99, "open": 99, "volume": 10} for s in symbols]

    async def screener(prompt):
        return json.dumps([
            {"symbol": "TCS", "reason": "Order win from a large US bank", "urgency": "normal"},
            {"symbol": "INFY", "reason": "Looks interesting", "urgency": "high"},
        ])

    engine = ScannerEngine(
        watchlist=Watchlist(["TCS", "INFY"]),
        quote_fetcher=quotes,
        llm_screener=screener,
        jev=FakeJev({"TCS": _score(2.4), "INFY": _score(0.3)}),
    )

    candidates = await engine.scan_once()

    assert _symbols(candidates) == ["TCS"]
    assert engine.status["last_candidates"][0]["metrics"]["catalyst_score"] == 2.4
    assert engine.status["screeners"]["jev"] is True
