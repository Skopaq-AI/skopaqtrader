"""Late fills are booked once across processes, and a booking that did not happen is
never counted as one — regressions from the v5 review (booking verifier).

1. A process that dies, or whose trade-row write fails, after journalling a booking left
   the fill looking booked: now ``booking`` goes before the write and ``booked`` after it,
   only ``booked`` totals count, the next booking starts from the last confirmed total,
   and a CRITICAL ``booking-unconfirmed`` alert names the order and shares. The worker's
   ``final`` line alone is not a booking.
2. A final fill read at once by a process holding the order lock and one that cannot use
   it was booked by both: every process now claims a final fill before booking it.
3. A journal that can be read but not written let both processes book: progress whose
   ``booking`` line was not written is not booked, and a claim that cannot be written
   books nothing (CRITICAL ``late-fill-unclaimed``: book it by hand).
4. Progress read without a price was left for manual booking while counted as booked: it
   is now left for the final fill, priced from the order's trades.

Two processes share one broker, journal directory and lock directory; their trade rows
go to an in-memory trades table through the real recorders.
"""
from __future__ import annotations

import asyncio
import dataclasses
from decimal import Decimal

import pytest

from skopaq.broker.models import OrderType, Side
from skopaq.db.models import TradeRecord
from skopaq.execution.order_journal import OrderJournal
from skopaq.execution.position_monitor import MonitorResult
from tests.unit.execution._fakes import IGNORE, Script
from tests.unit.execution._memrepo import cli_config, memrepo  # noqa: F401
from tests.unit.execution.test_fill_recording import _second_process, _stuck_exit
from tests.unit.execution.test_position_monitor_live import (  # noqa: F401
    TCS,
    Live,
    _filled,
    _lookups,
    alerts,
)
from tests.unit.execution.test_trade_row_booking import (
    _cycle,
    _open_live_row,
    _recorders,
    _slow_reads,
)

PROGRESS_7 = {"status": "PENDING", "traded_qty": 7, "traded_price": "89.9"}


def _summary(memrepo):
    for r in sorted(memrepo.rows.values(), key=lambda r: r.created_at):
        print(" ", r.side, r.quantity, r.fill_price, r.order_id,
              "closed" if r.closed_at else "open", r.pnl)


async def _kill_resumes(monitor):
    """The process dies: its resume tasks stop (the order lock's fd is closed), and a
    recording it had started never finishes."""
    for task in list(monitor._resumes.values()):
        task.cancel()
    for _ in range(20):
        await asyncio.sleep(0)


# ── 1. A process dies between the journal line and the trade row ────────────


async def test_sell_progress_crash_after_journal_before_row(alerts, memrepo):
    """A (daemon) books its exit's first 4. The exit is stuck and trades to 7; A's resume
    journals `stuck reported=7` and dies before the SELL row is written. B (recovery
    monitor) takes over and sees it final at 10. Truth: 10 sold."""
    cfg = cli_config()
    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    positions, result = await _stuck_exit(a)
    assert memrepo.sold("TCS") == 4

    async def dies(tracked, conf):
        await asyncio.Event().wait()          # killed while writing the trade row

    a.monitor._on_late_fill = dies
    a.broker.orders["EQ-1"].override = PROGRESS_7
    await a.monitor._resync(positions, result)
    lines = [e for e in a.router.journal.entries() if e.get("order_id") == "EQ-1"]
    print("journal:", [(e["event"], e.get("filled"), e.get("reported")) for e in lines])
    await _kill_resumes(a.monitor)

    b = _second_process(a, [])
    _recorders(b, cfg)
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(b, b_pos, b_res)
    _summary(memrepo)
    print("SELL booked:", memrepo.sold("TCS"), "open BUY qty:", memrepo.open_buy_qty("TCS"),
          "| truth: 10 sold, 0 open")
    print("alerts:", [(a_[0], a_[1]) for a_ in alerts.alerts][-6:])
    assert memrepo.sold("TCS") == 10, "3 sold shares never booked"
    assert memrepo.open_buy_qty("TCS") == 0
    # B saw A's booking of 7 never confirmed: alerted once, naming the order and shares
    assert alerts.keys("CRITICAL").count("booking-unconfirmed:EQ-1:7") == 1
    text = alerts.text("booking-unconfirmed:EQ-1:7")
    assert "EQ-1" in text and "may not be booked — check trade rows" in text


