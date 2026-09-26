"""One read of the broker's order book, positions and holdings — in that order.

Every "how many shares can still be sold" calculation reads through
:func:`read_broker_snapshot`, so the order of the reads is the same everywhere.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Awaitable, Callable

from skopaq.broker.models import Holding, Position
from skopaq.broker.order_status import OrderSnapshot, parse_order_book
from skopaq.risk.calendar import now_ist

logger = logging.getLogger(__name__)

_BOOK_RETRY_DELAY_S = 0.5


@dataclass(frozen=True)
class BrokerSnapshot:
    """The broker's order book, positions and holdings, read in that order."""

    orders: tuple[OrderSnapshot, ...]   # today's order book, read FIRST
    positions: tuple[Position, ...]     # read after the book
    holdings: tuple[Holding, ...]       # read last
    read_at: datetime                   # host wall clock (IST) when the book read started
    book_error: str = ""                # non-empty: the book could not be read (after one retry)
    holdings_error: str = ""            # non-empty: holdings unreadable (() understates: safe)


async def read_broker_snapshot(
    client: Any,
    *,
    need_holdings: bool = True,
    extra_terminal: frozenset[str] = frozenset(),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    wall: Callable[[], datetime] = now_ist,
) -> BrokerSnapshot:
    """Order book, then positions, then holdings — never the other way round.

    Reading the book first means an order that fills between the reads is counted twice
    (pending in the book, sold in positions), which understates what can be sold; the other
    order would count it zero times. A book error is retried once after 0.5 s and then
    reported in book_error; a positions error raises (callers decide)."""
    orders: tuple[OrderSnapshot, ...] = ()
    book_error = ""
    read_at = wall()
    for attempt in range(2):
        read_at = wall()
        try:
            rows = await client.get_order_book()
            if not isinstance(rows, list):
                # The real client returns a list or raises; anything else is unreadable,
                # never "no open orders"
                raise TypeError(f"order book is a {type(rows).__name__}, not a list of rows")
            orders = parse_order_book(rows, extra_terminal=extra_terminal)
            book_error = ""
            break
        except Exception as exc:
            book_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Order book read failed (attempt %d of 2): %s", attempt + 1, book_error)
            if attempt == 0:
                await sleep(_BOOK_RETRY_DELAY_S)

    positions = tuple(await client.get_positions())

    holdings: tuple[Holding, ...] = ()
    holdings_error = ""
    if need_holdings:
        try:
            holdings = tuple(await client.get_holdings())
        except Exception as exc:
            holdings_error = f"{type(exc).__name__}: {exc}"
            logger.warning("Holdings read failed; counting none: %s", holdings_error)

    return BrokerSnapshot(
        orders=orders,
        positions=positions,
        holdings=holdings,
        read_at=read_at,
        book_error=book_error,
        holdings_error=holdings_error,
    )
