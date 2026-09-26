"""Executor, live SELLs: the symbol's lock, then the order book, positions and holdings,
then the safety check, then the broker — and only broker-confirmed fills downstream.

A live router over a scripted INDstocks client (FakeClient) and virtual time (FakeClock):
no network, no real sleeping. Paper stays as it was (test_executor_short_sale.py).
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from skopaq.broker.client import BrokerError
from skopaq.broker.models import OrderType, Position, Quote, TradingSignal
from skopaq.broker.paper_engine import PaperEngine
from skopaq.constants import SafetyRules
from skopaq.execution import order_alerts
from skopaq.execution.executor import Executor
from skopaq.execution.order_router import OrderRouter
from skopaq.execution.safety_checker import SafetyChecker
from skopaq.execution.sell_lock import SellLock
from tests.unit.execution._fakes import IGNORE, AlertSpy, FakeClient, FakeClock, Script, row

RULES = SafetyRules(market_hours_only=False, require_stop_loss=False)
FILLED_5 = {"status": "SUCCESS", "traded_qty": 5, "traded_price": "95"}


class _RecordingLock:
    """Wraps the router's SellLock and records when it is taken and released."""

    def __init__(self, lock, calls):
        self._lock, self._calls = lock, calls

    async def __aenter__(self):
        await self._lock.__aenter__()
        self._calls.append(("lock",))
        return self

    async def __aexit__(self, *exc):
        self._calls.append(("unlock",))
        return await self._lock.__aexit__(*exc)


class Live:
    """A live Executor over FakeClient, with the lock and the safety check recorded."""

    def __init__(self, positions=(), *, record=True):
        self.clock = FakeClock()
        self.client = FakeClient(self.clock, positions=list(positions))
        self.router = OrderRouter(MagicMock(trading_mode="live"), PaperEngine(),
                                  live_client=self.client, sleep=self.clock.sleep,
                                  clock=self.clock.clock, wall=self.clock.wall)
        self.safety = SafetyChecker(rules=RULES)
        self.executor = Executor(self.router, self.safety, clock=self.clock.clock)
        if record:
            real_lock, real_validate = self.router.sell_lock, self.safety.validate

            def sell_lock(order):
                lock = real_lock(order)
                return None if lock is None else _RecordingLock(lock, self.client.calls)

            def validate(*args, **kwargs):
                self.client.calls.append(("validate",))
                return real_validate(*args, **kwargs)

            self.router.sell_lock = sell_lock
            self.safety.validate = validate

    async def run(self, signal):
        with patch("skopaq.execution.order_router.resolve_security_id",
                   new=AsyncMock(return_value="11536")):
            result = await self.executor.execute_signal(signal)
        for _ in range(3):
            await asyncio.sleep(0)       # a live trade notification is sent in the background
        return result


def _held(qty=5, avg=100.0):
    return Position(symbol="TCS", security_id="11536", quantity=Decimal(qty),
                    average_price=avg, product="CNC")


def _exit(qty=5, ltp=94.0):
    """A protective exit, as the monitor and CLOSING build it."""
    return TradingSignal(symbol="TCS", action="SELL", confidence=80, entry_price=ltp,
                         order_type=OrderType.MARKET, quantity=Decimal(qty),
                         reasoning="HARD STOP")


def _limit_sell(qty=5, price=95.0):
    return TradingSignal(symbol="TCS", action="SELL", confidence=80, entry_price=price,
                         quantity=Decimal(qty))


@pytest.fixture
def notify():
    with patch("skopaq.notifications.notify_trade_event", new_callable=AsyncMock) as mock:
        yield mock


@pytest.fixture
def alerts(monkeypatch):
    spy = AlertSpy()
    monkeypatch.setattr(order_alerts, "_alerter", spy)
    return spy


async def test_a_live_sell_locks_then_reads_book_positions_holdings(notify, alerts):   # T16
    live = Live([_held(5)])
    live.client.place_effects = [Script(timeline=[(0, FILLED_5)])]

    result = await live.run(_exit(5))

    names = live.client.names()
    assert names[:6] == ["lock", "get_order_book", "get_positions", "get_holdings",
                         "get_funds", "validate"]
    assert names.index("place_order") > names.index("validate")
    assert names[-1] == "unlock"                 # held until the broker's answer is final
    assert result.success and result.filled_quantity == 5


async def test_an_unreadable_book_refuses_the_sell(notify, alerts):   # T17
    live = Live([_held(5)])
    live.client.book_error_always = BrokerError("HTTP 503: upstream unavailable", 503,
                                                kind="http")

    result = await live.run(_exit(5))

    assert not result.success and not result.safety_passed
    assert "Cannot read the broker's order book" in result.rejection_reason
    assert "place_order" not in live.client.names()
    [rejected] = notify.await_args_list
    assert rejected.args[4] == "REJECTED"
    assert "Cannot read the broker's order book" in rejected.kwargs["reason"]
    assert alerts.keys("CRITICAL") == ["sell-refused:TCS:book-unreadable"]