async def test_buy_final_crash_after_journal_before_row(alerts, memrepo):
    """A stuck LIMIT BUY of 10 reported 4 (row 4). A's resume reads it final at 10: the
    worker journals `final 10`, then A dies before the row update. B never picks it up
    (the journal says final). Truth: 10 bought."""
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

    async def dies(tracked, conf):
        await asyncio.Event().wait()

    a.monitor._on_late_fill = dies
    a.broker.orders[buy].override = _filled(10, "100")
    positions, result = [], MonitorResult()
    await a.monitor._resync(positions, result, first=True)
    await _kill_resumes(a.monitor)

    b = _second_process(a, [])
    _recorders(b, cfg)
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    await _cycle(b, b_pos, b_res)
    _summary(memrepo)
    print("BUY row qty:", memrepo.find_by_order_id(buy).quantity, "| truth: 10")
    assert memrepo.open_buy_qty("TCS") == 10, "6 bought shares never booked"
    assert f"booking-unconfirmed:{buy}:10" in alerts.keys("CRITICAL")


# ── 2. The lock held in one process, unusable in the other ──────────────────


@pytest.mark.parametrize("lockless", ["A", "B"])
async def test_final_booked_twice_when_only_one_process_has_the_lock(alerts, memrepo,
                                                                     lockless):
    cfg = cli_config()
    _open_live_row(memrepo, 5, 95, older=True)            # an older lot, still held
    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    positions, result = await _stuck_exit(a)               # A booked 4
    b = _second_process(a, [])
    _recorders(b, cfg)
    (a.router if lockless == "A" else b._router)._lock_dir = None
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    await _slow_reads(a.broker)
    a.broker.orders["EQ-1"].override = _filled(10, "90")
    await asyncio.gather(b._resync(b_pos, b_res), a.monitor._resync(positions, result))
    await asyncio.gather(b._settle_resumes(b_res), a.monitor._settle_resumes(result))
    _summary(memrepo)
    print(lockless, "lockless: SELL booked:", memrepo.sold("TCS"), "open BUY qty:",
          memrepo.open_buy_qty("TCS"), "realized:", memrepo.realized(),
          "| truth: 10 sold, 5 open (older lot), realized -100")
    assert memrepo.sold("TCS") == 10 and memrepo.open_buy_qty("TCS") == 5


# ── 3. The journal stops accepting writes (disk full) ───────────────────────


async def test_progress_booked_twice_when_the_journal_cannot_be_written(alerts, memrepo,
                                                                       monkeypatch):
    cfg = cli_config()
    _open_live_row(memrepo, 5, 95, older=True)
    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    positions, result = await _stuck_exit(a)               # A booked 4
    b = _second_process(a, [])
    _recorders(b, cfg)
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)              # B adopted it (reported 4)

    def full(self, event, **kw):
        self._write_failed(OSError(28, "No space left on device"))

    monkeypatch.setattr(OrderJournal, "record", full)
    a.broker.orders["EQ-1"].override = PROGRESS_7
    await _cycle(a.monitor, positions, result)
    await _cycle(b, b_pos, b_res)
    print("after progress 7: SELL booked", memrepo.sold("TCS"), "(truth 7)")
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(a.monitor, positions, result)
    await _cycle(b, b_pos, b_res)
    _summary(memrepo)
    print("SELL booked:", memrepo.sold("TCS"), "open BUY qty:", memrepo.open_buy_qty("TCS"),
          "realized:", memrepo.realized(), "| truth: 10 sold, 5 open")
    print("alerts:", [c for c in alerts.alerts if "journal" in str(c)][:3])
    assert memrepo.sold("TCS") == 10 and memrepo.open_buy_qty("TCS") == 5
    assert "booked twice" in alerts.text("journal-write-failed")


# ── 4. The normal case: pieces alternating between two processes ─────────────


