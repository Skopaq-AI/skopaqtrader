"""Unit tests for OrderRouter — paper/live dispatch and security_id resolution."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skopaq.broker.models import (
    ExecutionResult,
    OrderRequest,
    OrderResponse,
    Side,
    TradingSignal,
)
from skopaq.broker.paper_engine import PaperEngine
from skopaq.execution.order_router import OrderRouter
from tests.unit.execution._fakes import FakeClient, FakeClock, Script

SUCCESS = {"status": "SUCCESS", "traded_qty": 10, "traded_price": "1799.5"}


def _make_config(mode: str = "paper") -> MagicMock:
    cfg = MagicMock()
    cfg.trading_mode = mode
    cfg.initial_paper_capital = 1_000_000.0
    return cfg


def _buy_order(symbol: str = "RELIANCE", security_id: str = "") -> OrderRequest:
    return OrderRequest(
        symbol=symbol,
        side=Side.BUY,
        quantity=Decimal("10"),
        price=1800.0,
        security_id=security_id,
    )


def _signal() -> TradingSignal:
    return TradingSignal(
        symbol="RELIANCE",
        action="BUY",
        confidence=72,
        entry_price=1800.0,
    )


class TestPaperRouting:
    """Paper mode routes everything to the paper engine."""

    def test_paper_mode_uses_paper_engine(self):
        config = _make_config("paper")
        paper = PaperEngine(initial_capital=1_000_000)
        router = OrderRouter(config, paper)
        assert router.mode == "paper"

    def test_paper_mode_ignores_live_client(self):
        config = _make_config("paper")
        paper = PaperEngine(initial_capital=1_000_000)
        live = MagicMock()
        router = OrderRouter(config, paper, live_client=live)
        # Even with live_client, paper mode should use paper engine
        assert router.mode == "paper"


class TestLiveRouting:
    """Live mode routes to INDstocks client."""

    @pytest.mark.asyncio
    async def test_live_fallback_when_no_client(self):
        """If live_client is None, falls back to paper."""
        config = _make_config("live")
        paper = PaperEngine(initial_capital=1_000_000)
        # Inject a quote so paper fill works
        from skopaq.broker.models import Quote
        paper.update_quote(Quote(symbol="RELIANCE", ltp=1800.0))

        router = OrderRouter(config, paper, live_client=None)
        result = await router.execute(_buy_order(), _signal())
        # Should succeed via paper fallback
        assert result.success
        assert result.mode == "paper"

    @pytest.mark.asyncio
    async def test_live_resolves_security_id(self):
        """Live mode should resolve security_id if empty."""
        config = _make_config("live")
        paper = PaperEngine(initial_capital=1_000_000)

        clock = FakeClock()
        live = FakeClient(clock)
        live.place_effects = [Script(timeline=[(0, SUCCESS)])]

        router = OrderRouter(config, paper, live_client=live,
                             sleep=clock.sleep, clock=clock.clock, wall=clock.wall)

        order = _buy_order(security_id="")  # Empty — needs resolution

        with patch(
            "skopaq.execution.order_router.resolve_security_id",
            new_callable=AsyncMock,
            return_value="10604",
        ) as mock_resolve:
            result = await router.execute(order, _signal())

            mock_resolve.assert_called_once_with(live, "RELIANCE", "NSE")
            assert order.security_id == "10604"
            assert result.success
            assert result.mode == "live"
            # Success is the broker's fill, not its acknowledgement
            assert (result.filled_quantity, result.fill_price) == (10, 1799.5)

    @pytest.mark.asyncio
    async def test_live_skips_resolve_if_security_id_set(self):
        """If security_id is already set, skip resolution."""
        config = _make_config("live")
        paper = PaperEngine(initial_capital=1_000_000)

        clock = FakeClock()
        live = FakeClient(clock)
        live.place_effects = [Script(timeline=[(0, SUCCESS)])]

        router = OrderRouter(config, paper, live_client=live,
                             sleep=clock.sleep, clock=clock.clock, wall=clock.wall)
        order = _buy_order(security_id="10604")

        with patch(
            "skopaq.execution.order_router.resolve_security_id",
            new_callable=AsyncMock,
        ) as mock_resolve:
            result = await router.execute(order, _signal())

            mock_resolve.assert_not_called()
            assert result.success

    @pytest.mark.asyncio
    async def test_live_order_never_filled_is_cancelled_not_reported_filled(self):
        """An acknowledged order that rests is cancelled after the timeout (no real sleep)."""
        clock = FakeClock()
        live = FakeClient(clock)                   # the order stays PENDING until cancelled
        router = OrderRouter(_make_config("live"), PaperEngine(initial_capital=1_000_000),
                             live_client=live, sleep=clock.sleep, clock=clock.clock,
                             wall=clock.wall)

        result = await router.execute(_buy_order(security_id="10604"), _signal())

        assert not result.success
        assert result.rejection_reason == "Not filled within 30s — cancelled at the broker"
        assert "cancel_order" in live.names()
        assert clock.t == 30.0

    @pytest.mark.asyncio
    async def test_live_broker_error_does_not_fallback(self):
        """Broker errors should NOT silently fall back to paper."""
        config = _make_config("live")
        paper = PaperEngine(initial_capital=1_000_000)

        live = AsyncMock()
        live.place_order = AsyncMock(
            side_effect=Exception("Connection refused"),
        )

        # A fixed wall clock: nothing is placed after 15:29:55 IST
        router = OrderRouter(config, paper, live_client=live, wall=FakeClock().wall)
        order = _buy_order(security_id="10604")

        result = await router.execute(order, _signal())
        assert not result.success
        assert result.mode == "live"
        assert "Broker error" in result.rejection_reason


def _live_client(book_rows) -> MagicMock:
    """A mock shaped like the real INDstocksClient (no invented methods)."""
    from skopaq.broker.client import INDstocksClient

    live = MagicMock(spec=INDstocksClient)
    live.get_order_book = AsyncMock(return_value=book_rows)
    return live


class TestOrderQueries:
    """Today's orders and the book-first broker snapshot."""

    @pytest.mark.asyncio
    async def test_live_get_orders_maps_the_order_book(self):
        """INDstocksClient has no get_orders; the router reads the order book instead."""
        live = _live_client([
            {"id": "EQ-1", "status": "success", "traded_qty": 5, "requested_qty": 5,
             "exch_order_id": "1100000017281712", "extra_info": ""},
            {"id": "EQ-2", "status": "FAILED", "exch_order_id": "",
             "extra_info": "RMS: Margin exceeds"},
            {"status": "PENDING"},                     # no id: dropped
        ])
        router = OrderRouter(_make_config("live"), PaperEngine(), live_client=live)
        orders = await router.get_orders()
        assert [(o.order_id, o.status, o.message, o.exchange_order_id) for o in orders] == [
            ("EQ-1", "SUCCESS", "", "1100000017281712"),
            ("EQ-2", "FAILED", "RMS: Margin exceeds", None),
        ]

    @pytest.mark.asyncio
    async def test_paper_get_orders_unchanged(self):
        paper = MagicMock()
        paper.get_orders.return_value = [OrderResponse(order_id="PAPER-1", status="COMPLETE")]
        live = _live_client([])
        router = OrderRouter(_make_config("paper"), paper, live_client=live)
        orders = await router.get_orders()
        assert [o.order_id for o in orders] == ["PAPER-1"]
        live.get_order_book.assert_not_called()

    @pytest.mark.asyncio
    async def test_broker_snapshot_is_none_in_paper_or_without_a_client(self):
        live = _live_client([])
        assert await OrderRouter(_make_config("paper"), PaperEngine(),
                                 live_client=live).broker_snapshot() is None
        assert await OrderRouter(_make_config("live"), PaperEngine()).broker_snapshot() is None
        live.get_order_book.assert_not_called()

    @pytest.mark.asyncio
    async def test_live_broker_snapshot_reads_the_book_first(self):
        calls: list[str] = []
        live = _live_client([])

        async def book():
            calls.append("book")
            return [{"id": "EQ-1", "status": "PENDING", "txn_type": "SELL",
                     "requested_qty": 5, "traded_qty": 0}]

        async def positions():
            calls.append("positions")
            return []

        async def holdings():
            calls.append("holdings")
            return []

        live.get_order_book = AsyncMock(side_effect=book)
        live.get_positions = AsyncMock(side_effect=positions)
        live.get_holdings = AsyncMock(side_effect=holdings)
        router = OrderRouter(_make_config("live"), PaperEngine(), live_client=live)
        snap = await router.broker_snapshot()
        assert calls == ["book", "positions", "holdings"]
        assert [o.order_id for o in snap.orders] == ["EQ-1"]


