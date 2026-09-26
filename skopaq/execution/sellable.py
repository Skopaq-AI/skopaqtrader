"""How many shares a SELL may still sell, given the broker's open orders (live only).

A pure calculation with no I/O. Callers read the order book, positions and holdings
through ``read_broker_snapshot`` (the book first) and pass them in:

    sellable = holdings + CNC positions
               − the open remainder of SELL orders
               − filled SELLs that positions do not show yet
               − Skopaq SELL placements whose outcome is unknown (for the lag window)
               − Skopaq SELL orders still unresolved that the book does not list yet
                 (for the lag window, or as long as a read finds them working)

The last three terms cover the broker lagging behind a SELL: positions that do not show a
fill yet, and a book that does not show an order yet; without them the same shares could
be sold twice. An uncertain placement keeps counting even when a similar order shows in
the book: without a ``remarks`` tag nothing tells it from someone else's SELL.

A protective exit of the day's position (``position_sellable``) is sized by that position
less Skopaq's own open, unconfirmed and not-yet-shown SELLs, then capped by ``sellable``:
older delivery holdings of the same stock never absorb a pending exit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, Sequence

from skopaq.broker.order_status import OrderSnapshot, OrderState, is_non_cnc_product
from skopaq.execution.safety_checker import _base_symbol
from skopaq.risk.calendar import IST

_ZERO = Decimal("0")
_OPEN = frozenset({OrderState.WORKING, OrderState.UNRECOGNISED})
_FILLED = frozenset({OrderState.FILLED, OrderState.PARTIAL_DONE})
SKEW_ALLOWANCE_S = 120.0   # host vs broker clock, when matching a placement by time


@dataclass(frozen=True)
class UncertainPlacement:
    """A Skopaq order whose placement outcome is unknown: no order id came back and the
    order book did not show it while we looked, but it may exist and be working."""

    internal_id: str
    symbol: str
    security_id: str
    qty: Decimal
    at: datetime                          # when it was sent (host wall clock)
    side: str = "SELL"                    # only SELLs count against the shares
    # Order ids already in the book just before it was sent (None: the book could not be
    # read then): none of them can be it
    before_ids: Optional[frozenset[str]] = None
    remark: str = ""                      # the ``remarks`` tag it was sent with (if enabled)


@dataclass(frozen=True)
class OwnOpenSell:
    """A Skopaq SELL order (this process's registry or this host's journal) that is not
    known to be final — stuck, unknown, interrupted or still being worked — and whose
    open remainder the book may not list yet."""

    order_id: str
    symbol: str
    security_id: str
    qty: Decimal                          # requested less what is known to have filled
    at: datetime                          # when it was placed (host wall clock)
    # A read of this process found it still working (stuck, or a resume of it cut short):
    # it counts whatever its age, not only for the lag window
    seen_working: bool = False


@dataclass(frozen=True)
class SellContext:
    """Open-order data for a SELL's no-short-sale check (live only; None in paper)."""

    orders: tuple[OrderSnapshot, ...]     # the book, read before the positions it goes with
    read_at: datetime                     # when that book read started (host wall clock)
    error: str = ""                       # the book could not be read: the SELL is refused
    override: bool = False                # SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK was used
    own_recent_exit_qty: Decimal = _ZERO  # this process's confirmed exits within the lag window
    lag_window_s: float = 600.0
    own_order_ids: frozenset[str] = frozenset()   # registry + journal: orders that are ours
    uncertain: tuple[UncertainPlacement, ...] = ()   # our unresolved uncertain SELLs
    holdings_error: str = ""              # holdings unreadable (none counted): say so if refused
    own_open: tuple[OwnOpenSell, ...] = ()   # our unresolved SELL orders (listed or not)
    # A protective exit of the day's position (monitor, CLOSING): it may sell only
    # ``position_sellable``, never older delivery holdings
    position_only: bool = False


@dataclass(frozen=True)
class SellableView:
    """The parts of a sellable quantity, for the decision and for its message."""

    holding_qty: Decimal
    position_qty: Decimal
    pending_qty: Decimal                  # open remainder of SELL orders
    unshown_fill_qty: Decimal             # filled SELLs positions do not show yet
    sellable: Decimal
    pending: tuple[OrderSnapshot, ...]    # the open SELLs counted (ids + raw statuses for alerts)
    excluded: tuple[str, ...]             # rows left out for their product (for the message)
    uncertain_qty: Decimal = _ZERO        # our uncertain SELL placements not in the book yet
    own_pending_qty: Decimal = _ZERO      # the part of pending_qty in SELL orders of ours
    own_open_qty: Decimal = _ZERO         # our unresolved SELL orders the book does not list
    own_open_ids: tuple[str, ...] = ()    # their order ids

    @property
    def position_sellable(self) -> Decimal:
        """What a protective exit of the day's position may sell: the position less
        Skopaq's own open SELLs (listed or not), fills positions do not show yet and
        uncertain SELLs of ours, capped by ``sellable``. Someone else's open SELL (a GTT
        on the holdings) is capped by ``sellable`` alone; older holdings never count."""
        own = (self.own_pending_qty + self.own_open_qty + self.unshown_fill_qty
               + self.uncertain_qty)
        return min(self.position_qty - own, self.sellable)


