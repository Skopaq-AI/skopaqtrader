"""INDstocks order rows and statuses, parsed in one place.

Everything Skopaq knows about the broker's order-book row keys and status strings
lives here, so the rest of the code compares ``OrderState`` values, never raw
strings. No I/O.

The REST order endpoints (POST /order, GET /order, GET /order-book) use 15 long
status names (docs "Order Status Types"): QUEUED, O-PENDING, SL-PENDING,
PROCESSING, ABORTED, INITIATED, SUCCESS, CANCELLED, MODIFIED, PENDING, EXPIRED,
FAILED, PARTIALLY FILLED, PARTIALLY FILLED - CANCELLED, PARTIALLY FILLED - EXPIRED.
The docs never say which are final; the split below is ours (and FlintTrade's and
OpenAlgo's). Rows carry ``id`` (the order id), ``txn_type``, ``status``,
``requested_qty`` / ``traded_qty`` (ints), ``traded_price`` (a string, ``""``
until filled), ``security_id``, ``product``, ``extra_info`` and ISO-8601
``created_at`` / ``updated_at``. ``name`` is the instrument's display name, not
its trading symbol, and rows have no trading symbol.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Iterable, Optional

from skopaq.risk.calendar import IST

logger = logging.getLogger(__name__)


class OrderState(StrEnum):
    """What an order's status means for Skopaq."""

    WORKING = "working"            # can still fill: QUEUED, O-PENDING, PENDING, PARTIALLY FILLED…
    FILLED = "filled"              # SUCCESS, or everything requested has traded
    PARTIAL_DONE = "partial"       # final with part filled; the rest is dead, not open
    CANCELLED = "cancelled"        # CANCELLED / EXPIRED (or an operator extra) with nothing filled
    REJECTED = "rejected"          # FAILED / ABORTED / REJECTED with nothing filled
    UNRECOGNISED = "unrecognised"  # in no table: treated as still working until a timeout


TERMINAL_STATES = frozenset({
    OrderState.FILLED, OrderState.PARTIAL_DONE, OrderState.CANCELLED, OrderState.REJECTED,
})

# Aliases (COMPLETE, OPEN, TRIGGER PENDING, ...) cover other brokers' vocabularies
# that might leak through, and the order-update feed.
_WORKING = frozenset({
    "QUEUED", "O-PENDING", "SL-PENDING", "PROCESSING", "INITIATED", "MODIFIED", "PENDING",
    "PARTIALLY FILLED", "OPEN", "TRIGGER PENDING", "CREATED", "RECEIVED",
})
_FILLED = frozenset({"SUCCESS", "COMPLETE", "COMPLETED", "EXECUTED", "TRADED", "FILLED"})
_PARTIAL_FINAL = frozenset({"PARTIALLY FILLED - CANCELLED", "PARTIALLY FILLED - EXPIRED"})
_CANCELLED = frozenset({"CANCELLED", "EXPIRED"})
_REJECTED = frozenset({"FAILED", "ABORTED", "REJECTED"})

# Short codes from the order-update feed that are unambiguous. Single letters
# (S, F, C, ...) are not mapped: they are too easy to misread.
_ALIASES = {
    "PF": "PARTIALLY FILLED",
    "PF-CANCELLED": "PARTIALLY FILLED - CANCELLED",
    "PFC": "PARTIALLY FILLED - CANCELLED",
    "PF-EXPIRED": "PARTIALLY FILLED - EXPIRED",
    "RJ": "REJECTED",
}

_NON_CNC_PRODUCTS = frozenset({"INTRADAY", "MIS", "MARGIN", "NRML", "MTF", "CO", "BO"})

# Warnings logged once per key (unrecognised status per order, row shape without an id).
_noted: set[tuple] = set()
_NOTED_MAX = 1000


