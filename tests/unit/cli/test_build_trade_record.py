"""Tests for _build_trade_record() — AnalysisResult → TradeRecord mapping.

Validates that the helper correctly converts execution results into
Supabase-ready TradeRecord objects, handling edge cases like:
- HOLD signals (should return None)
- Failed executions (should return None)
- Missing prices/quantities
- Paper vs live mode flag
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

import pytest

from skopaq.broker.models import Exchange, ExecutionResult, TradingSignal
from skopaq.db.models import TradeRecord
from skopaq.graph.skopaq_graph import AnalysisResult


# Import the function under test — it's a module-level function in main.py
def _import_build_trade_record():
    """Lazy import to avoid heavy CLI dependencies in tests."""
    import importlib
    mod = importlib.import_module("skopaq.cli.main")
    return mod._build_trade_record


def _make_result(
    action: str = "BUY",
    symbol: str = "RELIANCE",
    confidence: int = 75,
    entry_price: float = 1359.51,
    fill_price: float = 1359.51,
    slippage: float = 1.51,
    brokerage: float = 5.0,
    mode: str = "paper",
    success: bool = True,
    quantity: Optional[int] = 10,
    reasoning: str = "AI detected bullish momentum",
    cache_hits: int = 5,
    cache_misses: int = 3,
    duration: float = 4.2,
) -> AnalysisResult:
    """Build a test AnalysisResult."""
    signal = TradingSignal(
        symbol=symbol,
        exchange=Exchange.NSE,
        action=action,
        confidence=confidence,
        entry_price=entry_price,
        quantity=quantity,
        reasoning=reasoning,
    )

    execution = ExecutionResult(
        success=success,
        fill_price=fill_price,
        slippage=slippage,
        brokerage=brokerage,
        mode=mode,
    ) if success or mode else None

    return AnalysisResult(
        symbol=symbol,
        trade_date="2026-03-03",
        signal=signal if action != "_NONE_" else None,
        execution=execution,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
        duration_seconds=duration,
    )


class TestBuildTradeRecord:
    """Tests for the _build_trade_record() helper function."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.build = _import_build_trade_record()
        # Minimal config mock
        self.config = type("Config", (), {"asset_class": "equity"})()

    def test_buy_creates_valid_record(self):
        """BUY result should produce a valid TradeRecord."""
        result = _make_result(action="BUY", fill_price=1359.51, slippage=1.51)
        record = self.build(result, self.config)

        assert record is not None
        assert isinstance(record, TradeRecord)
        assert record.symbol == "RELIANCE"
        assert record.side == "BUY"
        assert record.quantity == 10
        assert record.fill_price == Decimal("1359.51")
        assert record.slippage == Decimal("1.51")
        assert record.brokerage == Decimal("5.0")
        assert record.is_paper is True
        assert record.status == "COMPLETE"
        assert record.signal_source == "skopaq-ai"
        assert record.consensus_score == 75

    def test_sell_creates_valid_record(self):
        """SELL result should produce a TradeRecord with side='SELL'."""
        result = _make_result(action="SELL", fill_price=1400.0)
        record = self.build(result, self.config)

        assert record is not None
        assert record.side == "SELL"

    def test_hold_returns_none(self):
        """HOLD signals should NOT produce a TradeRecord."""
        result = _make_result(action="HOLD")
        record = self.build(result, self.config)

        assert record is None

    def test_no_signal_returns_none(self):
        """Missing signal should return None."""
        result = _make_result(action="_NONE_")
        record = self.build(result, self.config)

        assert record is None

    def test_failed_execution_returns_none(self):
        """Failed execution should return None."""
        result = _make_result(success=False)
        # Manually set execution to failed
        result.execution = ExecutionResult(
            success=False,
            mode="paper",
            rejection_reason="Safety check failed",
        )
        record = self.build(result, self.config)

        assert record is None

    def test_no_execution_returns_none(self):
        """Missing execution (analysis-only) should return None."""
        result = _make_result()
        result.execution = None
        record = self.build(result, self.config)

        assert record is None

    def test_paper_mode_flag(self):
        """Paper mode execution should set is_paper=True."""
        result = _make_result(mode="paper")
        record = self.build(result, self.config)

        assert record.is_paper is True

    def test_live_mode_flag(self):
        """Live mode execution should set is_paper=False."""
        result = _make_result(mode="live")
        record = self.build(result, self.config)

        assert record.is_paper is False

    def test_entry_reason_populated(self):
        """entry_reason should contain the signal reasoning."""
        result = _make_result(reasoning="Strong RSI divergence")
        record = self.build(result, self.config)

        assert record.entry_reason == "Strong RSI divergence"

    def test_entry_reason_truncated(self):
        """Reasoning longer than 2000 chars should be truncated."""
        long_reasoning = "A" * 5000
        result = _make_result(reasoning=long_reasoning)
        record = self.build(result, self.config)

        assert len(record.entry_reason) == 2000

    def test_model_signals_has_cache_stats(self):
        """model_signals should contain cache hit/miss counts."""
        result = _make_result(cache_hits=10, cache_misses=2, duration=3.5)
        record = self.build(result, self.config)

        assert record.model_signals["cache_hits"] == 10
        assert record.model_signals["cache_misses"] == 2
        assert record.model_signals["duration_seconds"] == 3.5

    def test_agent_decision_has_action_and_confidence(self):
        """agent_decision should capture the action and confidence."""
        result = _make_result(action="BUY", confidence=85)
        record = self.build(result, self.config)

        assert record.agent_decision["action"] == "BUY"
        assert record.agent_decision["confidence"] == 85

    def test_default_quantity_is_one(self):
        """When signal has no quantity, default to 1."""
        result = _make_result(quantity=None)
        record = self.build(result, self.config)

        assert record.quantity == 1

    def test_none_entry_price(self):
        """Market orders may have no entry_price."""
        result = _make_result(entry_price=None)
        # Force entry_price to None
        result.signal.entry_price = None
        record = self.build(result, self.config)

        assert record.price is None

    def test_zero_slippage(self):
        """Zero slippage should produce Decimal('0')."""
        result = _make_result(slippage=0.0)
        record = self.build(result, self.config)

        assert record.slippage == Decimal("0")


