"""Trade rows book every share the broker confirmed exactly once — through the real
recorders (``_record_exit``, ``_record_late_fill``, the lifecycle) into an in-memory trades
table (the loss limits read realized P&L back from it).

Regressions from the v4 review (accounting verifier):

- A stuck order's progress is booked when a process sees it — a stuck BUY's shares before
  any exit sells them, and progress seen only by the last process of the day — and the
  booked total is shared across processes through the journal, read under the order
  lock, so two processes never book the same shares.
- With the lock directory unusable, a final late fill is booked once (claimed through
  the journal) and progress is not booked by either process.
- A paper SELL never closes live BUY rows, and a refused one closes nothing.
- A live SELL spanning more than five open rows closes them all.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from skopaq.broker.models import ExecutionResult, OrderType, Side, TradingSignal
from skopaq.broker.paper_engine import PaperEngine
from skopaq.constants import PAPER_SAFETY_RULES
from skopaq.db.models import TradeRecord
from skopaq.execution.executor import Executor
from skopaq.execution.order_journal import OrderJournal
from skopaq.execution.order_router import OrderRouter
from skopaq.execution.position_monitor import MonitorResult
from skopaq.execution.safety_checker import SafetyChecker
from skopaq.risk.calendar import IST
from tests.unit.execution._fakes import IGNORE, Script
from tests.unit.execution._memrepo import cli_config, memrepo  # noqa: F401
from tests.unit.execution.test_daemon_live import LiveDaemon, _report
from tests.unit.execution.test_daemon_live import _lookups as _daemon_lookups  # noqa: F401
from tests.unit.execution.test_fill_recording import _second_process, _stuck_exit
from tests.unit.execution.test_position_monitor_live import (  # noqa: F401
    TCS,
    Live,
    _filled,
    _lookups,
    alerts,
)

FIRST = pytest.mark.parametrize("first", ["A", "B"])


def _recorders(monitor, cfg):
    """Route a monitor's exits and late fills to the real recorders."""
    from skopaq.cli.main import _record_exit, _record_late_fill

    monitor._on_exit = lambda s, e: _record_exit(cfg, None, None, s, e)
    monitor._on_late_fill = lambda t, c: _record_late_fill(cfg, None, None, t, c)


async def _cycle(monitor, positions, result, *, first=False):
    await monitor._resync(positions, result, first=first)
    await monitor._settle_resumes(result)


def _open_live_row(memrepo, qty, price, *, order_id=None, older=False):
    row = memrepo.insert(TradeRecord(symbol="TCS", side="BUY", quantity=Decimal(qty),
                                     order_id=order_id, fill_price=Decimal(price),
                                     is_paper=False, status="COMPLETE"))
    if older:          # a lot carried from an earlier day
        memrepo.rows[row.id] = row.model_copy(
            update={"created_at": datetime(2026, 9, 20, tzinfo=timezone.utc)})
    return row


# ── Progress is booked when it is seen ───────────────────────────────────────


@pytest.mark.parametrize("older_lot", [False, True])
async def test_a_stuck_buys_progress_is_booked_before_an_exit_sells_it(alerts, memrepo,
                                                                     older_lot):
    """The daemon's LIMIT BUY 10 @100 reported 4 (row EQ-1: 4 @100); its cancel was never
    confirmed and it keeps buying (7 by t=20). The monitor adopts what positions hold and
    its hard stop sells it; the BUY is then final at 7. Booked: 7 bought, 7 sold at 90."""
    cfg = cli_config()
    if older_lot:
        _open_live_row(memrepo, 5, 95, older=True)
    live = Live({"TCS": (0, 100.0)}, ltps={TCS: 90.0})
    _recorders(live.monitor, cfg)
    buy = await live.open_order("TCS", Side.BUY, 10, Script(
        timeline=[(0, {"status": "PENDING", "traded_qty": 4, "traded_price": "100"}),
                  (20, {"status": "PENDING", "traded_qty": 7, "traded_price": "100"})],
        on_cancel=[IGNORE] * 5000), order_type=OrderType.LIMIT)
    tracked = live.track(buy, "TCS", "BUY", 10, state="stuck", purpose="entry")
    tracked.filled_reported, tracked.avg_price_reported = Decimal(4), Decimal(100)
    _open_live_row(memrepo, 4, 100, order_id=buy)
    await live.clock.sleep(25)

    positions, result = [], MonitorResult()
    await _cycle(live.monitor, positions, result, first=True)
    assert memrepo.find_by_order_id(buy).quantity == 7          # progress booked when seen
    live.broker.place_effects = [Script(timeline=[(0, _filled(7, "90"))])]
    await live.monitor._check_positions(positions, 1, result)     # the rule tier: hard stop
    await positions[0].exit_task
    # The BUY's cancel is finally confirmed: final, 7 bought
    live.broker.orders[buy].override = {"status": "PARTIALLY FILLED - CANCELLED",
                                        "traded_qty": 7, "traded_price": "100"}
    await _cycle(live.monitor, positions, result)

    assert memrepo.open_buy_qty("TCS") == (5 if older_lot else 0)
    assert memrepo.sold("TCS") == 7
    assert memrepo.realized() == Decimal(-70)                     # 7 x (90 - 100)