def normalise_status(raw: object) -> str:
    """Upper-case a broker status and tidy its spelling; ``""`` for anything not a string.

    ``partially_executed`` → ``PARTIALLY FILLED``, ``PARTIALLY FILLED-CANCELLED`` →
    ``PARTIALLY FILLED - CANCELLED``; ``O-PENDING`` and ``SL-PENDING`` keep their dash.
    """
    if not isinstance(raw, str):
        return ""
    s = " ".join(raw.strip().upper().replace("_", " ").split())
    if s.startswith("PARTIALLY EXECUTED"):
        s = "PARTIALLY FILLED" + s[len("PARTIALLY EXECUTED"):]
    if s.startswith("PARTIALLY FILLED"):
        rest = re.sub(r"^\s*-?\s*", "", s[len("PARTIALLY FILLED"):])
        s = f"PARTIALLY FILLED - {rest}" if rest else "PARTIALLY FILLED"
    return _ALIASES.get(re.sub(r"\s*-\s*", "-", s), s)


def classify(
    status: str,
    traded: Optional[Decimal],
    requested: Optional[Decimal],
    *,
    extra_terminal: frozenset[str] = frozenset(),
) -> OrderState:
    """The state of an order with this (normalised) status and quantities.

    The quantities cross-check the status: everything traded is FILLED whatever the
    status says, and a "nothing filled" final status with a fill is PARTIAL_DONE.
    ``extra_terminal`` holds normalised statuses an operator declared final
    (``SKOPAQ_ORDER_EXTRA_TERMINAL_STATUSES``).
    """
    has_fill = traded is not None and traded > 0
    if status in _FILLED:
        return OrderState.FILLED
    if traded is not None and requested is not None and requested > 0 and traded >= requested:
        return OrderState.FILLED
    if status in _PARTIAL_FINAL:
        return OrderState.PARTIAL_DONE
    if status in _CANCELLED or status in _REJECTED or status in extra_terminal:
        if has_fill:
            return OrderState.PARTIAL_DONE
        return OrderState.REJECTED if status in _REJECTED else OrderState.CANCELLED
    if status in _WORKING:
        return OrderState.WORKING
    return OrderState.UNRECOGNISED


def is_known_status(status: str) -> bool:
    """A (normalised) status ``classify`` already has a meaning for."""
    return status in _WORKING or status in _FILLED or status in _PARTIAL_FINAL or (
        status in _CANCELLED or status in _REJECTED)


@dataclass(frozen=True)
class OrderSnapshot:
    """One order as the broker reported it (a book row or a GET /order answer)."""

    order_id: str
    status: str                        # normalised
    status_raw: str                    # as sent, for messages and alerts
    state: OrderState
    side: str                          # BUY | SELL | ""
    security_id: str
    symbol: str                        # a trading-symbol key if the row has one (never `name`)
    name: str
    product: str
    segment: str
    order_type: str
    validity: str
    requested_qty: Optional[Decimal]
    traded_qty: Optional[Decimal]
    traded_price: Optional[Decimal]
    exch_order_id: str
    message: str                       # extra_info: the broker's reason on a failure
    remarks: str
    created_at: Optional[datetime]
    updated_at: Optional[datetime]
    exchange: str = ""                 # NSE | BSE: security ids differ per exchange
    isin: str = ""                     # shared across exchanges ("" for derivatives)

    @property
    def remaining_qty(self) -> Optional[Decimal]:
        """Requested minus traded (never below 0); None when the requested quantity is unknown.

        Only still open while the state is WORKING/UNRECOGNISED; on a final status the
        remainder is dead.
        """
        if self.requested_qty is None:
            return None
        return max(self.requested_qty - (self.traded_qty or Decimal("0")), Decimal("0"))

    @property
    def filled_qty(self) -> Optional[Decimal]:
        """How much has filled, or None when the broker did not say.

        With ``traded_qty``: that (capped at the requested quantity). Without it,
        only the status can answer: FILLED means all of it, CANCELLED/REJECTED mean
        none; a partial final or working order has an unknown fill — never read it as 0.
        A "PARTIALLY FILLED - …" row reporting 0 traded contradicts itself (the status
        says something executed): unknown too, like SUCCESS reporting nothing traded.
        """
        if self.state is OrderState.PARTIAL_DONE and not self.traded_qty:
            return None
        if self.traded_qty is not None:
            if self.requested_qty is not None:
                return min(self.traded_qty, self.requested_qty)
            return self.traded_qty
        if self.state is OrderState.FILLED:
            return self.requested_qty
        if self.state in (OrderState.CANCELLED, OrderState.REJECTED):
            return Decimal("0")
        return None


