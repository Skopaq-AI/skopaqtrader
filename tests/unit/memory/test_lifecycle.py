"""Tests for TradeLifecycleManager — BUY → SELL linkage + auto-reflection.

Validates that:
- BUY trades log but don't trigger reflection (no P&L yet)
- SELL trades find the open BUY, compute P&L, trigger reflection
- HOLD signals are no-ops
- Error results are no-ops
- Missing open BUY is handled gracefully (no crash)
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest

from skopaq.broker.models import Exchange, ExecutionResult, TradingSignal
from skopaq.db.models import TradeRecord
from skopaq.graph.skopaq_graph import AnalysisResult
from skopaq.memory.lifecycle import TradeLifecycleManager, _format_returns


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def trade_repo():
    """Mock TradeRepository."""
    repo = MagicMock()
    repo.find_open_buy.return_value = None
    repo.update.return_value = None
    return repo


@pytest.fixture
def graph():
    """Mock SkopaqTradingGraph with reflect() method."""
    g = MagicMock()
    g.reflect.return_value = None
    return g


@pytest.fixture
def memory_store():
    """Mock MemoryStore."""
    store = MagicMock()
    store.save.return_value = 5
    return store


@pytest.fixture
def lifecycle(trade_repo, graph, memory_store):
    """Fully wired TradeLifecycleManager."""
    return TradeLifecycleManager(trade_repo, graph, memory_store)


def _make_buy_result(symbol: str = "RELIANCE") -> AnalysisResult:
    """Build an AnalysisResult for a BUY signal."""
    return AnalysisResult(
        symbol=symbol,
        trade_date="2025-06-01",
        signal=TradingSignal(
            symbol=symbol,
            exchange=Exchange.NSE,
            action="BUY",
            confidence=75,
            entry_price=2500.0,
        ),
        execution=ExecutionResult(
            success=True,
            fill_price=2500.0,
            mode="paper",
        ),
    )


def _make_sell_result(
    symbol: str = "RELIANCE",
    fill_price: float = 2700.0,
    trade_id: Optional[UUID] = None,
) -> AnalysisResult:
    """Build an AnalysisResult for a SELL signal."""
    exec_result = ExecutionResult(
        success=True,
        fill_price=fill_price,
        mode="paper",
    )

    return AnalysisResult(
        symbol=symbol,
        trade_date="2025-06-15",
        signal=TradingSignal(
            symbol=symbol,
            exchange=Exchange.NSE,
            action="SELL",
            confidence=80,
            entry_price=fill_price,
        ),
        execution=exec_result,
        trade_id=trade_id,  # Set on AnalysisResult (populated by _run_lifecycle)
    )


def _make_hold_result(symbol: str = "RELIANCE") -> AnalysisResult:
    """Build an AnalysisResult for a HOLD signal."""
    return AnalysisResult(
        symbol=symbol,
        trade_date="2025-06-01",
        signal=TradingSignal(
            symbol=symbol,
            exchange=Exchange.NSE,
            action="HOLD",
            confidence=40,
        ),
    )


def _make_error_result(symbol: str = "RELIANCE") -> AnalysisResult:
    """Build an AnalysisResult with an error."""
    return AnalysisResult(
        symbol=symbol,
        trade_date="2025-06-01",
        error="LLM quota exceeded",
    )


def _make_open_buy_record(
    symbol: str = "RELIANCE",
    price: Decimal = Decimal("2500.00"),
    fill_price: Optional[Decimal] = Decimal("2500.00"),
    quantity: int = 10,
) -> TradeRecord:
    """Build a TradeRecord representing an open BUY position."""
    return TradeRecord(
        id=uuid4(),
        symbol=symbol,
        exchange="NSE",
        side="BUY",
        quantity=quantity,
        price=price,
        fill_price=fill_price,
        status="FILLED",
        is_paper=True,
    )


# ── Tests: on_trade dispatch ────────────────────────────────────────────────


class TestOnTrade:
    """Tests for the on_trade() dispatcher."""

    @pytest.mark.asyncio
    async def test_hold_is_noop(self, lifecycle, trade_repo, graph):
        """HOLD signal should do absolutely nothing."""
        result = _make_hold_result()

        await lifecycle.on_trade(result)

        trade_repo.find_open_buy.assert_not_called()
        graph.reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_error_result_is_noop(self, lifecycle, trade_repo, graph):
        """Error results should be skipped entirely."""
        result = _make_error_result()

        await lifecycle.on_trade(result)

        trade_repo.find_open_buy.assert_not_called()
        graph.reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_signal_is_noop(self, lifecycle, trade_repo, graph):
        """Result with no signal should be skipped."""
        result = AnalysisResult(
            symbol="RELIANCE",
            trade_date="2025-06-01",
            signal=None,
        )

        await lifecycle.on_trade(result)

        trade_repo.find_open_buy.assert_not_called()
        graph.reflect.assert_not_called()


# ── Tests: BUY handling ─────────────────────────────────────────────────────


class TestHandleBuy:
    """Tests for BUY trade handling."""

    @pytest.mark.asyncio
    async def test_buy_does_not_trigger_reflection(self, lifecycle, graph):
        """BUY should NOT call reflect() — no P&L outcome yet."""
        result = _make_buy_result()

        await lifecycle.on_trade(result)

        graph.reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_buy_does_not_query_repository(self, lifecycle, trade_repo):
        """BUY should NOT call find_open_buy() — nothing to close."""
        result = _make_buy_result()

        await lifecycle.on_trade(result)

        trade_repo.find_open_buy.assert_not_called()


# ── Tests: SELL handling ────────────────────────────────────────────────────


class TestHandleSell:
    """Tests for SELL trade handling (the core of lifecycle management)."""

    @pytest.mark.asyncio
    async def test_sell_triggers_reflection(self, lifecycle, trade_repo, graph):
        """SELL with matching BUY should trigger graph.reflect()."""
        open_buy = _make_open_buy_record()
        trade_repo.find_open_buy.return_value = open_buy

        result = _make_sell_result(fill_price=2700.0)

        await lifecycle.on_trade(result)

        graph.reflect.assert_called_once()
        # Verify the reflect argument contains P&L info
        reflect_arg = graph.reflect.call_args[0][0]
        assert "RELIANCE" in reflect_arg
        assert "PROFIT" in reflect_arg
        # The symbol lets the graph settle that ticker's pending decisions,
        # the opening decision with the realized return
        assert graph.reflect.call_args.kwargs["symbol"] == "RELIANCE"
        buy_price = float(open_buy.fill_price or open_buy.price)
        assert graph.reflect.call_args.kwargs["realized_return"] == pytest.approx(
            (2700.0 - buy_price) / buy_price
        )

    @pytest.mark.asyncio
    async def test_sell_computes_correct_pnl(self, lifecycle, trade_repo, graph):
        """P&L should be (sell_price - buy_price) * quantity."""
        open_buy = _make_open_buy_record(
            price=Decimal("2500.00"),
            fill_price=Decimal("2500.00"),
            quantity=10,
        )
        trade_repo.find_open_buy.return_value = open_buy

        result = _make_sell_result(fill_price=2700.0)

        await lifecycle.on_trade(result)

        reflect_arg = graph.reflect.call_args[0][0]
        # P&L = (2700 - 2500) * 10 = 2000
        assert "2000.00" in reflect_arg
        assert "PROFIT" in reflect_arg

    @pytest.mark.asyncio
    async def test_sell_loss_scenario(self, lifecycle, trade_repo, graph):
        """SELL at a loss should reflect LOSS outcome."""
        open_buy = _make_open_buy_record(
            price=Decimal("2500.00"),
            fill_price=Decimal("2500.00"),
            quantity=10,
        )
        trade_repo.find_open_buy.return_value = open_buy

        result = _make_sell_result(fill_price=2300.0)

        await lifecycle.on_trade(result)

        reflect_arg = graph.reflect.call_args[0][0]
        # P&L = (2300 - 2500) * 10 = -2000
        assert "-2000.00" in reflect_arg
        assert "LOSS" in reflect_arg

    @pytest.mark.asyncio
    async def test_sell_marks_buy_as_closed(self, lifecycle, trade_repo, graph):
        """SELL should update the BUY record with closed_at timestamp."""
        open_buy = _make_open_buy_record()
        trade_repo.find_open_buy.return_value = open_buy

        result = _make_sell_result()

        await lifecycle.on_trade(result)

        # Verify update was called on the BUY trade with a closed_at key
        calls = trade_repo.update.call_args_list
        close_calls = [
            c for c in calls
            if c[0][0] == open_buy.id and "closed_at" in c[0][1]
        ]
        assert len(close_calls) == 1, f"Expected one close call for BUY, got: {calls}"

    @pytest.mark.asyncio
    async def test_sell_without_open_buy_skips_reflection(self, lifecycle, trade_repo, graph):
        """SELL without matching BUY should NOT trigger reflection."""
        trade_repo.find_open_buy.return_value = None

        result = _make_sell_result()

        await lifecycle.on_trade(result)

        graph.reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_sell_survives_find_open_buy_error(self, lifecycle, trade_repo, graph):
        """If find_open_buy() raises, we skip gracefully (no crash)."""
        trade_repo.find_open_buy.side_effect = Exception("DB connection lost")

        result = _make_sell_result()

        await lifecycle.on_trade(result)

        graph.reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_sell_survives_reflect_error(self, lifecycle, trade_repo, graph):
        """If reflect() raises, we log but don't crash."""
        open_buy = _make_open_buy_record()
        trade_repo.find_open_buy.return_value = open_buy
        graph.reflect.side_effect = Exception("LLM timeout")

        result = _make_sell_result()

        # Should not raise
        await lifecycle.on_trade(result)

    @pytest.mark.asyncio
    async def test_sell_uses_fill_price_over_signal_price(self, lifecycle, trade_repo, graph):
        """SELL should prefer execution.fill_price over signal.entry_price."""
        open_buy = _make_open_buy_record(fill_price=Decimal("2500.00"), quantity=10)
        trade_repo.find_open_buy.return_value = open_buy

        # fill_price=2700, but signal entry_price will be different
        result = _make_sell_result(fill_price=2700.0)
        # Override signal entry_price to something else
        result.signal.entry_price = 2650.0

        await lifecycle.on_trade(result)

        reflect_arg = graph.reflect.call_args[0][0]
        # Should use fill_price (2700), not entry_price (2650)
        assert "Exit Price: 2700.0" in reflect_arg

    @pytest.mark.asyncio
    async def test_sell_links_trade_id_to_buy(self, lifecycle, trade_repo, graph):
        """SELL with trade_id should update the SELL record with opening_trade_id."""
        open_buy = _make_open_buy_record()
        trade_repo.find_open_buy.return_value = open_buy

        sell_id = uuid4()
        result = _make_sell_result(fill_price=2700.0, trade_id=sell_id)

        await lifecycle.on_trade(result)

        # Should have called update for SELL trade with opening_trade_id
        link_calls = [
            c for c in trade_repo.update.call_args_list
            if c[0][0] == sell_id and "opening_trade_id" in c[0][1]
        ]
        assert len(link_calls) == 1
        assert link_calls[0][0][1]["opening_trade_id"] == str(open_buy.id)

    @pytest.mark.asyncio
    async def test_sell_stores_pnl_on_buy_record(self, lifecycle, trade_repo, graph):
        """SELL should store P&L on the closing BUY record."""
        open_buy = _make_open_buy_record(
            price=Decimal("2500.00"), fill_price=Decimal("2500.00"), quantity=10,
        )
        trade_repo.find_open_buy.return_value = open_buy

        result = _make_sell_result(fill_price=2700.0)

        await lifecycle.on_trade(result)

        # Find the update call for the BUY trade
        close_calls = [
            c for c in trade_repo.update.call_args_list
            if c[0][0] == open_buy.id and "closed_at" in c[0][1]
        ]
        assert len(close_calls) == 1
        update_data = close_calls[0][0][1]
        assert "pnl" in update_data
        assert update_data["pnl"] == "2000.00"  # (2700-2500)*10

    @pytest.mark.asyncio
    async def test_sell_stores_pnl_on_sell_record(self, lifecycle, trade_repo, graph):
        """SELL should store P&L on the SELL trade record."""
        open_buy = _make_open_buy_record(
            price=Decimal("2500.00"), fill_price=Decimal("2500.00"), quantity=10,
        )
        trade_repo.find_open_buy.return_value = open_buy

        sell_id = uuid4()
        result = _make_sell_result(fill_price=2700.0, trade_id=sell_id)

        await lifecycle.on_trade(result)

        # Find the update call for the SELL trade
        link_calls = [
            c for c in trade_repo.update.call_args_list
            if c[0][0] == sell_id and "pnl" in c[0][1]
        ]
        assert len(link_calls) == 1
        assert link_calls[0][0][1]["pnl"] == "2000.00"

    @pytest.mark.asyncio
    async def test_sell_without_trade_id_skips_linkage(self, lifecycle, trade_repo, graph):
        """SELL without trade_id should still reflect but skip DB linkage."""
        open_buy = _make_open_buy_record()
        trade_repo.find_open_buy.return_value = open_buy

        # No trade_id — trade wasn't persisted (e.g., Supabase down)
        result = _make_sell_result(fill_price=2700.0, trade_id=None)

        await lifecycle.on_trade(result)

        # Reflection should still happen
        graph.reflect.assert_called_once()
        # But only 1 update call (for the BUY close), not 2 (no SELL link)
        assert trade_repo.update.call_count == 1


