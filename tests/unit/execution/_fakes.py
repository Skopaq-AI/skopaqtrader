"""Test doubles for the live order worker: virtual time, a scripted broker, an alert spy.

Nothing here touches the network or the real clock: a 30-second fill timeout runs in
milliseconds, and every broker call is recorded in order.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Callable, Optional

from skopaq.broker.client import BrokerError
from skopaq.broker.models import CancelOrderRequest, Funds, OrderRequest, OrderResponse
from skopaq.risk.calendar import IST

# A Friday morning, well inside market hours
BASE_WALL = datetime(2026, 9, 25, 11, 0, tzinfo=IST)

_FINAL = {"SUCCESS", "CANCELLED", "EXPIRED", "FAILED", "ABORTED", "REJECTED",
          "PARTIALLY FILLED - CANCELLED", "PARTIALLY FILLED - EXPIRED"}


class FakeClock:
    """Virtual time: ``clock()``, ``sleep()`` and an IST wall clock.

    ``sleep(s)`` parks the caller until virtual time reaches now + s. Time moves only
    when every sleeper is parked (the earliest one advances it), so concurrent tasks see
    one consistent clock.
    """

    def __init__(self, wall_start: datetime = BASE_WALL) -> None:
        self.t = 0.0
        self._wall0 = wall_start
        self._wakes: list[float] = []

    def clock(self) -> float:
        return self.t

    def wall(self) -> datetime:
        return self._wall0 + timedelta(seconds=self.t)

    async def sleep(self, seconds: float) -> None:
        wake = self.t + max(float(seconds), 0.0)
        self._wakes.append(wake)
        try:
            while True:
                await asyncio.sleep(0)
                if self.t >= wake:
                    return
                if wake <= min(self._wakes):
                    await asyncio.sleep(0)     # let tasks woken at this instant park again
                    if wake <= min(self._wakes):
                        self.t = max(self.t, wake)
                        return
        finally:
            self._wakes.remove(wake)


def row(status: str, traded: Optional[int] = 0, requested: int = 10, price: str = "",
        **extra) -> dict:
    """An order-book / GET /order row in the documented INDstocks shape."""
    r = {
        "id": "EQ-1", "status": status, "txn_type": "SELL", "security_id": "11536",
        "requested_qty": requested, "traded_price": price, "product": "CNC",
        "segment": "EQUITY", "order_type": "MARKET", "validity": "DAY", "extra_info": "",
        "exch_order_id": "",
    }
    if traded is not None:
        r["traded_qty"] = traded
    r.update(extra)
    return r


IGNORE = "ignore"   # on_cancel: the broker acknowledges the cancel, the order keeps working


@dataclass
class Script:
    """What one placed order does.

    timeline: (seconds after placing, row fields) — the latest entry reached applies; a
    field set to None is left out of the row. on_cancel: one reaction per cancel call (an
    exception to raise, row fields from then on, or IGNORE); after that a cancel makes the
    order CANCELLED (PARTIALLY FILLED - CANCELLED if something traded).
    """

    timeline: list = field(default_factory=lambda: [(0.0, {"status": "PENDING"})])
    on_cancel: list = field(default_factory=list)
    trades: object = None                  # list of fills, an exception, or None ([])
    visible_after: Optional[float] = 0.0   # the book shows it this long after placing; None: never
    in_get_order: bool = True              # GET /order finds it
    uncertain: Optional[Exception] = None  # place_order raises this although the order exists


@dataclass
class _Order:
    order_id: str
    request: OrderRequest
    script: Script
    placed_at: float
    created_wall: datetime
    override: Optional[dict] = None
    override_at: float = 0.0


class FakeClient:
    """A scripted stand-in for INDstocksClient that records every call."""

    def __init__(self, clock: FakeClock, *, positions=None, holdings=None,
                 ltp: float = 100.0) -> None:
        self.clock = clock
        self.calls: list[tuple] = []
        # One entry per place_order call: an exception (nothing placed), a Script, or a list
        # of Scripts (several orders appear, e.g. an ambiguous uncertain placement)
        self.place_effects: list = []
        self.default_script: Callable[[], Script] = Script
        self.orders: dict[str, _Order] = {}
        self.extra_rows: list[dict] = []
        self.book_errors: list = []            # one per get_order_book call: exception or None
        self.book_error_always: Optional[Exception] = None
        self.get_order_error: Optional[Exception] = None
        self.positions = [] if positions is None else positions
        self.holdings = [] if holdings is None else holdings
        self.ltp = ltp
        self.trade_book: list = []
        self.funds = Funds(available_cash=500_000, available_margin=500_000,
                           total_collateral=500_000)
        self.broker_skew_s = 0.0               # broker created_at = host wall + skew
        self._n = 0

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def placed(self) -> list[tuple]:
        return [c for c in self.calls if c[0] == "place_order"]

    def _create(self, order: OrderRequest, script: Script) -> str:
        self._n += 1
        order_id = f"EQ-{self._n}"
        created = self.clock.wall() + timedelta(seconds=self.broker_skew_s)
        self.orders[order_id] = _Order(order_id, order, script, self.clock.t, created)
        return order_id

    def row_of(self, order_id: str) -> dict:
        o = self.orders[order_id]
        elapsed = self.clock.t - o.placed_at
        fields: dict = {}
        for offset, f in o.script.timeline:
            if offset <= elapsed:
                fields = f
        if o.override is not None:
            fields = o.override
        now = self.clock.wall() + timedelta(seconds=self.broker_skew_s)
        base = row(
            "PENDING", id=order_id, txn_type=o.request.side.value,
            security_id=o.request.security_id, requested_qty=int(o.request.quantity),
            order_type=o.request.order_type.value, created_at=o.created_wall.isoformat(),
            updated_at=now.isoformat(),
        )
        base.update(fields)
        return {k: v for k, v in base.items() if v is not None}

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        self.calls.append(("place_order", order.order_type.value, order.quantity, order.price,
                           self.clock.t))
        effect = self.place_effects.pop(0) if self.place_effects else self.default_script()
        if isinstance(effect, BaseException):
            raise effect
        scripts = effect if isinstance(effect, list) else [effect]
        ids = [self._create(order, s) for s in scripts]
        if scripts[0].uncertain is not None:
            raise scripts[0].uncertain
        return OrderResponse(order_id=ids[0], status="INITIATED")

    async def get_order(self, order_id: str, segment: str = "EQUITY") -> dict:
        self.calls.append(("get_order", order_id))
        if self.get_order_error is not None:
            raise self.get_order_error
        o = self.orders.get(order_id)
        if o is None or not o.script.in_get_order:
            return {}
        return self.row_of(order_id)

    async def get_order_book(self) -> list:
        self.calls.append(("get_order_book", self.clock.t))
        if self.book_errors:
            error = self.book_errors.pop(0)
            if error is not None:
                raise error
        if self.book_error_always is not None:
            raise self.book_error_always
        rows = []
        for o in self.orders.values():
            visible = o.script.visible_after
            if visible is not None and self.clock.t - o.placed_at >= visible:
                rows.append(self.row_of(o.order_id))
        return rows + list(self.extra_rows)

    async def cancel_order(self, req: CancelOrderRequest) -> OrderResponse:
        self.calls.append(("cancel_order", req.order_id, self.clock.t))
        o = self.orders.get(req.order_id)
        if o is None:
            raise BrokerError("Position could not be found.", 400, kind="http")
        if o.script.on_cancel:
            effect = o.script.on_cancel.pop(0)
            if isinstance(effect, BaseException):
                raise effect
            if effect == IGNORE:
                return OrderResponse(order_id=req.order_id, status="")
            o.override, o.override_at = dict(effect), self.clock.t
            return OrderResponse(order_id=req.order_id, status="")
        current = self.row_of(req.order_id)
        if str(current.get("status", "")).upper() in _FINAL:
            raise BrokerError("Position could not be found.", 400, kind="http")
        traded = current.get("traded_qty") or 0
        o.override = {
            "status": "PARTIALLY FILLED - CANCELLED" if traded else "CANCELLED",
            "traded_qty": traded, "traded_price": current.get("traded_price", ""),
        }
        o.override_at = self.clock.t
        return OrderResponse(order_id=req.order_id, status="CANCELLED")

    async def get_trades(self, order_id: str, segment: str = "EQUITY") -> list:
        self.calls.append(("get_trades", order_id))
        trades = self.orders[order_id].script.trades
        if isinstance(trades, BaseException):
            raise trades
        return list(trades or [])

    async def get_trade_book(self, segment: str = "EQUITY") -> list:
        self.calls.append(("get_trade_book",))
        return list(self.trade_book)

    async def get_positions(self) -> list:
        self.calls.append(("get_positions",))
        if isinstance(self.positions, BaseException):
            raise self.positions
        return list(self.positions)

    async def get_holdings(self) -> list:
        self.calls.append(("get_holdings",))
        if isinstance(self.holdings, BaseException):
            raise self.holdings
        return list(self.holdings)

    async def get_funds(self) -> Funds:
        self.calls.append(("get_funds",))
        return self.funds

    async def get_ltp(self, scrip_code: str) -> float:
        self.calls.append(("get_ltp", scrip_code))
        if isinstance(self.ltp, BaseException):
            raise self.ltp
        return self.ltp


# Security ids of the symbols the position tests trade (scrip code: NSE_<id>)
SIDS = {"TCS": "11536", "INFY": "1594", "RELIANCE": "2885"}


class PositionBroker(FakeClient):
    """A FakeClient whose positions follow the fills of its orders (unless ``lag``).

    held: symbol -> (quantity, average price) before today's orders. ``by_symbol`` queues
    a Script per symbol (used before ``place_effects``), so concurrent exits of different
    symbols get their own scripts whatever order they reach the broker in. ``ltps`` maps
    a scrip code (``NSE_<id>``) to its LTP.
    """

    def __init__(self, clock: FakeClock, held: dict, *, ltps: Optional[dict] = None,
                 lag: bool = False) -> None:
        super().__init__(clock)
        self.held = dict(held)
        self.ltps = dict(ltps or {})
        self.lag = lag
        self.by_symbol: dict[str, list] = {}

    def traded(self, symbol: str, side: str) -> int:
        """Shares of ``symbol`` today's orders on ``side`` have filled."""
        return sum(
            int(self.row_of(order_id).get("traded_qty") or 0)
            for order_id, o in self.orders.items()
            if o.request.security_id == SIDS[symbol] and o.request.side.value == side
        )

    def placed_orders(self, symbol: Optional[str] = None) -> list:
        return [o for o in self.orders.values()
                if symbol is None or o.request.symbol == symbol]

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        queue = self.by_symbol.get(order.symbol)
        if queue:
            self.place_effects.insert(0, queue.pop(0))
        return await super().place_order(order)

    async def get_positions(self) -> list:
        from skopaq.broker.models import Position

        self.calls.append(("get_positions",))
        if isinstance(self.positions, BaseException):
            raise self.positions
        rows = []
        for symbol, (quantity, average) in self.held.items():
            bought = sold = 0
            if not self.lag:
                bought, sold = self.traded(symbol, "BUY"), self.traded(symbol, "SELL")
            rows.append(Position(
                symbol=symbol, security_id=SIDS[symbol], product="CNC",
                quantity=Decimal(quantity + bought - sold), average_price=average,
                buy_quantity=Decimal(bought), sell_quantity=Decimal(sold),
            ))
        return rows

    async def get_ltp(self, scrip_code: str) -> float:
        self.calls.append(("get_ltp", scrip_code))
        return self.ltps.get(scrip_code, self.ltp)


class AlertSpy:
    """Records OrderAlerter.alert calls (no dedup, no sending)."""

    def __init__(self) -> None:
        self.alerts: list[tuple] = []

    def alert(self, severity: str, key: str, text: str, *, order_ids=(),
              dedup_s: float = 0.0) -> None:
        self.alerts.append((severity, key, text, tuple(order_ids)))

    def keys(self, severity: Optional[str] = None) -> list[str]:
        return [a[1] for a in self.alerts if severity is None or a[0] == severity]

    def text(self, key_prefix: str) -> str:
        return "\n".join(a[2] for a in self.alerts if a[1].startswith(key_prefix))


def qty(n) -> Decimal:
    return Decimal(str(n))
