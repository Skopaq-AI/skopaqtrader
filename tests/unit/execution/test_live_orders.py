"""LiveOrderWorker: a live order counts only what INDstocks confirms (design v2 §6).

Every test runs on virtual time (FakeClock) against a scripted broker (FakeClient), so
30-second timeouts take milliseconds and every broker call is recorded in order.
"""

from __future__ import annotations

import asyncio
import math
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, patch

import pytest

from skopaq.broker.client import BrokerError, OrderPlacementUncertain
from skopaq.broker.models import (
    OrderRequest,
    OrderType,
    Position,
    Side,
    TradingSignal,
    is_unconfirmed,
)
from skopaq.execution.live_orders import (
    FillSettings,
    LiveOrderWorker,
    OrderDeadlines,
    OrderRegistry,
    TrackedOrder,
    round_down_to_tick,
    tick_for,
)
from skopaq.execution.order_journal import OrderJournal
from skopaq.risk.calendar import IST
from tests.unit.execution._fakes import IGNORE, AlertSpy, FakeClient, FakeClock, Script, row

SUCCESS_10 = {"status": "SUCCESS", "traded_qty": 10, "traded_price": "100"}


@pytest.fixture(autouse=True)
def _csv_tick():
    """Re-pricing uses the fallback tick unless a test sets one (no instruments download)."""
    with patch("skopaq.execution.live_orders.resolve_tick_size",
               new=AsyncMock(return_value=None)) as mock:
        yield mock


def held(n: int = 10, symbol: str = "TCS", security_id: str = "11536") -> list[Position]:
    return [Position(symbol=symbol, security_id=security_id, quantity=Decimal(n),
                     average_price=90.0, product="CNC")]


def buy(n: int = 10, price: float = 101.0) -> OrderRequest:
    return OrderRequest(symbol="TCS", side=Side.BUY, quantity=Decimal(n),
                        order_type=OrderType.LIMIT, price=price, security_id="11536")


def exit_sell(n: int = 10, symbol: str = "TCS", security_id: str = "11536") -> OrderRequest:
    return OrderRequest(symbol=symbol, side=Side.SELL, quantity=Decimal(n),
                        order_type=OrderType.MARKET, security_id=security_id)


def sig(action: str = "BUY", price: float = 101.0, symbol: str = "TCS") -> TradingSignal:
    return TradingSignal(symbol=symbol, action=action, entry_price=price)


class Rig:
    """A worker wired to a FakeClient, FakeClock and AlertSpy."""

    def __init__(self, *, positions=None, holdings=None, ltp=100.0, journal_dir=None,
                 wall=None, **settings) -> None:
        self.clock = FakeClock(wall) if wall else FakeClock()
        self.client = FakeClient(self.clock, positions=positions, holdings=holdings, ltp=ltp)
        self.alerts = AlertSpy()
        self.settings = FillSettings(**settings)
        self.deadlines = OrderDeadlines(clock=self.clock.clock, wall=self.clock.wall)
        self.registry = OrderRegistry(clock=self.clock.clock)
        self.journal = (OrderJournal(journal_dir, wall=self.clock.wall, alerter=self.alerts)
                        if journal_dir else None)
        self.worker = LiveOrderWorker(
            self.client, self.settings, deadlines=self.deadlines, registry=self.registry,
            journal=self.journal, alerter=self.alerts, sleep=self.clock.sleep,
            clock=self.clock.clock, wall=self.clock.wall,
        )

    def cancels(self) -> list[float]:
        return [c[2] for c in self.client.calls if c[0] == "cancel_order"]

    async def advance_to(self, t: float) -> None:
        while self.clock.t < t:
            await asyncio.sleep(0)


# ── Entries: one order; whatever has not filled is cancelled ─────────────────


async def test_filled_on_the_first_poll_reports_the_trades_vwap():   # T1
    rig = Rig()
    rig.client.place_effects = [Script(
        timeline=[(0, {"status": "SUCCESS", "traded_qty": 10, "traded_price": "100.00"})],
        trades=[{"quantity": 6, "price": 99.5}, {"quantity": 4, "price": 101}],
    )]
    result = await rig.worker.execute(buy(), sig())

    assert result.success and result.mode == "live"
    assert (result.outcome, result.filled_quantity, result.requested_quantity) == (
        "filled", 10, 10)
    assert result.fill_price == pytest.approx(100.1)
    assert result.fill_price_source == "trades"
    assert result.order_ids == ["EQ-1"]
    assert (result.order.order_id, result.order.status) == ("EQ-1", "SUCCESS")
    assert result.remaining_open is False and result.fill_unconfirmed is False
    assert result.brokerage == 20.0
    assert "cancel_order" not in rig.client.names()
    assert rig.alerts.alerts == []


async def test_unreadable_trades_fall_back_to_the_order_price():   # T2
    rig = Rig()
    rig.client.place_effects = [Script(
        timeline=[(0, {"status": "SUCCESS", "traded_qty": 10, "traded_price": "100.40"})],
        trades=BrokerError("HTTP 404", 404, kind="http"),
    )]
    result = await rig.worker.execute(buy(), sig())

    assert result.success
    assert (result.fill_price, result.fill_price_source) == (pytest.approx(100.4), "order")


async def test_polls_until_the_broker_reports_success():   # T3
    rig = Rig()
    rig.client.place_effects = [Script(timeline=[
        (0, {"status": "INITIATED"}), (1, {"status": "PENDING"}), (3, SUCCESS_10)])]
    result = await rig.worker.execute(buy(), sig())

    assert result.success and result.filled_quantity == 10
    assert rig.client.names().count("get_order") == 4          # t = 0, 1, 2, 3
    assert rig.clock.t == 3 < rig.settings.timeout_s


async def test_broker_failed_is_a_rejection_without_a_cancel_or_alert():   # T4
    rig = Rig()
    rig.client.place_effects = [Script(timeline=[
        (0, {"status": "FAILED", "extra_info": "RMS: Margin exceeds"})])]
    result = await rig.worker.execute(buy(), sig())

    assert not result.success and result.outcome == "rejected"
    assert result.rejection_reason == "Broker FAILED: RMS: Margin exceeds"
    assert result.brokerage == 0.0
    assert "cancel_order" not in rig.client.names()
    assert rig.alerts.keys("CRITICAL") == []


