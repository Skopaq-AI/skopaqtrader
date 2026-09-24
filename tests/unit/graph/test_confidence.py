"""Tests for confidence extraction from the risk debate state.

Covers the 4-priority extraction pipeline in ``skopaq.graph.skopaq_graph``:
1. Parse ``CONFIDENCE: N`` from judge_decision text
2. Explicit dict keys (forward-compatible)
3. Debater agreement heuristic
4. Default fallback (50)
"""

from __future__ import annotations

import pytest

from skopaq.graph.skopaq_graph import _extract_confidence, _estimate_agreement


# ── Priority 1: Parse from judge_decision text ─────────────────────────


class TestParseConfidenceFromText:
    """CONFIDENCE: N parsing from judge_decision."""

    def test_standard_format(self):
        risk_state = {"judge_decision": "I recommend BUY.\nCONFIDENCE: 75"}
        assert _extract_confidence(risk_state) == 75

    def test_case_insensitive(self):
        risk_state = {"judge_decision": "Some reasoning.\nconfidence: 82"}
        assert _extract_confidence(risk_state) == 82

    def test_extra_whitespace(self):
        risk_state = {"judge_decision": "Text here.\nCONFIDENCE :  60"}
        assert _extract_confidence(risk_state) == 60

    def test_mid_text(self):
        """CONFIDENCE line embedded in the middle of text."""
        risk_state = {
            "judge_decision": "First paragraph.\nCONFIDENCE: 88\nMore text after."
        }
        assert _extract_confidence(risk_state) == 88

    def test_clamps_above_100(self):
        risk_state = {"judge_decision": "CONFIDENCE: 150"}
        assert _extract_confidence(risk_state) == 100

    def test_zero_is_valid(self):
        risk_state = {"judge_decision": "CONFIDENCE: 0"}
        assert _extract_confidence(risk_state) == 0

    def test_ignores_prose_without_colon(self):
        """'with high confidence in the market' should NOT match."""
        risk_state = {
            "judge_decision": "I say BUY with high confidence in the market"
        }
        # No colon-number format → falls through to fallback
        assert _extract_confidence(risk_state) == 50

    def test_gemini_list_format(self):
        """Gemini 3 returns content as list of dicts."""
        risk_state = {
            "judge_decision": [
                {"type": "text", "text": "Buy RELIANCE.\nCONFIDENCE: 72"}
            ]
        }
        assert _extract_confidence(risk_state) == 72


# ── Priority 2: Explicit dict keys ─────────────────────────────────────


class TestExplicitDictKeys:
    """Forward-compatible: explicit confidence/score/certainty keys."""

    def test_confidence_key(self):
        risk_state = {"confidence": 65}
        assert _extract_confidence(risk_state) == 65

    def test_score_key(self):
        risk_state = {"score": 80}
        assert _extract_confidence(risk_state) == 80

    def test_certainty_key(self):
        risk_state = {"certainty": 40.7}
        assert _extract_confidence(risk_state) == 40

    def test_clamps_dict_value(self):
        risk_state = {"confidence": 200}
        assert _extract_confidence(risk_state) == 100

    def test_text_takes_priority_over_dict(self):
        """Judge text CONFIDENCE line should win over dict key."""
        risk_state = {
            "judge_decision": "CONFIDENCE: 90",
            "confidence": 30,
        }
        assert _extract_confidence(risk_state) == 90


# ── Priority 3: Debater agreement heuristic ────────────────────────────


class TestDebaterHeuristic:
    """Heuristic based on debater agreement when no explicit confidence."""

    def test_unanimous_buy(self):
        risk_state = {
            "count": 3,
            "current_aggressive_response": "I strongly recommend BUY",
            "current_conservative_response": "Even I agree, BUY is right",
            "current_neutral_response": "The data supports BUY",
        }
        assert _extract_confidence(risk_state) == 85  # 35 + 1.0 * 50

    def test_majority_agreement(self):
        risk_state = {
            "count": 3,
            "current_aggressive_response": "BUY now",
            "current_conservative_response": "HOLD for safety",
            "current_neutral_response": "I lean BUY",
        }
        assert _extract_confidence(risk_state) == 65  # 35 + 0.6 * 50

    def test_full_disagreement(self):
        risk_state = {
            "count": 3,
            "current_aggressive_response": "BUY",
            "current_conservative_response": "SELL",
            "current_neutral_response": "HOLD",
        }
        assert _extract_confidence(risk_state) == 50  # 35 + 0.3 * 50

    def test_empty_responses(self):
        risk_state = {
            "count": 3,
            "current_aggressive_response": "",
            "current_conservative_response": "",
            "current_neutral_response": "",
        }
        assert _extract_confidence(risk_state) == 60  # 35 + 0.5 * 50


# ── Priority 4: Default fallback ───────────────────────────────────────


