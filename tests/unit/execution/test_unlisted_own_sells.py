"""An own SELL order that is still unresolved (stuck, unknown, interrupted or still being
worked) but that the order book does not list yet counts against the shares, for the lag
window, in every sellable check: the Executor's, the monitor's resync, CLOSING's and the
worker's re-placement. Otherwise the book's lag lets the same shares be sold twice.

Virtual time (FakeClock) over a scripted broker; the reviewers' reproductions.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from skopaq.broker.client import BrokerError
from skopaq.broker.order_status import parse_order_row
from tests.unit.execution._fakes import BASE_WALL, IGNORE, Script, row
from tests.unit.execution.test_daemon_live import LiveDaemon, _report
from tests.unit.execution.test_daemon_live import _lookups as _daemon_lookups  # noqa: F401
from tests.unit.execution.test_executor_sell_context import Live as LiveExecutor
from tests.unit.execution.test_executor_sell_context import _exit, _held
from tests.unit.execution.test_position_monitor_live import (  # noqa: F401
    TCS,
    Live,
    _filled,
    _lookups,
    alerts,
)

NOT_FOUND = BrokerError("API error 400: Position could not be found.", 400, kind="http")
PENDING_CONFIRMATION = BrokerError("API error 400: The order is already pending for "
                                   "confirmation", 400, kind="http")


def _sell_orders(broker, symbol="TCS"):
    return {o.order_id: (broker.row_of(o.order_id)["status"],
                         broker.row_of(o.order_id).get("traded_qty"), o.placed_at)
            for o in broker.placed_orders(symbol)}


# ── The pure calculation ─────────────────────────────────────────────────────


def _context(orders=(), own_open=(), **kw):
    from skopaq.execution.sellable import SellContext

    return SellContext(orders=tuple(orders), read_at=BASE_WALL, own_open=tuple(own_open), **kw)


def _open(order_id="EQ-1", qty=5, age_s=30.0):
    from skopaq.execution.sellable import OwnOpenSell

    return OwnOpenSell(order_id=order_id, symbol="TCS", security_id="11536",
                       qty=Decimal(qty), at=BASE_WALL - timedelta(seconds=age_s))


def test_an_own_open_sell_the_book_does_not_list_counts_for_the_lag_window():
    from skopaq.execution.sellable import sellable_quantity

    view = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                             positions=[_held(5)], holdings=[], context=_context(
                                 own_open=[_open(qty=5)]), order_qty=Decimal(5))
    assert (view.sellable, view.own_open_qty, view.own_open_ids) == (0, 5, ("EQ-1",))

    # Older than the lag window: no longer counted (the book would list it by now)
    old = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                            positions=[_held(5)], holdings=[], context=_context(
                                own_open=[_open(qty=5, age_s=601)]), order_qty=Decimal(5))
    assert old.sellable == 5 and old.own_open_qty == 0


def test_an_own_open_sell_the_book_lists_is_counted_once_from_its_row():
    from skopaq.execution.sellable import sellable_quantity

    listed = parse_order_row(row("PENDING", traded=0, requested=5, id="EQ-1"))
    view = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                             positions=[_held(5)], holdings=[], context=_context(
                                 orders=[listed], own_open=[_open(qty=5)]),
                             order_qty=Decimal(5))
    assert (view.pending_qty, view.own_open_qty, view.sellable) == (5, 0, 0)


# ── Executor: the next exit's check ──────────────────────────────────────────


async def test_an_exit_no_read_finds_is_counted_by_the_next_exit(alerts):
    with patch("skopaq.notifications.notify_trade_event", new_callable=AsyncMock):
        live = LiveExecutor([_held(5)], record=False)
        client = live.client
        # EQ-1 is accepted (an id comes back), but GET /order finds nothing, the book lists
        # it only after 45 s and every cancel is answered "could not be found". It is
        # working at the exchange and fills at t=60.
        lagging = Script(timeline=[(0, {"status": "PENDING"}),
                                   (60, {"status": "SUCCESS", "traded_qty": 5,
                                         "traded_price": "94"})],
                         in_get_order=False, visible_after=45.0, on_cancel=[NOT_FOUND] * 20)
        client.place_effects = [lagging, Script(timeline=[(0, {"status": "SUCCESS",
                                                               "traded_qty": 5,
                                                               "traded_price": "93"})])]
        first = await live.run(_exit(5))
        assert not first.success and first.remaining_open and first.order_ids == ["EQ-1"]

        await live.clock.sleep(10)              # the monitor's next cycle: the stop fires again
        second = await live.run(_exit(5))

    assert not second.success and second.safety_passed is False
    assert "not listed in the order book yet" in second.rejection_reason
    assert [o for o in client.orders] == ["EQ-1"]      # no second SELL of the same 5 shares


# ── The monitor's rule tier ──────────────────────────────────────────────────


async def test_the_monitor_never_resells_over_an_exit_no_read_finds(alerts):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, monitor_poll_interval_seconds=10)
    lagging = Script(timeline=[(0, {"status": "PENDING"}), (60, _filled(10, "90"))],
                     in_get_order=False, visible_after=45.0, on_cancel=[NOT_FOUND] * 50)
    live.broker.by_symbol["TCS"] = [lagging, Script(timeline=[(0, _filled(10, "89"))])]

    await live.run(stop_at=120)

    assert list(_sell_orders(live.broker)) == ["EQ-1"], _sell_orders(live.broker)


async def test_the_monitor_never_resells_over_a_stuck_exit_the_book_does_not_list(alerts):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, monitor_poll_interval_seconds=10)
    # GET /order reads it PENDING; every cancel is answered "already pending"; the book lists
    # it only after 45 s; it fills at t=60
    stuck = Script(timeline=[(0, {"status": "PENDING"}), (60, _filled(10, "90"))],
                   visible_after=45.0, on_cancel=[PENDING_CONFIRMATION] * 50)
    live.broker.by_symbol["TCS"] = [stuck, Script(timeline=[(0, _filled(10, "89"))])]

    await live.run(stop_at=120)

    assert list(_sell_orders(live.broker)) == ["EQ-1"], _sell_orders(live.broker)


async def test_a_resync_marks_the_position_pending_while_its_exit_is_unlisted(alerts):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0})
    order_id = await live.open_order("TCS", "SELL", 10, Script(visible_after=None))
    live.track(order_id, "TCS", "SELL", 10, state="stuck", purpose="exit")
    live.broker.orders[order_id].script.on_cancel = [IGNORE] * 100
    from skopaq.execution.position_monitor import MonitorResult

    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    await live.monitor._settle_resumes(result)

    [pos] = positions
    assert (pos.quantity, pos.pending_exit, pos.stuck_orders) == (0, True, [order_id])


# ── CLOSING ──────────────────────────────────────────────────────────────────


async def test_closing_does_not_sell_over_its_own_stuck_exit_the_book_does_not_list(alerts):
    live = LiveDaemon({"TCS": (10, 100.0)}, lag=True)
    # This session's exit SELL 10 is still working and the book does not list it; its first
    # cancels are ignored (two CLOSING passes), then one is confirmed
    stuck = await live.stuck_exit("TCS", 10, Script(on_cancel=[IGNORE] * 10,
                                                    visible_after=None))
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(10))])]
    placed = {}
    real_place = live.broker.place_order

    async def place(order):
        response = await real_place(order)
        placed[response.order_id] = live.broker.row_of(stuck)["status"]
        return response

    live.broker.place_order = place

    await live.daemon._phase_close(_report())

    # Sold only once the stuck order was final at the broker (cancelled with nothing traded)
    assert placed == {"EQ-2": "CANCELLED"}, placed


@pytest.mark.parametrize("last_event, refused", [("placed", True), ("stuck", True),
                                                 ("final", False)])
async def test_another_process_s_unlisted_sell_counts_from_the_journal(alerts, last_event,
                                                                     refused):
    """This host's other Skopaq process (the daemon, say) has a SELL of the same 5 shares
    out that the book does not list yet: only its journal knows it."""
    with patch("skopaq.notifications.notify_trade_event", new_callable=AsyncMock):
        live = LiveExecutor([_held(5)], record=False)
        journal = live.router.journal
        journal.record("placed", order_id="EQ-77", symbol="TCS", security_id="11536",
                       side="SELL", qty=Decimal(5), purpose="exit", status="INITIATED")
        if last_event != "placed":
            journal.record(last_event, order_id="EQ-77", filled=Decimal(0),
                           status="PENDING" if last_event == "stuck" else "CANCELLED")
        live.client.place_effects = [Script(timeline=[(0, {"status": "SUCCESS",
                                                           "traded_qty": 5,
                                                           "traded_price": "93"})])]
        result = await live.run(_exit(5))

    assert (not result.success) is refused
    if refused:
        assert "EQ-77" in result.rejection_reason
        assert live.client.placed() == []
