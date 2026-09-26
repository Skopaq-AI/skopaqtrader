"""A fresh own exit keeps counting against the shares until the broker shows it.

Regressions from the v4 review (double-sell verifier):

- Sells positions already show from earlier today (a morning exit, the user's own sale
  of holdings) are not this process's fresh exit: they never cancel it out while the book
  and positions both lag behind it.
- An own SELL a read found still working ("stuck") keeps counting however old it is, while
  the order book does not list it.
- An own SELL known to be final — finished by another process (journal), or already final
  when its exit was interrupted — no longer counts as open.

Each exit case runs with holdings 0 and 50: with long-term holdings the second SELL would
sell them.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from skopaq.broker.client import BrokerError
from skopaq.broker.models import Holding, OrderType, Position, TradingSignal
from skopaq.execution.live_orders import TrackedOrder, own_open_sells
from skopaq.execution.position_monitor import _left_unresolved
from skopaq.execution.sellable import OwnOpenSell, SellContext, sellable_quantity
from tests.unit.execution._fakes import BASE_WALL, Script, row
from tests.unit.execution.test_daemon_live import LiveDaemon, _report
from tests.unit.execution.test_daemon_live import _lookups as _daemon_lookups  # noqa: F401
from tests.unit.execution.test_executor_sell_context import Live as LiveExecutor
from tests.unit.execution.test_executor_sell_context import _held
from tests.unit.execution.test_position_monitor_live import (  # noqa: F401
    TCS,
    Live,
    _filled,
    _lookups,
    alerts,
)

HOLDINGS = pytest.mark.parametrize("long_term", [0, 50])
PENDING_CONFIRMATION = BrokerError("API error 400: The order is already pending for "
                                   "confirmation", 400, kind="http")


def _holdings(qty):
    return [Holding(symbol="TCS", security_id="11536", quantity=Decimal(qty),
                    average_price=50.0)]


def _exit(qty, *, position_only=True):
    return TradingSignal(symbol="TCS", action="SELL", confidence=80, entry_price=94.0,
                         order_type=OrderType.MARKET, quantity=Decimal(qty),
                         reasoning="HARD STOP", position_only=position_only)


def _filled_now(qty, price="94", visible_after=None):
    return Script(timeline=[(0, {"status": "SUCCESS", "traded_qty": qty,
                                 "traded_price": price})], visible_after=visible_after)


# The morning: a first TCS trade bought 10 and sold them (EQ-100 at 09:30, in the book);
# a second TCS trade bought 10 at 10:30. Positions: bought 20, sold 10, net 10.
MORNING_EXIT = row("SUCCESS", traded=10, requested=10, id="EQ-100", traded_price="99",
                   updated_at=(BASE_WALL - timedelta(minutes=90)).isoformat())
POSITION = Position(symbol="TCS", security_id="11536", product="CNC", quantity=Decimal(10),
                    average_price=100.0, buy_quantity=Decimal(20), sell_quantity=Decimal(10))


# ── Earlier sells positions show do not cancel out a fresh exit ─────────────


def test_an_older_sale_in_the_book_does_not_absorb_a_fresh_confirmed_exit():
    """The book lists only the morning exit (outside the lag window); positions show its
    10 sold; this process confirmed 10 more sold seconds ago that neither shows yet."""
    context = SellContext(orders=(_snapshot(MORNING_EXIT),), read_at=BASE_WALL,
                          own_recent_exit_qty=Decimal(10), own_order_ids=frozenset())
    view = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                             positions=[POSITION], holdings=[], context=context,
                             order_qty=Decimal(10))
    assert view.unshown_fill_qty == 10
    assert view.sellable == 0 and view.position_sellable == 0


def test_a_fresh_exit_positions_already_show_is_not_counted_twice():
    """Positions show both sales (sold 20): the fresh exit is in them, nothing is unshown."""
    shown = POSITION.model_copy(update={"quantity": Decimal(0), "buy_quantity": Decimal(20),
                                        "sell_quantity": Decimal(20)})
    context = SellContext(orders=(_snapshot(MORNING_EXIT),), read_at=BASE_WALL,
                          own_recent_exit_qty=Decimal(10), own_order_ids=frozenset())
    view = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                             positions=[shown], holdings=_holdings(50), context=context,
                             order_qty=Decimal(10))
    assert view.unshown_fill_qty == 0 and view.sellable == 50


@HOLDINGS
async def test_a_second_exit_after_a_filled_exit_the_book_does_not_list_yet(alerts,
                                                                           long_term):
    with patch("skopaq.notifications.notify_trade_event", new_callable=AsyncMock):
        live = LiveExecutor([POSITION], record=False)
        live.client.holdings = _holdings(long_term)
        live.client.extra_rows = [MORNING_EXIT]
        # The second trade's exit fills at once (GET /order: SUCCESS); the book lists it
        # only after 60 s; positions do not show it yet
        live.client.place_effects = [_filled_now(10, visible_after=60.0),
                                     _filled_now(10, "93")]
        first = await live.run(_exit(10))
        await live.clock.sleep(10)
        second = await live.run(_exit(10))    # CLOSING / a second process / a stale monitor
    assert first.success and first.filled_quantity == 10
    assert not second.success, "sold the same 10 shares twice"
    assert len(live.client.placed()) == 1


@HOLDINGS
async def test_closing_after_a_filled_exit_the_book_does_not_list_yet(alerts, long_term):
    live = LiveDaemon({"TCS": (10, 100.0)}, lag=True)
    live.broker.holdings = _holdings(long_term)

    async def positions():
        return [POSITION]

    live.broker.get_positions = positions
    live.broker.extra_rows = [MORNING_EXIT]
    # MONITORING's EOD exit EQ-9 sold the day's 10 (confirmed by GET /order) seconds ago;
    # neither the book nor positions show it yet
    live.router.registry.record_confirmed_exit("TCS", "11536", Decimal(10), order_id="EQ-9")
    live.router.journal.record("placed", order_id="EQ-9", symbol="TCS", security_id="11536",
                               side="SELL", qty=Decimal(10), purpose="exit",
                               status="INITIATED")
    live.router.journal.record("final", order_id="EQ-9", filled=Decimal(10),
                               status="SUCCESS")
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(10))])]

    await live.daemon._phase_close(_report())

    assert [int(o.request.quantity) for o in live.broker.placed_orders("TCS")] == []


@pytest.mark.parametrize("variant", ["no-earlier-sale", "user-sold-5-holdings"])
async def test_an_earlier_sale_by_the_user_does_not_absorb_a_fresh_exit(alerts, variant):
    if variant == "no-earlier-sale":
        pos = Position(symbol="TCS", security_id="11536", product="CNC", quantity=Decimal(10),
                       average_price=100.0, buy_quantity=Decimal(10), sell_quantity=Decimal(0))
        extra, qty, held = [], 10, 50
    else:
        # The user sold 5 of their 50 long-term TCS at 09:30 (in the book, and positions
        # show it); Skopaq bought 10 at 10:30: positions bought 10, sold 5, net 5
        pos = Position(symbol="TCS", security_id="11536", product="CNC", quantity=Decimal(5),
                       average_price=100.0, buy_quantity=Decimal(10), sell_quantity=Decimal(5))
        extra = [row("SUCCESS", traded=5, requested=5, id="USER-1", traded_price="99",
                     updated_at=(BASE_WALL - timedelta(minutes=90)).isoformat())]
        qty, held = 5, 45
    with patch("skopaq.notifications.notify_trade_event", new_callable=AsyncMock):
        live = LiveExecutor([pos], record=False)
        live.client.holdings = _holdings(held)
        live.client.extra_rows = extra
        live.client.place_effects = [_filled_now(qty, visible_after=60.0),
                                     _filled_now(qty, "93")]
        first = await live.run(_exit(qty))
        await live.clock.sleep(10)
        second = await live.run(_exit(qty))
    assert first.success
    assert not second.success
    assert len(live.client.placed()) == 1


# ── An own SELL seen working counts whatever its age ─────────────────────────


@HOLDINGS
async def test_the_monitor_does_not_resell_once_an_unlisted_stuck_exit_ages(alerts,
                                                                          long_term):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, monitor_poll_interval_seconds=10)
    live.broker.holdings = _holdings(long_term)
    # EQ-1: GET /order reads it PENDING the whole time, every cancel is answered "already
    # pending", the book never lists it; it fills at t=900
    stuck = Script(timeline=[(0, {"status": "PENDING"}), (900, _filled(10, "90"))],
                   visible_after=None, on_cancel=[PENDING_CONFIRMATION] * 2000)
    live.broker.by_symbol["TCS"] = [stuck, Script(timeline=[(0, _filled(10, "89"))])]

    await live.run(stop_at=1000)

    assert [o.order_id for o in live.broker.placed_orders("TCS")] == ["EQ-1"]


@HOLDINGS
async def test_closing_does_not_resell_while_an_unlisted_stuck_exit_works(alerts,
                                                                        long_term):
    live = LiveDaemon({"TCS": (10, 100.0)})
    live.broker.holdings = _holdings(long_term)
    # MONITORING's exit EQ-1 (placed at t=0) is stuck: GET /order reads PENDING, cancels
    # are answered "already pending", the book never lists it
    stuck = await live.stuck_exit("TCS", 10, Script(
        timeline=[(0, {"status": "PENDING"})], visible_after=None,
        on_cancel=[PENDING_CONFIRMATION] * 2000))
    live.router.journal.record("placed", order_id=stuck, symbol="TCS", security_id="11536",
                               side="SELL", qty=Decimal(10), purpose="exit",
                               status="INITIATED")
    await live.clock.sleep(700)          # CLOSING starts 11m40s later
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(10))])]
    status_at_place = {}
    real_place = live.broker.place_order

    async def place(order):
        response = await real_place(order)
        status_at_place[response.order_id] = live.broker.row_of(stuck)["status"]
        return response

    live.broker.place_order = place
    await live.daemon._phase_close(_report())

    # No SELL placed while EQ-1 was still working at the broker
    assert all(status != "PENDING" for status in status_at_place.values()), status_at_place


def test_a_resume_cut_short_keeps_the_order_counted_whatever_its_age(alerts):
    """_left_unresolved marks a still-unresolved order "interrupted": unlike an order that
    was already final when its exit was interrupted, it may still be working."""
    live = Live({"TCS": (10, 100.0)})
    tracked = live.track("EQ-7", "TCS", "SELL", 10, state="stuck", purpose="exit")
    _left_unresolved(live.router, tracked)
    later = live.clock.wall() + timedelta(hours=1)

    opens = own_open_sells(live.router.registry, live.router.journal, later)

    assert [(o.order_id, o.qty) for o in opens] == [("EQ-7", 10)]
    context = SellContext(orders=(), read_at=later, own_open=opens)
    view = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                             positions=[_held(10)], holdings=[], context=context,
                             order_qty=Decimal(10))
    assert view.own_open_qty == 10 and view.sellable == 0


def test_an_unlisted_order_never_seen_working_stops_counting_after_the_lag_window():
    """Only a read that found it working keeps an unlisted order counted past the window."""
    old = BASE_WALL - timedelta(minutes=11)
    for seen, counted in ((False, 0), (True, 10)):
        context = SellContext(orders=(), read_at=BASE_WALL, own_open=(OwnOpenSell(
            order_id="EQ-3", symbol="TCS", security_id="11536", qty=Decimal(10), at=old,
            seen_working=seen),))
        view = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                                 positions=[_held(10)], holdings=[], context=context,
                                 order_qty=Decimal(10))
        assert view.own_open_qty == counted


# ── An own SELL known to be final is not open ────────────────────────────────


async def test_an_order_another_process_finished_no_longer_counts_as_open(alerts):
    with patch("skopaq.notifications.notify_trade_event", new_callable=AsyncMock):
        live = LiveExecutor([_held(5)], record=False)
        # This process's exit EQ-5 ended stuck (the book does not list it); another Skopaq
        # process then resumed it: CANCELLED, nothing traded (journal final)
        live.router.registry.track(TrackedOrder(
            order_id="EQ-5", side="SELL", symbol="TCS", security_id="11536",
            segment="EQUITY", requested=Decimal(5), purpose="exit", state="stuck"))
        live.router.journal.record("placed", order_id="EQ-5", symbol="TCS",
                                   security_id="11536", side="SELL", qty=Decimal(5),
                                   purpose="exit", status="INITIATED")
        live.router.journal.record("final", order_id="EQ-5", filled=Decimal(0),
                                   status="CANCELLED")
        live.client.place_effects = [_filled_now(5)]
        result = await live.run(_exit(5))
    assert result.success, result.rejection_reason


async def test_an_exit_interrupted_after_its_order_ended_does_not_count_its_remainder(
        alerts):
    """The first attempt ended final (4 of 10 traded, the rest cancelled); the exit was
    then interrupted during its second attempt. The first order's dead remainder (6) is
    not an open SELL; the 4 it sold still count."""
    with patch("skopaq.notifications.notify_trade_event", new_callable=AsyncMock):
        live = LiveExecutor([_held(10)], record=False)
        live.client.place_effects = [
            Script(timeline=[(0, {"status": "PENDING"}),
                             (2, {"status": "PENDING", "traded_qty": 4, "traded_price": "94"})],
                   visible_after=1000.0),
            Script(timeline=[(0, {"status": "PENDING"})], visible_after=1000.0)]
        task = asyncio.ensure_future(live.run(_exit(10)))
        while len(live.client.placed()) < 2:
            await live.clock.sleep(0.5)
        await live.clock.sleep(1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        registry = live.router.registry
        assert registry.get("EQ-1").state == "interrupted"     # its fill is still to record
        opens = own_open_sells(registry, live.router.journal, live.clock.wall())
        live.client.place_effects = [_filled_now(6, "93")]
        await live.clock.sleep(5)
        again = await live.run(_exit(6))
    assert opens == ()
    assert again.success, again.rejection_reason
    assert again.filled_quantity == 6


def _snapshot(raw):
    from skopaq.broker.order_status import parse_order_row

    return parse_order_row(raw)
