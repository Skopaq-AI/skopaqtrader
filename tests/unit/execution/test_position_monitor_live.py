"""PositionMonitor, live: exits count what the broker confirmed, positions are resynced from
the order book, and no position is dropped or sold twice on a lagging or failed read.

A live router over a scripted INDstocks client (PositionBroker) and virtual time (FakeClock):
no network, no real sleeping. The paper monitor is test_position_monitor.py, unchanged.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skopaq.agents.sell_analyst import SellDecision
from skopaq.broker.client import BrokerError
from skopaq.broker.models import ExecutionResult, OrderRequest, OrderType, Side
from skopaq.broker.paper_engine import PaperEngine
from skopaq.constants import SafetyRules
from skopaq.execution import order_alerts
from skopaq.execution.executor import Executor
from skopaq.execution.live_orders import TrackedOrder
from skopaq.execution.order_alerts import OrderAlerter
from skopaq.execution.order_router import OrderRouter
from skopaq.execution.position_monitor import MonitoredPosition, MonitorResult, PositionMonitor
from skopaq.execution.safety_checker import SafetyChecker
from skopaq.risk.calendar import IST
from tests.unit.execution._fakes import (
    BASE_WALL,
    IGNORE,
    SIDS,
    AlertSpy,
    FakeClock,
    PositionBroker,
    Script,
    row,
)

RULES = SafetyRules(market_hours_only=False, require_stop_loss=False)
TCS, INFY = "NSE_11536", "NSE_1594"


def _filled(qty: int, price: str = "95") -> dict:
    return {"status": "SUCCESS", "traded_qty": qty, "traded_price": price}


def _config(**overrides) -> MagicMock:
    cfg = MagicMock()
    values = dict(
        trading_mode="live", monitor_poll_interval_seconds=1, monitor_hard_stop_pct=0.04,
        monitor_eod_exit_minutes_before_close=10, monitor_ai_interval_cycles=2,
        monitor_trailing_stop_enabled=False, monitor_trailing_stop_pct=0.02,
        monitor_resync_cycles=3, daemon_min_profit_threshold_pct=0.5,
        daemon_min_profit_threshold_inr=150.0,
    )
    values.update(overrides)
    for key, value in values.items():
        setattr(cfg, key, value)
    return cfg


class Live:
    """A live monitor over PositionBroker, with its exits and late fills recorded."""

    def __init__(self, held, *, ltps=None, lag=False, wall=BASE_WALL, sell_on_stop=True,
                 llm=None, **config):
        self.clock = FakeClock(wall)
        self.broker = PositionBroker(self.clock, held, ltps=ltps, lag=lag)
        self.config = _config(**config)
        self.router = OrderRouter(self.config, PaperEngine(), live_client=self.broker,
                                  sleep=self.clock.sleep, clock=self.clock.clock,
                                  wall=self.clock.wall)
        self.executor = Executor(self.router, SafetyChecker(rules=RULES), clock=self.clock.clock)
        self.stop = asyncio.Event()
        self.exits: list = []
        self.late: list = []

        async def on_exit(signal, execution):
            self.exits.append((signal, execution))

        async def on_late_fill(tracked, conf):
            self.late.append((tracked, conf))

        self.monitor = PositionMonitor(
            self.executor, self.broker, self.router, self.config, llm=llm,
            stop_event=self.stop, ai_enabled=llm is not None, on_exit=on_exit,
            sell_on_stop=sell_on_stop, on_late_fill=on_late_fill,
            sleep=self.clock.sleep, wall=self.clock.wall,
        )

    async def run(self, stop_at: float | None = None, at=()) -> MonitorResult:
        """Run the monitor. ``at``: (virtual time, callback) pairs run between polls once
        that time is reached; ``stop_at`` sets the stop event the same way (a task
        sleeping on the virtual clock would advance it while the monitor is busy)."""
        events = sorted([*at, *([(stop_at, self.stop.set)] if stop_at is not None else [])],
                        key=lambda event: event[0])
        pause = self.monitor._pause

        async def paced(seconds):
            stopped = await pause(seconds)
            while events and self.clock.t >= events[0][0]:
                events.pop(0)[1]()
            return stopped or self.stop.is_set()

        self.monitor._pause = paced
        return await self.monitor.run()

    async def open_order(self, symbol, side, qty, script, order_type=OrderType.MARKET):
        """An order already at the broker (placed by an earlier call or process)."""
        self.broker.place_effects = [script]
        response = await self.broker.place_order(OrderRequest(
            symbol=symbol, side=side, quantity=Decimal(qty), order_type=order_type,
            price=100.0 if order_type == OrderType.LIMIT else None,
            security_id=SIDS[symbol]))
        return response.order_id

    def track(self, order_id, symbol, side, qty, *, state, purpose):
        tracked = TrackedOrder(order_id=order_id, side=side, symbol=symbol,
                               security_id=SIDS[symbol], segment="EQUITY",
                               requested=Decimal(qty), purpose=purpose, state=state,
                               entry_deadline=0.0 if purpose == "entry" else None)
        self.router.registry.track(tracked)
        return tracked

    def journal(self, **line):
        path = self.router.journal.path_for(BASE_WALL.date())
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": 999_999, "segment": "EQUITY", **line}) + "\n")

    def cancels(self, order_id):
        return [c for c in self.broker.calls if c[0] == "cancel_order" and c[1] == order_id]


@pytest.fixture(autouse=True)
def _lookups(monkeypatch):
    """Symbols resolve without the instruments CSV; notifications go nowhere."""
    async def security_id(client, symbol, exchange="NSE"):
        return SIDS[symbol]

    async def scrip_code(client, symbol, exchange="NSE"):
        return f"NSE_{SIDS[symbol]}"

    monkeypatch.setattr("skopaq.execution.order_router.resolve_security_id", security_id)
    monkeypatch.setattr("skopaq.broker.scrip_resolver.resolve_scrip_code", scrip_code)
    monkeypatch.setattr("skopaq.notifications.notify", AsyncMock())


@pytest.fixture
def alerts(monkeypatch):
    spy = AlertSpy()
    monkeypatch.setattr(order_alerts, "_alerter", spy)
    return spy


def _pos(qty=10, **kw) -> MonitoredPosition:
    return MonitoredPosition(symbol="TCS", scrip_code=TCS, entry_price=100.0, quantity=qty,
                             security_id="11536", **kw)


# ── _execute_sell: only the confirmed quantity counts ────────────────────────


async def test_a_partial_exit_keeps_the_rest_and_the_reason_to_sell_it(alerts):   # T1
    live = Live({"TCS": (10, 100.0)})
    live.executor.execute_signal = AsyncMock(return_value=ExecutionResult(
        success=True, mode="live", fill_price=93.0, filled_quantity=Decimal(4),
        requested_quantity=Decimal(10), outcome="partial", order_ids=["EQ-1", "EQ-2"]))
    pos, result = _pos(10), MonitorResult()

    ok = await live.monitor._execute_sell(pos, ltp=94.0, reason="HARD STOP", result=result)

    assert ok is False
    assert (pos.quantity, pos.exit_intent, pos.pending_exit) == (6, "HARD STOP", False)
    assert result.sells_executed == 1
    assert result.total_pnl == pytest.approx((93.0 - 100.0) * 4)   # the broker's fill x 4
    assert len(live.exits) == 1


async def test_an_exit_that_may_still_be_working_marks_the_position_pending(alerts):   # T2
    live = Live({"TCS": (10, 100.0)})
    live.executor.execute_signal = AsyncMock(return_value=ExecutionResult(
        success=False, mode="live", outcome="open", order_ids=["EQ-1"], remaining_open=True,
        rejection_reason="Order EQ-1 may still be working at the broker"))
    pos, result = _pos(10), MonitorResult()

    assert await live.monitor._execute_sell(pos, ltp=94.0, reason="HARD STOP",
                                            result=result) is False

    assert pos.pending_exit and pos.quantity == 10 and result.sells_failed == 1
    assert result.exits_blocked == []       # not refused: the broker may be working it
    # A pending position is resynced every cycle (the monitor never re-sells it blind)
    assert live.monitor._resync_due(1, [pos]) is True
    assert live.monitor._resync_due(1, [_pos(10)]) is False
    assert live.monitor._resync_due(3, [_pos(10)]) is True


async def test_a_refused_live_exit_is_counted_as_blocked(alerts):
    live = Live({"TCS": (10, 100.0)})
    live.executor.execute_signal = AsyncMock(return_value=ExecutionResult(
        success=False, mode="live", safety_passed=False,
        rejection_reason="Cannot read the broker's order book"))
    result = MonitorResult()

    await live.monitor._execute_sell(_pos(10), ltp=94.0, reason="HARD STOP", result=result)
    await live.monitor._execute_sell(_pos(10), ltp=94.0, reason="HARD STOP", result=result)

    assert result.exits_blocked == ["TCS"]
    assert result.sells_failed == 2


# ── Resync: the order book first, and never drop on a failed read ─────────────


async def test_a_lagging_position_is_not_sold_again_after_its_exit(alerts):   # T3
    live = Live({"TCS": (10, 100.0), "INFY": (5, 100.0)}, ltps={TCS: 90.0, INFY: 100.0},
                lag=True, monitor_resync_cycles=1)
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(10, "90"))])]

    result = await live.run(stop_at=20)

    assert len(live.broker.placed_orders("TCS")) == 1   # positions still show 10: not re-sold
    assert result.sells_executed == 1
    assert result.positions_monitored == 2               # TCS was not adopted again
    assert result.positions_left == ["INFY"]


async def test_an_empty_positions_read_drops_a_position_only_on_the_second_read(alerts):  # T4
    live = Live({"TCS": (10, 100.0)})
    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    assert [(p.symbol, p.quantity, p.security_id) for p in positions] == [("TCS", 10, "11536")]

    live.broker.held = {}                  # a successful read with no rows at all
    await live.monitor._resync(positions, result)
    assert [(p.symbol, p.quantity) for p in positions] == [("TCS", 0)]   # kept, not sold
    assert "position-dropped:TCS" not in alerts.keys()

    await live.monitor._resync(positions, result)
    assert positions == []
    assert alerts.keys("WARNING") == ["position-dropped:TCS"]


async def test_a_filled_sell_in_the_book_drops_the_position_at_once(alerts):
    live = Live({"TCS": (10, 100.0)})
    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)

    live.broker.held = {}
    live.broker.extra_rows = [row("SUCCESS", traded=10, requested=10, id="EQ-9",
                                  updated_at=BASE_WALL.isoformat())]
    await live.monitor._resync(positions, result)

    assert positions == []
    assert "position-dropped:TCS" in alerts.keys("WARNING")


async def test_a_failed_or_partial_read_drops_nothing(alerts):   # T5
    live = Live({"TCS": (10, 100.0)})
    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)

    live.broker.positions = BrokerError("HTTP 503: upstream unavailable", 503, kind="http")
    await live.monitor._resync(positions, result)
    assert [(p.symbol, p.quantity) for p in positions] == [("TCS", 10)]

    live.broker.positions = []
    live.broker.held = {}
    live.broker.book_error_always = BrokerError("HTTP 503", 503, kind="http")
    for _ in range(3):
        await live.monitor._resync(positions, result)
    assert [p.symbol for p in positions] == ["TCS"]      # the book is unreadable: kept
    assert "position-dropped:TCS" not in alerts.keys()


async def test_a_failed_first_read_raises(alerts):
    """Nothing to monitor is not the same as positions that cannot be read."""
    live = Live({"TCS": (10, 100.0)})
    live.broker.positions = BrokerError("HTTP 503", 503, kind="http")
    with pytest.raises(BrokerError):
        await live.monitor.run()


async def test_an_intraday_position_is_left_to_the_broker(alerts, caplog):
    caplog.set_level(logging.INFO, logger="skopaq.execution.position_monitor")
    live = Live({})
    positions, result = [], MonitorResult()

    async def rows():
        from skopaq.broker.models import Position
        return [Position(symbol="TCS", security_id="11536", product="INTRADAY",
                         quantity=Decimal(5), average_price=100.0)]

    live.broker.get_positions = rows
    await live.monitor._resync(positions, result, first=True)
    await live.monitor._resync(positions, result)

    assert positions == []
    assert sum("not managed by Skopaq (INTRADAY)" in r.getMessage()
               for r in caplog.records) == 1                      # logged once


# ── The loop ─────────────────────────────────────────────────────────────────


async def test_the_rest_of_a_partial_ai_exit_is_sold_next_without_asking_again(alerts):  # T6
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 99.0}, llm=MagicMock())
    partial = {"status": "PARTIALLY FILLED", "traded_qty": 4, "traded_price": "99"}
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, partial)]), Script(), Script(),
                                    Script(timeline=[(0, _filled(6, "99"))])]
    decision = SellDecision(action="SELL", confidence=75, reasoning="RSI rolling over")

    with patch("skopaq.execution.position_monitor.analyze_exit",
               new=AsyncMock(return_value=decision)) as ai:
        result = await live.run(stop_at=900)

    orders = live.broker.placed_orders("TCS")
    assert [int(o.request.quantity) for o in orders] == [10, 6, 6, 6]
    ai.assert_awaited_once()                          # the remainder did not ask the AI again
    [first, second] = [signal for signal, _ in live.exits]
    assert second.reasoning == first.reasoning and "AI SELL" in second.reasoning
    assert second.quantity == 6
    assert result.sells_executed == 2 and result.positions_left == []


async def test_an_unconfirmed_buy_keeps_the_monitor_alive_until_it_resolves(alerts):   # T7
    live = Live({"TCS": (0, 101.0)})
    buy = await live.open_order("TCS", Side.BUY, 10, Script(
        on_cancel=[IGNORE] * 6 + [_filled(10, "101")]), order_type=OrderType.LIMIT)
    live.track(buy, "TCS", "BUY", 10, state="unknown", purpose="entry")

    result = await live.run(stop_at=60)

    assert len(live.cancels(buy)) == 7                # retried on the next resync
    [(tracked, conf)] = live.late
    assert (tracked.order_id, conf.filled_qty) == (buy, 10)
    assert result.late_fills == 1
    assert "late-fill:" + buy in alerts.keys("WARNING")
    assert result.positions_monitored == 1            # the filled BUY is monitored now
    assert result.positions_left == ["TCS"] and result.orders_unconfirmed == []


async def test_an_own_stuck_exit_is_cancelled_again_and_its_late_fill_recorded(alerts):  # T8
    live = Live({"TCS": (10, 100.0)})
    stuck = await live.open_order("TCS", Side.SELL, 10, Script(
        on_cancel=[IGNORE] * 6 + [_filled(10, "95")]))
    live.track(stuck, "TCS", "SELL", 10, state="stuck", purpose="exit")

    result = await live.run(stop_at=60)

    assert len(live.cancels(stuck)) == 7
    [(tracked, conf)] = live.late
    assert (tracked.side, conf.filled_qty, conf.avg_price) == ("SELL", 10, Decimal("95"))
    assert "exit-late:" + stuck in alerts.keys("WARNING")
    assert live.router.registry.recent_exit_qty("TCS", "11536", 600) == 10
    assert len(live.broker.orders) == 1               # nothing new was placed over it
    assert result.positions_left == [] and result.orders_unconfirmed == []


async def test_a_foreign_open_sell_blocking_an_exit_is_alerted_once(monkeypatch):   # T9
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0})
    live.broker.extra_rows = [row("O-PENDING", traded=0, requested=10, id="GTT-9",
                                  order_type="LIMIT", updated_at=BASE_WALL.isoformat())]
    sent = []

    async def notify(severity, key, text, *, order_ids=()):
        sent.append((severity, key, text, order_ids))

    monkeypatch.setattr(order_alerts, "_alerter", OrderAlerter(notify, clock=live.clock.clock))

    result = await live.run(stop_at=30)              # the stop-loss fires every cycle

    blocked = [s for s in sent if s[1] == "exit-blocked:TCS:foreign-open-sell"]
    assert len(blocked) == 1
    severity, _, text, order_ids = blocked[0]
    assert severity == "CRITICAL" and "GTT-9 O-PENDING" in text and order_ids == ("GTT-9",)
    assert live.broker.orders == {}                  # nothing sold into the user's order
    assert result.exits_blocked == ["TCS"] and result.positions_left == ["TCS"]


async def test_an_order_left_by_a_killed_process_is_resumed_not_treated_as_foreign(alerts):  # T10
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0})
    left = await live.open_order("TCS", Side.SELL, 10,
                                 Script(timeline=[(0, {"status": "O-PENDING"})]))
    live.journal(ts=(BASE_WALL - timedelta(minutes=20)).isoformat(), event="stuck",
                 order_id=left, symbol="TCS", security_id="11536", side="SELL", qty="10",
                 purpose="exit")
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(10, "90"))])]

    result = await live.run(stop_at=60)

    assert live.cancels(left)                         # our own: cancelled, then re-sold
    assert not any(k.startswith("exit-blocked") for k in alerts.keys())
    [resold] = [o for o in live.broker.placed_orders("TCS") if o.order_id != left]
    assert int(resold.request.quantity) == 10
    assert result.sells_executed == 1 and result.positions_left == []


async def test_a_fresh_order_of_another_live_process_is_left_to_it(alerts):
    live = Live({"TCS": (10, 100.0)})
    working = await live.open_order("TCS", Side.SELL, 10, Script())
    live.journal(ts=(BASE_WALL - timedelta(seconds=10)).isoformat(), event="placed",
                 order_id=working, symbol="TCS", security_id="11536", side="SELL", qty="10",
                 purpose="exit")

    await live.run(stop_at=20)

    assert live.cancels(working) == []                # its process may still be working it
    assert not any(k.startswith("exit-blocked") for k in alerts.keys())


async def test_an_uncertain_placement_found_later_in_the_book_is_adopted(alerts):
    live = Live({"TCS": (0, 101.0)})
    found = await live.open_order("TCS", Side.BUY, 10, Script(
        timeline=[(0, _filled(10, "101"))]), order_type=OrderType.LIMIT)
    live.journal(ts=BASE_WALL.isoformat(), event="uncertain", order_id="", internal_id="abc",
                 symbol="TCS", security_id="11536", side="BUY", qty="10", purpose="entry")

    result = await live.run(stop_at=10)

    [(tracked, conf)] = live.late                    # recorded as the BUY it was
    assert (tracked.order_id, tracked.internal_id, conf.filled_qty) == (found, "abc", 10)
    assert found in live.router.journal.own_ids_today()
    assert result.positions_monitored == 1


async def test_the_shutdown_pass_does_not_sell_what_positions_lag_behind(alerts):   # T15
    live = Live({"TCS": (10, 100.0)}, lag=True)

    def sold_elsewhere():
        live.broker.extra_rows = [row("SUCCESS", traded=10, requested=10, id="EQ-77",
                                      updated_at=live.clock.wall().isoformat())]

    with patch.object(live.monitor, "_check_safety", return_value=None), \
         patch.object(live.monitor, "_should_eod_exit", return_value=True):
        result = await live.run(stop_at=5, at=[(5, sold_elsewhere)])

    assert live.broker.orders == {}
    assert result.positions_left == []


@pytest.mark.parametrize(("sell_on_stop", "sold"), [(True, 1), (False, 0)])
async def test_the_shutdown_pass_runs_only_when_the_monitor_owns_the_close(
        alerts, sell_on_stop, sold):
    live = Live({"TCS": (10, 100.0)}, sell_on_stop=sell_on_stop)
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(10, "100"))])]
    live.stop.set()

    with patch.object(live.monitor, "_should_eod_exit", return_value=True):
        result = await live.monitor.run()

    assert len(live.broker.orders) == sold
    assert result.sells_executed == sold


async def test_a_resting_exit_does_not_hold_up_another_positions_stop(alerts):   # T16
    live = Live({"TCS": (10, 100.0), "INFY": (5, 100.0)}, ltps={TCS: 90.0, INFY: 90.0})
    live.broker.by_symbol = {"TCS": [Script(), Script(), Script()],
                             "INFY": [Script(timeline=[(0, _filled(5, "90"))])]}

    result = await live.run(stop_at=15)

    [first_tcs, *_] = live.broker.placed_orders("TCS")
    [infy] = live.broker.placed_orders("INFY")
    assert first_tcs.placed_at < 1 and infy.placed_at < 1      # both in the first cycle
    assert len(live.broker.placed_orders("TCS")) == 3          # awaited to its end after the stop
    assert result.sells_executed == 1 and result.sells_failed == 1
    assert result.positions_left == ["TCS"]
    assert "exit-not-filled:TCS" in alerts.keys("CRITICAL")


async def test_a_stop_is_seen_between_positions(alerts):
    live = Live({"TCS": (10, 100.0), "INFY": (5, 100.0)})

    def rule(pos, ltp):
        live.stop.set()
        return None

    with patch.object(live.monitor, "_check_safety", side_effect=rule):
        await live.monitor.run()

    assert [c for c in live.broker.calls if c[0] == "get_ltp"] == [("get_ltp", TCS)]


async def test_the_monitor_ends_after_the_close_with_the_positions_left(alerts):
    live = Live({"TCS": (10, 100.0)}, wall=datetime(2026, 9, 25, 15, 30, 40, tzinfo=IST))

    result = await live.run(stop_at=600)

    assert live.broker.orders == {}                   # nothing placed after 15:29:55
    assert result.positions_left == ["TCS"]
    assert live.clock.wall().time() < datetime(2026, 9, 25, 15, 32).time()
    assert "positions-left:2026-09-25:monitor" in alerts.keys("CRITICAL")


async def test_inside_the_daemon_the_monitor_leaves_positions_to_closing(alerts):
    live = Live({"TCS": (10, 100.0)}, sell_on_stop=False)
    live.stop.set()

    result = await live.monitor.run()

    assert result.positions_left == ["TCS"]
    assert not any(k.startswith("positions-left") for k in alerts.keys())


async def test_a_stop_arms_the_shutdown_deadline(alerts):
    live = Live({"TCS": (10, 100.0)})
    await live.run(stop_at=5)
    assert live.router.deadlines.stopping
    assert live.router.deadlines.settle_by() == pytest.approx(5 + 240)


async def test_an_unexpected_error_in_a_resync_does_not_end_the_monitor(alerts, monkeypatch):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, monitor_resync_cycles=1)
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(10, "90"))])]
    calls = []

    def broken(self, *args):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("journal corrupted")

    monkeypatch.setattr(PositionMonitor, "_adopt_journal", broken)
    result = await live.run(stop_at=20)

    assert result.sells_executed == 1 and result.positions_left == []


# ── Review fixes ─────────────────────────────────────────────────────────────


async def test_an_uncertain_exit_is_not_sold_again_once_the_reconcile_window_ends(alerts):
    from skopaq.broker.client import OrderPlacementUncertain

    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True,
                monitor_poll_interval_seconds=10)
    # POST /order times out after sending; the order exists and fills, but the book shows
    # it only 30 s later (after the 15 s reconcile window and a poll)
    live.broker.by_symbol["TCS"] = [
        Script(timeline=[(0, {"status": "PENDING"}), (1, _filled(10, "90"))],
               uncertain=OrderPlacementUncertain("ReadTimeout", kind="transport"),
               visible_after=30.0),
        Script(timeline=[(0, _filled(10, "90"))]),
    ]
    await live.run(stop_at=60)

    assert len(live.broker.placed_orders("TCS")) == 1, "the same 10 shares were sold twice"


async def test_one_empty_read_after_an_earlier_sale_of_it_drops_nothing(alerts):
    # Bought 10, 5 sold earlier today (a filled SELL in the book; positions show net 5).
    # That earlier sale is already in the tracked quantity: it cannot corroborate a drop
    live = Live({"TCS": (10, 100.0)})
    await live.open_order("TCS", Side.SELL, 5, Script(timeline=[(0, _filled(5, "101"))]))
    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    assert [(p.symbol, p.quantity) for p in positions] == [("TCS", 5)]

    live.broker.held = {}                      # one successful read with no rows
    await live.monitor._resync(positions, result)
    assert [p.symbol for p in positions] == ["TCS"]
    assert not [k for k in alerts.keys() if k.startswith("position-dropped")]

    await live.monitor._resync(positions, result)          # the second empty read in a row
    assert positions == []


async def test_a_new_sale_covering_it_still_drops_at_once(alerts):
    live = Live({"TCS": (10, 100.0)})
    await live.open_order("TCS", Side.SELL, 5, Script(timeline=[(0, _filled(5, "101"))]))
    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    # The user sells the other 5 in the app: a new filled SELL covers what is tracked
    await live.open_order("TCS", Side.SELL, 5, Script(timeline=[(0, _filled(5, "102"))]))
    live.broker.held = {}
    await live.monitor._resync(positions, result)
    assert positions == []
    assert "position-dropped:TCS" in alerts.keys("WARNING")


async def test_a_glitch_after_a_partial_sale_does_not_end_the_monitor(alerts):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 100.0}, monitor_resync_cycles=1)
    await live.open_order("TCS", Side.SELL, 5, Script(timeline=[(0, _filled(5, "101"))]))
    saved = dict(live.broker.held)

    def glitch():
        live.broker.held = {}

    def recover():
        live.broker.held = saved

    result = await live.run(stop_at=600, at=[(3, glitch), (4, recover)])
    assert live.clock.t >= 600, "the monitor stopped protecting TCS after one empty read"
    assert result.positions_left == ["TCS"] or live.stop.is_set()


async def test_a_late_buy_fill_positions_do_not_show_yet_is_still_monitored(alerts):
    # The unconfirmed BUY resolves (filled) in a resync at ~t=11, but positions show it
    # only at t=26: the monitor must wait for it rather than end with it unprotected
    live = Live({"TCS": (0, 101.0)}, lag=True)
    buy = await live.open_order("TCS", Side.BUY, 10, Script(
        on_cancel=[IGNORE] * 6 + [_filled(10, "101")]), order_type=OrderType.LIMIT)
    live.track(buy, "TCS", "BUY", 10, state="unknown", purpose="entry")

    def positions_catch_up():
        live.broker.lag = False

    result = await live.run(stop_at=120, at=[(26, positions_catch_up)])

    assert [(t.order_id, c.filled_qty) for t, c in live.late] == [(buy, 10)]
    assert result.positions_monitored == 1
    assert live.clock.t >= 26


async def test_a_confirmed_buy_positions_never_show_keeps_the_monitor_and_is_left(alerts):
    live = Live({"TCS": (0, 101.0)}, lag=True)
    live.router.worker._settings = dataclasses.replace(live.router.worker.settings,
                                                       sell_fill_lag_window_s=60.0)
    live.router.registry.record_confirmed_buy("TCS", "11536", Decimal(10), order_id="EQ-9")

    result = await live.run(stop_at=3600)

    assert live.clock.t >= 3600                       # still held: watched past the lag window
    assert result.positions_left == ["TCS"]           # rc 4
    assert len([k for k in alerts.keys("CRITICAL") if k.startswith("positions-left:")]) == 1


async def test_a_fresh_buy_left_by_a_dead_daemon_keeps_the_monitor_until_it_resolves(alerts):
    # The daemon placed a BUY 10 s ago and was killed while confirming it; the recovery
    # monitor starts at once. The BUY is still pending (it fills at t=60)
    live = Live({"TCS": (0, 101.0)})
    buy = await live.open_order("TCS", Side.BUY, 10, Script(
        timeline=[(0, {"status": "PENDING"}), (60, _filled(10, "101"))]),
        order_type=OrderType.LIMIT)
    live.journal(ts=(BASE_WALL - timedelta(seconds=10)).isoformat(), event="placed",
                 order_id=buy, symbol="TCS", security_id="11536", side="BUY", qty="10",
                 purpose="entry")

    result = await live.run(stop_at=600)

    assert live.clock.t >= 600                        # stayed, adopted it, and monitors TCS
    assert [(t.order_id, c.filled_qty) for t, c in live.late] == [(buy, 10)]
    assert result.positions_monitored == 1


async def test_a_journalled_order_still_being_worked_counts_as_unconfirmed(alerts):
    live = Live({"TCS": (0, 101.0)})
    buy = await live.open_order("TCS", Side.BUY, 10, Script(), order_type=OrderType.LIMIT)
    live.journal(ts=(BASE_WALL - timedelta(seconds=10)).isoformat(), event="placed",
                 order_id=buy, symbol="TCS", security_id="11536", side="BUY", qty="10",
                 purpose="entry")

    result = await live.run(stop_at=5)                # stopped before it could adopt it

    assert result.orders_unconfirmed == [buy]         # rc 4
    assert live.cancels(buy) == []                    # still its own process's to work


async def test_an_uncertain_buy_not_in_the_book_yet_keeps_the_monitor(alerts):
    live = Live({"TCS": (0, 101.0)}, lag=True)        # positions show it from t=35
    live.journal(ts=(BASE_WALL - timedelta(seconds=20)).isoformat(), event="uncertain",
                 order_id="", internal_id="abc", symbol="TCS", security_id="11536",
                 side="BUY", qty="10", purpose="entry")
    live.broker.default_script = lambda: Script(timeline=[(0, _filled(10, "101"))],
                                                visible_after=30)
    await live.broker.place_order(OrderRequest(symbol="TCS", side=Side.BUY, quantity=Decimal(10),
                                               order_type=OrderType.LIMIT, price=101.0,
                                               security_id="11536"))

    def positions_catch_up():
        live.broker.lag = False

    result = await live.run(stop_at=600, at=[(35, positions_catch_up)])

    assert live.clock.t >= 35
    assert result.positions_monitored == 1            # adopted once the book showed it


async def test_the_final_state_does_not_trust_one_empty_read(alerts):
    # Every exit is refused by the broker; the one positions read at the very end comes
    # back empty. TCS is still tracked, with no sale covering it: it is still held (rc 4)
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0},
                wall=datetime(2026, 9, 25, 15, 29, 0, tzinfo=IST))
    live.broker.default_script = None
    live.broker.place_effects = [BrokerError("RMS: Margin exceeds", 400, kind="http")] * 50
    real_positions = live.broker.get_positions

    async def empty_at_the_end():
        if live.clock.wall().time() >= datetime(2026, 9, 25, 15, 31).time():
            live.broker.calls.append(("get_positions",))
            return []
        return await real_positions()

    live.broker.get_positions = empty_at_the_end
    result = await live.run(stop_at=3600)

    assert result.positions_left == ["TCS"]
    assert result.exits_blocked == [] or result.exits_blocked == ["TCS"]
    assert [k for k in alerts.keys("CRITICAL") if k.startswith("positions-left:")]


async def test_one_empty_read_after_the_monitors_own_partial_exit_drops_nothing(alerts):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0})
    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    [pos] = positions
    live.broker.place_effects = [Script(
        timeline=[(0, {"status": "PENDING", "traded_qty": 6, "traded_price": "90"})],
        on_cancel=[{"status": "PARTIALLY FILLED - CANCELLED", "traded_qty": 6,
                    "traded_price": "90"}])]
    live.router.worker._settings = dataclasses.replace(live.router.worker.settings,
                                                       exit_max_attempts=1)
    assert await live.monitor._execute_sell(pos, 90.0, "HARD STOP", result) is False
    assert pos.quantity == 4

    live.broker.held = {}                              # one successful read with no rows
    await live.monitor._resync(positions, result)
    assert positions == [pos], "its own 6 sold shares are already out of the 4 tracked"


async def test_an_abandoned_exit_has_its_fills_recorded(alerts):
    # An exit still running when the monitor has to end is cancelled; what its orders
    # sold (4, then 6 as the second order is cancelled) is recorded, not lost
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    [pos] = positions
    live.broker.place_effects = [
        Script(timeline=[(0, {"status": "PENDING"}),
                         (2, {"status": "PENDING", "traded_qty": 4, "traded_price": "90"})]),
        Script(on_cancel=[_filled(6, "89")]),
    ]
    pos.exit_task = asyncio.create_task(live.monitor._execute_sell(pos, 90.0, "HARD STOP",
                                                                   result))
    while live.clock.t < 15:
        await asyncio.sleep(0)
    await live.monitor._abandon_exits(positions, result)

    # Nothing was reported for either order, so each whole fill is a late fill
    assert sorted((t.order_id, c.filled_qty) for t, c in live.late) == [
        ("EQ-1", 4), ("EQ-2", 6)]
    assert result.late_fills == 2
    assert live.router.registry.unresolved() == []


async def test_an_order_like_an_uncertain_placement_is_watched_even_when_the_journal_failed(
        alerts):
    from skopaq.execution.sellable import UncertainPlacement

    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 100.0})
    live.router._journal = None                       # nothing journalled (a write failure)
    live.router.registry.record_uncertain(UncertainPlacement(
        internal_id="abc", symbol="TCS", security_id="11536", qty=Decimal(10),
        at=BASE_WALL, side="SELL"))
    order_id = await live.open_order("TCS", Side.SELL, 10, Script())   # the book shows it

    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    await live.monitor._resync(positions, result)
    await live.monitor._settle_resumes(result)        # (resumes run in the background)

    tracked = live.router.registry.get(order_id)
    assert tracked is not None and tracked.guessed    # watched: it only looks like ours
    assert live.cancels(order_id) == []               # never cancelled
    assert [p.internal_id for p in live.router.registry.uncertain()] == ["abc"]   # counts on
    assert "placement-match:abc" in alerts.keys("CRITICAL")



async def test_a_stuck_order_being_resumed_does_not_slow_another_positions_stop(alerts):
    # TCS has an exit of ours whose cancel the broker never confirms; INFY falls through
    # its stop at t=5. The stuck order's resumes (10 s cancel windows) run in the
    # background: INFY's exit is placed at the next poll
    live = Live({"TCS": (10, 100.0), "INFY": (5, 100.0)}, ltps={TCS: 100.0, INFY: 100.0})
    stuck = await live.open_order("TCS", Side.SELL, 10, Script(on_cancel=[IGNORE] * 1000))
    live.track(stuck, "TCS", "SELL", 10, state="stuck", purpose="exit")
    live.broker.by_symbol["INFY"] = [Script(timeline=[(0, _filled(5, "90"))])]

    def crash():
        live.broker.ltps[INFY] = 90.0

    await live.run(stop_at=80, at=[(5, crash)])

    [infy] = live.broker.placed_orders("INFY")
    assert infy.placed_at <= 7
    assert len(live.cancels(stuck)) > 5               # and the stuck order is still worked