class TestLiveWorkerWiring:
    """The live order worker exists only in live mode with a live client (paper untouched)."""

    @pytest.mark.asyncio
    async def test_paper_router_with_a_live_client_has_no_worker(self):   # T35
        from skopaq.broker.models import Quote

        paper = PaperEngine(initial_capital=1_000_000)
        paper.update_quote(Quote(symbol="RELIANCE", ltp=1800.0))
        live = MagicMock()
        router = OrderRouter(_make_config("paper"), paper, live_client=live)
        assert router.worker is None

        result = await router.execute(_buy_order(), _signal())
        assert result.success and result.mode == "paper"
        assert (result.filled_quantity, result.outcome, result.order_ids) == (None, "", [])
        assert live.mock_calls == []

    def test_live_router_without_a_client_has_no_worker(self):
        assert OrderRouter(_make_config("live"), PaperEngine()).worker is None

    def test_arm_shutdown_sets_the_live_deadlines(self):
        clock = FakeClock()
        router = OrderRouter(_make_config("live"), PaperEngine(), live_client=FakeClient(clock),
                             sleep=clock.sleep, clock=clock.clock, wall=clock.wall)
        assert router.worker is not None and not router.deadlines.stopping

        router.arm_shutdown(240)
        # Room to cancel and settle the last attempt: cancel window 10 s + 2 polls of 1 s
        assert (router.deadlines.settle_by(), router.deadlines.place_by()) == (240, 228)

    def test_arm_shutdown_is_a_no_op_in_paper(self):
        router = OrderRouter(_make_config("paper"), PaperEngine(), live_client=MagicMock())
        router.arm_shutdown(240)
        assert not router.deadlines.stopping

    def test_the_worker_shares_the_routers_registry(self):
        clock = FakeClock()
        router = OrderRouter(_make_config("live"), PaperEngine(), live_client=FakeClient(clock),
                             wall=clock.wall)
        assert router.worker.registry is router.registry
        assert router.worker.deadlines is router.deadlines


