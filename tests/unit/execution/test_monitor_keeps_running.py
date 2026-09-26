"""Live, the monitor keeps protecting what is held until the close — it never ends early on
reads that may have been wrong.

- A position dropped after empty positions reads (a broker glitch) is taken back as soon
  as a read shows it again, and the loop confirms with a fresh read before it ends; inside
  the daemon, MONITORING that still ends early with positions held is started again rather
  than handing them to CLOSING (a MARKET sale in the middle of the day).
- A confirmed BUY that positions never show keeps `skopaq monitor` running (not an rc 4 at
  once, restarted by the scheduler every tick); the positions-left CRITICAL goes out once a
  day for the same state, and the scheduler backs off a monitor that ends at once.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

from skopaq.execution.position_monitor import MonitorResult, PositionMonitor
from skopaq.execution.scheduler import JobResult, SchedulerState, _tick
from tests.unit.execution._fakes import BASE_WALL
from tests.unit.execution.test_daemon_live import LiveDaemon
from tests.unit.execution.test_position_monitor_live import (  # noqa: F401
    TCS,
    Live,
    _lookups,
    alerts,
)


def _glitch(broker, clock, start: float, end: float) -> list:
    """Positions read back empty (successfully) between ``start`` and ``end``."""
    real = broker.get_positions
    reads = []

    async def glitchy():
        empty = start <= clock.t < end
        reads.append((clock.t, "empty" if empty else "ok"))
        if empty:
            broker.calls.append(("get_positions",))
            return []
        return await real()

    broker.get_positions = glitchy
    return reads


async def test_a_position_dropped_on_a_glitch_is_monitored_again(alerts):
    live = Live({"TCS": (10, 100.0)}, sell_on_stop=False, monitor_poll_interval_seconds=10,
                monitor_resync_cycles=3)
    reads = _glitch(live.broker, live.clock, 45, 85)

    with patch.object(live.monitor, "_check_safety", return_value=None):
        result = await live.run(stop_at=300)

    assert ("empty" in {what for _, what in reads})
    assert live.clock.t >= 300                         # ran until the stop, not the glitch
    assert live.broker.placed() == []
    assert result.positions_left == ["TCS"]


async def test_the_daemon_does_not_close_positions_midday_after_a_glitch(alerts):
    live = LiveDaemon({"TCS": (10, 100.0)}, ltps={TCS: 100.0},
                      monitor_resync_cycles=3, monitor_poll_interval_seconds=10)
    _glitch(live.broker, live.clock, 45, 85)
    # The session's BUY of TCS was confirmed at 09:40
    live.router.registry.record_confirmed_buy("TCS", "11536", Decimal(10), order_id="EQ-0",
                                              ago_s=4800)

    async def stop_later():
        await live.clock.sleep(300)
        live.daemon._stop.set()

    stopper = asyncio.create_task(stop_later())
    await live.daemon._phase_monitor()
    ended = live.clock.t
    await stopper

    assert ended >= 300                                # MONITORING ran until the stop
    assert live.broker.placed() == []                  # nothing sold on the glitch


async def test_monitoring_is_restarted_when_it_ends_early_with_positions_held(alerts):
    live = LiveDaemon({"TCS": (10, 100.0)})
    runs = [MonitorResult(sells_executed=1, cycles=5, positions_left=["TCS"]),
            MonitorResult(cycles=7)]
    with patch.object(PositionMonitor, "run", new=AsyncMock(side_effect=runs)) as run:
        result = await live.daemon._phase_monitor()

    assert run.await_count == 2
    assert (result.sells_executed, result.cycles, result.positions_left) == (1, 12, [])


async def test_monitoring_is_not_restarted_after_a_stop_or_the_eod_exit(alerts):
    left = MonitorResult(positions_left=["TCS"])
    stopped = LiveDaemon({"TCS": (10, 100.0)})
    stopped.daemon._stop.set()
    late = LiveDaemon({"TCS": (10, 100.0)},
                      wall=BASE_WALL.replace(hour=15, minute=21))
    for live in (stopped, late):
        with patch.object(PositionMonitor, "run", new=AsyncMock(return_value=left)) as run:
            assert (await live.daemon._phase_monitor()).positions_left == ["TCS"]
        assert run.await_count == 1


async def test_a_buy_positions_never_show_keeps_the_recovery_monitor_running(alerts):
    live = Live({"TCS": (0, 101.0)}, lag=True)
    live.journal(ts=(BASE_WALL - timedelta(seconds=700)).isoformat(), event="final",
                 order_id="EQ-9", symbol="TCS", security_id="11536", side="BUY", qty="10",
                 filled="10", avg_price="101", purpose="entry")

    result = await live.run(stop_at=900)

    assert live.clock.t >= 900                         # not an rc 4 at t=0
    assert result.positions_left == ["TCS"]
    assert len([k for k in alerts.keys("CRITICAL") if k.startswith("positions-left:")]) == 1


async def test_the_positions_left_alert_goes_out_once_a_day(alerts):
    for _ in range(3):                                 # three `skopaq monitor` processes
        live = Live({"TCS": (0, 101.0)}, lag=True)
        live.journal(ts=(BASE_WALL - timedelta(seconds=700)).isoformat(), event="final",
                     order_id="EQ-9", symbol="TCS", security_id="11536", side="BUY",
                     qty="10", filled="10", avg_price="101", purpose="entry")
        live.stop.set()
        result = await live.monitor.run()
        assert result.positions_left == ["TCS"]

    assert len([k for k in alerts.keys("CRITICAL") if k.startswith("positions-left:")]) == 1


def test_the_scheduler_backs_off_a_recovery_monitor_that_ends_at_once(tmp_path):
    from tests.unit.execution.test_scheduler import MONDAY, MONDAY_DATE, Recorder, _at, _live

    settings = _live(tmp_path)
    state, rec = SchedulerState(settings.state_dir, settings.log_dir), Recorder()
    state.mark_started("daemon", MONDAY_DATE, note="2026-09-28T09:15:02+05:30")
    state.record_exit("daemon", MONDAY_DATE, 1)
    state.mark_started("monitor", MONDAY_DATE, note="2026-09-28T10:00:00+05:30")
    launches = []
    now = {"t": _at(MONDAY, "11:00")}

    def runner(cmd, **kwargs):
        launches.append(now["t"])
        return JobResult(4)                            # ends at once: positions left

    for i in range(20):                                # 20 ticks, 30 s apart
        now["t"] = _at(MONDAY, "11:00") + timedelta(seconds=30 * i)
        _tick(now["t"], settings, state, runner=runner, alert=rec.alert, ping=rec.ping,
              stop=threading.Event(), clock=lambda: now["t"])

    assert [t - launches[0] for t in launches] == [timedelta(0), timedelta(minutes=5)]