async def test_progress_seen_only_by_the_last_process_of_the_day_is_booked(alerts):
    """The daemon's exit EQ-1 of 10 reported 4 (booked); its cancels are ignored and it
    trades to 7 while CLOSING resumes it until the last order time. The exchange cancels
    the rest at the close (final: 7) after the daemon has ended, and nothing reads the
    order again (the next day reads the next day's journal)."""
    live = LiveDaemon({"TCS": (10, 100.0)}, wall=datetime(2026, 9, 25, 15, 20, tzinfo=IST))
    stuck = await live.stuck_exit("TCS", 10, Script(
        timeline=[(0, {"status": "PENDING", "traded_qty": 4, "traded_price": "90"}),
                  (60, {"status": "PENDING", "traded_qty": 7, "traded_price": "89.9"})],
        on_cancel=[IGNORE] * 5000))
    tracked = live.router.registry.get(stuck)
    tracked.filled_reported, tracked.avg_price_reported = Decimal(4), Decimal(90)
    live.router.journal.record("stuck", order_id=stuck, symbol="TCS", security_id="11536",
                               segment="EQUITY", side="SELL", qty=Decimal(10),
                               purpose="exit", filled=Decimal(4), avg_price=Decimal(90))
    deltas = []

    async def record_late_fill(t, c):
        deltas.append(c.filled_qty - t.filled_reported)

    live.daemon._record_late_fill = record_late_fill
    await live.daemon._phase_close(_report())
    live.broker.orders[stuck].override = {"status": "PARTIALLY FILLED - CANCELLED",
                                          "traded_qty": 7, "traded_price": "89.9571"}
    tomorrow = OrderJournal(live.router.journal.directory,
                            wall=lambda: datetime(2026, 9, 26, 9, 15, tzinfo=IST))

    assert tomorrow.today_unresolved() == []
    assert 4 + sum(deltas) == 7                   # sold at the broker: 7
    latest = live.router.journal.latest_by_order()[stuck]
    assert (latest["event"], latest["reported"]) == ("stuck", "7")


async def test_a_stuck_exit_filling_in_pieces_is_booked_once_across_two_processes(
        alerts, memrepo):
    """Two lots are open: an older 5 @95 and today's 10 @100. Today's stop-loss exit of 10
    is stuck and trades 4, then 7, then all 10, while the daemon A and a `skopaq monitor`
    B both resume it."""
    cfg = cli_config()
    _open_live_row(memrepo, 5, 95, older=True)
    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    positions, result = await _stuck_exit(a)
    b = _second_process(a, [])
    _recorders(b, cfg)
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    a.broker.orders["EQ-1"].override = {"status": "PENDING", "traded_qty": 7,
                                         "traded_price": "89.9"}
    await _cycle(b, b_pos, b_res)
    await _cycle(a.monitor, positions, result)
    assert memrepo.sold("TCS") == 7                # the progress, booked once
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(b, b_pos, b_res)
    await _cycle(a.monitor, positions, result)

    assert memrepo.sold("TCS") == 10
    assert memrepo.open_buy_qty("TCS") == 5        # the older lot is still held


async def test_progress_of_a_stuck_exit_across_two_processes_totals_what_was_sold(alerts):
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)       # the daemon
    positions, result = await _stuck_exit(a)                           # A reported 4
    [(_, reported)] = a.exits
    booked: list = []

    async def a_late(tracked, conf):
        booked.append(("A", conf.filled_qty - tracked.filled_reported))

    a.monitor._on_late_fill = a_late
    late: list = []
    b = _second_process(a, late)
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    a.broker.orders["EQ-1"].override = {"status": "PENDING", "traded_qty": 7,
                                         "traded_price": "89.9"}
    await _cycle(b, b_pos, b_res)
    await _cycle(a.monitor, positions, result)
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(b, b_pos, b_res)
    await _cycle(a.monitor, positions, result)

    booked += [(who, qty) for who, _, qty in late]
    assert int(reported.filled_quantity) + sum(q for _, q in booked) == 10, booked


