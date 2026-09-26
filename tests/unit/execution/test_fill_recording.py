"""A fill the broker confirmed is recorded exactly once — never lost to a cancellation,
never recorded by two Skopaq processes.

- Once an order is final in the registry and the journal, nothing would record it again:
  its recording (an exit, a late fill) runs to the end even if the task doing it is
  cancelled, and abandoned orders are recorded one by one as each resume ends.
- One process at a time resumes an order and records its late fill (a per-order lock on
  the shared lock directory); what another process has recorded of it (its progress, or
  its final fill) is read from the journal under that lock and not recorded again.

Virtual time (FakeClock) over a scripted broker (PositionBroker).
"""

from __future__ import annotations

import asyncio
import fcntl
import os

from skopaq.broker.paper_engine import PaperEngine
from skopaq.execution.executor import Executor
from skopaq.execution.order_router import OrderRouter
from skopaq.execution.position_monitor import (
    MonitorResult,
    PositionMonitor,
    record_late_fill,
    resume_order,
)
from skopaq.execution.safety_checker import SafetyChecker
from tests.unit.execution._fakes import IGNORE, Script
from tests.unit.execution.test_position_monitor_live import (  # noqa: F401
    RULES,
    TCS,
    Live,
    _config,
    _filled,
    _lookups,
    alerts,
)

PARTIAL_4 = {"status": "PENDING", "traded_qty": 4, "traded_price": "90"}


async def _tracked_position(live: Live):
    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    return positions, result


# ── Never lost to a cancellation ─────────────────────────────────────────────


async def test_an_abandoned_exit_records_each_order_as_its_own_resume_ends(alerts,
                                                                          monkeypatch):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _tracked_position(live)
    [pos] = positions
    live.broker.place_effects = [
        # attempt 1: 4 trade, the rest is cancelled at the attempt timeout (final: 4)
        Script(timeline=[(0, {"status": "PENDING"}), (2, PARTIAL_4)]),
        # attempt 2: the broker acknowledges cancels but the order keeps resting
        Script(on_cancel=[IGNORE] * 50),
    ]
    pos.exit_task = asyncio.create_task(live.monitor._execute_sell(pos, 90.0, "HARD STOP",
                                                                   result))
    while live.clock.t < 15:
        await asyncio.sleep(0)

    # Once the abandoned orders are resumed, one GET /order for EQ-2 stalls for 30 s
    import skopaq.execution.position_monitor as pm

    real_resume, real_get_order = pm.resume_orders, live.broker.get_order
    resuming, stalled = [], []

    async def resume_orders(*args, **kwargs):
        resuming.append(live.clock.t)
        return await real_resume(*args, **kwargs)

    async def slow_get_order(order_id, segment="EQUITY"):
        if order_id == "EQ-2" and resuming and not stalled:
            stalled.append(live.clock.t)
            await live.clock.sleep(30)
        return await real_get_order(order_id, segment)

    monkeypatch.setattr(pm, "resume_orders", resume_orders)
    live.broker.get_order = slow_get_order
    await live.monitor._abandon_exits(positions, result)

    assert stalled                                   # the stall did hit a resume
    assert [(t.order_id, c.filled_qty) for t, c in live.late] == [("EQ-1", 4)]
    assert result.late_fills == 1
    registry, journal = live.router.registry, live.router.journal
    assert registry.get("EQ-1").state == "final"
    # EQ-2's resume was cut short: still unresolved here and in the journal (resumed by
    # the next resync, CLOSING or `skopaq monitor`)
    assert [t.order_id for t in registry.unresolved()] == ["EQ-2"]
    assert [e["order_id"] for e in journal.today_unresolved()] == ["EQ-2"]
    assert journal.latest_by_order()["EQ-2"]["event"] == "interrupted"


async def test_an_exit_cancelled_while_its_notification_is_sent_is_recorded(alerts,
                                                                            monkeypatch):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _tracked_position(live)
    [pos] = positions
    live.broker.place_effects = [Script(timeline=[(0, {"status": "PENDING"}),
                                                  (1, _filled(10, "90"))])]

    async def slow_notify(*args, **kwargs):          # Telegram taking a few seconds
        await live.clock.sleep(5)

    monkeypatch.setattr("skopaq.notifications.notify_trade_event", slow_notify)
    pos.exit_task = asyncio.create_task(live.monitor._execute_sell(pos, 90.0, "EOD exit",
                                                                   result))
    await live.clock.sleep(3)          # filled at t=1; the notification is still being sent
    await live.monitor._abandon_exits(positions, result)
    await live.router.registry.drain_recordings()

    [(signal, execution)] = live.exits
    assert execution.filled_quantity == 10 and signal.symbol == "TCS"