@pytest.mark.parametrize("status,outcome", [("ABORTED", "rejected"),
                                            ("CANCELLED", "cancelled"),
                                            ("EXPIRED", "cancelled")])
async def test_final_without_a_fill_fails(status, outcome):   # T5
    rig = Rig()
    rig.client.place_effects = [Script(timeline=[(0, {"status": status})])]
    result = await rig.worker.execute(buy(), sig())

    assert not result.success and result.outcome == outcome
    assert result.rejection_reason.startswith(f"Broker {status}")
    assert result.filled_quantity == 0


async def test_synchronous_rejection_is_not_polled():   # T6
    rig = Rig()
    rig.client.place_effects = [BrokerError("HTTP 400: RMS: Margin exceeds", 400, kind="http")]
    result = await rig.worker.execute(buy(), sig())

    assert not result.success and result.outcome == "rejected"
    assert result.rejection_reason.startswith("Broker rejected")
    assert "Margin exceeds" in result.rejection_reason
    assert result.order is None and result.order_ids == []
    assert "get_order" not in rig.client.names()


async def test_partially_filled_buy_counts_only_the_filled_part():   # T7
    rig = Rig()
    rig.client.place_effects = [Script(timeline=[(0, {
        "status": "PARTIALLY FILLED - CANCELLED", "traded_qty": 4, "traded_price": "100.5"})])]
    result = await rig.worker.execute(buy(), sig())

    assert result.success and result.outcome == "partial"
    assert result.filled_quantity == 4 and result.fill_price == pytest.approx(100.5)
    assert result.broker_message == "filled 4 of 10; rest cancelled"
    assert rig.alerts.keys("WARNING") == ["entry-partial:EQ-1"]


async def test_entry_not_filled_in_time_is_cancelled():   # T8
    rig = Rig()
    result = await rig.worker.execute(buy(), sig())

    assert not result.success and result.outcome == "cancelled"
    assert result.remaining_open is False
    assert result.rejection_reason == "Not filled within 30s — cancelled at the broker"
    assert rig.cancels() == [30.0]


async def test_entry_timeout_keeps_the_part_that_filled():   # T9
    rig = Rig()
    rig.client.place_effects = [Script(timeline=[
        (0, {"status": "PENDING"}),
        (5, {"status": "PARTIALLY FILLED", "traded_qty": 3, "traded_price": "100.5"})])]
    result = await rig.worker.execute(buy(), sig())

    assert result.success and result.outcome == "partial" and result.filled_quantity == 3
    assert result.fill_price == pytest.approx(100.5)
    assert rig.cancels() == [30.0]


async def test_fill_racing_the_cancel_counts_as_filled():   # T10
    rig = Rig()
    rig.client.place_effects = [Script(on_cancel=[
        {"status": "SUCCESS", "traded_qty": 10, "traded_price": "101"}])]
    result = await rig.worker.execute(buy(), sig())

    assert result.success and result.outcome == "filled" and result.filled_quantity == 10


async def test_a_refused_cancel_is_retried_two_seconds_later():   # T12
    rig = Rig()
    rig.client.place_effects = [Script(on_cancel=[
        BrokerError("HTTP 400: The order is already pending with the exchange", 400,
                    kind="http")])]
    result = await rig.worker.execute(buy(), sig())

    assert rig.cancels() == [30.0, 32.0]
    assert result.outcome == "cancelled"


async def test_position_not_found_stops_cancelling_but_keeps_reading():   # T12
    rig = Rig()
    rig.client.place_effects = [Script(
        timeline=[(0, {"status": "PENDING"}), (30.5, SUCCESS_10)],
        on_cancel=[BrokerError("HTTP 400: Position could not be found.", 400, kind="http")],
    )]
    result = await rig.worker.execute(buy(), sig())

    assert rig.cancels() == [30.0]
    assert result.success and result.filled_quantity == 10


NOT_FOUND = BrokerError("API error 400: Position could not be found.", 400, kind="http")


async def test_a_not_found_cancel_is_sent_again_while_the_order_still_reads_working():
    """An entry reaches its timeout; the OMS answers the first cancel "could not be found"
    (it has not registered the order yet) while every read shows it PENDING: the cancel is
    sent again 2 s later instead of leaving a live BUY working at the broker."""
    rig = Rig()
    rig.client.place_effects = [Script(timeline=[(0, {"status": "PENDING"})],
                                       on_cancel=[NOT_FOUND])]
    result = await rig.worker.execute(buy(10), sig())

    assert rig.cancels() == [30.0, 32.0]
    assert rig.client.row_of("EQ-1")["status"] == "CANCELLED"
    assert result.outcome == "cancelled" and not result.remaining_open


async def test_an_interrupted_order_whose_first_cancel_is_not_found_is_still_cancelled():
    """Ctrl+C (or an MCP cancel) just as the POST answer arrives: the first cancel gets
    "could not be found", the order then reads PENDING, and the next cancel closes it."""
    rig = Rig(positions=held(10))
    rig.client.place_effects = [Script(timeline=[(0, {"status": "PENDING"})],
                                       on_cancel=[NOT_FOUND])]
    real_place = rig.client.place_order
    box = {}

    async def place(order):
        response = await real_place(order)
        box["task"].cancel()
        return response

    rig.client.place_order = place
    box["task"] = asyncio.ensure_future(rig.worker.execute(buy(10), sig()))
    with pytest.raises(asyncio.CancelledError):
        await box["task"]

    assert len(rig.cancels()) == 2
    assert rig.client.row_of("EQ-1")["status"] == "CANCELLED"


async def test_status_falls_back_to_the_book_and_sticks_to_it():   # T14
    rig = Rig()
    rig.client.place_effects = [Script(
        timeline=[(0, {"status": "PENDING"}), (2, SUCCESS_10)], in_get_order=False)]
    result = await rig.worker.execute(buy(), sig())

    assert result.success
    assert rig.client.names().count("get_order") == 1