def _sell_order(symbol: str = "TCS", security_id: str = "", qty: int = 5) -> OrderRequest:
    from skopaq.broker.models import OrderType

    return OrderRequest(symbol=symbol, side=Side.SELL, quantity=Decimal(qty),
                        order_type=OrderType.MARKET, security_id=security_id)


class TestSellInputs:
    """GAP 2: positions, holdings and open orders for a SELL's check, read book-first."""

    @staticmethod
    def _router(clock, live, config=None):
        return OrderRouter(config or _make_config("live"), PaperEngine(), live_client=live,
                           sleep=clock.sleep, clock=clock.clock, wall=clock.wall)

    @staticmethod
    def _positions():
        from skopaq.broker.models import Position

        return [Position(symbol="TCS", security_id="11536", quantity=Decimal(10),
                         product="CNC")]

    @pytest.mark.asyncio
    async def test_none_in_paper_or_without_a_client(self):
        live = _live_client([])
        paper_router = OrderRouter(_make_config("paper"), PaperEngine(), live_client=live)
        no_client = OrderRouter(_make_config("live"), PaperEngine())

        assert await paper_router.sell_inputs(_sell_order()) is None
        assert await no_client.sell_inputs(_sell_order()) is None
        assert paper_router.sell_lock(_sell_order()) is None
        assert no_client.sell_lock(_sell_order()) is None
        assert live.mock_calls == []

    @pytest.mark.asyncio
    async def test_resolves_the_security_id_then_reads_book_positions_holdings(self):
        from skopaq.broker.models import Holding
        from tests.unit.execution._fakes import row

        clock = FakeClock()
        live = FakeClient(clock, positions=self._positions(),
                          holdings=[Holding(symbol="TCS", quantity=Decimal(2))])
        live.extra_rows = [row("PENDING", id="EQ-9", requested=4)]
        router = self._router(clock, live)
        order = _sell_order(security_id="")

        with patch("skopaq.execution.order_router.resolve_security_id",
                   new_callable=AsyncMock, return_value="11536") as resolve:
            inputs = await router.sell_inputs(order)

        resolve.assert_awaited_once_with(live, "TCS", "NSE")
        assert order.security_id == "11536"      # book rows have no symbol: match by id
        assert live.names() == ["get_order_book", "get_positions", "get_holdings"]
        context = inputs.context
        assert [o.order_id for o in context.orders] == ["EQ-9"]
        assert (context.error, context.override, context.lag_window_s) == ("", False, 600.0)
        assert context.read_at == clock.wall()
        assert [p.quantity for p in inputs.positions] == [10]
        assert [h.quantity for h in inputs.holdings] == [2]

    @pytest.mark.asyncio
    async def test_the_context_knows_this_processs_orders_and_confirmed_exits(self):
        from skopaq.execution.live_orders import TrackedOrder
        from skopaq.execution.order_journal import OrderJournal

        clock = FakeClock()
        live = FakeClient(clock, positions=self._positions())
        router = self._router(clock, live)
        router.registry.track(TrackedOrder(order_id="EQ-5", side="SELL", symbol="TCS",
                                           security_id="11536", segment="EQUITY",
                                           requested=Decimal(3)))
        router.registry.record_confirmed_exit("TCS", "11536", Decimal(3))
        router.registry.record_confirmed_exit("INFY", "1594", Decimal(7))
        # An order a killed process journalled today (same journal directory)
        OrderJournal.from_config(MagicMock(), wall=clock.wall).record(
            "placed", order_id="EQ-6", symbol="TCS", side="SELL", qty=Decimal(1))

        inputs = await router.sell_inputs(_sell_order(security_id="11536"))

        assert inputs.context.own_order_ids >= {"EQ-5", "EQ-6"}
        assert inputs.context.own_recent_exit_qty == 3

    @pytest.mark.asyncio
    async def test_an_unreadable_book_becomes_the_contexts_error(self):
        from skopaq.broker.client import BrokerError

        clock = FakeClock()
        live = FakeClient(clock, positions=self._positions())
        live.book_error_always = BrokerError("HTTP 503: unavailable", 503, kind="http")
        router = self._router(clock, live)

        inputs = await router.sell_inputs(_sell_order(security_id="11536"))

        assert "HTTP 503" in inputs.context.error
        assert not inputs.context.override and inputs.context.orders == ()
        assert live.names() == ["get_order_book", "get_order_book", "get_positions",
                                "get_holdings"]

    @pytest.mark.asyncio
    async def test_the_override_checks_without_the_book_and_alerts(self, monkeypatch, caplog):
        import logging

        from skopaq.broker.client import BrokerError
        from skopaq.execution import order_alerts
        from tests.unit.execution._fakes import AlertSpy

        spy = AlertSpy()
        monkeypatch.setattr(order_alerts, "_alerter", spy)
        config = _make_config("live")
        config.allow_sell_without_order_book = True
        clock = FakeClock()
        live = FakeClient(clock, positions=self._positions())
        live.book_error_always = BrokerError("HTTP 503", 503, kind="http")
        with caplog.at_level(logging.CRITICAL, logger="skopaq.execution.order_router"):
            router = self._router(clock, live, config)
        assert "SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK is on" in caplog.text   # said at start

        inputs = await router.sell_inputs(_sell_order(security_id="11536"))

        assert inputs.context.override and inputs.context.error == ""
        assert spy.keys("CRITICAL") == ["sell-without-book:TCS"]

    def test_the_override_needs_an_explicit_true(self):
        clock = FakeClock()
        # _make_config is a MagicMock: its allow_sell_without_order_book is a mock, not True
        assert not self._router(clock, FakeClient(clock)).allows_sell_without_order_book

    @pytest.mark.asyncio
    async def test_a_positions_error_propagates(self):
        from skopaq.broker.client import BrokerError

        clock = FakeClock()
        live = FakeClient(clock, positions=BrokerError("HTTP 500", 500, kind="http"))
        with pytest.raises(BrokerError):
            await self._router(clock, live).sell_inputs(_sell_order(security_id="11536"))

    @pytest.mark.asyncio
    async def test_an_unresolved_security_id_still_checks_conservatively(self):
        from tests.unit.execution._fakes import row

        clock = FakeClock()
        live = FakeClient(clock, positions=self._positions())
        live.extra_rows = [row("PENDING", id="EQ-9", requested=4, security_id="1594")]
        order = _sell_order(security_id="")
        with patch("skopaq.execution.order_router.resolve_security_id",
                   new_callable=AsyncMock, side_effect=RuntimeError("CSV download failed")):
            inputs = await self._router(clock, live).sell_inputs(order)

        # Without the id, every open SELL counts against it (it cannot be told apart)
        from skopaq.execution.sellable import sellable_quantity

        view = sellable_quantity(symbol="TCS", security_id=order.security_id, product="CNC",
                                 positions=inputs.positions, holdings=inputs.holdings,
                                 context=inputs.context, order_qty=order.quantity)
        assert (view.pending_qty, view.sellable) == (4, 6)

    def test_sell_lock_waits_at_most_one_exit_and_never_past_settle_by(self, tmp_path):
        from skopaq.execution.sell_lock import SellLock

        clock = FakeClock()
        router = self._router(clock, FakeClient(clock))
        lock = router.sell_lock(_sell_order())

        assert isinstance(lock, SellLock)
        assert lock.path == tmp_path / "locks" / "sell-TCS.lock"   # SKOPAQ_ORDER_LOCK_DIR
        assert lock.wait_s == router.worker.settings.exit_worst_case_s + 10

        router.arm_shutdown(60)
        assert router.sell_lock(_sell_order()).wait_s == 60
        clock.t = 100.0
        assert router.sell_lock(_sell_order()).wait_s == 0
        assert router.sell_lock(_buy_order()) is None


