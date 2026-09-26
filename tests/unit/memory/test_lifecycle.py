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


class TestRecordingWithoutReflection:
    """graph=None: the position is still closed with its P&L, nothing reflects."""

    @pytest.mark.asyncio
    async def test_closes_buy_with_pnl_and_skips_reflection(self, trade_repo):
        buy = _make_open_buy_record(quantity=10)
        trade_repo.find_open_buy.return_value = buy

        await TradeLifecycleManager(trade_repo, None).on_trade(
            _make_sell_result(fill_price=2400.0))

        trade_id, fields = trade_repo.update.call_args_list[0].args
        assert trade_id == buy.id
        assert fields["pnl"] == "-1000.00"
        assert "closed_at" in fields


class TestSignalTracker:
    """Closed positions feed skopaq/learning/tracker.py (the MCP learning tools)."""

    @pytest.mark.asyncio
    async def test_closed_position_is_recorded(self, lifecycle, trade_repo, monkeypatch):
        buy = _make_open_buy_record(quantity=10)
        buy.agent_decision = {"action": "BUY", "confidence": 72}
        trade_repo.find_open_buy.return_value = buy
        recorded = []
        monkeypatch.setattr("skopaq.learning.tracker.record_signal", recorded.append)

        await lifecycle.on_trade(_make_sell_result(fill_price=2600.0))

        record, = recorded
        assert (record.symbol, record.confidence, record.won) == ("RELIANCE", 72, True)
        assert (record.entry_price, record.exit_price, record.pnl) == (2500.0, 2600.0, 1000.0)
        assert record.sector == "Energy"

    @pytest.mark.asyncio
    async def test_tracker_failure_does_not_stop_the_lifecycle(self, lifecycle, trade_repo, graph,
                                                               monkeypatch):
        trade_repo.find_open_buy.return_value = _make_open_buy_record()

        def broken(record):
            raise RuntimeError("no database")

        monkeypatch.setattr("skopaq.learning.tracker.record_signal", broken)
        await lifecycle.on_trade(_make_sell_result())
        graph.reflect.assert_called_once()


# ── Live SELLs: only the filled quantity closes BUY rows ─────────────────────


OPENED = datetime(2026, 9, 25, 4, 0, tzinfo=timezone.utc)


def _live_sell(filled: int, price: float = 2700.0, trade_id=None) -> AnalysisResult:
    return AnalysisResult(
        symbol="RELIANCE", trade_date="2026-09-25",
        signal=TradingSignal(symbol="RELIANCE", exchange=Exchange.NSE, action="SELL",
                             confidence=80, entry_price=price),
        execution=ExecutionResult(success=True, fill_price=price, mode="live",
                                  filled_quantity=Decimal(filled),
                                  requested_quantity=Decimal(filled), outcome="filled"),
        trade_id=trade_id,
    )


def _open_row(quantity: int, **kw) -> TradeRecord:
    kw.setdefault("created_at", OPENED)
    return TradeRecord(id=uuid4(), symbol="RELIANCE", exchange="NSE", side="BUY",
                       quantity=quantity, price=Decimal("2500.00"),
                       fill_price=Decimal("2500.00"), status="COMPLETE", is_paper=False, **kw)


def _closing_updates(repo, trade_id) -> list[dict]:
    return [c.args[1] for c in repo.update.call_args_list
            if c.args[0] == trade_id and "closed_at" in c.args[1]]


@pytest.fixture
def order_alerts_spy(monkeypatch):
    from skopaq.execution import order_alerts
    from tests.unit.execution._fakes import AlertSpy

    spy = AlertSpy()
    monkeypatch.setattr(order_alerts, "_alerter", spy)
    return spy