async def test_fill_without_any_price_is_estimated_and_warned():   # T28
    rig = Rig()
    rig.client.place_effects = [Script(timeline=[
        (0, {"status": "SUCCESS", "traded_qty": 10, "traded_price": ""})])]
    result = await rig.worker.execute(buy(price=101.0), sig())

    assert result.success
    assert (result.fill_price, result.fill_price_source) == (101.0, "estimate")
    assert rig.alerts.keys("WARNING") == ["fill-price-unknown:EQ-1"]


async def test_fill_price_from_the_trade_book_by_exchange_order_id():
    rig = Rig()
    rig.client.place_effects = [Script(timeline=[(0, {
        "status": "SUCCESS", "traded_qty": 10, "traded_price": "", "exch_order_id": "X1"})])]
    rig.client.trade_book = [
        {"exch_order_id": "X1", "quantity": 10, "price": 100.2},
        {"exch_order_id": "X2", "quantity": 5, "price": 50.0},
    ]
    result = await rig.worker.execute(buy(), sig())

    assert (result.fill_price, result.fill_price_source) == (pytest.approx(100.2), "trade_book")


async def test_uncertain_buy_without_a_match_is_unconfirmed():   # T23
    rig = Rig()
    rig.client.place_effects = [OrderPlacementUncertain("HTTP 502", 502, kind="http")]
    result = await rig.worker.execute(buy(), sig())

    assert not result.success and is_unconfirmed(result) and result.remaining_open
    assert result.outcome == "unknown"
    assert len(rig.client.placed()) == 1


# ── Protective exits: worked until filled, bounded ───────────────────────────


@pytest.mark.parametrize("traded", [None, 4])
async def test_exit_takes_the_larger_of_traded_and_the_trades(traded):   # T11
    rig = Rig(positions=held(), exit_max_attempts=1)
    rig.client.place_effects = [Script(
        timeline=[(0, {"status": "PARTIALLY FILLED - EXPIRED", "traded_qty": traded})],
        trades=[{"quantity": 4, "price": 100}, {"quantity": 2, "price": 101}],
    )]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert result.success and result.outcome == "partial"
    assert result.filled_quantity == 6 and result.fill_unconfirmed is False


async def test_exit_with_an_unknown_fill_stops_after_one_order():   # T11
    rig = Rig(positions=held())
    rig.client.place_effects = [Script(timeline=[
        (0, {"status": "PARTIALLY FILLED - EXPIRED", "traded_qty": None})])]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert len(rig.client.placed()) == 1
    assert not result.success and result.fill_unconfirmed and result.outcome == "unknown"
    assert "fill-qty-unknown:EQ-1" in rig.alerts.keys("CRITICAL")


async def test_cancel_never_confirmed_leaves_the_exit_stuck(tmp_path):   # T13
    rig = Rig(positions=held(), journal_dir=tmp_path)
    rig.client.default_script = lambda: Script(on_cancel=[IGNORE] * 20)
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert len(rig.client.placed()) == 1                       # never re-placed over it
    assert not result.success and result.remaining_open and result.outcome == "open"
    assert "EQ-1" in result.rejection_reason
    stuck = [a for a in rig.alerts.alerts if a[1] == "order-stuck:EQ-1"]
    assert stuck and stuck[0][0] == "CRITICAL" and stuck[0][3] == ("EQ-1",)
    assert "exit-not-filled:TCS" in rig.alerts.keys("CRITICAL")
    assert rig.registry.get("EQ-1").state == "stuck"
    assert [e["event"] for e in rig.journal.entries()] == ["placed", "stuck"]
    assert rig.clock.t == pytest.approx(20)                    # attempt 10 s + cancel window 10 s


async def test_resting_exit_is_cancelled_and_replaced_at_a_tick_rounded_limit():   # T15
    rig = Rig(positions=held(), ltp=99.0)
    rig.client.place_effects = [
        Script(timeline=[(0, {"status": "PENDING"}), (3, {
            "status": "PARTIALLY FILLED", "traded_qty": 4, "traded_price": "100"})]),
        Script(timeline=[(0, {"status": "SUCCESS", "traded_qty": 6, "traded_price": "98.60"})],
               trades=[{"quantity": 6, "price": 98.6}]),
    ]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert result.success and result.outcome == "filled" and result.filled_quantity == 10
    assert result.order_ids == ["EQ-1", "EQ-2"]
    first, second = rig.client.placed()
    assert first[1:4] == ("MARKET", 10, None)
    assert second[1:4] == ("LIMIT", 6, 98.5)                   # floor(99 × 0.995 = 98.505, 0.05)
    assert ("get_ltp", "NSE_11536") in rig.client.calls
    assert result.fill_price == pytest.approx(99.16)          # (4 × 100 + 6 × 98.6) / 10
    assert result.brokerage == 40.0
    assert rig.registry.recent_exit_qty("TCS", "11536", 600) == 10


@pytest.mark.parametrize("ltp,csv_tick,expected", [
    ("1234.57", "0.10", "1228.3"),     # CSV tick
    ("1234.57", None, "1228"),         # no tick: 1.00 at ₹1,000–20,000
    ("6123.45", "0.50", "6092.5"),
    ("25000.37", None, "24875"),       # 5.00 above ₹20,000
    ("100", "5", "99.5"),              # implausible CSV tick on a ₹100 stock: 0.05
])
def test_reprice_rounds_down_to_a_plausible_tick(ltp, csv_tick, expected):   # T16
    tick = tick_for(Decimal(ltp), Decimal(csv_tick) if csv_tick else None)
    assert round_down_to_tick(Decimal(ltp) * Decimal("0.995"), tick) == Decimal(expected)


async def test_replaced_exit_uses_the_instrument_tick(_csv_tick):   # T16
    _csv_tick.return_value = Decimal("0.10")
    rig = Rig(positions=held(), ltp=1234.57)
    rig.client.place_effects = [Script(), Script(timeline=[
        (0, {"status": "SUCCESS", "traded_qty": 10, "traded_price": "1228.3"})])]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 1234.0))

    assert result.success
    assert rig.client.placed()[1][3] == 1228.3
    _csv_tick.assert_awaited_with(rig.client, "TCS", "NSE")