def to_decimal(value: object) -> Optional[Decimal]:
    """A number from an int, float, Decimal or numeric string; None for anything else.

    ``""``, None, booleans, NaN/infinity and mocks give None.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value)) if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            number = Decimal(text)
        except InvalidOperation:
            return None
        return number if number.is_finite() else None
    return None


def parse_broker_time(value: object) -> Optional[datetime]:
    """An ISO-8601 broker timestamp; a naive one is taken as IST. None if unparseable."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=IST)


def _first(row: dict, *keys: str) -> object:
    """The first of ``keys`` whose value is present and not None/empty."""
    for key in keys:
        value = row.get(key)
        if value is not None and value != "":
            return value
    return None


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return ""


def _security_id(value: object) -> str:
    """``2885``, ``"2885"`` and a scrip code ``"NSE_2885"`` all give ``"2885"``."""
    text = _text(value)
    prefix, sep, rest = text.partition("_")
    if sep and prefix.isalpha() and rest:
        return rest
    return text


def note_unrecognised(order_id: str, status_raw: str) -> None:
    """Log an unrecognised status once per (order, status)."""
    _note_once(
        ("status", order_id, status_raw),
        "Order %s has a status Skopaq does not recognise (%r); treating it as still "
        "working until it times out. SKOPAQ_ORDER_EXTRA_TERMINAL_STATUSES can declare "
        "it final.", order_id, status_raw,
    )


def _note_once(key: tuple, message: str, *args: object) -> None:
    if key in _noted:
        return
    if len(_noted) >= _NOTED_MAX:
        _noted.clear()
    _noted.add(key)
    logger.warning(message, *args)


def parse_order_row(
    row: object, *, extra_terminal: frozenset[str] = frozenset(),
) -> Optional[OrderSnapshot]:
    """One broker order row as an ``OrderSnapshot``; None unless it is a dict with an order id.

    Reads the documented keys first and common alternates after them
    (``order_id``, ``transaction_type``, ``filled_quantity``, ...).
    """
    if not isinstance(row, dict):
        return None
    order_id = ""
    for key in ("id", "order_id", "orderId"):
        order_id = _text(row.get(key))
        if order_id:
            break
    if not order_id:
        return None

    status_raw = _text(_first(row, "order_status", "status", "orderStatus"))
    status = normalise_status(status_raw)
    requested = to_decimal(_first(row, "requested_qty", "quantity", "qty", "order_qty"))
    traded = to_decimal(_first(row, "traded_qty", "filled_qty", "filled_quantity",
                               "traded_quantity"))
    state = classify(status, traded, requested, extra_terminal=extra_terminal)
    if state is OrderState.UNRECOGNISED:
        note_unrecognised(order_id, status_raw)

    side = _text(_first(row, "txn_type", "transaction_type", "side")).upper()
    return OrderSnapshot(
        order_id=order_id,
        status=status,
        status_raw=status_raw,
        state=state,
        side=side if side in ("BUY", "SELL") else "",
        security_id=_security_id(_first(row, "security_id", "securityId", "scrip_code")),
        symbol=_text(_first(row, "trading_symbol", "tradingsymbol", "symbol")),
        name=_text(row.get("name")),
        product=_text(row.get("product")).upper(),
        segment=_text(row.get("segment")).upper(),
        order_type=_text(row.get("order_type")).upper(),
        validity=_text(row.get("validity")).upper(),
        requested_qty=requested,
        traded_qty=traded,
        traded_price=to_decimal(_first(row, "traded_price", "average_price", "avg_price",
                                       "average_traded_price")),
        exch_order_id=_text(_first(row, "exch_order_id", "exchange_order_id")),
        message=_text(_first(row, "extra_info", "message", "error_message", "rejection_reason")),
        remarks=_text(row.get("remarks")),
        created_at=parse_broker_time(row.get("created_at")),
        updated_at=parse_broker_time(row.get("updated_at")),
        exchange=_text(row.get("exchange")).upper(),
        isin=_text(row.get("isin")).upper(),
    )