def same_instrument(symbol_a: str, security_id_a: str, symbol_b: str, security_id_b: str,
                    *, exchange_a: str = "", exchange_b: str = "", isin_a: str = "",
                    isin_b: str = "") -> bool:
    """The ISIN decides when both sides have one. Otherwise the security id, when both
    have one and they are not on different exchanges (the same shares have a different
    security id on NSE and BSE); otherwise the base symbol."""
    if isin_a and isin_b:
        return isin_a.upper() == isin_b.upper()
    other_exchange = bool(exchange_a and exchange_b) and exchange_a.upper() != exchange_b.upper()
    if security_id_a and security_id_b and not other_exchange:
        return str(security_id_a) == str(security_id_b)
    return bool(symbol_a) and bool(symbol_b) and _base_symbol(symbol_a) == _base_symbol(symbol_b)


def _row_is_for(row: OrderSnapshot, symbol: str, security_id: str, exchange: str = "",
                isin: str = "") -> bool:
    """Book rows carry a security id (and an exchange and ISIN) but no trading symbol.
    A row that cannot be attributed — none of those comparable, and no symbol — counts
    against the SELL (conservative); so does one on another exchange without an ISIN."""
    if row.isin and isin:
        return row.isin.upper() == isin.upper()
    other_exchange = bool(row.exchange and exchange) and row.exchange.upper() != exchange.upper()
    if row.security_id and security_id and not other_exchange:
        return row.security_id == security_id
    if row.symbol and symbol:
        return _base_symbol(row.symbol) == _base_symbol(symbol)
    return True


def instrument_isin(symbol: str, security_id: str, exchange: str, rows: Sequence) -> str:
    """The instrument's ISIN, from a position or holding row that is plainly it (the same
    security id on the same exchange, or the same symbol); "" when none says."""
    for r in rows:
        isin = getattr(r, "isin", "") or ""
        if not isin:
            continue
        if same_instrument(symbol, security_id, getattr(r, "symbol", ""),
                           getattr(r, "security_id", ""), exchange_a=exchange,
                           exchange_b=getattr(r, "exchange", "") or ""):
            return isin
    return ""