def _too_many() -> BrokerError:
    return BrokerError("HTTP 429: Too many requests", 429, kind="http")


async def test_rate_limited_exit_backs_off_without_using_an_attempt():   # T17
    rig = Rig(positions=held(), exit_max_attempts=1)
    rig.client.place_effects = [_too_many(), _too_many(), Script(timeline=[(0, SUCCESS_10)])]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert result.success and result.filled_quantity == 10
    assert [c[4] for c in rig.client.placed()] == [0, 1, 3]    # backoff 1 s, then 2 s


async def test_rate_limited_until_the_cap_stops_the_exit():   # T17
    rig = Rig(positions=held())
    rig.client.place_effects = [_too_many() for _ in range(10)]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert not result.success
    assert [c[4] for c in rig.client.placed()] == [0, 1, 3, 7, 15]   # 15 s cap
    assert "exit-rejected:TCS" in rig.alerts.keys("CRITICAL")


async def test_price_rejection_on_a_limit_switches_to_market_in_the_same_attempt():   # T18
    rig = Rig(positions=held())
    rig.client.place_effects = [
        Script(),
        BrokerError("HTTP 400: Price should be a multiple of the tick size", 400, kind="http"),
        Script(timeline=[(0, SUCCESS_10)]),
    ]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert result.success
    assert [c[1] for c in rig.client.placed()] == ["MARKET", "LIMIT", "MARKET"]


async def test_blocked_market_order_is_replaced_by_a_limit():   # T18
    rig = Rig(positions=held())
    rig.client.place_effects = [
        BrokerError("HTTP 400: Market orders are blocked for this instrument.", 400,
                    kind="http"),
        Script(timeline=[(0, SUCCESS_10)]),
    ]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert result.success
    assert [c[1] for c in rig.client.placed()] == ["MARKET", "LIMIT"]


async def test_rms_rejection_stops_the_exit_without_switching_type():   # T19
    rig = Rig(positions=held())
    rig.client.place_effects = [BrokerError("HTTP 400: RMS: Margin exceeds", 400, kind="http")]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert not result.success and result.outcome == "rejected"
    assert len(rig.client.placed()) == 1
    assert {"exit-rejected:TCS", "exit-not-filled:TCS"} <= set(rig.alerts.keys("CRITICAL"))


async def test_uncertain_placement_adopts_the_one_new_order_in_the_book():   # T20
    rig = Rig(positions=held())
    rig.client.extra_rows = [row("PENDING", id="EQ-OLD")]      # an older identical order
    rig.client.place_effects = [Script(
        uncertain=OrderPlacementUncertain("HTTP 502", 502, kind="http"), visible_after=2.0,
        timeline=[(0, SUCCESS_10)])]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert result.success and result.order_ids == ["EQ-1"]
    assert len(rig.client.placed()) == 1
    assert rig.registry.get("EQ-1") is not None
    assert not [k for k in rig.alerts.keys() if k.startswith("placement-uncertain")]


async def test_uncertain_placement_never_found_stops_the_exit():   # T21
    rig = Rig(positions=held())
    rig.client.place_effects = [Script(
        uncertain=OrderPlacementUncertain("ReadTimeout", kind="transport"), visible_after=None)]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert not result.success and result.remaining_open and result.outcome == "unknown"
    assert len(rig.client.placed()) == 1                       # never re-sent
    assert [k for k in rig.alerts.keys("CRITICAL") if k.startswith("placement-uncertain:")]
    assert rig.clock.t == pytest.approx(15)                    # the reconcile window


async def test_reconcile_by_new_ids_ignores_clock_skew():   # T22
    rig = Rig(positions=held())
    rig.client.broker_skew_s = -300                            # host clock 5 min ahead
    rig.client.place_effects = [Script(
        uncertain=OrderPlacementUncertain("HTTP 500", 500, kind="http"), visible_after=1.0,
        timeline=[(0, SUCCESS_10)])]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert result.success and result.order_ids == ["EQ-1"]


@pytest.mark.parametrize("skew,adopted", [(-90, True), (-300, False)])
async def test_reconcile_without_a_snapshot_uses_a_time_window(skew, adopted):   # T22
    rig = Rig(positions=held())
    rig.client.broker_skew_s = skew
    rig.client.book_errors = [BrokerError("HTTP 503", 503, kind="http")]   # the id snapshot
    rig.client.place_effects = [Script(
        uncertain=OrderPlacementUncertain("HTTP 500", 500, kind="http"), visible_after=1.0,
        timeline=[(0, SUCCESS_10)])]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert result.success is adopted
    assert result.remaining_open is not adopted


async def test_ambiguous_uncertain_placement_lists_the_candidates():   # T23
    rig = Rig(positions=held())
    rig.client.place_effects = [[
        Script(uncertain=OrderPlacementUncertain("HTTP 500", 500, kind="http")), Script()]]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert not result.success and result.remaining_open and result.outcome == "unknown"
    assert "EQ-1" in result.rejection_reason and "EQ-2" in result.rejection_reason
    [alert] = [a for a in rig.alerts.alerts if a[1].startswith("placement-uncertain:")]
    assert alert[0] == "CRITICAL" and alert[3] == ("EQ-1", "EQ-2")
    assert len(rig.client.placed()) == 1


async def test_exit_filled_across_attempts_reports_the_vwap():   # T24
    rig = Rig(positions=held())
    rig.client.place_effects = [
        Script(timeline=[(0, {"status": "PENDING"}), (2, {
            "status": "PARTIALLY FILLED", "traded_qty": 6, "traded_price": "100"})]),
        Script(timeline=[(0, {"status": "SUCCESS", "traded_qty": 4, "traded_price": "99"})]),
    ]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert result.success and result.filled_quantity == 10
    assert result.fill_price == pytest.approx(99.6)


