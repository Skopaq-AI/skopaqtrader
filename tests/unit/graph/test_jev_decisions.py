"""How Jev's answers change entry signals and exit decisions."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from skopaq.agents.sell_analyst import SellDecision, _decide_with_jev
from skopaq.llm.jev import JevVerdict


class FakeJev:
    """Stands in for skopaq.llm.jev.Jev: returns a fixed verdict."""

    def __init__(self, verdict: JevVerdict | None, min_confidence: float = 0.6):
        self.verdict = verdict
        self.min_confidence = min_confidence
        self.asked: list[str] = []

    async def trade_action(self, decision: str):
        self.asked.append(decision)
        return self.verdict

    async def exit_action(self, analysis: str):
        self.asked.append(analysis)
        return self.verdict


def _verdict(choice, probabilities, confidence):
    return JevVerdict(choice, confidence, probabilities, "jev-1.13.0")


# ── Entry signal (SkopaqTradingGraph) ───────────────────────────────────────


def _graph(jev, rating="Overweight"):
    from skopaq.graph.skopaq_graph import SkopaqTradingGraph

    graph = SkopaqTradingGraph({"yfinance_symbol_suffix": ".NS"}, MagicMock(), jev=jev)
    upstream = MagicMock()
    decision_text = f"**Rating**: {rating}\n\n**Confidence**: 90"
    upstream.propagate.return_value = (
        {"final_trade_decision": decision_text,
         "risk_debate_state": {"judge_decision": decision_text}},
        rating,
    )
    graph._graph = upstream
    return graph


@pytest.mark.asyncio
async def test_jev_probability_becomes_confidence():
    jev = FakeJev(_verdict("BUY", {"BUY": 0.71, "HOLD": 0.25, "SELL": 0.04}, 0.55))
    result = await _graph(jev).analyze("RELIANCE", "2026-09-24")

    assert result.signal.action == "BUY"
    assert result.signal.confidence == 71  # not the PM's self-reported 90
    assert jev.asked == ["**Rating**: Overweight\n\n**Confidence**: 90"]
    assert result.signal.reasoning.startswith("[Jev jev-1.13.0: BUY")


@pytest.mark.asyncio
async def test_confident_contradiction_downgrades_to_hold():
    jev = FakeJev(_verdict("HOLD", {"BUY": 0.1, "HOLD": 0.85, "SELL": 0.05}, 0.8))
    result = await _graph(jev).analyze("RELIANCE", "2026-09-24")

    assert result.signal.action == "HOLD"
    assert result.signal.confidence == 85


@pytest.mark.asyncio
async def test_uncertain_contradiction_keeps_action_with_low_confidence():
    jev = FakeJev(_verdict("HOLD", {"BUY": 0.4, "HOLD": 0.45, "SELL": 0.15}, 0.2))
    result = await _graph(jev).analyze("RELIANCE", "2026-09-24")

    assert result.signal.action == "BUY"
    assert result.signal.confidence == 40


@pytest.mark.asyncio
async def test_jev_never_turns_hold_into_a_trade():
    jev = FakeJev(_verdict("BUY", {"BUY": 0.9, "HOLD": 0.08, "SELL": 0.02}, 0.9))
    result = await _graph(jev, rating="Hold").analyze("RELIANCE", "2026-09-24")

    assert result.signal.action == "HOLD"
    assert result.signal.confidence == 8


@pytest.mark.asyncio
async def test_jev_failure_keeps_pm_confidence():
    result = await _graph(FakeJev(None)).analyze("RELIANCE", "2026-09-24")

    assert result.signal.action == "BUY"
    assert result.signal.confidence == 90


@pytest.mark.asyncio
async def test_without_jev_nothing_changes(monkeypatch):
    monkeypatch.setattr("skopaq.llm.jev.get_jev", lambda: None)
    result = await _graph(None).analyze("RELIANCE", "2026-09-24")

    assert result.signal.confidence == 90


# ── Exit decision (sell analyst) ─────────────────────────────────────────────


LLM_SAYS_HOLD = SellDecision(action="HOLD", confidence=70, reasoning="Trend intact")
LLM_SAYS_SELL = SellDecision(action="SELL", confidence=80, reasoning="MACD crossed down")


@pytest.mark.asyncio
async def test_confident_jev_sell_overrides_llm_hold():
    jev = FakeJev(_verdict("SELL", {"SELL": 0.88, "HOLD": 0.12}, 0.76))
    decision = await _decide_with_jev(LLM_SAYS_HOLD, "analysis text", jev)

    assert decision.action == "SELL"
    assert decision.confidence == 88
    assert decision.reasoning.endswith("Trend intact")
    assert jev.asked == ["analysis text"]


@pytest.mark.asyncio
async def test_unconfident_jev_sell_becomes_hold():
    jev = FakeJev(_verdict("SELL", {"SELL": 0.55, "HOLD": 0.45}, 0.1))
    decision = await _decide_with_jev(LLM_SAYS_SELL, "analysis text", jev)

    assert decision.action == "HOLD"
    assert decision.confidence == 45


@pytest.mark.asyncio
async def test_exit_falls_back_to_llm_when_jev_fails():
    decision = await _decide_with_jev(LLM_SAYS_SELL, "analysis text", FakeJev(None))
    assert decision == LLM_SAYS_SELL


@pytest.mark.asyncio
async def test_exit_without_jev_keeps_llm_decision(monkeypatch):
    monkeypatch.setattr("skopaq.llm.jev.get_jev", lambda: None)
    assert await _decide_with_jev(LLM_SAYS_SELL, "analysis text") == LLM_SAYS_SELL


@pytest.mark.asyncio
async def test_broken_jev_setup_never_fails_the_analysis(monkeypatch):
    def broken():
        raise ValueError("bad SKOPAQ_JEV_MIN_CONFIDENCE")

    monkeypatch.setattr("skopaq.llm.jev.get_jev", broken)
    result = await _graph(None).analyze("RELIANCE", "2026-09-24")

    assert result.error is None
    assert result.signal.confidence == 90
