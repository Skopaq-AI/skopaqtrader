"""Protective exits sell the day's position, never the user's older delivery holdings.

The monitor's tracked and adopted positions and CLOSING's targets are sized by the position
less Skopaq's own open, unconfirmed and not-yet-shown SELLs, then capped by what the account
can sell. Holdings of the same stock must not absorb a pending, uncertain or partly filled
exit (the reviewers' holdings=50 probes). The Executor re-checks that size under the SELL
lock, from the same book-first read, for exits marked ``position_only``; an analysis SELL
(`skopaq trade`) may still sell holdings.

Each case runs with holdings 0 (the behaviour to match) and 50.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from skopaq.broker.client import BrokerError, OrderPlacementUncertain
from skopaq.broker.models import Holding, OrderType, TradingSignal
from skopaq.execution.position_monitor import MonitorResult
from tests.unit.execution._fakes import BASE_WALL, IGNORE, Script, row
from tests.unit.execution.test_daemon_live import LiveDaemon, _report
from tests.unit.execution.test_daemon_live import _lookups as _daemon_lookups  # noqa: F401
from tests.unit.execution.test_executor_sell_context import Live as LiveExecutor
from tests.unit.execution.test_executor_sell_context import _held
from tests.unit.execution.test_fill_recording import _second_process
from tests.unit.execution.test_position_monitor_live import (  # noqa: F401
    TCS,
    Live,
    _filled,
    _lookups,
    alerts,
)

HOLDINGS = pytest.mark.parametrize("long_term", [0, 50])


def _holdings(qty):
    return [Holding(symbol="TCS", security_id="11536", quantity=Decimal(qty),
                    average_price=50.0)]


def _sells(broker):
    return [(o.order_id, int(o.request.quantity), broker.row_of(o.order_id)["status"],
             broker.row_of(o.order_id).get("traded_qty"))
            for o in broker.placed_orders("TCS")]


async def _tracked(live):
    positions, result = [], MonitorResult()
    await live.monitor._resync(positions, result, first=True)
    [pos] = positions
    return pos, positions, result


async def _next_cycle(live, positions, result):
    await live.monitor._check_positions(positions, 1, result)
    for pos in positions:
        if pos.exit_task is not None:
            await pos.exit_task


# ── The monitor ──────────────────────────────────────────────────────────────


@HOLDINGS
async def test_the_monitor_does_not_resell_over_its_stuck_exit(alerts, long_term):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    live.broker.holdings = _holdings(long_term)
    pos, positions, result = await _tracked(live)
    live.broker.place_effects = [Script(on_cancel=[IGNORE] * 1000)]
    await live.monitor._execute_sell(pos, 90.0, "HARD STOP", result)

    await live.monitor._resync(positions, result)
    assert (pos.quantity, pos.pending_exit) == (0, True)

    live.broker.place_effects = [Script(timeline=[(0, _filled(10, "90"))])]
    await _next_cycle(live, positions, result)
    await live.monitor._settle_resumes(result)
    assert [s[0] for s in _sells(live.broker)] == ["EQ-1"], _sells(live.broker)


@HOLDINGS
async def test_the_monitor_does_not_resell_over_its_uncertain_exit(alerts, long_term):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    live.broker.holdings = _holdings(long_term)
    pos, positions, result = await _tracked(live)
    # POST /order times out although the order exists; the book shows it only after 60 s
    live.broker.place_effects = [Script(
        timeline=[(0, {"status": "PENDING"})],
        uncertain=OrderPlacementUncertain("ReadTimeout", kind="transport"),
        visible_after=60.0)]
    await live.monitor._execute_sell(pos, 90.0, "HARD STOP", result)

    await live.monitor._resync(positions, result)
    assert (pos.quantity, pos.pending_exit) == (0, True)

    live.broker.place_effects = [Script(timeline=[(0, _filled(10, "90"))])]
    await _next_cycle(live, positions, result)
    await live.monitor._settle_resumes(result)
    assert [s[0] for s in _sells(live.broker)] == ["EQ-1"], _sells(live.broker)


@HOLDINGS
async def test_the_monitor_sells_only_the_rest_of_a_partial_exit(alerts, long_term):
    live = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    live.broker.holdings = _holdings(long_term)
    pos, positions, result = await _tracked(live)
    # The exit trades 4, then the broker refuses the re-placement
    live.broker.place_effects = [
        Script(timeline=[(0, {"status": "PENDING"}),
                         (2, {"status": "PENDING", "traded_qty": 4, "traded_price": "90"})]),
        BrokerError("RMS: rejected", 400, kind="http")]
    await live.monitor._execute_sell(pos, 90.0, "HARD STOP", result)
    assert pos.quantity == 6

    await live.monitor._resync(positions, result)
    assert pos.quantity == 6

    live.broker.place_effects = [Script(timeline=[(0, _filled(6, "90"))])]
    await _next_cycle(live, positions, result)
    sells = _sells(live.broker)
    assert [(q, traded) for _, q, _, traded in sells] == [(10, 4), (6, 6)], sells


@HOLDINGS
async def test_two_monitors_sell_the_same_position_once(alerts, long_term):
    a = Live({"TCS": (10, 100.0)}, ltps={TCS: 90.0}, lag=True)
    a.broker.holdings = _holdings(long_term)
    a.broker.default_script = lambda: Script(timeline=[(0, {"status": "PENDING"}),
                                                       (2, _filled(10, "90"))])
    b = _second_process(a, [])
    pa, ra, pb, rb = [], MonitorResult(), [], MonitorResult()
    await a.monitor._resync(pa, ra, first=True)
    await b._resync(pb, rb, first=True)
    assert [p.quantity for p in pa + pb] == [10, 10]

    await asyncio.gather(a.monitor._check_positions(pa, 1, ra), b._check_positions(pb, 1, rb))
    await asyncio.gather(*(p.exit_task for p in pa + pb if p.exit_task is not None))

    assert [(q, status) for _, q, status, _ in _sells(a.broker)] == [(10, "SUCCESS")]
    assert ra.sells_executed + rb.sells_executed == 1


# ── CLOSING ──────────────────────────────────────────────────────────────────


@HOLDINGS
async def test_closing_waits_for_its_own_stuck_exit(alerts, long_term):
    live = LiveDaemon({"TCS": (10, 100.0)}, lag=True)
    live.broker.holdings = _holdings(long_term)
    stuck = await live.stuck_exit("TCS", 10, Script(on_cancel=[IGNORE] * 20))
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(10))])]
    placed = {}
    real_place = live.broker.place_order

    async def place(order):
        response = await real_place(order)
        placed[response.order_id] = live.broker.row_of(stuck)["status"]
        return response

    live.broker.place_order = place
    await live.daemon._phase_close(_report())

    # Sold only once its own exit was final (cancelled, nothing traded)
    assert placed == {"EQ-2": "CANCELLED"}, placed


@HOLDINGS
async def test_closing_after_a_filled_exit_sells_nothing_more(alerts, long_term):
    # MONITORING's exit sold the day's 5 TCS (confirmed, in the book); positions lag
    live = LiveDaemon({"TCS": (5, 100.0)}, lag=True)
    live.broker.holdings = _holdings(long_term)
    live.broker.extra_rows = [row("SUCCESS", traded=5, requested=5, id="EQ-9",
                                  updated_at=BASE_WALL.isoformat())]
    live.router.registry.record_confirmed_exit("TCS", "11536", Decimal(5), order_id="EQ-9")
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(5))])]

    await live.daemon._phase_close(_report())

    assert live.broker.placed_orders("TCS") == []


@HOLDINGS
async def test_closing_after_a_partial_exit_sells_only_the_rest(alerts, long_term):
    # MONITORING's exit sold 4 of the day's 10 TCS (confirmed, final in the book); lag
    live = LiveDaemon({"TCS": (10, 100.0)}, lag=True)
    live.broker.holdings = _holdings(long_term)
    live.broker.extra_rows = [row("PARTIALLY FILLED - CANCELLED", traded=4, requested=10,
                                  id="EQ-9", traded_price="95",
                                  updated_at=BASE_WALL.isoformat())]
    live.router.registry.record_confirmed_exit("TCS", "11536", Decimal(4), order_id="EQ-9")
    live.broker.by_symbol["TCS"] = [Script(timeline=[(0, _filled(6, "95"))])]

    await live.daemon._phase_close(_report())

    assert [int(o.request.quantity) for o in live.broker.placed_orders("TCS")] == [6]


# ── The Executor's re-check under the lock ───────────────────────────────────


def _exit(qty, *, position_only):
    return TradingSignal(symbol="TCS", action="SELL", confidence=80, entry_price=94.0,
                         order_type=OrderType.MARKET, quantity=Decimal(qty),
                         reasoning="HARD STOP", position_only=position_only)


@pytest.mark.parametrize("position_only, sent", [(True, False), (False, True)])
async def test_a_position_exit_is_rechecked_against_the_position(alerts, position_only, sent):
    with patch("skopaq.notifications.notify_trade_event", new_callable=AsyncMock):
        live = LiveExecutor([_held(10)], record=False)
        live.client.holdings = _holdings(50)
        # Another Skopaq process's exit already sold the day's 10 (confirmed, in the book);
        # positions do not show it yet
        live.client.extra_rows = [row("SUCCESS", traded=10, requested=10, id="EQ-9",
                                      updated_at=BASE_WALL.isoformat())]
        live.router.registry.record_confirmed_exit("TCS", "11536", Decimal(10),
                                                   order_id="EQ-9")
        live.client.place_effects = [Script(timeline=[(0, {"status": "SUCCESS",
                                                           "traded_qty": 10,
                                                           "traded_price": "94"})])]
        result = await live.run(_exit(10, position_only=position_only))

    # A protective exit of the position is refused (the holdings are the user's); an
    # analysis SELL of 10 may sell them
    assert bool(live.client.placed()) is sent
    if not sent:
        assert result.safety_passed is False
        assert "10 sold but not yet in positions" in result.rejection_reason
        assert "holdings are not sold by an exit" in result.rejection_reason
