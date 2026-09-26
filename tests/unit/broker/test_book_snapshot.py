"""read_broker_snapshot (skopaq/broker/book_snapshot.py): the order book is read before
positions, and positions before holdings; how each read's failure is reported."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from skopaq.broker.book_snapshot import BrokerSnapshot, read_broker_snapshot
from skopaq.broker.client import BrokerError
from skopaq.broker.models import Holding, Position
from skopaq.broker.order_status import OrderState
from skopaq.risk.calendar import IST

WALL = datetime(2026, 9, 25, 15, 20, tzinfo=IST)

BOOK = [
    {"id": "EQ-1", "status": "PENDING", "txn_type": "SELL", "security_id": "2885",
     "requested_qty": 5, "traded_qty": 0, "product": "CNC"},
    {"status": "PENDING"},                                    # no id: dropped
]


class FakeClient:
    """Scripted reads that record the order they were made in."""

    def __init__(self, book_answers=None, positions=None, holdings=None):
        # Each book read takes the next answer (an exception is raised); the last repeats.
        self.calls: list[str] = []
        self._book = list(book_answers) if book_answers is not None else [BOOK]
        self._positions = positions
        self._holdings = holdings

    async def get_order_book(self):
        self.calls.append("book")
        answer = self._book.pop(0) if len(self._book) > 1 else self._book[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    async def get_positions(self):
        self.calls.append("positions")
        if isinstance(self._positions, Exception):
            raise self._positions
        return self._positions or [Position(symbol="RELIANCE", quantity=Decimal("5"),
                                            security_id="2885")]

    async def get_holdings(self):
        self.calls.append("holdings")
        if isinstance(self._holdings, Exception):
            raise self._holdings
        return self._holdings or [Holding(symbol="RELIANCE", quantity=Decimal("10"))]


class Sleeps:
    def __init__(self):
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.mark.asyncio
async def test_reads_book_then_positions_then_holdings():
    client = FakeClient()
    snap = await read_broker_snapshot(client, wall=lambda: WALL)
    assert client.calls == ["book", "positions", "holdings"]
    assert isinstance(snap, BrokerSnapshot)
    assert [o.order_id for o in snap.orders] == ["EQ-1"]
    assert snap.orders[0].state is OrderState.WORKING
    assert snap.positions[0].symbol == "RELIANCE"
    assert snap.holdings[0].quantity == Decimal("10")
    assert isinstance(snap.orders, tuple)
    assert isinstance(snap.positions, tuple)
    assert isinstance(snap.holdings, tuple)
    assert snap.read_at == WALL
    assert (snap.book_error, snap.holdings_error) == ("", "")


@pytest.mark.asyncio
async def test_book_error_is_retried_once():
    client = FakeClient(book_answers=[BrokerError("boom", kind="transport"), BOOK])
    sleeps = Sleeps()
    snap = await read_broker_snapshot(client, sleep=sleeps, wall=lambda: WALL)
    assert client.calls == ["book", "book", "positions", "holdings"]
    assert sleeps.calls == [0.5]
    assert snap.book_error == ""
    assert [o.order_id for o in snap.orders] == ["EQ-1"]


@pytest.mark.asyncio
async def test_book_error_twice_is_reported():
    client = FakeClient(book_answers=[BrokerError("status failure", kind="error_body")])
    sleeps = Sleeps()
    snap = await read_broker_snapshot(client, sleep=sleeps, wall=lambda: WALL)
    assert client.calls == ["book", "book", "positions", "holdings"]
    assert sleeps.calls == [0.5]
    assert "status failure" in snap.book_error
    assert snap.orders == ()
    assert len(snap.positions) == 1                  # positions are still read, after the book


@pytest.mark.asyncio
async def test_unexpected_book_payload_is_a_book_error():
    client = FakeClient(book_answers=[{"orders": "?"}])       # not a list of rows
    snap = await read_broker_snapshot(client, sleep=Sleeps(), wall=lambda: WALL)
    assert snap.book_error
    assert snap.orders == ()


@pytest.mark.asyncio
async def test_positions_error_raises():
    client = FakeClient(positions=BrokerError("positions down", status_code=503, kind="http"))
    with pytest.raises(BrokerError, match="positions down"):
        await read_broker_snapshot(client, wall=lambda: WALL)
    assert client.calls == ["book", "positions"]


@pytest.mark.asyncio
async def test_holdings_error_is_reported_and_empty():
    client = FakeClient(holdings=BrokerError("holdings down", kind="transport"))
    snap = await read_broker_snapshot(client, wall=lambda: WALL)
    assert snap.holdings == ()
    assert "holdings down" in snap.holdings_error
    assert snap.book_error == ""


@pytest.mark.asyncio
async def test_holdings_can_be_skipped():
    client = FakeClient()
    snap = await read_broker_snapshot(client, need_holdings=False, wall=lambda: WALL)
    assert client.calls == ["book", "positions"]
    assert snap.holdings == ()
    assert snap.holdings_error == ""


@pytest.mark.asyncio
async def test_extra_terminal_statuses_are_passed_on():
    client = FakeClient(book_answers=[[{"id": "EQ-2", "status": "DONE-ISH", "requested_qty": 5,
                                        "traded_qty": 0}]])
    snap = await read_broker_snapshot(client, extra_terminal=frozenset({"DONE-ISH"}),
                                      wall=lambda: WALL)
    assert snap.orders[0].state is OrderState.CANCELLED