async def test_an_exit_cancelled_while_it_is_recorded_finishes_the_recording(alerts):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _tracked_position(live)
    [pos] = positions
    live.broker.place_effects = [Script(timeline=[(0, _filled(10, "90"))])]
    written = []

    async def slow_on_exit(signal, execution):       # a slow trades-table write
        await live.clock.sleep(5)
        written.append(execution.filled_quantity)

    live.monitor._on_exit = slow_on_exit
    pos.exit_task = asyncio.create_task(live.monitor._execute_sell(pos, 90.0, "EOD exit",
                                                                   result))
    await live.clock.sleep(2)
    pos.exit_task.cancel()
    await asyncio.wait([pos.exit_task])
    await live.router.registry.drain_recordings()

    assert written == [10]


async def test_a_late_fill_whose_recording_is_cancelled_is_still_recorded(alerts):
    live = Live({"TCS": (10, 100.0)})
    order_id = await live.open_order("TCS", "SELL", 10, Script(timeline=[(0, _filled(10))]))
    tracked = live.track(order_id, "TCS", "SELL", 10, state="stuck", purpose="exit")
    conf = await live.router.worker.resume(tracked, cancel=True)
    recorded = []

    async def slow_late_fill(t, c):
        await live.clock.sleep(5)
        recorded.append(c.filled_qty)

    task = asyncio.create_task(record_late_fill(live.router, tracked, conf, slow_late_fill))
    await live.clock.sleep(1)
    task.cancel()
    await asyncio.wait([task])
    await live.router.registry.drain_recordings()

    assert recorded == [10] and tracked.filled_reported == 10


# ── Recorded by one process only ─────────────────────────────────────────────


def _second_process(live: Live, late: list) -> PositionMonitor:
    """Another Skopaq process on the host: its own router and registry; the same broker,
    journal directory and lock directory."""
    config = _config()
    router = OrderRouter(config, PaperEngine(), live_client=live.broker,
                         sleep=live.clock.sleep, clock=live.clock.clock, wall=live.clock.wall)
    executor = Executor(router, SafetyChecker(rules=RULES), clock=live.clock.clock)

    async def on_late_fill(tracked, conf):
        late.append(("B", tracked.order_id, conf.filled_qty - tracked.filled_reported))

    return PositionMonitor(executor, live.broker, router, config, stop_event=asyncio.Event(),
                           sell_on_stop=True, on_late_fill=on_late_fill,
                           sleep=live.clock.sleep, wall=live.clock.wall)


async def _stuck_exit(a: Live):
    """Process A's exit of 10 trades 4, then the broker ignores its cancels (stuck)."""
    positions, result = await _tracked_position(a)
    [pos] = positions
    a.broker.place_effects = [Script(timeline=[(0, {"status": "PENDING"}), (2, PARTIAL_4)],
                                     on_cancel=[IGNORE] * 200)]
    await a.monitor._execute_sell(pos, 90.0, "HARD STOP", result)
    return positions, result


async def test_a_stuck_exit_known_to_two_processes_is_recorded_once(alerts):
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)     # the daemon
    positions, result = await _stuck_exit(a)
    [(_, reported)] = a.exits
    assert reported.filled_quantity == 4

    late: list = []
    b = _second_process(a, late)                     # a `skopaq monitor` started meanwhile
    b_positions, b_result = [], MonitorResult()
    await b._resync(b_positions, b_result, first=True)
    await b._settle_resumes(b_result)                # (resumes run in the background)
    assert [t.order_id for t in b._router.registry.unresolved()] == ["EQ-1"]

    a.broker.orders["EQ-1"].override = _filled(10, "90")      # it completes at the broker
    await b._resync(b_positions, b_result)
    await b._settle_resumes(b_result)
    await a.monitor._resync(positions, result)
    await a.monitor._settle_resumes(result)

    late += [("A", t.order_id, c.filled_qty) for t, c in a.late]
    assert late == [("B", "EQ-1", 6)]                # the 6 late shares, recorded once
    assert a.router.registry.get("EQ-1").state == "final"
    assert a.router.registry.unresolved() == []


