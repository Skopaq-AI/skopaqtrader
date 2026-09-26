"""Placements whose outcome is unknown: which order is recorded, and what a later match
proves.

- An exit interrupted while a later attempt is being placed or looked up records THAT
  attempt (its remainder and its internal id), not the first order's.
- An order in the book that only looks like an uncertain placement (same instrument, side,
  quantity, created about then) may be someone else's: it is watched and never cancelled,
  and the placement keeps counting against the shares until the lag window ends. Only an
  order carrying the placement's ``remarks`` tag is adopted as ours outright.

Virtual time (FakeClock) over a scripted broker (PositionBroker / FakeClient).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from skopaq.broker.client import OrderPlacementUncertain
from skopaq.broker.models import Holding, OrderRequest, OrderType, Side, TradingSignal
from skopaq.execution.live_orders import uncertain_sells
from skopaq.execution.position_monitor import MonitorResult, own_order_ids, sellable_view
from tests.unit.execution._fakes import BASE_WALL, PositionBroker, Script, row
from tests.unit.execution.test_executor_sell_context import Live as ExecLive
from tests.unit.execution.test_executor_sell_context import _exit as exec_exit
from tests.unit.execution.test_executor_sell_context import notify  # noqa: F401
from tests.unit.execution.test_position_monitor_live import (  # noqa: F401
    TCS,
    Live,
    _lookups,
    alerts,
)

GATEWAY_504 = OrderPlacementUncertain("HTTP 504", 504, kind="http")


def _exit(qty: int = 10) -> OrderRequest:
    return OrderRequest(symbol="TCS", side=Side.SELL, quantity=Decimal(qty),
                        order_type=OrderType.MARKET, security_id="11536")


def _sig() -> TradingSignal:
    return TradingSignal(symbol="TCS", action="SELL", entry_price=100.0)


def _slow_second_post(broker: PositionBroker, delay: float) -> None:
    """The broker creates the second order at once, but its answer takes ``delay`` s."""
    real = broker.place_order

    async def place_order(order):
        n = len(broker.placed())
        response = await real(order)
        for _ in range(int(delay) if n == 1 else 0):
            await broker.clock.sleep(1)          # in steps, so the test can cut in
        return response

    broker.place_order = place_order


def _first_attempt(filled: int) -> Script:
    """Attempt 1 rests; ``filled`` of it trades at t=2 and the rest is cancelled at 10 s."""
    if not filled:
        return Script()
    return Script(timeline=[(0, {"status": "PENDING"}),
                            (2, {"status": "PARTIALLY FILLED", "traded_qty": filled,
                                 "traded_price": "100"})])


async def _interrupt(live: Live, task: asyncio.Task, seconds_into_attempt_2: float) -> float:
    """Cancel ``task`` this long after its second placement was sent; returns when it was
    sent (virtual time)."""
    while len(live.broker.placed()) < 2:
        await asyncio.sleep(0)
    sent = live.clock.t
    while live.clock.t < sent + seconds_into_attempt_2:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return sent


# ── The attempt in flight is the one recorded ────────────────────────────────


@pytest.mark.parametrize("filled_first", [0, 4])
@pytest.mark.parametrize("where", ["post", "reconcile"])
async def test_an_exit_interrupted_during_a_later_attempt_records_that_attempt(
        alerts, filled_first, where):
    held = {"TCS": (10, 100.0)}
    live = Live(held, lag=True)
    if where == "post":
        # Attempt 2's POST reaches the broker; its answer takes 5 s: interrupted at 2 s
        _slow_second_post(live.broker, 5.0)
        second = Script(visible_after=30.0)
        cut_at = 2.0
    else:
        # Attempt 2's POST gets a 504 although the order exists; the book shows it only
        # 30 s later: interrupted 3 s into the reconcile window
        second = Script(uncertain=GATEWAY_504, visible_after=30.0)
        cut_at = 3.0
    live.broker.place_effects = [_first_attempt(filled_first), second]
    task = asyncio.create_task(live.router.worker.execute(_exit(10), _sig()))
    sent = await _interrupt(live, task, cut_at)

    remaining = 10 - filled_first
    eq2 = live.broker.orders["EQ-2"]
    assert eq2.request.quantity == remaining
    registry, journal = live.router.registry, live.router.journal
    [placement] = registry.uncertain()
    assert (placement.qty, placement.internal_id) == (remaining, str(eq2.request.internal_id))
    assert placement.before_ids is not None and "EQ-1" in placement.before_ids
    [record] = journal.unresolved_uncertain()
    assert (record["qty"], record["internal_id"]) == (str(remaining),
                                                      str(eq2.request.internal_id))
    assert "EQ-1" in record["before_ids"]
    assert [u.qty for u in uncertain_sells(registry, journal)] == [remaining]
    assert f"a SELL of {remaining}" in alerts.text("order-interrupted")

    # The book shows EQ-2 30 s after it was sent: the monitor watches it (never cancels
    # it), and the remainder still counts until the lag window ends
    while live.clock.t < sent + 31:
        await live.clock.sleep(1)
    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    watched = registry.get("EQ-2")
    assert watched is not None and watched.guessed
    assert watched.requested == remaining
    assert watched.internal_id == str(eq2.request.internal_id)
    assert live.cancels("EQ-2") == []
    assert "placement-match:" + str(eq2.request.internal_id) in alerts.keys("CRITICAL")
    snap = await live.monitor._snapshot()
    view = sellable_view(live.router, snap, "TCS", "11536", own_order_ids(live.router))
    assert view.uncertain_qty == remaining

    # A later process (fresh registry) sees the same from the journal
    later = Live(held, lag=True)
    assert [u.qty for u in uncertain_sells(later.router.registry, journal)] == [remaining]

    live.clock.t = sent + live.router.worker.settings.sell_fill_lag_window_s + 1
    snap = await live.monitor._snapshot()
    view = sellable_view(live.router, snap, "TCS", "11536", own_order_ids(live.router))
    assert view.uncertain_qty == 0


# ── An order that only looks like a placement is never taken over ────────────


def _old_uncertain_sell(live: Live, minutes_ago: float = 90) -> None:
    live.journal(ts=(BASE_WALL - timedelta(minutes=minutes_ago)).isoformat(),
                 event="uncertain", order_id="", internal_id="old-exit", symbol="TCS",
                 security_id="11536", side="SELL", qty="10", purpose="exit")


async def test_an_old_uncertain_sell_never_adopts_a_users_later_sell(alerts):   # (a)
    live = Live({"TCS": (20, 100.0)})
    _old_uncertain_sell(live)
    user = await live.open_order("TCS", Side.SELL, 10, Script(
        timeline=[(0, {"status": "PENDING"})]), order_type=OrderType.LIMIT)

    with patch.object(live.monitor, "_check_safety", return_value=None):
        await live.run(stop_at=30)

    assert live.cancels(user) == []
    assert live.router.registry.get(user) is None
    assert user not in live.router.journal.own_ids_today()


async def test_an_old_uncertain_sell_never_books_a_users_sale(alerts):   # (b)
    live = Live({"TCS": (20, 100.0)})
    _old_uncertain_sell(live)
    await live.open_order("TCS", Side.SELL, 10, Script(
        timeline=[(0, {"status": "SUCCESS", "traded_qty": 10, "traded_price": "105"})]))

    with patch.object(live.monitor, "_check_safety", return_value=None):
        await live.run(stop_at=30)

    assert live.late == []


async def test_a_user_selling_after_the_uncertain_alert_keeps_their_order(alerts):   # (c)
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, monitor_poll_interval_seconds=10)
    # The monitor's stop-loss exit gets a 502 from a gateway: it never reached the broker
    live.broker.place_effects = [OrderPlacementUncertain("HTTP 502", status_code=502,
                                                         kind="http")]
    user_ids = []

    def user_sells_in_the_app():
        user_ids.append(live.broker._create(
            OrderRequest(symbol="TCS", side=Side.SELL, quantity=Decimal(10),
                         order_type=OrderType.LIMIT, price=89.5, security_id="11536"),
            Script(timeline=[(0, {"status": "PENDING"})])))

    await live.run(stop_at=120, at=[(60, user_sells_in_the_app)])
    [user] = user_ids

    assert live.cancels(user) == []
    assert live.broker.row_of(user)["status"] == "PENDING"
    assert len(live.broker.placed()) == 1            # Skopaq sold nothing more meanwhile
    assert "placement-match:" in "".join(alerts.keys("CRITICAL"))


async def test_a_users_same_size_sell_does_not_hide_an_uncertain_sell(notify, alerts):  # (d)
    live = ExecLive([], record=False)
    live.client.holdings = [Holding(symbol="TCS", security_id="11536", quantity=Decimal(20))]
    # Our SELL 10: POST times out, the order exists and rests; the book shows it 120 s later
    live.client.place_effects = [Script(
        timeline=[(0, {"status": "PENDING"})],
        uncertain=OrderPlacementUncertain("ReadTimeout", kind="transport"),
        visible_after=120.0)]
    first = await live.run(exec_exit(10))
    assert first.remaining_open

    # The user sells 10 of the same shares in the broker's app (a resting LIMIT)
    live.client.extra_rows = [row("PENDING", traded=0, requested=10, id="USER-1",
                                  order_type="LIMIT",
                                  created_at=live.clock.wall().isoformat(),
                                  updated_at=live.clock.wall().isoformat())]
    live.client.place_effects = [Script(timeline=[(0, {"status": "SUCCESS", "traded_qty": 10,
                                                        "traded_price": "94"})])]
    second = await live.run(exec_exit(10))

    assert not second.success and "uncertain" in second.rejection_reason
    assert len(live.client.placed()) == 1            # 20 held: 10 ours + 10 the user's


async def test_an_order_tagged_with_the_placements_remark_is_adopted(alerts):
    live = Live({"TCS": (10, 100.0)})
    live.journal(ts=BASE_WALL.isoformat(), event="uncertain", order_id="",
                 internal_id="abc", symbol="TCS", security_id="11536", side="SELL",
                 qty="10", purpose="exit", remark="skopaq-abc", before_ids=[])
    order_id = await live.open_order("TCS", Side.SELL, 10, Script(
        timeline=[(0, {"status": "PENDING", "remarks": "skopaq-abc"})]))

    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    await live.monitor._resync(positions, result)
    await live.monitor._settle_resumes(result)       # (resumes run in the background)

    tracked = live.router.registry.get(order_id)
    assert tracked is not None and not tracked.guessed
    assert live.cancels(order_id)                    # ours: its exit is cancelled and settled
    assert live.router.journal.unresolved_uncertain() == []


# ── A confirmed SELL of another process, while the book lags ─────────────────


async def test_a_sell_another_process_confirmed_counts_while_the_book_lags(alerts):
    from unittest.mock import MagicMock

    from skopaq.broker.paper_engine import PaperEngine
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter
    from skopaq.execution.safety_checker import SafetyChecker
    from tests.unit.execution._fakes import FakeClock
    from tests.unit.execution.test_position_monitor_live import RULES

    def process(clock, broker):
        router = OrderRouter(MagicMock(trading_mode="live"), PaperEngine(), live_client=broker,
                             sleep=clock.sleep, clock=clock.clock, wall=clock.wall)
        return Executor(router, SafetyChecker(rules=RULES), clock=clock.clock)

    def exit_signal():
        return TradingSignal(symbol="TCS", action="SELL", confidence=80, entry_price=94.0,
                             order_type=OrderType.MARKET, quantity=Decimal(10),
                             reasoning="STOP")

    # Process A sells 10: GET /order confirms SUCCESS at once, but the order book shows
    # the order only 60 s later, and positions lag too. Process B sells 1 s later.
    clock = FakeClock()
    broker = PositionBroker(clock, {"TCS": (10, 100.0)}, lag=True)
    broker.place_effects = [Script(timeline=[(0, {"status": "SUCCESS", "traded_qty": 10,
                                                  "traded_price": "94"})],
                                   visible_after=60.0)]
    broker.default_script = lambda: Script(timeline=[(0, {"status": "SUCCESS", "traded_qty": 10,
                                                          "traded_price": "93"})])
    a, b = process(clock, broker), process(clock, broker)
    first = await a.execute_signal(exit_signal())
    await clock.sleep(1)
    second = await b.execute_signal(exit_signal())

    assert first.success and first.filled_quantity == 10
    assert not second.success and "sold but not yet in positions" in second.rejection_reason
    assert len(broker.placed()) == 1