def parse_order_book(
    rows: object, *, extra_terminal: frozenset[str] = frozenset(),
) -> tuple[OrderSnapshot, ...]:
    """Every row with an order id; rows without one are dropped (logged once per shape).

    One odd row never throws away the whole book. A non-list gives ``()`` — callers
    that must tell "empty" from "unreadable" check the payload type themselves.
    """
    if not isinstance(rows, list):
        return ()
    parsed: list[OrderSnapshot] = []
    for row in rows:
        snap = parse_order_row(row, extra_terminal=extra_terminal)
        if snap is not None:
            parsed.append(snap)
            continue
        shape = tuple(sorted(row)) if isinstance(row, dict) else type(row).__name__
        _note_once(("shape", shape), "Order book row without an order id dropped (keys: %s)",
                   shape)
    return tuple(parsed)


def find_order(rows: Iterable[OrderSnapshot], order_id: str) -> Optional[OrderSnapshot]:
    """The row with this order id, or None."""
    for snap in rows:
        if snap.order_id == order_id:
            return snap
    return None


@dataclass(frozen=True)
class FillSummary:
    """The fills of one order: total quantity, VWAP (None unless every fill has a price)."""

    qty: Decimal
    vwap: Optional[Decimal]
    count: int


def parse_fills(rows: object) -> FillSummary:
    """Sum per-fill rows (GET /order/trades, /trades/{id}, or trade-book rows of one order)."""
    if not isinstance(rows, list):
        return FillSummary(Decimal("0"), None, 0)
    qty = Decimal("0")
    value = Decimal("0")
    priced = True
    count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        q = to_decimal(_first(row, "quantity", "qty", "traded_qty", "filled_quantity"))
        if q is None or q <= 0:
            continue
        p = to_decimal(_first(row, "price", "trade_price", "traded_price"))
        qty += q
        count += 1
        if p is None or p <= 0:
            priced = False
        else:
            value += q * p
    vwap = value / qty if count and priced else None
    return FillSummary(qty, vwap, count)


def is_non_cnc_product(product: str) -> bool:
    """True for intraday/margin products (not delivery). ``""`` (unknown) is not non-CNC."""
    return isinstance(product, str) and product.strip().upper() in _NON_CNC_PRODUCTS


class RejectKind(StrEnum):
    """Why POST /order refused an order, which decides what to try next."""

    RATE_LIMITED = "rate_limited"      # back off and retry
    PRICE = "price"                    # tick size / circuit / price band: try MARKET
    MARKET_BLOCKED = "market_blocked"  # "Market orders are blocked": try a LIMIT
    AUTH = "auth"                      # token problem: stop
    NOT_SENT = "not_sent"              # never reached the broker: retry
    OTHER = "other"                    # RMS, validation, ...: stop


def classify_rejection(status_code: int, message: str, kind: str = "") -> RejectKind:
    """Map a broker refusal (HTTP status, message, ``BrokerError.kind``) to a ``RejectKind``."""
    text = (message or "").lower()
    if kind == "not_sent":
        return RejectKind.NOT_SENT
    if status_code == 429 or "rate limit" in text or "too many requests" in text:
        return RejectKind.RATE_LIMITED
    if "market orders are blocked" in text:
        return RejectKind.MARKET_BLOCKED
    if any(word in text for word in ("tick", "circuit", "price band", "pricewithinrange",
                                     "price range", "limit price")):
        return RejectKind.PRICE
    if status_code in (401, 403) or "token" in text:
        return RejectKind.AUTH
    return RejectKind.OTHER