async def test_an_order_another_process_is_resuming_is_left_to_it(alerts, tmp_path):
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    await _stuck_exit(a)
    tracked = a.router.registry.get("EQ-1")
    a.broker.orders["EQ-1"].override = _filled(10, "90")

    lock = a.router.order_lock("EQ-1")
    fd = os.open(lock.path, os.O_RDWR | os.O_CREAT, 0o644)     # held by another process
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert await resume_order(a.router, tracked, a.monitor._on_late_fill) == 0
        assert a.late == [] and tracked.state == "stuck"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    assert await resume_order(a.router, tracked, a.monitor._on_late_fill) == 1
    assert [(t.order_id, c.filled_qty) for t, c in a.late] == [("EQ-1", 10)]


async def test_a_stuck_exit_filling_in_pieces_across_two_processes_is_recorded_once(alerts):
    """Progress on a still-working order is recorded by the first process to see it, and
    journalled as reported: the daemon A and a `skopaq monitor` B never both book 4 -> 7."""
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)     # the daemon
    positions, result = await _stuck_exit(a)                       # A reported 4
    [(_, reported)] = a.exits
    assert reported.filled_quantity == 4

    late: list = []
    b = _second_process(a, late)
    b_positions, b_result = [], MonitorResult()
    await b._resync(b_positions, b_result, first=True)
    await b._settle_resumes(b_result)

    async def resync_both():
        await b._resync(b_positions, b_result)
        await b._settle_resumes(b_result)
        await a.monitor._resync(positions, result)
        await a.monitor._settle_resumes(result)

    # It trades 3 more (7 of 10) and keeps working (cancels still ignored) ...
    a.broker.orders["EQ-1"].override = {"status": "PENDING", "traded_qty": 7,
                                         "traded_price": "89.9"}
    await resync_both()
    assert late == [("B", "EQ-1", 3)] and a.late == []     # the progress, booked once
    assert a.router.registry.get("EQ-1").filled_reported == 7   # read from the journal
    # ... and the sellable checks count the 7 sold
    from skopaq.execution.live_orders import recent_exit_qty

    for router in (a.router, b._router):
        assert recent_exit_qty(router.registry, router.journal, "TCS", "11536", 600,
                               a.clock.wall()) == 7

    # ... then it completes at the broker
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await resync_both()

    late += [("A", t.order_id, c.filled_qty - 7) for t, c in a.late]
    assert late in ([("B", "EQ-1", 3), ("B", "EQ-1", 3)],
                    [("B", "EQ-1", 3), ("A", "EQ-1", 3)]), late
    assert int(reported.filled_quantity) + sum(q for *_, q in late) == 10


async def test_progress_seen_by_a_process_that_stopped_is_still_recorded_later(alerts):
    """B saw the stuck exit at 7, recorded the 3 beyond A's 4 and stopped; a later
    `skopaq monitor` C adopts it from the journal knowing that 7 were recorded, and records
    the other 3 once final."""
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    await _stuck_exit(a)                                           # A reported 4
    a.broker.orders["EQ-1"].override = {"status": "PENDING", "traded_qty": 7,
                                         "traded_price": "89.9"}
    late: list = []
    b = _second_process(a, late)
    b_positions, b_result = [], MonitorResult()
    await b._resync(b_positions, b_result, first=True)
    await b._settle_resumes(b_result)
    assert late == [("B", "EQ-1", 3)]

    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    c = _second_process(a, late)                                   # A and B are gone
    c_positions, c_result = [], MonitorResult()
    await c._resync(c_positions, c_result, first=True)
    await c._settle_resumes(c_result)

    # (the helper labels every process "B"): 4 reported + 3 + 3 = the 10 sold
    assert late == [("B", "EQ-1", 3), ("B", "EQ-1", 3)]


async def test_a_resume_cut_short_journals_the_reported_average_price(alerts):
    """A process adopting an order left unresolved prices its late shares from what was
    reported before (4 @ 90), not from the whole order's VWAP."""
    from decimal import Decimal

    from skopaq.execution.live_orders import OrderRegistry
    from skopaq.execution.position_monitor import _left_unresolved

    live = Live({"TCS": (10, 100.0)})
    tracked = live.track("EQ-1", "TCS", "SELL", 10, state="stuck", purpose="exit")
    tracked.filled_reported, tracked.avg_price_reported = Decimal(4), Decimal(90)
    _left_unresolved(live.router, tracked)

    adopter = OrderRegistry()
    assert adopter.load_journal(live.router.journal.today_unresolved()) == 1
    adopted = adopter.get("EQ-1")
    assert (adopted.filled_reported, adopted.avg_price_reported) == (4, 90)