class TestLiveSell:
    @pytest.mark.asyncio
    async def test_a_partial_exit_saves_the_remainder_before_closing_the_row(self, graph):  # T14
        calls = MagicMock()
        repo = calls.repo
        open_buy = _open_row(10)
        repo.find_open_buy.return_value = open_buy
        repo.insert.side_effect = lambda record: record

        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(3))

        names = [c[0] for c in calls.mock_calls if c[0] in ("repo.insert", "repo.update")]
        assert names[:2] == ["repo.insert", "repo.update"]      # remainder first
        remainder = repo.insert.call_args.args[0]
        assert (remainder.quantity, remainder.order_id, remainder.closed_at) == (7, None, None)
        assert remainder.pnl is None and remainder.is_paper is False
        assert remainder.model_signals["split_from"] == str(open_buy.id)
        assert remainder.model_signals["opened_at"] == OPENED.isoformat()
        assert remainder.entry_reason == f"Remainder of {open_buy.id} after a partial exit"
        [closed] = _closing_updates(repo, open_buy.id)
        assert closed["quantity"] == "3" and closed["pnl"] == "600.00"   # (2700-2500) x 3
        assert closed["exit_reason"] == "Partial exit: sold 3 of 10"
        assert "600.00" in graph.reflect.call_args.args[0]

    @pytest.mark.asyncio
    async def test_a_failed_split_keeps_the_row_open_and_the_next_exit_counts_it(  # T21
            self, graph, order_alerts_spy):
        repo = MagicMock()
        open_buy = _open_row(10)
        repo.find_open_buy.return_value = open_buy
        repo.insert.side_effect = RuntimeError("supabase down")

        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(3))

        assert _closing_updates(repo, open_buy.id) == []        # not closed
        [kept] = [c.args[1] for c in repo.update.call_args_list if c.args[0] == open_buy.id]
        [partial] = kept["model_signals"]["pending_partials"]
        assert (partial["qty"], partial["pnl"], partial["sell_price"]) == ("3", "600.00", "2700.0")
        assert order_alerts_spy.keys("WARNING") == [f"partial-not-split:{open_buy.id}"]

        # The rest (7) is sold later at 2600: the row closes with both parts' P&L
        repo.reset_mock()
        repo.find_open_buy.return_value = _open_row(
            10, model_signals={"pending_partials": [partial]})
        repo.find_open_buy.return_value.id = open_buy.id
        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(7, price=2600.0))

        [closed] = _closing_updates(repo, open_buy.id)
        assert closed["pnl"] == "1300.00"                        # 100 x 7 + 600
        repo.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_sell_closes_several_lots_newest_first(self, graph):   # T22
        repo = MagicMock()
        newer, older = _open_row(5), _open_row(10)
        repo.find_open_buy.side_effect = [newer, older]
        repo.insert.side_effect = lambda record: record
        sell_id = uuid4()

        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(12, trade_id=sell_id))

        [first] = _closing_updates(repo, newer.id)
        assert first["pnl"] == "1000.00" and "quantity" not in first    # all 5
        [second] = _closing_updates(repo, older.id)
        assert second["quantity"] == "7" and second["pnl"] == "1400.00"
        assert repo.insert.call_args.args[0].quantity == 3              # 3 of the older left
        [link] = [c.args[1] for c in repo.update.call_args_list if c.args[0] == sell_id]
        assert link["opening_trade_id"] == str(newer.id) and link["pnl"] == "2400.00"
        graph.reflect.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_full_live_exit_closes_the_row_as_before(self, graph):   # T23
        repo = MagicMock()
        open_buy = _open_row(10)
        repo.find_open_buy.return_value = open_buy

        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(10))

        [closed] = _closing_updates(repo, open_buy.id)
        assert set(closed) == {"closed_at", "pnl", "exit_reason"}
        assert closed["pnl"] == "2000.00" and closed["exit_reason"] == "Closed by SELL (P&L: 8.00%)"
        repo.insert.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_live_sell_whose_rows_cannot_be_read_is_not_booked(self, graph):
        repo = MagicMock()
        repo.find_open_buy.side_effect = RuntimeError("503 Service Unavailable")

        assert await TradeLifecycleManager(repo, graph).on_trade(_live_sell(8)) is False
        repo.update.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_read_failing_after_a_row_was_closed_is_alerted(self, graph,
                                                                   order_alerts_spy):
        """Booking the SELL again would close the first row twice: it counts as booked,
        and the shares left unbooked are alerted."""
        repo = MagicMock()
        first = _open_row(5)
        repo.find_open_buy.side_effect = [first, RuntimeError("503 Service Unavailable")]

        assert await TradeLifecycleManager(repo, graph).on_trade(_live_sell(8)) is True
        assert len(_closing_updates(repo, first.id)) == 1
        assert "3 sold share(s) not booked" in order_alerts_spy.text(
            "sell-not-booked:RELIANCE")

    @pytest.mark.asyncio
    async def test_selling_more_than_the_open_rows_hold_is_logged(self, graph, caplog):
        repo = MagicMock()
        repo.find_open_buy.side_effect = [_open_row(5), None]
        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(8))
        assert any("3 more than the open BUY rows hold" in r.getMessage()
                   for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_live_sell_closes_live_rows_only(self, graph):
        """A newer open paper row (`skopaq trade --paper`) is never closed by a live exit:
        its P&L would land on the paper side, out of the live loss limits."""
        live_row = _open_row(10)
        paper_row = _open_row(5).model_copy(update={
            "is_paper": True, "price": Decimal("2000.00"), "fill_price": Decimal("2000.00")})
        closed: set = set()

        def find_open_buy(symbol, is_paper=None):
            rows = [paper_row, live_row]                  # newest first
            return next((r for r in rows if r.id not in closed
                         and (is_paper is None or r.is_paper is is_paper)), None)

        def update(trade_id, fields):
            if "closed_at" in fields:
                closed.add(trade_id)

        repo = MagicMock()
        repo.find_open_buy.side_effect = find_open_buy
        repo.update.side_effect = update
        repo.insert.side_effect = lambda record: record

        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(10))

        assert _closing_updates(repo, paper_row.id) == []
        [row] = _closing_updates(repo, live_row.id)
        assert row["pnl"] == "2000.00"                   # (2700 - 2500) x 10

    @pytest.mark.asyncio
    async def test_a_paper_sell_closes_the_whole_row_as_before(self, graph):   # T24
        repo = MagicMock()
        open_buy = _make_open_buy_record(quantity=10)
        repo.find_open_buy.return_value = open_buy
        result = _make_sell_result(fill_price=2700.0)
        result.signal.quantity = Decimal(3)          # paper: the whole BUY row closes

        await TradeLifecycleManager(repo, graph).on_trade(result)

        [closed] = _closing_updates(repo, open_buy.id)
        assert closed["pnl"] == "2000.00"
        repo.insert.assert_not_called()