@pytest.mark.parametrize("order", ["ABAB", "BABA", "AABB", "BBAA"])
async def test_sell_pieces_alternating_processes_book_true_pnl(alerts, memrepo, order):
    """Exit of 10 reported 4 @90; it then trades 5 (avg 89.96), 7 (89.9), 9 (89.8667)
    and ends 10 @89.85 — each piece read by the process named in ``order``. An older lot
    (5 @95) must stay open. Truth: 10 sold, realized 10 x (89.85 - 100) = -101.5."""
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
    views = [{"status": "PENDING", "traded_qty": 5, "traded_price": "89.96"},
             {"status": "PENDING", "traded_qty": 7, "traded_price": "89.9"},
             {"status": "PENDING", "traded_qty": 9, "traded_price": "89.8667"},
             _filled(10, "89.85")]
    for who, view in zip(order, views):
        a.broker.orders["EQ-1"].override = view
        if who == "A":
            await _cycle(a.monitor, positions, result)
        else:
            await _cycle(b, b_pos, b_res)
    # both read once more
    await _cycle(a.monitor, positions, result)
    await _cycle(b, b_pos, b_res)
    _summary(memrepo)
    print(order, "sold", memrepo.sold("TCS"), "open", memrepo.open_buy_qty("TCS"),
          "realized", memrepo.realized())
    assert memrepo.sold("TCS") == 10 and memrepo.open_buy_qty("TCS") == 5
    assert abs(memrepo.realized() - Decimal("-101.5")) < Decimal("0.01")