class TestUnusableDirectories:
    """An order-journal or lock directory that cannot be resolved (``~user`` for a user
    this host does not have) never breaks the router: paper ignores both, live runs
    without the journal or the cross-process lock (with a warning)."""

    async def test_paper_never_resolves_the_live_only_directories(self, monkeypatch):
        monkeypatch.setenv("SKOPAQ_ORDER_LOCK_DIR", "~nosuchuser-skopaq/locks")
        monkeypatch.setenv("SKOPAQ_ORDER_JOURNAL_DIR", "~nosuchuser-skopaq/orders")
        router = OrderRouter(_make_config("paper"), PaperEngine(), live_client=MagicMock())
        assert await router.get_orders() == []
        assert router.journal is None and router.sell_lock(_buy_order()) is None

    def test_live_runs_without_the_journal_and_the_lock(self, monkeypatch):
        monkeypatch.setenv("SKOPAQ_ORDER_LOCK_DIR", "~nosuchuser-skopaq/locks")
        monkeypatch.setenv("SKOPAQ_ORDER_JOURNAL_DIR", "~nosuchuser-skopaq/orders")
        router = OrderRouter(_make_config("live"), PaperEngine(), live_client=MagicMock())
        sell = _buy_order().model_copy(update={"side": Side.SELL})
        assert router.worker is not None and router.journal is None
        assert router.sell_lock(sell) is None and router.order_lock("EQ-1") is None