# ── Tests: _format_returns ──────────────────────────────────────────────────


class TestFormatReturns:
    """Tests for the P&L formatting helper."""

    def test_profit_format(self):
        result = _format_returns(
            symbol="RELIANCE",
            pnl=Decimal("2000.00"),
            pnl_pct=Decimal("8.00"),
            buy_price=Decimal("2500.00"),
            sell_price=2700.0,
        )

        assert "Symbol: RELIANCE" in result
        assert "Entry Price: 2500.00" in result
        assert "Exit Price: 2700.0" in result
        assert "2000.00 INR" in result
        assert "8.00%" in result
        assert "PROFIT" in result

    def test_loss_format(self):
        result = _format_returns(
            symbol="TCS",
            pnl=Decimal("-500.00"),
            pnl_pct=Decimal("-2.50"),
            buy_price=Decimal("4000.00"),
            sell_price=3900.0,
        )

        assert "Symbol: TCS" in result
        assert "LOSS" in result

    def test_breakeven_format(self):
        result = _format_returns(
            symbol="INFY",
            pnl=Decimal("0"),
            pnl_pct=Decimal("0"),
            buy_price=Decimal("1500.00"),
            sell_price=1500.0,
        )

        assert "BREAKEVEN" in result

    def test_none_prices(self):
        """Should handle None prices gracefully."""
        result = _format_returns(
            symbol="WIPRO",
            pnl=Decimal("0"),
            pnl_pct=Decimal("0"),
            buy_price=None,
            sell_price=None,
        )

        assert "Symbol: WIPRO" in result
        assert "Entry Price: None" in result