async def test_exit_that_never_fills_ends_in_a_critical_alert():   # T25
    rig = Rig(positions=held())
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert not result.success and result.outcome == "cancelled"
    assert [(c[1], c[3]) for c in rig.client.placed()] == [
        ("MARKET", None), ("LIMIT", 99.5), ("LIMIT", 99.0)]    # buffer 0.5 %, then 1 %
    assert "3 attempt" in result.rejection_reason
    assert rig.alerts.keys("CRITICAL") == ["exit-not-filled:TCS"]


async def test_exit_partly_filled_after_all_attempts_is_critical():
    rig = Rig(positions=held())
    rig.client.place_effects = [Script(timeline=[(0, {"status": "PENDING"}), (1, {
        "status": "PARTIALLY FILLED", "traded_qty": 3, "traded_price": "100"})])]
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert result.success and result.outcome == "partial" and result.filled_quantity == 3
    assert result.broker_message == "sold 3 of 10 after 3 attempt(s)"
    assert "exit-partial:TCS" in rig.alerts.keys("CRITICAL")


async def test_replace_blocked_when_positions_show_nothing_left():   # T26
    rig = Rig(positions=[])
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert len(rig.client.placed()) == 1
    assert "exit-replace-blocked:TCS" in rig.alerts.keys("CRITICAL")
    assert not result.success


async def test_replace_blocked_when_the_book_cannot_be_read():   # T26
    rig = Rig(positions=held())
    rig.client.book_error_always = BrokerError("HTTP 503", 503, kind="http")
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert len(rig.client.placed()) == 1
    assert "exit-replace-blocked:TCS" in rig.alerts.keys("CRITICAL")
    assert not result.success


async def test_replace_blocked_by_a_foreign_open_sell_names_it():   # T26
    rig = Rig(positions=held())
    rig.client.extra_rows = [row("O-PENDING", id="GTT-7")]      # a user's resting SELL 10
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert len(rig.client.placed()) == 1 and not result.success
    text = rig.alerts.text("exit-replace-blocked:TCS")
    assert "GTT-7" in text and "O-PENDING" in text