class TestLiveSellReviewFixes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("execution", [
        # refused before the broker: safety, the lock, the router's own failure
        ExecutionResult(success=False, mode="live", safety_passed=False,
                        rejection_reason="Cannot read the broker's order book (HTTP 503)"),
        ExecutionResult(success=False, mode="live",
                        rejection_reason="Another Skopaq process is already selling"),
        ExecutionResult(success=False, mode="live", rejection_reason="Broker error: down"),
        # reached the broker but nothing (or nothing known) filled
        ExecutionResult(success=False, mode="live", filled_quantity=Decimal(0),
                        outcome="cancelled"),
        ExecutionResult(success=True, mode="live", fill_price=80.0),   # no confirmed quantity
    ])
    async def test_a_live_sell_that_sold_nothing_closes_no_row(self, graph, execution):
        repo = MagicMock()
        open_buy = _open_row(5)
        repo.find_open_buy.return_value = open_buy
        result = AnalysisResult(
            symbol="RELIANCE", trade_date="2026-09-25",
            signal=TradingSignal(symbol="RELIANCE", exchange=Exchange.NSE, action="SELL",
                                 confidence=80, entry_price=80.0),
            execution=execution)

        await TradeLifecycleManager(repo, graph).on_trade(result)

        assert _closing_updates(repo, open_buy.id) == []
        repo.insert.assert_not_called()
        graph.reflect.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_refused_sell_through_run_lifecycle_leaves_the_buy_open(self, graph):
        from skopaq.cli.main import _run_lifecycle

        repo = MagicMock()
        open_buy = _open_row(5)
        repo.find_open_buy.return_value = open_buy
        config = MagicMock(supabase_url="https://x.supabase.co", asset_class="equity",
                           reflection_enabled=False)
        config.supabase_service_key.get_secret_value.return_value = "k"
        refused = AnalysisResult(
            symbol="RELIANCE", trade_date="2026-09-25",
            signal=TradingSignal(symbol="RELIANCE", exchange=Exchange.NSE, action="SELL",
                                 confidence=80, entry_price=80.0, quantity=Decimal(5)),
            execution=ExecutionResult(success=False, mode="live", safety_passed=False,
                                      rejection_reason="Cannot read the broker's order book"))
        with patch("supabase.create_client", return_value=MagicMock()), \
             patch("skopaq.db.repositories.TradeRepository", return_value=repo):
            await _run_lifecycle(config, None, None, refused)

        assert _closing_updates(repo, open_buy.id) == []

    @pytest.mark.asyncio
    async def test_a_kept_partial_is_tracked_and_reflected_once(self, graph, monkeypatch):
        recorded = []
        monkeypatch.setattr("skopaq.learning.tracker.record_signal",
                            lambda rec: recorded.append(rec.pnl))
        repo = MagicMock()
        open_buy = _open_row(10)
        repo.find_open_buy.return_value = open_buy
        repo.insert.side_effect = RuntimeError("supabase down")
        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(3))      # +600
        [kept] = [c.args[1] for c in repo.update.call_args_list if c.args[0] == open_buy.id]
        partial = kept["model_signals"]["pending_partials"][0]

        repo.reset_mock()
        again = _open_row(10, model_signals={"pending_partials": [partial]})
        again.id = open_buy.id
        repo.find_open_buy.return_value = again
        sell_id = uuid4()
        await TradeLifecycleManager(repo, graph).on_trade(
            _live_sell(7, price=2600.0, trade_id=sell_id))                     # +700

        assert recorded == [600.0, 700.0]                  # the position's P&L is 1300
        reflected = [c.args[0].split("Realized P&L: ")[1].split(" ")[0]
                     for c in graph.reflect.call_args_list]
        assert reflected == ["600.00", "700.00"]
        [closed] = _closing_updates(repo, open_buy.id)
        assert closed["pnl"] == "1300.00"                  # the BUY row books both parts
        [link] = [c.args[1] for c in repo.update.call_args_list if c.args[0] == sell_id]
        assert link["pnl"] == "700.00"                     # this SELL's own P&L

    @pytest.mark.asyncio
    async def test_a_saved_remainder_is_undone_when_the_row_cannot_be_closed(
            self, graph, order_alerts_spy):
        repo = MagicMock()
        open_buy = _open_row(10)
        repo.find_open_buy.return_value = open_buy
        remainder_id = uuid4()

        def insert(record):
            return record.model_copy(update={"id": remainder_id})

        def update(trade_id, fields):
            if "closed_at" in fields:
                raise RuntimeError("HTTP 503 from Supabase")

        repo.insert.side_effect = insert
        repo.update.side_effect = update
        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(3))

        assert len(_closing_updates(repo, open_buy.id)) == 2   # tried twice
        repo.delete.assert_called_once_with(remainder_id)      # no duplicated open shares
        assert f"partial-not-split:{open_buy.id}" in order_alerts_spy.keys()

    @pytest.mark.asyncio
    async def test_a_retried_close_needs_no_undo(self, graph):
        repo = MagicMock()
        open_buy = _open_row(10)
        repo.find_open_buy.return_value = open_buy
        repo.insert.side_effect = lambda record: record
        failures = iter([RuntimeError("HTTP 503")])

        def update(trade_id, fields):
            if "closed_at" in fields:
                error = next(failures, None)
                if error:
                    raise error

        repo.update.side_effect = update
        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(3))

        assert len(_closing_updates(repo, open_buy.id)) == 2
        repo.delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_row_that_could_not_be_closed_is_never_closed_twice(
            self, graph, order_alerts_spy):
        newer, older = _open_row(5), _open_row(10)
        closed = set()
        repo = MagicMock()

        def find_open_buy(symbol, is_paper=None):
            return next((r for r in (newer, older) if r.id not in closed), None)

        def update(trade_id, fields):
            if "closed_at" in fields:
                if trade_id == newer.id:
                    raise RuntimeError("HTTP 503 from Supabase")
                closed.add(trade_id)

        repo.find_open_buy.side_effect = find_open_buy
        repo.update.side_effect = update
        repo.insert.side_effect = lambda record: record
        await TradeLifecycleManager(repo, graph).on_trade(_live_sell(12))

        assert _closing_updates(repo, older.id) == []      # not given the newer row's shares
        repo.insert.assert_not_called()
        assert [k for k in order_alerts_spy.keys() if k.startswith("sell-not-booked:")]
