"""TradingDaemon, live: unconfirmed BUYs count, CLOSING resumes its own stuck exits, sells only
what open SELL orders and lagging positions leave, runs its exits concurrently, and stops
all order work within the shutdown budget after a stop.

A live router over a scripted INDstocks client (PositionBroker) and virtual time (FakeClock).
Paper CLOSING is test_daemon.py and test_protective_exits.py, unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skopaq.broker.client import BrokerError
from skopaq.broker.models import ExecutionResult, OrderRequest, OrderType, Position, Side
from skopaq.broker.paper_engine import PaperEngine
from skopaq.constants import SafetyRules
from skopaq.execution import order_alerts
from skopaq.execution.daemon import DaemonSessionReport, TradingDaemon
from skopaq.execution.executor import Executor
from skopaq.execution.live_orders import FillSettings, TrackedOrder
from skopaq.execution.order_router import OrderRouter
from skopaq.execution.position_monitor import MonitorResult
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
TCS = "NSE_11536"


def _report() -> DaemonSessionReport:
    return DaemonSessionReport(session_date="2026-09-25")


def _filled(qty: int, price: str = "95") -> dict:
    return {"status": "SUCCESS", "traded_qty": qty, "traded_price": price}


def _config(**overrides) -> MagicMock:
    cfg = MagicMock()
    values = dict(
        trading_mode="live", daemon_max_trades_per_session=3,
        daemon_max_candidates_to_analyze=5, daemon_scan_delay_after_open_seconds=0,
        daemon_min_profit_threshold_pct=0.5, daemon_min_profit_threshold_inr=150.0,
        monitor_poll_interval_seconds=1, monitor_hard_stop_pct=0.04,
        monitor_eod_exit_minutes_before_close=10, monitor_ai_interval_cycles=6,
        monitor_trailing_stop_enabled=False, monitor_trailing_stop_pct=0.02,
        monitor_resync_cycles=3, reflection_enabled=False, supabase_url="",
        scheduler_kill_after_seconds=300, order_shutdown_margin_seconds=60,
    )
    values.update(overrides)
    for key, value in values.items():
        setattr(cfg, key, value)
    return cfg


class LiveDaemon:
    """A TradingDaemon past PRE_OPEN: a live router, executor and client over PositionBroker."""

    def __init__(self, held, *, ltps=None, lag=False, wall=BASE_WALL, fill_settings=None,
                 **config):
        self.clock = FakeClock(wall)
        self.broker = PositionBroker(self.clock, held, ltps=ltps, lag=lag)
        self.config = _config(**config)
        self.router = OrderRouter(self.config, PaperEngine(), live_client=self.broker,
                                  fill_settings=fill_settings, sleep=self.clock.sleep,
                                  clock=self.clock.clock, wall=self.clock.wall)
        self.stop = asyncio.Event()
        self.daemon = TradingDaemon(self.config, stop_event=self.stop, sleep=self.clock.sleep,
                                    wall=self.clock.wall)
        self.daemon._client = self.broker
        self.daemon._router = self.router
        self.daemon._executor = Executor(self.router, SafetyChecker(rules=RULES),
                                         clock=self.clock.clock)
        self.recorded: list = []
        self.late: list = []

        async def record_exit(signal, execution):
            self.recorded.append((signal, execution))

        async def record_late_fill(tracked, conf):
            self.late.append((tracked, conf))

        self.daemon._record_exit = record_exit
        self.daemon._record_late_fill = record_late_fill

    async def stuck_exit(self, symbol, qty, script):
        """An exit this session placed whose cancel was never confirmed."""
        self.broker.place_effects = [script]
        response = await self.broker.place_order(OrderRequest(
            symbol=symbol, side=Side.SELL, quantity=Decimal(qty),
            order_type=OrderType.MARKET, security_id=SIDS[symbol]))
        self.router.registry.track(TrackedOrder(
            order_id=response.order_id, side="SELL", symbol=symbol, security_id=SIDS[symbol],
            segment="EQUITY", requested=Decimal(qty), purpose="exit", state="stuck"))
        return response.order_id

    def order_times(self, name: str) -> list[float]:
        return [c[-1] for c in self.broker.calls if c[0] == name]


@pytest.fixture(autouse=True)
def _lookups(monkeypatch):
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


# ── ANALYZING: an unconfirmed BUY ────────────────────────────────────────────


async def test_an_unconfirmed_buy_uses_a_trade_slot_without_opening_a_trade():   # T18
    daemon = TradingDaemon(_config(daemon_max_trades_per_session=1))
    daemon._graph = AsyncMock()
    daemon._router = MagicMock()
    unconfirmed = ExecutionResult(success=False, mode="live", outcome="open",
                                  order_ids=["EQ-1"], remaining_open=True,
                                  rejection_reason="Order EQ-1 may still be working")
    daemon._graph.analyze_and_execute = AsyncMock(return_value=MagicMock(
        error=None, signal=MagicMock(action="BUY", confidence=80, quantity=Decimal(10)),
        execution=unconfirmed))
    candidates = [MagicMock(symbol="AAA", urgency="high"),
                  MagicMock(symbol="BBB", urgency="normal")]
    report = _report()

    with patch("skopaq.cli.main._compute_risk_scales", return_value=(1.0, 1.0)), \
         patch("skopaq.cli.main._run_lifecycle", new_callable=AsyncMock) as lifecycle:
        buys = await daemon._phase_analyze_and_trade(candidates, report)

    assert (report.orders_unconfirmed, report.trades_opened, report.trades_rejected) == (1, 0, 0)
    assert report.candidates_analyzed == 1        # max_trades=1 is used up by it
    assert buys == []
    lifecycle.assert_not_awaited()                # its row comes once the fill is adopted


async def test_a_partial_buy_opens_a_trade_for_the_filled_quantity(caplog):
    caplog.set_level(logging.INFO, logger="skopaq.execution.daemon")
    daemon = TradingDaemon(_config())
    daemon._graph = AsyncMock()
    daemon._router = MagicMock()
    partial = ExecutionResult(success=True, mode="live", fill_price=100.0, outcome="partial",
                              filled_quantity=Decimal(3), requested_quantity=Decimal(10),
                              order_ids=["EQ-1"])
    daemon._graph.analyze_and_execute = AsyncMock(return_value=MagicMock(
        error=None, signal=MagicMock(action="BUY", confidence=80, quantity=Decimal(10)),
        execution=partial))
    report = _report()

    with patch("skopaq.cli.main._compute_risk_scales", return_value=(1.0, 1.0)), \
         patch("skopaq.cli.main._run_lifecycle", new_callable=AsyncMock):
        await daemon._phase_analyze_and_trade([MagicMock(symbol="AAA", urgency="high")], report)

    assert (report.trades_opened, report.orders_unconfirmed) == (1, 0)
    assert any("PARTIAL 3/10" in r.getMessage() for r in caplog.records)


def _session(daemon, trade, **patches):
    return (
        patch.object(daemon, "_phase_pre_open", new_callable=AsyncMock),
        patch.object(daemon, "_halt_status", return_value=MagicMock(halted=False)),
        patch.object(daemon, "_phase_scan", new_callable=AsyncMock, return_value=["TCS"]),
        patch.object(daemon, "_phase_analyze_and_trade", side_effect=trade),
        patch.object(daemon, "_settle_due_decisions", new_callable=AsyncMock, return_value=0),
        patch.object(daemon, "_notify_report", new_callable=AsyncMock),
    )


async def _trade_unconfirmed(candidates, report):
    report.orders_unconfirmed = 1
    return []


async def test_monitoring_and_closing_run_for_an_unconfirmed_buy():   # T18
    daemon = TradingDaemon(_config())
    first, second, third, fourth, fifth, sixth = _session(daemon, _trade_unconfirmed)
    with first, second, third, fourth, fifth, sixth, \
         patch.object(daemon, "_phase_monitor", new_callable=AsyncMock,
                      return_value=MonitorResult()) as monitor, \
         patch.object(daemon, "_phase_close", new_callable=AsyncMock) as close:
        report = await daemon.run_session()
    monitor.assert_awaited_once()
    close.assert_awaited_once()
    assert report.orders_unconfirmed == 1


async def test_a_failure_with_only_an_unconfirmed_buy_still_runs_closing():   # T18
    daemon = TradingDaemon(_config())
    first, second, third, fourth, fifth, sixth = _session(daemon, _trade_unconfirmed)
    with first, second, third, fourth, fifth, sixth, \
         patch.object(daemon, "_phase_monitor", new_callable=AsyncMock,
                      side_effect=RuntimeError("positions unavailable")), \
         patch.object(daemon, "_phase_close", new_callable=AsyncMock) as close:
        report = await daemon.run_session()
    assert report.failed and report.trades_opened == 0
    close.assert_awaited_once()


async def test_the_session_drains_order_alerts_at_the_end(monkeypatch):
    drained = []

    class Spy(AlertSpy):
        async def drain(self, timeout=10.0):
            drained.append(timeout)

    monkeypatch.setattr(order_alerts, "_alerter", Spy())
    daemon = TradingDaemon(_config())
    with patch.object(daemon, "_phase_pre_open", new_callable=AsyncMock,
                      side_effect=RuntimeError("token expired")), \
         patch.object(daemon, "_settle_due_decisions", new_callable=AsyncMock, return_value=0), \
         patch.object(daemon, "_notify_report", new_callable=AsyncMock):
        await daemon.run_session()
    assert drained


def test_the_report_shows_what_was_left_open():
    daemon = TradingDaemon(_config())
    report = DaemonSessionReport(session_date="2026-09-25", orders_unconfirmed=1,
                                 positions_left=["TCS"], exits_blocked=["INFY"])
    text = daemon._log_report(report)
    assert "Unconfirmed orders: 1" in text
    assert "Positions left open: TCS" in text and "Exits blocked: INFY" in text
    assert "Positions left" not in daemon._log_report(DaemonSessionReport(session_date="x"))


# ── CLOSING, paper: one pass, no sleep ───────────────────────────────────────


async def test_closing_in_paper_is_one_pass_without_a_sleep():   # T19
    sleep = AsyncMock()
    daemon = TradingDaemon(_config(trading_mode="paper"), sleep=sleep)
    daemon._router = AsyncMock()
    daemon._router.get_positions = AsyncMock(return_value=[
        Position(symbol="TCS", quantity=Decimal(3), average_price=100.0, last_price=99.0)])
    daemon._executor = AsyncMock()
    daemon._executor.execute_signal = AsyncMock(return_value=MagicMock(
        success=False, rejection_reason="refused"))

    with patch("asyncio.sleep", new_callable=AsyncMock) as real_sleep:
        await daemon._phase_close()

    daemon._executor.execute_signal.assert_awaited_once()     # no second pass
    sleep.assert_not_awaited()
    real_sleep.assert_not_awaited()


# ── CLOSING, live ────────────────────────────────────────────────────────────


async def test_closing_resumes_an_own_stuck_exit_then_sells_the_rest(alerts):   # T17
    live = LiveDaemon({"TCS": (10, 100.0)})
    stuck = await live.stuck_exit("TCS", 10, Script(on_cancel=[
        {"status": "PARTIALLY FILLED - CANCELLED", "traded_qty": 4, "traded_price": "95"}]))
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(6, "95"))])]
    report = _report()

    await live.daemon._phase_close(report)

    names = [c[:2] for c in live.broker.calls if c[0] in ("cancel_order", "place_order")]
    assert names[0] == ("place_order", "MARKET")               # the test's stuck exit
    assert names[1] == ("cancel_order", stuck)                 # resumed first
    [resold] = [o for o in live.broker.placed_orders() if o.order_id != stuck]
    assert int(resold.request.quantity) == 6                   # only what is left
    [(tracked, conf)] = live.late
    assert (tracked.order_id, conf.filled_qty) == (stuck, 4)   # its late fill is recorded
    assert len(live.recorded) == 1
    assert report.positions_left == [] and report.exits_blocked == []


@pytest.mark.parametrize(("pending", "sold"), [(5, None), (2, 3)])
async def test_closing_sells_only_what_open_sell_orders_leave(alerts, pending, sold):
    live = LiveDaemon({"TCS": (5, 100.0)})
    live.broker.extra_rows = [row("O-PENDING", traded=0, requested=pending, id="GTT-9",
                                  order_type="LIMIT", updated_at=BASE_WALL.isoformat())]
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(3))])]
    report = _report()

    await live.daemon._phase_close(report)

    orders = live.broker.placed_orders("TCS")
    if sold is None:
        assert orders == []
        assert "exit-blocked:TCS:closing-skip" in alerts.keys("CRITICAL")
        assert "GTT-9 O-PENDING" in alerts.text("exit-blocked:TCS")
        assert report.exits_blocked == ["TCS"]
    else:
        assert [int(o.request.quantity) for o in orders] == [sold]
    assert report.positions_left == ["TCS"]                    # the GTT still holds shares
    assert "positions-left:2026-09-25:daemon" in alerts.keys("CRITICAL")


async def test_closing_does_not_sell_again_what_positions_have_not_caught_up_with(alerts):
    live = LiveDaemon({"TCS": (5, 100.0)}, lag=True)
    live.broker.extra_rows = [row("SUCCESS", traded=5, requested=5, id="EQ-9",
                                  updated_at=BASE_WALL.isoformat())]
    live.router.registry.record_confirmed_exit("TCS", "11536", Decimal(5))   # the monitor's
    report = _report()

    await live.daemon._phase_close(report)

    assert live.broker.orders == {}
    assert not any(k.startswith(("exit-blocked", "positions-left")) for k in alerts.keys())
    assert report.positions_left == []


async def test_closing_after_the_last_order_time_places_nothing_and_alerts(alerts):
    live = LiveDaemon({"TCS": (5, 100.0)}, wall=datetime(2026, 9, 25, 15, 29, 58, tzinfo=IST))
    report = _report()

    await live.daemon._phase_close(report)

    assert live.broker.orders == {}
    assert report.positions_left == ["TCS"]
    assert "positions-left:2026-09-25:daemon" in alerts.keys("CRITICAL")


@pytest.mark.parametrize(("wall", "placed"), [
    (BASE_WALL, 2),                                         # time left: a second pass
    (datetime(2026, 9, 25, 15, 29, 40, tzinfo=IST), 1),     # too close to 15:29:55
])
async def test_live_closing_retries_once_only_while_there_is_time(alerts, wall, placed):  # T19
    live = LiveDaemon({"TCS": (10, 100.0)}, wall=wall)
    live.broker.place_effects = [BrokerError("RMS: Margin exceeds", 400, kind="http"),
                                 Script(timeline=[(0, _filled(10, "99"))])]
    report = _report()

    await live.daemon._phase_close(report)

    assert len(live.broker.placed()) == placed
    assert report.positions_left == ([] if placed == 2 else ["TCS"])


async def test_closing_sells_every_position_concurrently(alerts):
    live = LiveDaemon({"TCS": (5, 100.0), "INFY": (3, 100.0)})
    live.broker.by_symbol = {"TCS": [Script(timeline=[(5, _filled(5))])],
                             "INFY": [Script(timeline=[(5, _filled(3))])]}

    await live.daemon._phase_close(_report())

    times = sorted(o.placed_at for o in live.broker.placed_orders())
    assert len(times) == 2 and times[1] < 1                  # the second did not wait 5 s


async def test_a_stop_during_monitoring_ends_all_order_work_within_the_budget(alerts):  # T20
    """SIGTERM at t=1 while the monitor works a resting exit: the monitor waits for it,
    CLOSING works every position concurrently, and nothing is placed after place_by or
    worked after settle_by (budget 150 - 60 = 90 s). Every order rests; cancels confirm.

    Virtual timeline: the monitor's exit 0-50 s (5 attempts), CLOSING's three exits from
    50 s, their 4th attempt (80 s) cut by place_by (79 s), no second pass."""
    settings = FillSettings(exit_max_attempts=5, shutdown_budget_s=90.0)
    live = LiveDaemon({"TCS": (10, 100.0), "INFY": (5, 100.0), "RELIANCE": (3, 100.0)},
                      ltps={TCS: 90.0}, fill_settings=settings,
                      scheduler_kill_after_seconds=150)
    report = _report()

    async def sigterm():
        await live.clock.sleep(1)
        live.stop.set()

    armer = asyncio.create_task(live.daemon._arm_on_stop())
    stopper = asyncio.create_task(sigterm())
    await live.daemon._phase_monitor()
    await live.daemon._phase_close(report)
    await stopper
    armer.cancel()

    deadlines = live.router.deadlines
    settle_by, place_by = deadlines.settle_by(), deadlines.place_by()
    assert settle_by == pytest.approx(1 + 90)
    assert place_by == pytest.approx(settle_by - (10 + 2 * 1))
    assert max(o.placed_at for o in live.broker.placed_orders()) <= place_by
    assert max(live.order_times("cancel_order")) <= settle_by
    for symbol in ("INFY", "RELIANCE"):                       # CLOSING tried each of them
        assert live.broker.placed_orders(symbol)
        assert "the shutdown/close deadline" in alerts.text(f"exit-not-filled:{symbol}")
    assert sorted(report.positions_left) == ["INFY", "RELIANCE", "TCS"]
    assert "positions-left:2026-09-25:daemon" in alerts.keys("CRITICAL")


async def test_a_stop_arms_the_router_deadline_even_before_pre_open_built_it():
    live = LiveDaemon({})
    router = live.daemon._router
    live.daemon._router = None
    live.stop.set()
    armer = asyncio.create_task(live.daemon._arm_on_stop())
    await asyncio.sleep(0)
    assert not router.deadlines.stopping                    # nothing to arm yet
    live.daemon._router = router
    live.daemon._arm_shutdown()                             # what run_session does after PRE_OPEN
    armer.cancel()
    assert router.deadlines.settle_by() == pytest.approx(240)


# ── Review fixes: a confirmed BUY that positions do not show yet ─────────────


async def test_monitoring_waits_for_a_confirmed_buy_positions_do_not_show_yet(alerts):
    from skopaq.broker.models import TradingSignal

    live = LiveDaemon({"TCS": (0, 101.0)})
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(5, "101"))])]
    bought = await live.daemon._executor.execute_signal(TradingSignal(
        symbol="TCS", action="BUY", confidence=80, entry_price=101.0,
        order_type=OrderType.LIMIT, quantity=Decimal(5), stop_loss=95.0))
    assert bought.success and bought.filled_quantity == 5
    t_fill = live.clock.t
    real_positions = live.broker.get_positions

    async def lagging():
        live.broker.lag = live.clock.t < t_fill + 30
        return await real_positions()

    live.broker.get_positions = lagging
    live.stop_later = asyncio.create_task(_stop_at(live, t_fill + 120))
    result = await live.daemon._phase_monitor()

    assert result.positions_monitored == 1


async def _stop_at(live, t):
    while live.clock.t < t:
        await asyncio.sleep(0)
    live.stop.set()


async def test_closing_reports_a_confirmed_buy_positions_do_not_show(alerts):
    live = LiveDaemon({"TCS": (0, 101.0)}, lag=True)
    live.router.registry.record_confirmed_buy("TCS", "11536", Decimal(5), order_id="EQ-9")
    report = _report()

    await live.daemon._phase_close(report)

    assert live.broker.placed() == []                 # nothing to sell yet (not in positions)
    assert report.positions_left == ["TCS"]
    assert "exit-blocked:TCS:unshown-buy" in alerts.keys("CRITICAL")
    assert [k for k in alerts.keys("CRITICAL") if k.startswith("positions-left:")]


async def test_closing_does_not_resell_shares_the_user_sold_on_bse(alerts):
    live = LiveDaemon({"TCS": (10, 100.0)})
    real_positions = live.broker.get_positions

    async def positions():
        rows = await real_positions()
        for p in rows:
            p.exchange, p.isin = "NSE", "INE467B01029"
        rows.append(Position(symbol="TCS", security_id="532540", exchange="BSE",
                             isin="INE467B01029", product="CNC", quantity=Decimal(-10),
                             sell_quantity=Decimal(10), average_price=100.0))
        return rows

    live.broker.get_positions = positions
    live.broker.extra_rows = [{
        "id": "EQ-77", "txn_type": "SELL", "status": "SUCCESS", "security_id": "532540",
        "exchange": "BSE", "isin": "INE467B01029", "product": "CNC", "requested_qty": 10,
        "traded_qty": 10, "traded_price": "99", "updated_at": live.clock.wall().isoformat()}]
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(10))])]
    report = _report()

    await live.daemon._phase_close(report)

    assert live.broker.placed_orders("TCS") == []          # a second sale would be a short
    assert report.positions_left == []


async def test_closing_keeps_resuming_its_own_stuck_exit_while_time_remains(alerts):
    # CLOSING at 15:20 (no stop; ten minutes left). Our own stuck exit's cancel is
    # confirmed only on the 7th try (~12 s): CLOSING resumes it again, then sells
    live = LiveDaemon({"TCS": (10, 100.0)}, wall=datetime(2026, 9, 25, 15, 20, tzinfo=IST))
    stuck = await live.stuck_exit("TCS", 10, Script(on_cancel=[IGNORE] * 6))
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(10, "99"))])]
    report = _report()

    await live.daemon._phase_close(report)

    assert len([c for c in live.broker.calls if c[0] == "cancel_order" and c[1] == stuck]) >= 7
    assert len(live.broker.placed()) == 2             # the stuck exit, then the new SELL
    assert report.positions_left == []


async def test_a_stop_during_analyzing_still_sends_the_rejected_buy_notification():
    """A live trade notification is sent in the background; run_session drains it on every
    way out — also the early return after a stop during ANALYZING with nothing opened."""
    from skopaq.broker.models import TradingSignal

    live = LiveDaemon({})
    d = live.daemon
    live.broker.place_effects = [Script(timeline=[
        (0.0, {"status": "PENDING"}), (1.0, {"status": "REJECTED", "extra_info": "RMS: margin"})])]
    sent = []

    async def slow_notify(text, *args, **kwargs):
        await asyncio.sleep(0.05)                 # a Telegram round trip
        sent.append(text.splitlines()[0])

    async def trade(candidates, report):
        result = await d._executor.execute_signal(TradingSignal(
            symbol="TCS", action="BUY", confidence=80, entry_price=100.0,
            order_type=OrderType.LIMIT, quantity=Decimal(5)))
        assert result.outcome == "rejected"
        live.stop.set()                           # SIGTERM arrives now
        report.trades_rejected += 1
        return []

    with patch.object(d, "_phase_pre_open", new_callable=AsyncMock), \
         patch.object(d, "_halt_status", return_value=MagicMock(halted=False)), \
         patch.object(d, "_phase_scan", new_callable=AsyncMock, return_value=["TCS"]), \
         patch.object(d, "_phase_analyze_and_trade", side_effect=trade), \
         patch.object(d, "_settle_due_decisions", new_callable=AsyncMock, return_value=0), \
         patch.object(d, "_notify_report", new_callable=AsyncMock), \
         patch("skopaq.notifications.notify", slow_notify):
        await d.run_session()

    assert len(sent) == 1 and "BUY TCS" in sent[0] and "FAILED" in sent[0], sent


async def test_closing_retries_an_exit_the_order_rate_refused_once_the_window_frees(
        alerts, monkeypatch):
    """CLOSING at 15:21: MONITORING's three exits were validated 10 s ago and the rules allow
    5 orders a minute, so the third CLOSING exit is refused. The next pass waits for the
    rate window to free (t=50) instead of retrying 5 s later and giving up."""
    import datetime as _dt

    from skopaq.execution import safety_checker as sc

    wall = datetime(2026, 9, 25, 15, 21, tzinfo=IST)
    live = LiveDaemon({"TCS": (10, 100.0), "INFY": (5, 100.0), "RELIANCE": (3, 100.0)},
                      wall=wall)
    clock = live.clock

    def filled(n):
        return Script(timeline=[(0, {"status": "SUCCESS", "traded_qty": n,
                                     "traded_price": "99"})])

    live.broker.by_symbol = {"TCS": [filled(10)] * 3, "INFY": [filled(5)] * 3,
                             "RELIANCE": [filled(3)] * 3}

    class ClockDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            w = clock.wall()
            return w.astimezone(tz) if tz else w.replace(tzinfo=None)

    monkeypatch.setattr(sc, "datetime", ClockDateTime)
    checker = SafetyChecker(rules=SafetyRules(market_hours_only=False, require_stop_loss=False,
                                              max_orders_per_minute=5))
    live.daemon._executor = Executor(live.router, checker, clock=clock.clock)
    ten_s_ago = (clock.wall() - _dt.timedelta(seconds=10)).astimezone(_dt.timezone.utc)
    checker._orders_this_minute = [ten_s_ago] * 3        # MONITORING's three exits
    report = _report()

    await live.daemon._phase_close(report)

    placed = {o.request.symbol: o.placed_at for o in live.broker.placed_orders()}
    assert set(placed) == {"TCS", "INFY", "RELIANCE"}, placed
    assert placed["RELIANCE"] >= 50.0                    # once the window had room again
    assert report.positions_left == []