@FIRST
async def test_a_stuck_sell_books_its_true_fills_whichever_process_reads_first(
        alerts, memrepo, first):
    cfg = cli_config()
    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    positions, result = await _stuck_exit(a)
    b = _second_process(a, [])
    _recorders(b, cfg)
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    order = [(b, b_pos, b_res), (a.monitor, positions, result)]
    if first == "A":
        order.reverse()
    for override in ({"status": "PENDING", "traded_qty": 7, "traded_price": "89.9"},
                     _filled(10, "89.8")):
        a.broker.orders["EQ-1"].override = override
        for monitor, p, r in order:
            await _cycle(monitor, p, r)

    assert memrepo.sold("TCS") == 10 and memrepo.open_buy_qty("TCS") == 0


@FIRST
async def test_a_stuck_buy_books_its_true_fills_whichever_process_reads_first(
        alerts, memrepo, first):
    cfg = cli_config()
    a = Live({"TCS": (0, 100.0)}, ltps={TCS: 100.0}, lag=True)
    _recorders(a.monitor, cfg)
    buy = await a.open_order("TCS", Side.BUY, 10, Script(
        timeline=[(0, {"status": "PENDING", "traded_qty": 4, "traded_price": "100"})],
        on_cancel=[IGNORE] * 5000), order_type=OrderType.LIMIT)
    tracked = a.track(buy, "TCS", "BUY", 10, state="stuck", purpose="entry")
    tracked.filled_reported, tracked.avg_price_reported = Decimal(4), Decimal(100)
    a.router.journal.record("stuck", order_id=buy, symbol="TCS", security_id="11536",
                            segment="EQUITY", side="BUY", qty=Decimal(10), purpose="entry",
                            filled=Decimal(4), avg_price=Decimal(100))
    _open_live_row(memrepo, 4, 100, order_id=buy)
    b = _second_process(a, [])
    _recorders(b, cfg)
    positions, result, b_pos, b_res = [], MonitorResult(), [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    await _cycle(a.monitor, positions, result, first=True)
    order = [(b, b_pos, b_res), (a.monitor, positions, result)]
    if first == "A":
        order.reverse()
    for override in ({"status": "PENDING", "traded_qty": 7, "traded_price": "101"},
                     _filled(10, "101.2")):
        a.broker.orders[buy].override = override
        for monitor, p, r in order:
            await _cycle(monitor, p, r)

    assert memrepo.open_buy_qty("TCS") == 10
    row = memrepo.find_by_order_id(buy)
    assert row.quantity == 10 and row.fill_price == Decimal("101.2")


# ── The lock directory unusable ──────────────────────────────────────────────


def _without_locks(*routers):
    for router in routers:
        router._lock_dir = None


async def _slow_reads(broker):
    """A real HTTP read yields to the event loop: two resumes interleave."""
    real = broker.get_order

    async def get_order(*args, **kwargs):
        for _ in range(3):
            await asyncio.sleep(0)
        return await real(*args, **kwargs)

    broker.get_order = get_order


async def test_a_final_late_fill_is_booked_once_without_the_order_lock(alerts):
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _stuck_exit(a)                           # A reported 4
    late: list = []

    async def a_late(t, c):
        late.append(("A", c.filled_qty - t.filled_reported))

    a.monitor._on_late_fill = a_late
    b = _second_process(a, late)
    _without_locks(a.router, b._router)
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    await _slow_reads(a.broker)
    a.broker.orders["EQ-1"].override = _filled(10, "90")
    await asyncio.gather(b._resync(b_pos, b_res), a.monitor._resync(positions, result))
    await asyncio.gather(b._settle_resumes(b_res), a.monitor._settle_resumes(result))

    assert sum(q for *_, q in late) == 6, late


async def test_progress_is_not_booked_twice_without_the_order_lock(alerts):
    """Without the lock neither process books progress (both would); the final late fill
    is claimed once."""
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _stuck_exit(a)                           # A reported 4
    late: list = []

    async def a_late(t, c):
        late.append(("A", c.filled_qty - t.filled_reported))

    a.monitor._on_late_fill = a_late
    b = _second_process(a, late)
    _without_locks(a.router, b._router)
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    await _slow_reads(a.broker)
    a.broker.orders["EQ-1"].override = {"status": "PENDING", "traded_qty": 7,
                                         "traded_price": "89.9"}
    await asyncio.gather(_cycle(b, b_pos, b_res), _cycle(a.monitor, positions, result))
    assert late == []
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await asyncio.gather(_cycle(b, b_pos, b_res), _cycle(a.monitor, positions, result))

    assert sum(q for *_, q in late) == 6, late


async def test_an_unusable_lock_file_counts_as_no_lock(alerts, tmp_path):
    """The lock directory is set but cannot be used (a file where the directory should be):
    the lock is not held, so the fill is claimed through the journal."""
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _stuck_exit(a)
    late: list = []

    async def a_late(t, c):
        late.append(("A", c.filled_qty - t.filled_reported))

    a.monitor._on_late_fill = a_late
    b = _second_process(a, late)
    blocked = tmp_path / "not-a-dir"
    blocked.write_text("")
    for router in (a.router, b._router):
        router._lock_dir = blocked
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    await _slow_reads(a.broker)
    a.broker.orders["EQ-1"].override = _filled(10, "90")
    await asyncio.gather(b._resync(b_pos, b_res), a.monitor._resync(positions, result))
    await asyncio.gather(b._settle_resumes(b_res), a.monitor._settle_resumes(result))

    assert sum(q for *_, q in late) == 6, late


# ── The lifecycle ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("paper_row", [False, True])
async def test_a_refused_paper_sell_closes_no_row(memrepo, monkeypatch, paper_row):
    from skopaq.cli.main import _run_lifecycle
    from skopaq.graph.skopaq_graph import AnalysisResult

    monkeypatch.setattr("skopaq.notifications.notify", AsyncMock())
    row = memrepo.insert(TradeRecord(symbol="TCS", side="BUY", quantity=Decimal(10),
                                     order_id="EQ-0", fill_price=Decimal(100),
                                     is_paper=paper_row, status="COMPLETE"))
    cfg = cli_config("paper")
    executor = Executor(OrderRouter(cfg, PaperEngine()),
                        SafetyChecker(rules=PAPER_SAFETY_RULES))
    signal = TradingSignal(symbol="TCS", action="SELL", entry_price=90.0,
                           quantity=Decimal(10), confidence=80, stop_loss=95.0)
    result = await executor.execute_signal(signal)       # the paper engine holds no TCS
    assert not result.success
    await _run_lifecycle(cfg, None, None, AnalysisResult(
        symbol="TCS", trade_date="2026-09-25", signal=signal, execution=result))

    assert memrepo.rows[row.id].closed_at is None
    assert memrepo.realized(is_paper=False) == 0 and memrepo.realized(is_paper=True) == 0


async def test_a_paper_sell_closes_the_paper_row_never_the_live_one(memrepo):
    from skopaq.cli.main import _run_lifecycle
    from skopaq.graph.skopaq_graph import AnalysisResult

    paper = memrepo.insert(TradeRecord(symbol="TCS", side="BUY", quantity=Decimal(10),
                                       fill_price=Decimal(100), is_paper=True,
                                       status="COMPLETE"))
    live = memrepo.insert(TradeRecord(symbol="TCS", side="BUY", quantity=Decimal(10),
                                      order_id="EQ-0", fill_price=Decimal(100),
                                      is_paper=False, status="COMPLETE"))   # the newest
    signal = TradingSignal(symbol="TCS", action="SELL", entry_price=90.0,
                           quantity=Decimal(10), confidence=80)
    execution = ExecutionResult(success=True, signal=signal, mode="paper", fill_price=90.0)
    await _run_lifecycle(cli_config("paper"), None, None, AnalysisResult(
        symbol="TCS", trade_date="2026-09-25", signal=signal, execution=execution))

    assert memrepo.rows[live.id].closed_at is None
    assert memrepo.rows[paper.id].closed_at is not None
    assert memrepo.realized(is_paper=True) == -100 and memrepo.realized(is_paper=False) == 0


async def test_a_live_sell_spanning_more_than_five_rows_closes_them_all(memrepo):
    from skopaq.cli.main import _record_exit

    for i in range(7):
        memrepo.insert(TradeRecord(symbol="TCS", side="BUY", quantity=Decimal(2),
                                   fill_price=Decimal(100), is_paper=False, status="COMPLETE",
                                   order_id=f"EQ-{i}"))
    signal = TradingSignal(symbol="TCS", action="SELL", entry_price=90.0,
                           order_type=OrderType.MARKET, quantity=Decimal(14))
    execution = ExecutionResult(success=True, signal=signal, mode="live", fill_price=90.0,
                                filled_quantity=Decimal(14), requested_quantity=Decimal(14),
                                outcome="filled", order_ids=["EQ-9"])
    await _record_exit(cli_config(), None, None, signal, execution)

    assert memrepo.open_buy_qty("TCS") == 0
    assert memrepo.realized() == Decimal(-140)