class TestLiveTradeRecord:
    """Live rows carry what the broker confirmed: the filled quantity and the order ids."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.build = _import_build_trade_record()
        self.config = type("Config", (), {"asset_class": "equity"})()

    def _live(self, **execution) -> AnalysisResult:
        result = _make_result(action="SELL", quantity=5, mode="live")
        result.execution = ExecutionResult(success=True, mode="live", fill_price=95.0,
                                           **execution)
        return result

    def test_a_partial_fill_records_the_filled_quantity_and_its_orders(self):
        record = self.build(self._live(
            filled_quantity=Decimal(3), requested_quantity=Decimal(5), outcome="partial",
            order_ids=["EQ-1"], fill_price_source="trades"), self.config)

        assert record.quantity == 3 and record.order_id == "EQ-1"
        assert record.model_signals["broker"] == {
            "outcome": "partial", "requested_qty": 5, "fill_price_source": "trades",
            "remaining_open": False, "fill_unconfirmed": False, "order_ids": ["EQ-1"]}
        assert record.model_signals["cache_hits"] == 5      # the rest is as before

    def test_a_multi_order_exit_joins_its_order_ids(self):
        record = self.build(self._live(filled_quantity=Decimal(5), outcome="filled",
                                       order_ids=["EQ-1", "EQ-2"]), self.config)
        assert record.order_id == "EQ-1,EQ-2"

    def test_a_late_fill_row_has_no_order_id(self):
        """trades.order_id is UNIQUE: the late fill's order may already be on a row."""
        record = self.build(self._live(filled_quantity=Decimal(6), outcome="late_fill",
                                       order_ids=["EQ-1"]), self.config)
        assert record.order_id is None and record.quantity == 6
        assert record.model_signals["broker"]["late_fill_of"] == "EQ-1"

    def test_a_paper_row_is_unchanged(self):
        record = self.build(_make_result(mode="paper"), self.config)
        assert record.order_id is None and record.quantity == 10
        assert "broker" not in record.model_signals