class TestReadsAfterAStop:
    """After a stop, the monitor's and CLOSING's broker reads (``broker_snapshot``) and a
    SELL's ``sell_inputs`` are cut at the shutdown deadline (with a short floor), like the
    worker's own calls, so a slow broker cannot hold the process past the SIGKILL."""

    class _SlowClient(FakeClient):
        async def get_order_book(self) -> list:
            import asyncio

            await asyncio.sleep(5)                   # a broker answering very slowly
            return []

    @staticmethod
    def _router(client):
        return OrderRouter(_make_config("live"), PaperEngine(), live_client=client)

    @pytest.mark.asyncio
    async def test_reads_are_cut_once_the_deadline_has_passed(self, monkeypatch):
        import time

        import skopaq.execution.order_router as order_router

        monkeypatch.setattr(order_router, "_MIN_READ_AFTER_STOP_S", 0.05, raising=False)
        router = self._router(self._SlowClient(FakeClock()))
        router.arm_shutdown(0.0)                     # past settle_by at once

        start = time.monotonic()
        with pytest.raises(TimeoutError):
            await router.broker_snapshot()
        with patch("skopaq.execution.order_router.resolve_security_id",
                   new_callable=AsyncMock, return_value="11536"), \
                pytest.raises(TimeoutError):
            await router.sell_inputs(_sell_order(security_id="11536"))
        assert time.monotonic() - start < 2.0

    @pytest.mark.asyncio
    async def test_reads_are_not_cut_before_a_stop(self):
        router = self._router(FakeClient(FakeClock()))
        assert router.deadlines.stopping is False
        snap = await router.broker_snapshot()
        assert snap.book_error == "" and snap.orders == ()