async def test_a_buy_never_reads_sell_inputs_or_locks(notify, alerts):   # T18
    live = Live(record=False)
    live.client.place_effects = [Script(timeline=[(0, {"status": "SUCCESS", "traded_qty": 1,
                                                       "traded_price": "100"})])]
    live.router.sell_inputs = AsyncMock()
    live.router.sell_lock = MagicMock()

    result = await live.run(TradingSignal(symbol="TCS", action="BUY", confidence=80,
                                          entry_price=100.0, quantity=Decimal(1)))

    assert result.success, result.rejection_reason
    live.router.sell_inputs.assert_not_called()
    live.router.sell_lock.assert_not_called()


async def test_a_partial_live_sell_counts_only_the_filled_shares(notify, alerts):   # T19
    live = Live([_held(5, avg=100.0)])
    live.client.place_effects = [Script(timeline=[(0, {
        "status": "PARTIALLY FILLED", "traded_qty": 3, "traded_price": "95"})])]

    result = await live.run(_limit_sell(5, 95.0))   # a LIMIT SELL: one order, rest cancelled

    assert result.success and result.filled_quantity == 3 and result.outcome == "partial"
    assert live.safety._day_pnl == pytest.approx((95 - 100) * 3)
    [sent] = notify.await_args_list
    assert sent.args[:5] == ("SELL", "TCS", 95.0, 3, "PARTIAL")
    assert sent.kwargs == {"pnl": 0, "order_id": "EQ-1",
                           "reason": "filled 3 of 5; rest cancelled"}


async def test_an_order_that_may_still_be_working_is_unconfirmed(notify, alerts):   # T20
    live = Live([_held(5)])
    live.client.place_effects = [Script(on_cancel=[IGNORE] * 50)]   # never cancels

    result = await live.run(_limit_sell(5, 95.0))

    assert not result.success and result.remaining_open
    [sent] = notify.await_args_list
    assert sent.args[:5] == ("SELL", "TCS", 95.0, 5, "UNCONFIRMED")   # the ordered quantity
    assert sent.kwargs["order_id"] == "EQ-1"
    assert "may still be working at the broker" in sent.kwargs["reason"]
    assert live.safety._day_pnl == 0


async def test_another_seller_holding_the_lock_refuses_the_sell(notify, alerts):   # T21
    live = Live([_held(5)])
    holder = SellLock("TCS", wait_s=0)            # same lock dir (SKOPAQ_ORDER_LOCK_DIR)

    async with holder:
        result = await live.run(_exit(5))

    assert not result.success
    assert result.rejection_reason == (
        "Another Skopaq process is already selling TCS — not sending a second SELL")
    assert "get_order_book" not in live.client.names()
    assert "place_order" not in live.client.names()
    assert alerts.keys() == ["sell-refused:TCS:lock-busy"]
    assert alerts.alerts[0][0] == "CRITICAL"      # a protective exit
    assert notify.await_args.args[4] == "REJECTED"
    # It waited (virtual time) for as long as one worst-case exit could take, plus 10 s
    assert live.clock.t == pytest.approx(live.router.worker.settings.exit_worst_case_s + 10,
                                         abs=0.3)


async def test_an_open_sell_blocking_a_limit_sell_is_a_warning_naming_it(notify, alerts):
    live = Live([_held(5)])
    live.client.extra_rows = [row("O-PENDING", traded=0, requested=5, id="GTT-9")]

    result = await live.run(_limit_sell(5, 95.0))

    assert not result.success
    assert "GTT-9 O-PENDING" in result.rejection_reason
    [(severity, key, text, order_ids)] = alerts.alerts
    assert (severity, key, order_ids) == ("WARNING", "sell-refused:TCS:open-sell", ("GTT-9",))
    assert "GTT-9 O-PENDING" in text


async def test_a_refused_live_sell_is_notified_once_per_ten_minutes(notify, alerts):
    live = Live([_held(5)])
    live.client.book_error_always = BrokerError("HTTP 503", 503, kind="http")

    for _ in range(3):                             # the monitor retries every 10 s
        await live.run(_exit(5))
        await live.clock.sleep(10)
    assert len(notify.await_args_list) == 1

    await live.clock.sleep(600)
    await live.run(_exit(5))
    assert len(notify.await_args_list) == 2