@pytest.mark.parametrize("order", ["ABA", "BAB", "BBA"])
async def test_buy_pieces_alternating_processes_with_an_exit_between(alerts, memrepo,
                                                                     order):
    """A stuck LIMIT BUY of 10 reported 2 @100 (row). It trades 5 (avg 100.6), then A's
    monitor sells 5 (hard stop at 90), then 8 (avg 100.75) and ends 10 @100.8 — pieces read
    by alternating processes. Truth: 10 bought for 1008; 5 sold at 90."""
    cfg = cli_config()
    a = Live({"TCS": (0, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    buy = await a.open_order("TCS", Side.BUY, 10, Script(
        timeline=[(0, {"status": "PENDING", "traded_qty": 2, "traded_price": "100"})],
        on_cancel=[IGNORE] * 5000), order_type=OrderType.LIMIT)
    tracked = a.track(buy, "TCS", "BUY", 10, state="stuck", purpose="entry")
    tracked.filled_reported, tracked.avg_price_reported = Decimal(2), Decimal(100)
    a.router.journal.record("stuck", order_id=buy, symbol="TCS", security_id="11536",
                            segment="EQUITY", side="BUY", qty=Decimal(10), purpose="entry",
                            filled=Decimal(2), avg_price=Decimal(100))
    _open_live_row(memrepo, 2, 100, order_id=buy)
    b = _second_process(a, [])
    _recorders(b, cfg)
    positions, result, b_pos, b_res = [], MonitorResult(), [], MonitorResult()

    async def run(who):
        if who == "A":
            await _cycle(a.monitor, positions, result)
        else:
            await _cycle(b, b_pos, b_res)

    a.broker.orders[buy].override = {"status": "PENDING", "traded_qty": 5,
                                     "traded_price": "100.6"}
    a.broker.held["TCS"] = (5, 100.6)
    await run(order[0])
    await _cycle(a.monitor, positions, result, first=True)
    print("after 5:", memrepo.find_by_order_id(buy).quantity,
          [(p.symbol, p.quantity) for p in positions])
    # A's hard stop sells the 5 positions show
    a.broker.place_effects = [Script(timeline=[(0, _filled(5, "90"))])]
    await a.monitor._check_positions(positions, 1, result)
    if positions and positions[0].exit_task:
        await positions[0].exit_task
    a.broker.held["TCS"] = (0, 0.0)
    a.broker.orders[buy].override = {"status": "PENDING", "traded_qty": 8,
                                     "traded_price": "100.75"}
    await run(order[1])
    a.broker.orders[buy].override = _filled(10, "100.8")
    await run(order[2])
    await _cycle(a.monitor, positions, result)
    await _cycle(b, b_pos, b_res)
    _summary(memrepo)
    bought = sum((r.quantity for r in memrepo.rows.values() if r.side == "BUY"
                  and not (r.model_signals or {}).get("split_from")), start=Decimal(0))
    cost = sum(((r.fill_price or 0) * r.quantity for r in memrepo.rows.values()
                if r.side == "BUY" and not (r.model_signals or {}).get("split_from")),
               start=Decimal(0))
    print(order, "sold", memrepo.sold("TCS"), "open", memrepo.open_buy_qty("TCS"),
          "realized", memrepo.realized(), "bought(orig rows)", bought, "cost", cost)
    assert memrepo.sold("TCS") == 5
    assert memrepo.open_buy_qty("TCS") == 5


# ── 5. A failed trade-row write while booking progress ──────────────────────


async def test_progress_booking_fails_once_journal_says_booked(alerts, memrepo):
    """Supabase fails once while A books the exit's progress (the open-BUY lookup). The
    journal already says reported=7; nothing retries it."""
    cfg = cli_config()
    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    positions, result = await _stuck_exit(a)
    real = memrepo.find_open_buy
    fails = {"n": 1}

    def flaky(symbol, is_paper=None):
        if fails["n"]:
            fails["n"] -= 1
            raise RuntimeError("503 Service Unavailable")
        return real(symbol, is_paper=is_paper)

    memrepo.find_open_buy = flaky
    a.broker.orders["EQ-1"].override = PROGRESS_7
    await _cycle(a.monitor, positions, result)
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(a.monitor, positions, result)
    b = _second_process(a, [])
    _recorders(b, cfg)
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    _summary(memrepo)
    print("SELL rows:", memrepo.sold("TCS"), "open BUY qty:", memrepo.open_buy_qty("TCS"),
          "realized:", memrepo.realized(), "| truth: 0 open, realized ~-102")
    print("alerts:", [(x[0], x[1]) for x in alerts.alerts])
    assert memrepo.open_buy_qty("TCS") == 0
    assert memrepo.sold("TCS") == 10                    # the failed write left no SELL row
    assert abs(memrepo.realized() - Decimal("-102")) < Decimal("0.01")
    assert "booking-unconfirmed:EQ-1:7" in alerts.keys("CRITICAL")


# ── 6. Progress read without a price ────────────────────────────────────────


async def test_unpriced_progress_in_adopting_process(alerts, memrepo):
    """B (recovery monitor) adopted A's stuck exit from the journal. A working order's
    row shows traded_qty 7 but no traded_price; the final read (trades endpoint) prices
    it at 89.8."""
    cfg = cli_config()
    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    positions, result = await _stuck_exit(a)
    b = _second_process(a, [])
    _recorders(b, cfg)
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    a.broker.orders["EQ-1"].override = {"status": "PENDING", "traded_qty": 7,
                                         "traded_price": ""}
    await _cycle(b, b_pos, b_res)
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(b, b_pos, b_res)
    _summary(memrepo)
    print("SELL rows:", memrepo.sold("TCS"), "open BUY qty:", memrepo.open_buy_qty("TCS"))
    print("alerts:", [(x[0], x[1]) for x in alerts.alerts if "unpriced" in x[1] or "late" in x[1]])
    assert memrepo.open_buy_qty("TCS") == 0


@pytest.mark.parametrize("lockless", ["A", "B"])
@pytest.mark.parametrize("first", ["A", "B"])
async def test_mixed_lock_sequential_is_booked_once(alerts, memrepo, lockless, first):
    cfg = cli_config()
    _open_live_row(memrepo, 5, 95, older=True)
    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    positions, result = await _stuck_exit(a)
    b = _second_process(a, [])
    _recorders(b, cfg)
    (a.router if lockless == "A" else b._router)._lock_dir = None
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)
    seq = [(a.monitor, positions, result), (b, b_pos, b_res)]
    if first == "B":
        seq.reverse()
    for view in (PROGRESS_7, _filled(10, "89.8")):
        a.broker.orders["EQ-1"].override = view
        for m, p, r in seq:
            await _cycle(m, p, r)
    print(lockless, first, "sold", memrepo.sold("TCS"), "open", memrepo.open_buy_qty("TCS"))
    assert memrepo.sold("TCS") == 10 and memrepo.open_buy_qty("TCS") == 5


@pytest.mark.parametrize("sold_between", [False, True])
async def test_buy_progress_crash_after_journal_before_row(alerts, memrepo, sold_between):
    """A stuck BUY of 10 reported 4 (row 4). A journals progress 7 and dies before the row
    update. Optionally the 7 held are sold (hard stop at 90) before B sees the BUY final at
    10. Truth: 10 bought @100; sold 7 @90 when sold_between."""
    cfg = cli_config()
    a = Live({"TCS": (0, 100.0)}, ltps={TCS: 90.0}, lag=True)
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

    async def dies(tracked, conf):
        await asyncio.Event().wait()

    a.monitor._on_late_fill = dies
    a.broker.orders[buy].override = {"status": "PENDING", "traded_qty": 7,
                                     "traded_price": "100"}
    positions, result = [], MonitorResult()
    await a.monitor._resync(positions, result, first=True)
    await _kill_resumes(a.monitor)

    b = _second_process(a, [])
    _recorders(b, cfg)
    b_pos, b_res = [], MonitorResult()
    a.broker.held["TCS"] = (7, 100.0)
    await _cycle(b, b_pos, b_res, first=True)
    if sold_between:
        b.broker = a.broker
        a.broker.place_effects = [Script(timeline=[(0, _filled(7, "90"))])]
        await b._check_positions(b_pos, 1, b_res)
        if b_pos and b_pos[0].exit_task:
            await b_pos[0].exit_task
        a.broker.held["TCS"] = (0, 0.0)
    a.broker.orders[buy].override = _filled(10, "100")
    await _cycle(b, b_pos, b_res)
    await _cycle(b, b_pos, b_res)
    _summary(memrepo)
    want_open = 3 if sold_between else 10
    print("sold_between", sold_between, "open BUY", memrepo.open_buy_qty("TCS"),
          "sold", memrepo.sold("TCS"), "realized", memrepo.realized(),
          "| truth open", want_open, "realized", -70 if sold_between else 0)
    assert memrepo.open_buy_qty("TCS") == want_open
    if sold_between:
        assert memrepo.realized() == Decimal(-70)


async def test_final_claimed_twice_when_journal_and_locks_unusable(alerts, memrepo,
                                                                  monkeypatch):
    """Both processes lockless (the claim path); then the journal directory fills up, so
    the claim marker cannot be created. Neither process can know the other is booking the
    fill: both used to 'win' the claim (16 sold). A claim that cannot be written fails
    closed: nothing is booked, and a CRITICAL alert says to book it by hand."""
    import os as _os

    cfg = cli_config()
    _open_live_row(memrepo, 5, 95, older=True)
    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    positions, result = await _stuck_exit(a)
    b = _second_process(a, [])
    _recorders(b, cfg)
    a.router._lock_dir = None
    b._router._lock_dir = None
    b_pos, b_res = [], MonitorResult()
    await _cycle(b, b_pos, b_res, first=True)

    def full(self, event, **kw):
        self._write_failed(OSError(28, "No space left on device"))

    real_open = _os.open

    def no_space(path, flags, *args):
        if str(path).endswith(".once"):
            raise OSError(28, "No space left on device")
        return real_open(path, flags, *args)

    monkeypatch.setattr(OrderJournal, "record", full)
    monkeypatch.setattr("skopaq.execution.order_journal.os.open", no_space)
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(a.monitor, positions, result)
    await _cycle(b, b_pos, b_res)
    _summary(memrepo)
    print("sold", memrepo.sold("TCS"), "open", memrepo.open_buy_qty("TCS"),
          "realized", memrepo.realized(), "| truth 10 sold, 5 open")
    # Never booked twice: only A's first 4 are booked, the other 6 are left to the user
    assert memrepo.sold("TCS") == 4 and memrepo.open_buy_qty("TCS") == 11
    assert alerts.keys("CRITICAL").count("late-fill-unclaimed:EQ-1") == 2   # A and B
    assert "not booked: journal unwritable — book by hand" in alerts.text(
        "late-fill-unclaimed:EQ-1")


# ── The booking itself: journalled, confirmed, alerted after the write ───────


def _booking_lines(journal, order_id):
    return [(e["event"], e.get("filled"), e.get("reported"))
            for e in journal.entries()
            if e.get("order_id") == order_id and e["event"] in ("booking", "booked")]


async def test_a_failed_booking_is_not_counted_and_the_next_one_starts_before_it(alerts):
    """Booking the exit's progress (4 -> 7) fails: nothing counts it as booked, a CRITICAL
    alert says so, and no "recorded" alert is sent. The final fill then books 4 -> 10."""
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _stuck_exit(a)
    tracked = a.router.registry.get("EQ-1")
    booked: list = []

    async def on_late_fill(t, c):
        if c.may_be_open:
            return False                                  # the trades table refused it
        booked.append((t.filled_reported, c.filled_qty))

    a.monitor._on_late_fill = on_late_fill
    a.broker.orders["EQ-1"].override = PROGRESS_7
    await _cycle(a.monitor, positions, result)

    assert tracked.filled_reported == 4
    assert _booking_lines(a.router.journal, "EQ-1") == [("booking", "7", "4")]
    assert a.router.journal.booking_state("EQ-1").booked == 4
    assert "booking-unconfirmed:EQ-1:7" in alerts.keys("CRITICAL")
    assert not [k for k in alerts.keys() if k.startswith("exit-late")]

    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(a.monitor, positions, result)

    assert booked == [(4, 10)]
    assert _booking_lines(a.router.journal, "EQ-1")[-2:] == [("booking", "10", "4"),
                                                            ("booked", "10", "4")]
    assert "exit-late:EQ-1" in alerts.keys("WARNING")


async def test_a_booking_that_raises_is_not_counted(alerts):
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _stuck_exit(a)

    async def on_late_fill(t, c):
        raise RuntimeError("503 Service Unavailable")

    a.monitor._on_late_fill = on_late_fill
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(a.monitor, positions, result)

    assert a.router.registry.get("EQ-1").filled_reported == 4
    assert a.router.journal.booking_state("EQ-1").booked == 4
    assert "503 Service Unavailable" in alerts.text("booking-unconfirmed:EQ-1:10")


async def test_the_recorded_alert_is_sent_only_after_the_rows_are_written(alerts):
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _stuck_exit(a)
    seen_during_write: list = []

    async def on_late_fill(t, c):
        seen_during_write.extend(k for k in alerts.keys() if k.startswith("exit-late"))
        assert _booking_lines(a.router.journal, "EQ-1") == [("booking", "10", "4")]

    a.monitor._on_late_fill = on_late_fill
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(a.monitor, positions, result)

    assert seen_during_write == []
    assert "exit-late:EQ-1" in alerts.keys("WARNING")
    assert _booking_lines(a.router.journal, "EQ-1") == [("booking", "10", "4"),
                                                       ("booked", "10", "4")]


async def test_a_final_line_without_a_booking_is_not_taken_as_booked(alerts, memrepo):
    """Another process read the exit final (its worker journalled ``final 10``) and died
    before claiming it. The final line alone is not a booking: B books the 6 late shares."""
    cfg = cli_config()
    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    _recorders(a.monitor, cfg)
    await _stuck_exit(a)                                   # A booked 4
    a.router.journal.record("final", order_id="EQ-1", filled=Decimal(10),
                            avg_price=Decimal("89.8"), status="SUCCESS")
    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    b = _second_process(a, [])
    _recorders(b, cfg)
    # B knows the order (it adopted it earlier), as A reported it: 4 booked
    b._router.registry.track(dataclasses.replace(a.router.registry.get("EQ-1")))

    await _cycle(b, [], MonitorResult(), first=True)

    assert memrepo.sold("TCS") == 10 and memrepo.open_buy_qty("TCS") == 0


async def test_progress_without_a_price_is_left_for_the_final_fill(alerts):
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _stuck_exit(a)
    booked: list = []

    async def on_late_fill(t, c):
        booked.append((t.filled_reported, c.filled_qty, c.avg_price))

    a.monitor._on_late_fill = on_late_fill
    a.broker.orders["EQ-1"].override = {"status": "PENDING", "traded_qty": 7,
                                         "traded_price": ""}
    await _cycle(a.monitor, positions, result)
    assert booked == [] and a.router.registry.get("EQ-1").filled_reported == 4
    # still counted against the shares (sellable checks)
    assert a.router.registry.confirmed_exit_of("EQ-1") == 7

    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(a.monitor, positions, result)
    assert booked == [(4, 10, Decimal("89.8"))]


async def test_progress_whose_booking_line_is_not_written_is_not_booked(alerts,
                                                                      monkeypatch):
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    positions, result = await _stuck_exit(a)
    booked: list = []

    async def on_late_fill(t, c):
        booked.append(c.filled_qty)

    a.monitor._on_late_fill = on_late_fill
    real = OrderJournal.record

    def no_booking_line(self, event, **kw):
        return False if event == "booking" else real(self, event, **kw)

    monkeypatch.setattr(OrderJournal, "record", no_booking_line)
    a.broker.orders["EQ-1"].override = PROGRESS_7
    await _cycle(a.monitor, positions, result)
    assert booked == []

    a.broker.orders["EQ-1"].override = _filled(10, "89.8")
    await _cycle(a.monitor, positions, result)
    assert booked == [10]              # a claimed final fill is booked all the same


# ── _run_lifecycle says whether the trade was booked ─────────────────────────


def _exit(qty):
    from skopaq.broker.models import ExecutionResult, TradingSignal

    signal = TradingSignal(symbol="TCS", action="SELL", entry_price=90.0,
                           order_type=OrderType.MARKET, quantity=Decimal(qty))
    return signal, ExecutionResult(success=True, signal=signal, mode="live", fill_price=90.0,
                                   filled_quantity=Decimal(qty),
                                   requested_quantity=Decimal(qty), outcome="late_fill",
                                   order_ids=["EQ-1"])


@pytest.mark.parametrize("rollback", [False, True])
async def test_an_exit_that_closes_no_row_is_not_booked(memrepo, rollback):
    from skopaq.cli.main import _record_exit

    _open_live_row(memrepo, 10, 100, order_id="EQ-0")

    def unreadable(symbol, is_paper=None):
        raise RuntimeError("503 Service Unavailable")

    memrepo.find_open_buy = unreadable
    signal, execution = _exit(3)
    assert await _record_exit(cli_config(), None, None, signal, execution,
                              rollback_unbooked=rollback) is False
    # A late fill's SELL row goes (it is booked again); a normal exit keeps its row
    assert memrepo.sold("TCS") == (0 if rollback else 3)
    assert memrepo.open_buy_qty("TCS") == 10


async def test_an_exit_whose_row_fails_but_whose_rows_close_is_booked(memrepo):
    from skopaq.cli.main import _record_exit

    _open_live_row(memrepo, 10, 100, order_id="EQ-0")
    real = memrepo.insert

    def insert(trade):
        if trade.side == "SELL":
            raise RuntimeError("503 Service Unavailable")
        return real(trade)

    memrepo.insert = insert
    signal, execution = _exit(10)
    assert await _record_exit(cli_config(), None, None, signal, execution) is True
    assert memrepo.open_buy_qty("TCS") == 0 and memrepo.realized() == Decimal(-100)


async def test_a_resume_cut_short_while_booking_is_not_booked_again_by_this_process(alerts):
    """A resume is cancelled (a resume timeout) while its booking of 4 -> 7 is still being
    written; the booking runs on. The next resume in the same process must not take that
    unconfirmed booking for an abandoned one and book the 3 shares again."""
    from skopaq.execution.position_monitor import resume_order

    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    await _stuck_exit(a)
    tracked = a.router.registry.get("EQ-1")
    written = asyncio.Event()
    writes: list = []

    async def slow_write(t, c):                           # a slow trades-table write
        await written.wait()
        writes.append((t.filled_reported, c.filled_qty))

    a.broker.orders["EQ-1"].override = PROGRESS_7
    first = asyncio.create_task(resume_order(a.router, tracked, slow_write))
    await a.clock.sleep(1)
    assert a.router.registry.booking == {"EQ-1"}
    first.cancel()
    await asyncio.wait([first])

    second = asyncio.create_task(resume_order(a.router, tracked, slow_write))
    for _ in range(50):
        await asyncio.sleep(0)
    written.set()
    assert await second == 0
    await a.router.registry.drain_recordings()
    assert writes == [(4, 7)] and tracked.filled_reported == 7
    assert a.router.journal.booking_state("EQ-1").booked == 7
    assert not [k for k in alerts.keys() if k.startswith("booking-unconfirmed")]