async def test_cancelled_mid_confirmation_cancels_the_order_and_reraises():   # T27
    rig = Rig()
    task = asyncio.create_task(rig.worker.execute(buy(), sig()))
    await rig.advance_to(5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert rig.cancels(), "the resting order must be cancelled before re-raising"
    assert "order-interrupted:EQ-1" in rig.alerts.keys("CRITICAL")
    assert rig.client.row_of("EQ-1")["status"] == "CANCELLED"


async def test_garbage_after_placement_ends_unknown_instead_of_raising():   # T29
    rig = Rig()
    rig.client.get_order_error = ValueError("garbage")
    rig.client.book_error_always = TypeError("not a list")
    rig.client.default_script = lambda: Script(on_cancel=[RuntimeError("boom")] * 20)
    result = await rig.worker.execute(buy(), sig())

    assert not result.success and result.remaining_open and result.outcome == "unknown"


async def test_unexpected_error_after_placement_is_reported_open(monkeypatch):   # T29
    rig = Rig()

    async def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(rig.worker, "_confirm", broken)
    result = await rig.worker.execute(buy(), sig())

    assert not result.success and result.remaining_open and result.order_ids == ["EQ-1"]
    assert [k for k in rig.alerts.keys("CRITICAL") if "EQ-1" in k]


# ── Deadlines: the shutdown budget and the close ─────────────────────────────


async def test_arming_the_shutdown_cancels_an_entry_at_once():   # T30
    rig = Rig()
    task = asyncio.create_task(rig.worker.execute(buy(), sig()))
    await rig.advance_to(3)
    rig.deadlines.arm_shutdown(240, 12)
    result = await task

    assert rig.cancels() and rig.cancels()[0] <= 4
    assert result.outcome == "cancelled"


async def test_shutdown_budget_bounds_three_resting_exits():   # T31
    positions = held(10, "TCS", "11536") + held(10, "INFY", "1594") + held(10, "SBIN", "3045")
    rig = Rig(positions=positions)
    orders = [exit_sell(symbol="TCS", security_id="11536"),
              exit_sell(symbol="INFY", security_id="1594"),
              exit_sell(symbol="SBIN", security_id="3045")]
    tasks = [asyncio.create_task(rig.worker.execute(o, sig("SELL", 100.0, o.symbol)))
             for o in orders]
    await rig.advance_to(5)
    rig.deadlines.arm_shutdown(25, 12)                         # settle_by 30, place_by 18
    results = await asyncio.gather(*tasks)

    assert rig.clock.t <= 30
    assert all(c[4] <= 18 for c in rig.client.placed())
    assert not any(r.success for r in results)
    assert sorted(k for k in rig.alerts.keys("CRITICAL") if k.startswith("exit-not-filled")) == [
        "exit-not-filled:INFY", "exit-not-filled:SBIN", "exit-not-filled:TCS"]


async def test_nothing_is_placed_after_15_29_55():   # T32
    rig = Rig(positions=held(), wall=datetime(2026, 9, 25, 15, 30, tzinfo=IST))
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert rig.client.placed() == []
    assert not result.success and result.outcome == "not_placed"
    assert "deadline" in result.rejection_reason
    assert "exit-not-filled:TCS" in rig.alerts.keys("CRITICAL")


async def test_one_attempt_at_most_just_before_the_close():   # T32
    rig = Rig(positions=held(), wall=datetime(2026, 9, 25, 15, 29, 50, tzinfo=IST))
    result = await rig.worker.execute(exit_sell(), sig("SELL", 100.0))

    assert len(rig.client.placed()) == 1
    assert not result.success


async def test_settle_by_cuts_a_stuck_cancel_window():   # T33
    rig = Rig()
    rig.client.default_script = lambda: Script(on_cancel=[IGNORE] * 20)
    task = asyncio.create_task(rig.worker.execute(buy(), sig()))
    await rig.advance_to(2)
    rig.deadlines.arm_shutdown(5, 3)                           # settle_by 7
    result = await task

    assert "order-deadline:EQ-1" in rig.alerts.keys("CRITICAL")
    assert result.remaining_open
    assert rig.clock.t <= 8


async def test_concurrent_orders_share_one_book_read_per_poll():   # T34
    rig = Rig()
    rig.client.get_order_error = BrokerError("HTTP 404", 404, kind="http")   # status from the book
    tasks = [asyncio.create_task(rig.worker.execute(buy(), sig())) for _ in range(3)]
    await asyncio.gather(*tasks)

    reads: dict[int, int] = {}
    for call in rig.client.calls:
        if call[0] == "get_order_book" and 2 <= call[1] < 29:
            reads[int(call[1])] = reads.get(int(call[1]), 0) + 1
    assert reads and max(reads.values()) <= 2


# ── Deadlines, registry, resume ──────────────────────────────────────────────


def test_deadlines_follow_the_close_and_an_armed_shutdown():
    clock = FakeClock(datetime(2026, 9, 25, 15, 29, 0, tzinfo=IST))
    deadlines = OrderDeadlines(clock=clock.clock, wall=clock.wall)
    assert deadlines.place_by() == pytest.approx(55)            # 15:29:55
    assert deadlines.settle_by() == math.inf and not deadlines.stopping

    deadlines.arm_shutdown(30, 12)
    deadlines.arm_shutdown(100, 1)                             # the first call wins
    assert deadlines.stopping
    assert (deadlines.settle_by(), deadlines.place_by()) == (30, 18)
    assert deadlines.can_place(18) and not deadlines.can_place(18.5)


def test_registry_counts_recent_confirmed_exits_per_instrument():
    clock = FakeClock()
    registry = OrderRegistry(clock=clock.clock)
    registry.record_confirmed_exit("TCS", "11536", Decimal(6))
    registry.record_confirmed_exit("TCS-EQ", "", Decimal(2))   # same instrument, by symbol
    registry.record_confirmed_exit("INFY", "1594", Decimal(5))

    assert registry.recent_exit_qty("TCS", "11536", 600) == 8
    clock.t = 700
    assert registry.recent_exit_qty("TCS", "11536", 600) == 0


async def test_resume_cancels_a_stuck_order_and_reads_its_final_fill():
    rig = Rig()
    order = exit_sell()
    rig.client._create(order, Script(
        timeline=[(0, {"status": "PENDING", "traded_qty": 3, "traded_price": "100"})],
        on_cancel=[{"status": "PARTIALLY FILLED - CANCELLED", "traded_qty": 7,
                    "traded_price": "99"}]))
    tracked = TrackedOrder(order_id="EQ-1", side="SELL", symbol="TCS", security_id="11536",
                           segment="EQUITY", requested=Decimal(10),
                           filled_reported=Decimal(3), purpose="exit", state="stuck")
    rig.registry.track(tracked)

    idle = await rig.worker.resume(tracked, cancel=False)
    assert idle.may_be_open and rig.cancels() == []

    final = await rig.worker.resume(tracked, cancel=True)
    assert (final.filled_qty, final.avg_price, final.may_be_open) == (7, Decimal("99"), False)
    assert rig.registry.get("EQ-1").state == "final"
    assert rig.registry.get("EQ-1").filled_reported == 3       # the caller records the late fill
    assert rig.registry.unresolved() == []


def test_registry_adopts_todays_unresolved_journal_orders():
    registry = OrderRegistry(clock=FakeClock().clock)
    adopted = registry.load_journal([
        {"event": "stuck", "order_id": "EQ-9", "symbol": "TCS", "security_id": "11536",
         "side": "SELL", "qty": "10", "filled": "3", "purpose": "exit"},
        {"event": "placed", "order_id": ""},                   # no id: skipped
    ])
    assert adopted == 1
    [tracked] = registry.unresolved()
    assert (tracked.order_id, tracked.state, tracked.requested, tracked.filled_reported) == (
        "EQ-9", "stuck", 10, 3)


async def test_the_worker_never_raises_even_when_reporting_fails(monkeypatch):   # I7
    rig = Rig()

    async def broken_confirm(*args, **kwargs):
        raise RuntimeError("bug")

    def broken_finish(*args, **kwargs):
        raise ValueError("report bug")

    monkeypatch.setattr(rig.worker, "_confirm", broken_confirm)
    monkeypatch.setattr(rig.worker, "_finish", broken_finish)
    result = await rig.worker.execute(buy(), sig())

    assert not result.success and result.remaining_open
    assert result.order_ids == ["EQ-1"] and "check the broker" in result.rejection_reason


async def test_a_limit_sell_is_one_attempt_cancelled_after_the_timeout(tmp_path):
    rig = Rig(positions=held(), journal_dir=tmp_path)
    order = OrderRequest(symbol="TCS", side=Side.SELL, quantity=Decimal(10),
                         order_type=OrderType.LIMIT, price=105.0, security_id="11536")
    result = await rig.worker.execute(order, sig("SELL", 105.0))

    assert len(rig.client.placed()) == 1 and rig.cancels() == [30.0]
    assert not result.success and result.outcome == "cancelled"
    assert rig.journal.entries()[0]["purpose"] == "entry"      # due for cancelling if adopted


def test_the_worker_survives_separate_event_loops():
    """A CLI may build the router once and run orders under separate asyncio.run calls."""
    rig = Rig()
    for _ in range(2):
        rig.client.place_effects = [Script(timeline=[(0, SUCCESS_10)])]
        assert asyncio.run(rig.worker.execute(buy(), sig())).success


# ── Review fixes: uncertain placements, deadlines, interruptions ─────────────


def _still_working(rig) -> list[str]:
    return [oid for oid in rig.client.orders
            if rig.client.row_of(oid)["status"] not in ("CANCELLED", "SUCCESS",
                                                         "PARTIALLY FILLED - CANCELLED")]


async def test_time_window_reconcile_never_adopts_the_exits_own_earlier_order():
    # Attempt 1 rests and is cancelled (EQ-1). Attempt 2: the id snapshot fails and POST
    # /order times out, but EQ-2 exists and shows in the book 1 s later. Matching by time
    # alone found EQ-1 (same instrument, side and quantity), re-placed EQ-3 over EQ-2 and
    # left EQ-2 working untracked
    rig = Rig(positions=held(10))
    rig.client.place_effects = [
        Script(),
        Script(uncertain=OrderPlacementUncertain("ReadTimeout", kind="transport"),
               visible_after=1.0),
    ]
    # book reads: attempt 1's id snapshot, attempt 2's sellable re-read, its id snapshot
    rig.client.book_errors = [None, None, BrokerError("HTTP 503", 503, kind="http")]
    result = await rig.worker.execute(exit_sell(10), sig("SELL", 100.0))

    assert "EQ-2" in result.order_ids
    assert len(result.order_ids) == len(set(result.order_ids))
    assert _still_working(rig) == []                           # nothing left untracked
    assert rig.registry.get("EQ-2") is not None


async def test_time_window_reconcile_never_reports_an_earlier_buys_fill():
    rig = Rig()
    rig.client.place_effects = [
        Script(timeline=[(0, SUCCESS_10)]),
        Script(uncertain=OrderPlacementUncertain("HTTP 502", 502, kind="http"),
               visible_after=5.0),
    ]
    first = await rig.worker.execute(buy(10), sig())
    assert first.success and first.order_ids == ["EQ-1"]
    await rig.clock.sleep(40)
    rig.client.book_errors = [BrokerError("HTTP 503", 503, kind="http")]   # the id snapshot
    second = await rig.worker.execute(buy(10), sig())

    assert not second.success                                  # EQ-2 rested: not filled
    assert second.order_ids == ["EQ-2"]
    assert rig.client.row_of("EQ-2")["status"] == "CANCELLED"  # cancelled after its timeout


async def test_a_known_order_is_no_match_so_the_placement_stays_uncertain(tmp_path):
    rig = Rig(positions=held(10), journal_dir=tmp_path)
    rig.client.place_effects = [
        Script(),
        Script(uncertain=OrderPlacementUncertain("ReadTimeout", kind="transport"),
               visible_after=None),                            # never shows while we look
    ]
    rig.client.book_errors = [None, None, BrokerError("HTTP 503", 503, kind="http")]
    result = await rig.worker.execute(exit_sell(10), sig("SELL", 100.0))

    assert result.order_ids == ["EQ-1"] and result.remaining_open
    assert len(rig.client.placed()) == 2                       # not re-sent after it
    assert [k for k in rig.alerts.keys("CRITICAL") if k.startswith("placement-uncertain:")]
    assert [e["event"] for e in rig.journal.uncertain_today()] == ["uncertain"]


async def test_time_window_reconcile_needs_one_candidate_for_the_whole_window():
    # Without an id snapshot, a lone candidate is adopted only if no other identical
    # order appears within the window (someone else's order is not taken for ours)
    rig = Rig(positions=held(10))
    rig.client.book_errors = [BrokerError("HTTP 503", 503, kind="http")]
    rig.client.place_effects = [Script(
        uncertain=OrderPlacementUncertain("HTTP 500", 500, kind="http"), visible_after=1.0)]
    task = asyncio.create_task(rig.worker.execute(exit_sell(10), sig("SELL", 100.0)))
    await rig.advance_to(5)
    rig.client.extra_rows = [row("PENDING", id="APP-7", created_at=rig.clock.wall().isoformat())]
    result = await task

    assert not result.success and result.remaining_open and result.order_ids == []
    [alert] = [a for a in rig.alerts.alerts if a[1].startswith("placement-uncertain:")]
    assert set(alert[3]) == {"EQ-1", "APP-7"}


async def test_a_slow_book_read_does_not_place_after_the_close():
    clock_start = datetime(2026, 9, 25, 15, 29, 40, tzinfo=IST)
    rig = Rig(wall=clock_start)
    real_book = rig.client.get_order_book

    async def slow_book():
        await rig.clock.sleep(20)                              # a degraded network
        return await real_book()

    rig.client.get_order_book = slow_book
    result = await rig.worker.execute(buy(), sig())

    assert rig.client.placed() == []                           # would land at 15:30:00
    assert result.outcome == "not_placed" and "deadline" in result.rejection_reason


async def test_a_hung_broker_call_ends_by_settle_by():
    rig = Rig()

    async def hung_cancel(req):
        rig.client.calls.append(("cancel_order", req.order_id, rig.clock.t))
        await asyncio.Event().wait()                           # never answers

    rig.client.cancel_order = hung_cancel
    task = asyncio.create_task(rig.worker.execute(buy(), sig()))
    await rig.advance_to(5)
    rig.deadlines.arm_shutdown(0.1, 0.05)                      # a stop with almost no budget
    result = await asyncio.wait_for(task, 10)                  # real seconds: it must not hang

    assert result.remaining_open
    assert "order-deadline:EQ-1" in rig.alerts.keys("CRITICAL")


async def test_a_placement_that_times_out_at_settle_by_is_uncertain():
    rig = Rig()

    async def hung_place(order):
        rig.client.calls.append(("place_order", order.order_type.value, order.quantity,
                                 order.price, rig.clock.t))
        await asyncio.Event().wait()

    rig.client.place_order = hung_place
    rig.deadlines.arm_shutdown(0.4, 0.0)
    result = await asyncio.wait_for(rig.worker.execute(buy(), sig()), 10)

    assert result.remaining_open and result.outcome == "unknown"   # never "rejected"
    assert [k for k in rig.alerts.keys("CRITICAL") if k.startswith("placement-uncertain:")]


def _interrupted_exit_rig(tmp_path):
    # Attempt 1: 4 of 10 trade, then the attempt timeout cancels the rest (final, 4).
    # Attempt 2: the re-placed LIMIT for 6 fills as the interrupted task cancels it
    rig = Rig(positions=held(10), journal_dir=tmp_path)
    rig.client.place_effects = [
        Script(timeline=[(0, {"status": "PENDING"}),
                         (2, {"status": "PENDING", "traded_qty": 4, "traded_price": "99"})]),
        Script(on_cancel=[{"status": "SUCCESS", "traded_qty": 6, "traded_price": "98"}]),
    ]
    return rig


async def _interrupt_at(rig, order, t):
    task = asyncio.create_task(rig.worker.execute(order, sig("SELL", 100.0)))
    await asyncio.wait_for(rig.advance_to(t), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)


async def test_an_interrupted_exit_leaves_every_fill_to_be_recorded(tmp_path):
    rig = _interrupted_exit_rig(tmp_path)
    await _interrupt_at(rig, exit_sell(10), 15)

    assert len(rig.client.placed()) == 2
    # This process: both orders unresolved with nothing reported, so a resync or CLOSING
    # records their fills (4 + 6) as late fills
    unresolved = {t.order_id: t for t in rig.registry.unresolved()}
    assert set(unresolved) == {"EQ-1", "EQ-2"}
    assert all(t.filled_reported == 0 for t in unresolved.values())
    # A later process: the journal hands them over with nothing reported either
    fresh = OrderRegistry(clock=rig.clock.clock)
    assert fresh.load_journal(rig.journal.today_unresolved()) == 2
    confs = [await rig.worker.resume(t, cancel=True) for t in fresh.unresolved()]
    assert sorted(c.filled_qty - 0 for c in confs) == [4, 6]
    text = rig.alerts.text("order-interrupted:")
    assert "EQ-1" in text and "EQ-2" in text and "10" in text


async def test_an_exit_interrupted_between_attempts_after_a_sale_is_alerted(tmp_path):
    rig = Rig(positions=held(10), journal_dir=tmp_path)
    rig.client.place_effects = [Script(timeline=[(0, {"status": "PARTIALLY FILLED",
                                                      "traded_qty": 4,
                                                      "traded_price": "100"})])]
    real_positions = rig.client.get_positions
    box = {}

    async def cancel_during_the_reread():
        box["task"].cancel()
        await asyncio.sleep(0)
        return await real_positions()

    rig.client.get_positions = cancel_during_the_reread       # attempt 2's sellable re-read
    box["task"] = asyncio.ensure_future(rig.worker.execute(exit_sell(10), sig("SELL", 100.0)))
    with pytest.raises(asyncio.CancelledError):
        await box["task"]

    [alert] = [a for a in rig.alerts.alerts if a[1].startswith("order-interrupted:")]
    assert alert[0] == "CRITICAL" and "EQ-1" in alert[3] and "4" in alert[2]
    [tracked] = rig.registry.unresolved()
    assert (tracked.order_id, tracked.filled_reported) == ("EQ-1", 0)
    assert "EQ-1" in {e["order_id"] for e in rig.journal.today_unresolved()}


async def test_a_placement_interrupted_while_reconciling_stays_adoptable(tmp_path):
    rig = Rig(positions=held(10), journal_dir=tmp_path)
    rig.client.place_effects = [Script(
        uncertain=OrderPlacementUncertain("HTTP 504", 504, kind="http"), visible_after=None)]
    task = asyncio.create_task(rig.worker.execute(exit_sell(10), sig("SELL", 100.0)))
    await asyncio.wait_for(rig.advance_to(3), 5)               # 3 s into the reconcile window
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [record] = rig.journal.uncertain_today()
    assert (record["security_id"], record["side"], record["qty"], record["purpose"],
            record["segment"]) == ("11536", "SELL", "10", "exit", "EQUITY")
    assert [k for k in rig.alerts.keys("CRITICAL") if k.startswith("order-interrupted:")]


def test_journal_adoption_keeps_the_reported_average_price():
    registry = OrderRegistry(clock=FakeClock().clock)
    registry.load_journal([{"event": "stuck", "order_id": "EQ-9", "symbol": "TCS",
                            "security_id": "11536", "side": "SELL", "qty": "10",
                            "filled": "4", "avg_price": "95", "purpose": "exit"}])
    [tracked] = registry.unresolved()
    assert (tracked.filled_reported, tracked.avg_price_reported) == (4, Decimal("95"))


def test_journal_adoption_prefers_what_was_reported_over_what_filled():
    registry = OrderRegistry(clock=FakeClock().clock)
    registry.load_journal([{"event": "interrupted", "order_id": "EQ-9", "symbol": "TCS",
                            "security_id": "11536", "side": "SELL", "qty": "10",
                            "filled": "6", "avg_price": "98", "reported": "0",
                            "purpose": "exit"}])
    [tracked] = registry.unresolved()
    assert (tracked.filled_reported, tracked.avg_price_reported) == (0, None)


# ── A "partially filled" final order that reports nothing traded ─────────────


async def test_an_entry_cancelled_as_partially_filled_with_zero_traded_is_unconfirmed():
    rig = Rig()
    rig.client.place_effects = [Script(on_cancel=[{
        "status": "PARTIALLY FILLED - CANCELLED", "traded_qty": 0}])]
    result = await rig.worker.execute(buy(), sig())

    assert not result.success
    assert (result.outcome, result.fill_unconfirmed) == ("unknown", True)
    assert "fill-qty-unknown:EQ-1" in rig.alerts.keys()


async def test_an_exit_cancelled_as_partially_filled_with_zero_traded_is_not_replaced():
    rig = Rig(positions=held(10))
    rig.client.place_effects = [Script(on_cancel=[{
        "status": "PARTIALLY FILLED - CANCELLED", "traded_qty": 0}])]
    result = await rig.worker.execute(exit_sell(10), sig("SELL", 100.0))

    assert len(rig.client.placed()) == 1              # never re-placed over an unknown fill
    assert result.fill_unconfirmed is True
    assert "fill-qty-unknown:EQ-1" in rig.alerts.keys("CRITICAL")
    # With trades showing 4 sold, the fill is known (4); the book row still says the order
    # may have sold all 10, so the rest is not re-placed blind (alerted instead)
    rig = Rig(positions=held(10))
    rig.client.place_effects = [
        Script(on_cancel=[{"status": "PARTIALLY FILLED - CANCELLED", "traded_qty": 0}],
               trades=[{"quantity": 4, "price": 100}]),
        Script(timeline=[(0, {"status": "SUCCESS", "traded_qty": 6, "traded_price": "99"})]),
    ]
    result = await rig.worker.execute(exit_sell(10), sig("SELL", 100.0))
    assert (result.filled_quantity, len(rig.client.placed())) == (4, 1)
    assert "exit-replace-blocked:TCS" in rig.alerts.keys("CRITICAL")