async def test_the_refusal_alert_is_deduplicated_by_the_process_alerter(notify, caplog):
    live = Live([_held(5)])
    live.client.book_error_always = BrokerError("HTTP 503", 503, kind="http")

    with caplog.at_level(logging.ERROR, logger="skopaq.execution.order_alerts"), \
         patch("skopaq.notifications.notify_order_alert", new_callable=AsyncMock) as send:
        for _ in range(3):
            await live.run(_exit(5))
        await order_alerts.get_alerter().drain(1)
    sent = [r for r in caplog.records if "sell-refused:TCS:book-unreadable" in r.getMessage()]
    assert len(sent) == 1
    assert send.await_count == 1


async def test_a_filled_live_sell_notifies_the_broker_fill(notify, alerts):
    live = Live([_held(5, avg=100.0)])
    live.client.place_effects = [Script(timeline=[(0, FILLED_5)])]

    result = await live.run(_exit(5))

    assert result.success
    [sent] = notify.await_args_list
    assert sent.args[:5] == ("SELL", "TCS", 95.0, 5, "FILLED")
    assert sent.kwargs["order_id"] == "EQ-1"
    assert live.safety._day_pnl == pytest.approx(-25.0)
    assert alerts.alerts == []


# ── Paper: unchanged ─────────────────────────────────────────────────────────


def _paper():
    paper = PaperEngine(initial_capital=1_000_000)
    paper.update_quote(Quote(symbol="RELIANCE", ltp=2500.0, close=2500.0))
    router = OrderRouter(MagicMock(trading_mode="paper"), paper)
    return Executor(router, SafetyChecker(rules=RULES)), router


async def test_paper_rejections_are_notified_every_time_as_before(notify, alerts):   # T22
    executor, router = _paper()
    sell = TradingSignal(symbol="RELIANCE", action="SELL", confidence=80, entry_price=2500.0,
                         quantity=Decimal(1))

    for _ in range(2):
        result = await executor.execute_signal(sell)
        assert "No short sales" in result.rejection_reason

    assert notify.await_args_list == [call("SELL", "RELIANCE", 2500.0, 1, "REJECTED")] * 2
    assert alerts.alerts == []
    assert router.sell_lock(executor._build_order(sell)) is None


async def test_paper_fill_notifications_are_unchanged(notify, alerts):   # T22
    executor, _ = _paper()
    buy = TradingSignal(symbol="RELIANCE", action="BUY", confidence=80, entry_price=2500.0,
                        quantity=Decimal(1))

    result = await executor.execute_signal(buy)

    assert result.success
    [sent] = notify.await_args_list
    assert sent == call("BUY", "RELIANCE", result.fill_price, 1, "FILLED", pnl=0,
                        order_id=result.order.order_id)


async def test_an_uncertain_sell_is_not_sold_again_while_the_book_lags(notify, alerts):
    # POST /order timed out but the order exists; the book shows it only after 20 s,
    # after the worker's 15 s reconcile window. A second SELL of the same shares right
    # after must count it (it may be working), not sell them again
    from skopaq.broker.client import OrderPlacementUncertain

    live = Live([_held(10)], record=False)
    live.client.place_effects = [Script(
        timeline=[(0, {"status": "PENDING"}),
                  (25, {"status": "SUCCESS", "traded_qty": 10, "traded_price": "94"})],
        uncertain=OrderPlacementUncertain("ReadTimeout", kind="transport"),
        visible_after=20.0)]
    first = await live.run(_exit(10))
    assert first.remaining_open and not first.success
    second = await live.run(_exit(10))

    assert len(live.client.placed()) == 1
    assert not second.success and "placement is uncertain" in second.rejection_reason
    assert "sell-refused:TCS:open-sell" in alerts.keys("CRITICAL")
    assert "counts as an open SELL" in alerts.text("placement-uncertain:")


async def test_unreadable_holdings_refuse_a_sell_as_unreadable_not_as_holding_nothing(
        notify, alerts):
    live = Live([], record=False)                     # 10 held as holdings, no positions
    live.client.holdings = BrokerError("API error 503: ServiceUnavailableException", 503,
                                       kind="http")

    result = await live.run(_exit(10))

    assert not result.success and live.client.placed() == []
    assert "Cannot read the broker's holdings" in result.rejection_reason
    assert "only 0 held" not in result.rejection_reason
    assert "sell-refused:TCS:holdings-unreadable" in alerts.keys("CRITICAL")


async def test_unreadable_holdings_do_not_matter_when_positions_cover_the_sell(notify, alerts):
    live = Live([_held(5)], record=False)
    live.client.holdings = BrokerError("API error 503", 503, kind="http")
    live.client.place_effects = [Script(timeline=[(0, FILLED_5)])]

    result = await live.run(_exit(5))

    assert result.success and result.filled_quantity == 5