class TestDefaultFallback:
    def test_empty_state(self):
        assert _extract_confidence({}) == 50

    def test_no_judge_no_count(self):
        risk_state = {"history": "some debate history"}
        assert _extract_confidence(risk_state) == 50


# ── _estimate_agreement helper ─────────────────────────────────────────


class TestEstimateAgreement:
    def test_unanimous(self):
        assert _estimate_agreement("BUY", "BUY", "BUY") == 1.0

    def test_majority(self):
        assert _estimate_agreement("BUY", "BUY", "HOLD") == 0.6

    def test_split(self):
        assert _estimate_agreement("BUY", "SELL", "HOLD") == 0.3

    def test_empty_responses(self):
        assert _estimate_agreement("", "", "") == 0.5

    def test_mixed_case(self):
        assert _estimate_agreement("buy this stock", "BUY now", "definitely buy") == 1.0

    def test_sell_unanimous(self):
        assert _estimate_agreement("SELL it", "SELL now", "SELL everything") == 1.0

    def test_buy_sell_in_same_response_treated_as_ambiguous(self):
        """If response says both BUY and SELL, it's excluded from both counts."""
        # "don't BUY, SELL instead" has both keywords → excluded from buy AND sell
        # Only 2/3 counted as SELL → majority (0.6), not unanimous
        assert _estimate_agreement(
            "don't BUY, SELL instead",
            "SELL now",
            "SELL please",
        ) == 0.6


# ── Upstream v0.5.1 decision format ─────────────────────────────────────────


class TestUpstreamRenderedConfidence:
    """Upstream's Portfolio Manager renders "**Confidence**: N" (markdown bold)."""

    def test_markdown_bold(self):
        risk_state = {"judge_decision": "**Rating**: Buy\n\n**Confidence**: 71"}
        assert _extract_confidence(risk_state) == 71

    def test_not_provided_falls_back(self):
        risk_state = {"judge_decision": "**Rating**: Buy\n\n**Confidence**: not provided"}
        assert _extract_confidence(risk_state) == 50


class TestRatingToAction:
    @pytest.mark.parametrize("rating,action", [
        ("Buy", "BUY"),
        ("Overweight", "BUY"),
        ("Hold", "HOLD"),
        ("Underweight", "SELL"),
        ("Sell", "SELL"),
        ("REVIEW", "HOLD"),
        ("", "HOLD"),
        ("SELLING PRESSURE", "HOLD"),
    ])
    def test_mapping(self, rating, action):
        from skopaq.graph.skopaq_graph import _rating_to_action

        assert _rating_to_action(rating) == action


class TestSkopaqGraphUpstreamCalls:
    """How SkopaqTradingGraph drives the upstream graph."""

    def _graph(self, config, memory_store=None):
        from unittest.mock import MagicMock

        from skopaq.graph.skopaq_graph import SkopaqTradingGraph

        graph = SkopaqTradingGraph(config, MagicMock(), memory_store=memory_store)
        upstream = MagicMock()
        upstream.propagate.return_value = (
            {"risk_debate_state": {"judge_decision": "**Rating**: Overweight\n**Confidence**: 64"}},
            "Overweight",
        )
        graph._graph = upstream
        return graph, upstream

    @pytest.mark.asyncio
    async def test_equity_symbol_gets_nse_suffix(self):
        graph, upstream = self._graph({"yfinance_symbol_suffix": ".NS"})
        result = await graph.analyze("RELIANCE", "2026-09-24")

        upstream.propagate.assert_called_once_with("RELIANCE.NS", "2026-09-24", asset_type="stock")
        assert result.signal.symbol == "RELIANCE"
        assert result.signal.action == "BUY"
        assert result.signal.confidence == 64

    @pytest.mark.asyncio
    async def test_crypto_symbol_passed_as_is(self):
        graph, upstream = self._graph({"asset_class": "crypto", "yfinance_symbol_suffix": ""})
        await graph.analyze("BTC-USD", "2026-09-24")

        upstream.propagate.assert_called_once_with("BTC-USD", "2026-09-24", asset_type="crypto")

    @pytest.mark.asyncio
    async def test_decision_log_saved_after_analysis(self):
        from unittest.mock import MagicMock

        store = MagicMock()
        graph, upstream = self._graph({"yfinance_symbol_suffix": ".NS"}, memory_store=store)
        await graph.analyze("TCS", "2026-09-24")

        store.save.assert_called_once_with(upstream)

    def test_reflect_settles_symbol(self):
        from unittest.mock import MagicMock

        store = MagicMock()
        graph, upstream = self._graph({"yfinance_symbol_suffix": ".NS"}, memory_store=store)
        graph.reflect("Realized P&L: 120 INR", symbol="INFY")

        upstream.settle_pending.assert_called_once_with("INFY.NS")
        store.save.assert_called_once_with(upstream)

    def test_reflect_without_symbol_is_noop(self):
        graph, upstream = self._graph({})
        graph.reflect("Realized P&L: 120 INR")

        upstream.settle_pending.assert_not_called()