def _qty(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return Decimal(str(value))
    return _ZERO


def sellable_quantity(
    *,
    symbol: str,
    security_id: str,
    product: str,
    positions: Sequence,
    holdings: Sequence,
    context: SellContext,
    order_qty: Decimal,
    exchange: str = "",
) -> SellableView:
    """What can still be sold of one instrument, from one book-first snapshot.

    Rows of the same shares on another exchange (another security id) count too: they
    are matched by ISIN (taken from the instrument's own position or holding row), or
    by symbol; an order-book row on another exchange without an ISIN counts against it.

    Product: a CNC SELL counts holdings and every position/order row that is not
    explicitly intraday/margin (``""`` counts); any other product counts only rows of
    that product (or ``""``), and no holdings. An open SELL whose remaining quantity is
    unknown counts as the whole order.

    Fills positions do not show yet: the day's filled SELLs in the book minus the sells
    positions already show (``sell_quantity``, else ``day_sell_quantity``), counting only
    fills within the lag window so a sale positions never show cannot block SELLs for
    the rest of the day; and at least Skopaq's own confirmed exits of the lag window not
    yet shown. Sells positions show that older fills in the book account for (a morning
    exit, the user's own sale of holdings) are not those exits and never cancel them out.

    Uncertain placements (``context.uncertain``) count in full while they are younger than
    the lag window, unless a book order carries their ``remarks`` tag (then that order is
    counted instead). A book order that merely looks like one (the same instrument and
    quantity, created about then) proves nothing — it may be someone else's SELL, and
    counting only it could sell the same shares twice — so it does not end the count.

    Our own unresolved SELL orders (``context.own_open``) that the book does not list by
    id count their open remainder while younger than the lag window (the book lags behind
    an accepted order as it lags behind a fill), and whatever their age once a read found
    them still working (``seen_working``); one the book lists is counted from its row.
    """
    order_product = (product or "").upper()
    cnc = order_product in ("", "CNC")

    def counts(row_product: str) -> bool:
        row_product = (row_product or "").upper()
        if cnc:
            return not is_non_cnc_product(row_product)
        return row_product in ("", order_product)

    excluded: list[str] = []
    isin = instrument_isin(symbol, security_id, exchange, [*positions, *holdings])

    def matches(r) -> bool:
        return same_instrument(symbol, security_id, getattr(r, "symbol", ""),
                               getattr(r, "security_id", ""), exchange_a=exchange,
                               exchange_b=getattr(r, "exchange", "") or "", isin_a=isin,
                               isin_b=getattr(r, "isin", "") or "")

    holding_qty = _ZERO
    if cnc:
        for h in holdings:
            if matches(h):
                holding_qty += _qty(getattr(h, "quantity", 0))

    position_qty = _ZERO
    shown = _ZERO
    for p in positions:
        if not matches(p):
            continue
        row_product = getattr(p, "product", "") or ""
        if not counts(row_product):
            excluded.append(f"{_qty(getattr(p, 'quantity', 0))} {row_product.upper()} position")
            continue
        position_qty += _qty(getattr(p, "quantity", 0))
        sold = _qty(getattr(p, "sell_quantity", 0))
        shown += sold if sold > 0 else _qty(getattr(p, "day_sell_quantity", 0))

    read_at = context.read_at if context.read_at.tzinfo else context.read_at.replace(tzinfo=IST)
    since = read_at - timedelta(seconds=context.lag_window_s)
    pending_qty = _ZERO
    own_pending = _ZERO
    pending: list[OrderSnapshot] = []
    book_filled = _ZERO
    recent = _ZERO
    for row in context.orders:
        if row.side == "BUY" or not _row_is_for(row, symbol, security_id, exchange, isin):
            continue
        if not counts(row.product):
            if row.state in _OPEN:
                excluded.append(f"{row.order_id} {row.product} SELL")
            continue
        if row.state in _OPEN:
            remaining = row.remaining_qty
            if remaining is None or remaining > 0:
                open_qty = order_qty if remaining is None else remaining
                pending_qty += open_qty
                if row.order_id in context.own_order_ids:
                    own_pending += open_qty
                pending.append(row)
        if row.side != "SELL":
            continue
        # Shares this SELL has sold: all of a final one's fill (unknown, or SUCCESS
        # reporting nothing traded: all it asked for), or the traded part of one still
        # working
        if row.state in _FILLED:
            sold = row.filled_qty
            if sold is None or (row.state is OrderState.FILLED and not row.traded_qty):
                sold = row.requested_qty if row.requested_qty is not None else order_qty
        elif row.state in _OPEN:
            sold = row.traded_qty or _ZERO
        else:
            continue
        if sold <= 0:
            continue
        book_filled += sold
        if row.updated_at is not None:
            is_recent = row.updated_at >= since
        else:
            is_recent = row.order_id in context.own_order_ids
        if is_recent:
            recent += sold

    # Of the sells positions show, those the book's older fills account for are not our
    # recent exits: only the rest can be them
    shown_recent = max(_ZERO, shown - (book_filled - recent))
    unshown = max(
        min(max(_ZERO, book_filled - shown), recent),
        max(_ZERO, context.own_recent_exit_qty - shown_recent),
    )
    uncertain_qty = sum(
        (u.qty for u in context.uncertain
         if u.side == "SELL" and same_instrument(symbol, security_id, u.symbol, u.security_id)
         and _at(u.at) >= since and not _shown(u, context)),
        start=_ZERO,
    )
    listed = {row.order_id for row in context.orders}
    unlisted = [o for o in context.own_open
                if o.order_id not in listed and o.qty > 0
                and (o.seen_working or _at(o.at) >= since)
                and same_instrument(symbol, security_id, o.symbol, o.security_id)]
    own_open_qty = sum((o.qty for o in unlisted), start=_ZERO)
    return SellableView(
        holding_qty=holding_qty,
        position_qty=position_qty,
        pending_qty=pending_qty,
        unshown_fill_qty=unshown,
        sellable=(holding_qty + position_qty - pending_qty - unshown - uncertain_qty
                  - own_open_qty),
        pending=tuple(pending),
        excluded=tuple(excluded),
        uncertain_qty=uncertain_qty,
        own_pending_qty=own_pending,
        own_open_qty=own_open_qty,
        own_open_ids=tuple(o.order_id for o in unlisted),
    )


def _at(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=IST)


def _shown(placement: UncertainPlacement, context: SellContext) -> bool:
    """The book shows this placement's order (it carries the placement's ``remarks`` tag):
    that order is counted instead. Without a tag nothing proves which order it is."""
    return bool(placement.remark) and any(
        row.remarks == placement.remark for row in context.orders)


def could_be_placement(row: OrderSnapshot, placement: UncertainPlacement,
                       own: frozenset[str] | set[str]) -> bool:
    """A book order that looks like the uncertain placement: not an order already known
    to be ours, not in the book before the placement was sent, the same instrument, side
    and quantity, and created within the clock-skew allowance of when it was sent.

    Only a guess: someone else's order placed about then (a user following the
    placement-uncertain alert, say) looks the same.
    """
    if row.order_id in own or row.side != placement.side or row.requested_qty != placement.qty:
        return False
    if placement.before_ids is not None and row.order_id in placement.before_ids:
        return False
    if not (row.security_id and placement.security_id
            and row.security_id == placement.security_id):
        return False
    if row.created_at is None:
        return False
    skew = timedelta(seconds=SKEW_ALLOWANCE_S)
    sent = _at(placement.at)
    return sent - skew <= row.created_at <= sent + skew
