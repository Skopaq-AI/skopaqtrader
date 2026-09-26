"""Position Monitor — two-tier auto-sell worker.

Safety tier (every poll):  hard stop-loss, trailing stop, EOD exit.
AI tier (every N polls):   Gemini 3 Flash sell analyst for intelligent exits.

Live (INDstocks), the monitor also keeps in step with the broker:

- It resyncs from one read of the order book, positions and holdings (in that order),
  so it knows what is still held, which SELL orders are open and which filled SELLs
  positions do not show yet. A position is never dropped on a failed read or on one
  empty read (sales already out of its quantity prove nothing), and never sold twice
  because positions lag behind a filled exit or the book behind an uncertain one.
- Each exit runs as its own task, and so does each AI analysis and each resume of an
  order left open, so neither a resting exit, a slow LLM call nor a cancel the broker
  does not confirm holds up another position's stop-loss. Only the quantity the broker
  confirmed counts; the rest of a partial exit is sold next cycle for the same reason.
- It resumes orders left open (its own stuck exits, unconfirmed BUYs, and orders an
  earlier Skopaq process journalled), records their late fills, and stays alive while
  any is unresolved — also while a placement's outcome is unknown, or a confirmed BUY
  does not show in positions yet. An exit blocked by someone else's open SELL is alerted.
- It never ends before the close on reads that may be wrong: with nothing left to
  watch, a fresh read confirms it first (a position dropped on a broker glitch is taken
  back). It stops by itself just after the close (15:31 IST) and reports what is left
  open (``positions_left``, ``orders_unconfirmed``); ``skopaq monitor`` then exits 4.

Paper mode runs the loop exactly as before.

Usage::

    monitor = PositionMonitor(executor, client, router, config, llm, stop_event)
    result = await monitor.run()
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, Optional

from skopaq.agents.sell_analyst import SellDecision, analyze_exit
from skopaq.broker.models import (
    OrderType,
    Position,
    TradingSignal,
    filled_quantity_of,
    is_unconfirmed,
)
from skopaq.broker.order_status import (
    OrderSnapshot,
    OrderState,
    is_non_cnc_product,
    to_decimal,
)
from skopaq.execution.live_orders import (
    Confirmation,
    LiveOrderWorker,
    TrackedOrder,
    own_open_sells,
    recent_exit_qty,
    shutdown_budget_seconds,
    uncertain_from_journal,
    uncertain_sells,
)
from skopaq.execution.order_alerts import get_alerter
from skopaq.execution.safety_checker import _base_symbol
from skopaq.execution.sell_lock import SellLockBusy
from skopaq.execution.sellable import (
    SellableView,
    SellContext,
    UncertainPlacement,
    could_be_placement,
    same_instrument,
    sellable_quantity,
)

if TYPE_CHECKING:
    from skopaq.broker.book_snapshot import BrokerSnapshot
    from skopaq.broker.client import INDstocksClient
    from skopaq.config import SkopaqConfig
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter

logger = logging.getLogger(__name__)

# IST = UTC+5:30
_IST = timezone(timedelta(hours=5, minutes=30))
_MARKET_CLOSE = time(15, 30)
# Live: the monitor ends by itself once the market has closed (nothing can be sold)
_AFTER_CLOSE = time(15, 31)
_RESYNC_CYCLES_DEFAULT = 3
_DEDUP_S = 600.0            # per-symbol monitor alerts: at most one per 10 minutes
_QUICK_RESUME_S = 1.0       # a resync waits this long for the resumes it starts
_ZERO = Decimal("0")
_FILLED_STATES = (OrderState.FILLED, OrderState.PARTIAL_DONE)

# Books a late fill (trade rows); False (or an exception) when the write failed
LateFillFn = Callable[[TrackedOrder, Confirmation], Awaitable[Optional[bool]]]


def _now_ist() -> datetime:
    return datetime.now(_IST)


@dataclass
class MonitoredPosition:
    """Per-position tracking state."""

    symbol: str
    scrip_code: str
    entry_price: float
    quantity: int
    high_water_mark: float = 0.0  # For trailing stop
    # Live only
    security_id: str = ""
    product: str = ""
    pending_exit: bool = False    # a SELL of ours may still be working at the broker
    exit_intent: str = ""         # a partial exit's reason: the rest is sold next cycle
    stuck_orders: list[str] = field(default_factory=list)   # our open SELLs for it
    blocked_by: list = field(default_factory=list)   # others' open SELLs (OrderSnapshot)
    zero_reads: int = 0           # successful reads in a row showing nothing held
    # Shares the day's filled SELLs of it had sold at the last read that showed it held:
    # only sales beyond these can corroborate that it is gone
    sells_seen: Decimal = Decimal("0")
    exit_task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)
    ai_task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self.high_water_mark <= 0:
            self.high_water_mark = self.entry_price


@dataclass
class MonitorResult:
    """Session summary returned by PositionMonitor.run()."""

    positions_monitored: int = 0
    sells_executed: int = 0
    sells_failed: int = 0
    total_pnl: float = 0.0
    exit_reasons: list[str] = field(default_factory=list)
    cycles: int = 0
    # Live only: what is left at the end (``skopaq monitor`` exits 4 when either is set)
    positions_left: list[str] = field(default_factory=list)       # still held
    orders_unconfirmed: list[str] = field(default_factory=list)   # may still be working
    exits_blocked: list[str] = field(default_factory=list)   # refused or blocked exits
    late_fills: int = 0           # fills the broker confirmed after they were reported


# ── Live helpers (shared with the daemon's CLOSING) ──────────────────────────


def live_worker(router: Any) -> Optional[LiveOrderWorker]:
    """The router's live order worker; None in paper, without a live client, or for a
    test double (so mocked routers keep today's code paths)."""
    worker = getattr(router, "worker", None) if router is not None else None
    return worker if isinstance(worker, LiveOrderWorker) else None


def own_order_ids(router: OrderRouter) -> set[str]:
    """Order ids that are Skopaq's: this process's registry plus today's journal."""
    own = set(router.registry.ids())
    if router.journal is not None:
        own |= router.journal.own_ids_today()
    return own


def _cnc_net(snap: BrokerSnapshot, symbol: str, security_id: str) -> Decimal:
    """Net quantity of the instrument's CNC (or unlabelled) position rows."""
    return sum(
        (p.quantity for p in snap.positions
         if not is_non_cnc_product(p.product)
         and same_instrument(symbol, security_id, p.symbol, p.security_id)),
        start=_ZERO,
    )


def sellable_view(router: OrderRouter, snap: BrokerSnapshot, symbol: str, security_id: str,
                  own: Iterable[str]) -> SellableView:
    """What of one instrument can still be sold (CNC), from a book-first snapshot, by the
    monitor's and CLOSING's exits (NSE orders; the same shares on BSE count too)."""
    lag = router.worker.settings.sell_fill_lag_window_s
    context = SellContext(
        orders=snap.orders, read_at=snap.read_at,
        own_recent_exit_qty=recent_exit_qty(router.registry, router.journal, symbol,
                                            security_id, lag, snap.read_at),
        lag_window_s=lag, own_order_ids=frozenset(own),
        uncertain=uncertain_sells(router.registry, router.journal),
        own_open=own_open_sells(router.registry, router.journal, snap.read_at),
    )
    # An open SELL whose remainder is unknown blocks the whole position
    order_qty = max(_cnc_net(snap, symbol, security_id), Decimal(1))
    return sellable_quantity(symbol=symbol, security_id=security_id, product="CNC",
                             positions=snap.positions, holdings=snap.holdings,
                             context=context, order_qty=order_qty, exchange="NSE")


@dataclass(frozen=True)
class HeldPosition:
    """One instrument's CNC position that is still held, per a book-first snapshot."""

    row: Position               # its first position row (symbol, average price)
    security_id: str
    view: SellableView

    @property
    def symbol(self) -> str:
        return self.row.symbol

    @property
    def net(self) -> Decimal:
        return self.view.position_qty

    @property
    def held(self) -> Decimal:
        """Net quantity less filled SELLs that positions do not show yet."""
        return self.view.position_qty - self.view.unshown_fill_qty

    @property
    def sellable(self) -> int:
        """What an exit of this position may sell now: the net quantity less Skopaq's own
        open SELLs (listed or not), unshown fills and uncertain SELLs, capped by what the
        account can sell (older holdings never absorb a pending exit)."""
        return int(max(_ZERO, self.view.position_sellable))


def held_positions(router: OrderRouter, snap: BrokerSnapshot, own: Iterable[str], *,
                   noted: Optional[set[str]] = None) -> list[HeldPosition]:
    """Today's CNC positions still held, one per instrument.

    Positions lagging behind a filled SELL are not held (their shares are sold).
    Intraday/margin rows are the broker's to square off: skipped, and logged once per
    symbol when ``noted`` is given.
    """
    own = frozenset(own)
    groups: dict[str, list[Position]] = {}
    for p in snap.positions:
        if is_non_cnc_product(p.product):
            note = f"{p.symbol}:{p.product.upper()}"
            if p.quantity > 0 and noted is not None and note not in noted:
                noted.add(note)
                logger.info("%s %s position of %s is not managed by Skopaq (%s) — the broker "
                            "squares it off", p.symbol, p.product.upper(), p.quantity,
                            p.product.upper())
            continue
        # One group per instrument: its NSE and BSE rows share an ISIN, not a security id
        groups.setdefault(p.isin or p.security_id or _base_symbol(p.symbol), []).append(p)
    held = []
    for rows in groups.values():
        rows.sort(key=lambda r: (r.exchange or "NSE").upper() != "NSE")   # an NSE row first
        security_id = next((r.security_id for r in rows if r.security_id), "")
        if sum((r.quantity for r in rows), start=_ZERO) <= 0:
            continue
        item = HeldPosition(rows[0], security_id,
                            sellable_view(router, snap, rows[0].symbol, security_id, own))
        if item.held > 0:
            held.append(item)
    return held


@dataclass(frozen=True)
class UnshownBuy:
    """Shares this process's confirmed BUYs bought that the broker's positions do not
    show yet (they lag behind fills). They cannot be sold until positions show them."""

    symbol: str
    security_id: str
    qty: Decimal
    age_s: float                # since the instrument's latest confirmed BUY


def unshown_buys(router: OrderRouter, snap: BrokerSnapshot) -> list[UnshownBuy]:
    """Confirmed BUY fills (the registry: this process's, and today's journal once the
    monitor has read it) beyond what positions show was bought today.

    Positions show a purchase as ``buy_qty``, or as net plus sold (their net is today's
    buys less sells). Shares a filled SELL in the book has sold since are not counted, so
    a position bought and sold again whose row is gone is never reported held.
    """
    groups: dict[str, list[tuple[float, str, str, Decimal]]] = {}
    for item in router.registry.confirmed_buys():
        groups.setdefault(item[2] or _base_symbol(item[1]), []).append(item)
    found = []
    for items in groups.values():
        symbol = items[0][1]
        security_id = next((sid for _, _, sid, _ in items if sid), "")
        bought = sum((qty for _, _, _, qty in items), start=_ZERO)
        rows = [p for p in snap.positions if not is_non_cnc_product(p.product)
                and same_instrument(symbol, security_id, p.symbol, p.security_id)]
        by_buy = sum((p.buy_quantity for p in rows), start=_ZERO)
        by_net = sum((p.quantity + (p.sell_quantity if p.sell_quantity > 0
                                    else p.day_sell_quantity) for p in rows), start=_ZERO)
        missing = min(bought - max(by_buy, by_net),
                      bought - _filled_sells(snap, symbol, security_id))
        if missing > 0:
            found.append(UnshownBuy(symbol, security_id, missing,
                                    min(age for age, _, _, _ in items)))
    return found


def _filled_sells(snap: BrokerSnapshot, symbol: str, security_id: str) -> Decimal:
    """Shares the day's filled SELL orders of this instrument sold (unknown fills: 0)."""
    total = _ZERO
    for o in snap.orders:
        if o.side != "SELL" or o.state not in _FILLED_STATES:
            continue
        if o.security_id and security_id:
            if o.security_id != security_id:
                continue
        elif not (o.symbol and _base_symbol(o.symbol) == _base_symbol(symbol)):
            continue
        total += o.filled_qty or _ZERO
    return total


async def record_late_fill(router: OrderRouter, tracked: TrackedOrder, conf: Confirmation,
                           on_late_fill: Optional[LateFillFn], *,
                           exclusive: bool = True) -> int:
    """Book what a resumed order filled beyond what is booked for it; 1 if it was booked.

    A SELL's fill counts as a confirmed exit (sellable checks subtract it until positions
    show it), a BUY's as a confirmed buy, booked or not. ``on_late_fill`` books it: an
    exit closing BUY rows, or the BUY's row; it returns False (or raises) when the trade
    rows could not be written.

    Each booking is journalled twice: ``booking`` before ``on_late_fill``, ``booked`` once
    it succeeded. Only ``booked`` totals count as booked (``OrderJournal.booking_state``):
    shares whose ``booking`` was never confirmed — the process died, or the write failed —
    are booked again from the last confirmed total by the next process to book the
    order, and a CRITICAL alert says they may not be booked (or, if that write did land,
    booked twice). A failed booking is alerted at once, and nothing counts it as booked.

    ``exclusive``: no other Skopaq process can be booking this order now (``resume_order``
    holds its order lock, or there is no journal). Then progress on an order still working
    (``may_be_open``) is booked as soon as it is seen — a stuck BUY's shares before an exit
    sells them, and progress the last process of the day sees — provided the read gave it
    a price (else the final read prices it, from the order's trades) and its ``booking``
    line was written (else the next process could not know of it). Without the lock
    progress is never booked: two processes could both book it.

    A final fill is booked only by the process that claims it first in the journal
    directory (``OrderJournal.claim``), lock or no lock — a process whose lock directory
    is unusable may read it at the same moment — and from the booked total re-read after
    the claim. A claim that cannot be written books nothing (CRITICAL: book it by hand).
    A lost claim leaves the fill to the winner, unless the winner started booking it under
    the order lock, never confirmed it, and this process now holds that lock: the winner
    is gone, so this process books it.

    Once started, a booking runs to the end even if the caller is cancelled.
    """
    if conf.filled_qty is None or conf.filled_qty <= tracked.filled_reported:
        return 0
    registry = router.registry
    record = (registry.record_confirmed_exit if tracked.side == "SELL"
              else registry.record_confirmed_buy)
    record(tracked.symbol, tracked.security_id, conf.filled_qty, order_id=tracked.order_id)
    journal = router.journal
    final = not conf.may_be_open
    if not final:
        if journal is not None and not exclusive:
            logger.info("%s %s: order %s has filled %s of %s so far and may still be working; "
                        "without the order lock it is booked once it is final",
                        tracked.side or "Order", tracked.symbol, tracked.order_id,
                        conf.filled_qty, tracked.requested)
            return 0
        if conf.avg_price is None:
            logger.info("%s %s: order %s has filled %s of %s so far, but the read gave no "
                        "price; booked once it is final (priced from its trades)",
                        tracked.side or "Order", tracked.symbol, tracked.order_id,
                        conf.filled_qty, tracked.requested)
            return 0
        if tracked.unbooked_at == conf.filled_qty:
            return 0      # booking this total failed here (alerted): more progress retries
    elif journal is not None:
        claimed = journal.claim(f"late-fill-{tracked.order_id}")
        if claimed is None:
            _alert_unclaimed(tracked, conf)
            return 0
        state = journal.booking_state(tracked.order_id)
        if not claimed and not (exclusive and _abandoned_final(state)):
            tracked.filled_reported = conf.filled_qty
            tracked.avg_price_reported = conf.avg_price
            logger.info("Order %s's late fill is booked by another Skopaq process",
                        tracked.order_id)
            return 0
        _raise_reported(tracked, state)
        if conf.filled_qty <= tracked.filled_reported:
            return 0
    if journal is not None:
        written = _journal_booking(journal, "booking", tracked, conf, locked=exclusive)
        if not final and not written:
            logger.warning("%s %s: order %s's progress (%s of %s) is not booked: its journal "
                           "line could not be written, so another process could book it "
                           "too; it is booked once the order is final",
                           tracked.side or "Order", tracked.symbol, tracked.order_id,
                           conf.filled_qty, tracked.requested)
            return 0
    registry.booking.add(tracked.order_id)
    booked = await registry.shielded(_persist_late_fill(router, tracked, conf, on_late_fill,
                                                        locked=exclusive))
    return 1 if booked else 0


def _journal_booking(journal: Any, event: str, tracked: TrackedOrder, conf: Confirmation, *,
                     locked: bool) -> bool:
    """A ``booking``/``booked`` line: the total booked once it succeeds (``filled``), and
    the total booked before it (``reported``)."""
    return bool(journal.record(
        event, order_id=tracked.order_id, internal_id=tracked.internal_id,
        symbol=tracked.symbol, security_id=tracked.security_id, segment=tracked.segment,
        side=tracked.side, qty=tracked.requested, purpose=tracked.purpose,
        filled=conf.filled_qty, avg_price=conf.avg_price,
        status=conf.status_raw or conf.status, reported=tracked.filled_reported,
        reported_avg=tracked.avg_price_reported, guessed=tracked.guessed,
        order_final=not conf.may_be_open, locked=locked))


async def _persist_late_fill(router: OrderRouter, tracked: TrackedOrder, conf: Confirmation,
                             on_late_fill: Optional[LateFillFn], *, locked: bool) -> bool:
    """Write the trade rows; on success journal ``booked``, take the total as reported and
    alert that it was recorded. On failure nothing counts as booked (CRITICAL)."""
    error = ""
    try:
        ok = on_late_fill is None or (await on_late_fill(tracked, conf)) is not False
    except Exception as exc:
        logger.warning("Booking the late fill of order %s failed", tracked.order_id,
                       exc_info=True)
        ok, error = False, str(exc) or type(exc).__name__
    finally:
        router.registry.booking.discard(tracked.order_id)
    if not ok:
        if conf.may_be_open:
            tracked.unbooked_at = conf.filled_qty
        _alert_unbooked(router, tracked, conf, error)
        return False
    delta = conf.filled_qty - tracked.filled_reported
    journal = router.journal
    if journal is not None:
        _journal_booking(journal, "booked", tracked, conf, locked=locked)
    tracked.filled_reported = conf.filled_qty
    tracked.avg_price_reported = conf.avg_price
    tracked.unbooked_at = None
    _alert_recorded(tracked, conf, delta)
    return True


def _alert_recorded(tracked: TrackedOrder, conf: Confirmation, delta: Decimal) -> None:
    price = f" at {conf.avg_price}" if conf.avg_price else ""
    if conf.may_be_open:
        what = "sold" if tracked.side == "SELL" else "filled"
        key = (f"{'exit-late' if tracked.side == 'SELL' else 'late-fill'}:"
               f"{tracked.order_id}:{conf.filled_qty}")
        text = (f"{tracked.side or 'Order'} {tracked.symbol}: order {tracked.order_id} "
                f"{what} {delta} more after it was reported and may still be working (now "
                f"{conf.filled_qty} of {tracked.requested}{price}); recorded"
                + (" as an exit" if tracked.side == "SELL" else ""))
    elif tracked.side == "SELL":
        key = f"exit-late:{tracked.order_id}"
        text = (f"SELL {tracked.symbol}: order {tracked.order_id} filled {delta} more "
                f"after it was reported (now {conf.filled_qty} of "
                f"{tracked.requested}{price}); recorded as an exit")
    else:
        key = f"late-fill:{tracked.order_id}"
        text = (f"{tracked.side or 'Order'} {tracked.symbol}: order {tracked.order_id} "
                f"filled {delta} after it was reported (now {conf.filled_qty} of "
                f"{tracked.requested}{price}); recorded, and the position is monitored")
    severity = "WARNING"
    if tracked.guessed:
        severity = "CRITICAL"
        text += (" — the order was matched to an uncertain placement of Skopaq's by its look "
                 "alone: check at the broker that it is Skopaq's")
    get_alerter().alert(severity, key, text, order_ids=[tracked.order_id])


def _alert_unbooked(router: OrderRouter, tracked: TrackedOrder, conf: Confirmation,
                    error: str) -> None:
    """This process's booking failed: the shares may not be booked."""
    total = conf.filled_qty
    key = f"booking-unconfirmed:{tracked.order_id}:{total}"
    if router.journal is not None:
        router.journal.once_today(key)        # another process noticing it stays quiet
    get_alerter().alert(
        "CRITICAL", key,
        f"{tracked.side or 'Order'} {tracked.symbol}: order {tracked.order_id} filled "
        f"{total - tracked.filled_reported} more (now {total} of {tracked.requested}), but "
        f"booking them failed" + (f" ({error})" if error else "") + ": they may not be "
        f"booked — check trade rows. Skopaq still counts {tracked.filled_reported} as "
        "booked; the next booking of this order starts from there",
        order_ids=[tracked.order_id])


def _alert_unconfirmed(router: OrderRouter, tracked: TrackedOrder, line: dict,
                       booked: Decimal) -> None:
    """A ``booking`` line no ``booked`` line confirms (seen holding the order lock)."""
    total = to_decimal(line.get("filled")) or _ZERO
    before = to_decimal(line.get("reported")) or _ZERO
    key = f"booking-unconfirmed:{tracked.order_id}:{total}"
    if router.journal is not None and not router.journal.once_today(key):
        return                                  # alerted today by another process
    get_alerter().alert(
        "CRITICAL", key,
        f"{tracked.side or 'Order'} {tracked.symbol}: order {tracked.order_id}: booking "
        f"{total - before} share(s) (to {total} of {tracked.requested}) was started by a "
        f"Skopaq process (pid {line.get('pid', '?')}) and never confirmed: they may not be "
        f"booked — check trade rows. Skopaq books from the last confirmed total ({booked}), "
        "so if that write did land they are booked twice",
        order_ids=[tracked.order_id])


def _alert_unclaimed(tracked: TrackedOrder, conf: Confirmation) -> None:
    """The final fill cannot be claimed (the journal directory is unwritable)."""
    get_alerter().alert(
        "CRITICAL", f"late-fill-unclaimed:{tracked.order_id}",
        f"{tracked.side or 'Order'} {tracked.symbol}: order {tracked.order_id} filled "
        f"{conf.filled_qty - tracked.filled_reported} more (now {conf.filled_qty} of "
        f"{tracked.requested}"
        + (f" at {conf.avg_price}" if conf.avg_price else "")
        + f"), not booked: journal unwritable — book by hand (with no journal to claim it "
        "in, another Skopaq process could book it too)",
        order_ids=[tracked.order_id])


def _abandoned_final(state: Any) -> bool:
    """A final fill's booking was started under the order lock and never confirmed: its
    process no longer holds the lock (it died, or it failed and alerted)."""
    return any(line.get("order_final") and line.get("locked") for line in state.unconfirmed)


def _raise_reported(tracked: TrackedOrder, state: Any) -> bool:
    """Take a larger booked total from the journal as reported here; True if raised."""
    if state is None or state.booked <= tracked.filled_reported:
        return False
    tracked.filled_reported, tracked.avg_price_reported = state.booked, state.avg_price
    return True


def _sync_booked(router: OrderRouter, tracked: TrackedOrder, *, exclusive: bool = True) -> bool:
    """Take what Skopaq processes have booked for this order (today's journal) as reported
    here, so none of it is booked twice; True when it is final and booked in full (another
    process finished it). Called before resuming it — holding its lock (``exclusive``):
    then a booking another process started and never confirmed is alerted."""
    journal = router.journal
    if journal is None:
        return False
    state = journal.booking_state(tracked.order_id)
    if _raise_reported(tracked, state):
        record = (router.registry.record_confirmed_exit if tracked.side == "SELL"
                  else router.registry.record_confirmed_buy)
        record(tracked.symbol, tracked.security_id, state.booked, order_id=tracked.order_id)
    if exclusive:
        for line in state.unconfirmed:
            if line.get("locked"):   # made under the lock we hold now: not in progress
                _alert_unconfirmed(router, tracked, line, state.booked)
    if not state.finished:
        return False
    tracked.state, tracked.final_seen = "final", True
    logger.info("Order %s was finished by another Skopaq process (booked %s)",
                tracked.order_id, state.booked)
    return True


async def resume_order(router: OrderRouter, tracked: TrackedOrder,
                       on_late_fill: Optional[LateFillFn]) -> int:
    """Resume one order — cancel it if it still works, read its final state — and book
    its late fill, holding its order lock: one Skopaq process at a time, and never one
    that another process has already finished and booked. What Skopaq processes booked
    for it is read from the journal first (``_sync_booked``). Progress the first read
    shows is booked before the cancel is sent (a stuck BUY's shares may be sold
    meanwhile), and any more once the cancel settles. Left for the next resync when
    another process holds the lock, or this one is resuming it already or still booking
    its late fill (a resume cut short: its booking runs on). Returns the late fills
    booked."""
    registry = router.registry
    if tracked.order_id in registry.resuming or tracked.order_id in registry.booking:
        return 0
    registry.resuming.add(tracked.order_id)
    try:
        lock = router.order_lock(tracked.order_id)
        async with lock or contextlib.nullcontext():
            exclusive = router.journal is None or (lock is not None and lock.held)
            if _sync_booked(router, tracked, exclusive=exclusive):
                return 0
            recorded = 0

            async def on_open(view: Confirmation) -> None:
                nonlocal recorded
                recorded += await record_late_fill(router, tracked, view, on_late_fill,
                                                   exclusive=exclusive)

            conf = await router.worker.resume(tracked, cancel=True, on_open=on_open)
            return recorded + await record_late_fill(router, tracked, conf, on_late_fill,
                                                     exclusive=exclusive)
    except SellLockBusy as exc:
        logger.info("%s — leaving it to that process for now", exc)
        return 0
    finally:
        registry.resuming.discard(tracked.order_id)


async def resume_orders(router: OrderRouter, orders: list[TrackedOrder],
                        on_late_fill: Optional[LateFillFn], *,
                        timeout: Optional[float] = None,
                        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
                        wall: Optional[Callable[[], datetime]] = None) -> int:
    """Resume ``orders`` concurrently (``resume_order``); each one found final has its late
    fill recorded as soon as its own resume ends, whatever happens to the others (one
    still working is recorded once a later resume finds it final). Nobody is working them
    any more.

    With ``timeout``, a resume still running then is cancelled and its order left
    unresolved — journalled as interrupted, what was reported for it kept — for a later
    resync, CLOSING or ``skopaq monitor``. ``sleep`` and ``wall`` measure that timeout on
    an injected clock (tests' virtual time). Returns the late fills recorded.
    """
    if not orders:
        return 0
    logger.info("Resuming %d open order(s): %s", len(orders),
                ", ".join(t.order_id for t in orders))
    tasks = {asyncio.ensure_future(resume_order(router, t, on_late_fill)): t for t in orders}
    try:
        done, pending = await _wait(set(tasks), timeout, sleep=sleep, wall=wall)
    except asyncio.CancelledError:
        for task in tasks:
            task.cancel()
        raise
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.wait(pending, timeout=1.0)
        for task in pending:
            _left_unresolved(router, tasks[task])
    late = 0
    for task in done:
        if task.cancelled():
            continue
        if task.exception() is not None:
            logger.error("Resuming order %s failed: %s", tasks[task].order_id,
                         task.exception())
            continue
        late += task.result()
    return late


async def _wait(tasks: set[asyncio.Task], timeout: Optional[float], *,
                sleep: Optional[Callable[[float], Awaitable[None]]] = None,
                wall: Optional[Callable[[], datetime]] = None,
                ) -> tuple[set[asyncio.Task], set[asyncio.Task]]:
    """``asyncio.wait(tasks, timeout=timeout)``, measured on ``sleep``/``wall`` when both
    are given (virtual time in tests)."""
    if timeout is None or sleep is None or wall is None:
        return await asyncio.wait(tasks, timeout=timeout)
    start = wall()
    while True:
        pending = {t for t in tasks if not t.done()}
        left = timeout - (wall() - start).total_seconds()
        if not pending or left <= 0:
            return tasks - pending, pending
        await sleep(min(0.5, left))


def _left_unresolved(router: OrderRouter, tracked: TrackedOrder) -> None:
    """A resume cut short: the order stays unresolved, here and in the journal."""
    if tracked.state == "final":
        return  # its resume finished; only its recording was still running (shielded)
    tracked.state = "interrupted"
    logger.error("Resuming order %s did not finish in time — left for the next resync or "
                 "`skopaq monitor`", tracked.order_id)
    journal = router.journal
    if journal is not None:
        journal.record("interrupted", order_id=tracked.order_id,
                       internal_id=tracked.internal_id, symbol=tracked.symbol,
                       security_id=tracked.security_id, segment=tracked.segment,
                       side=tracked.side, qty=tracked.requested, purpose=tracked.purpose,
                       reported=tracked.filled_reported,
                       reported_avg=tracked.avg_price_reported, guessed=tracked.guessed,
                       note="resume cut short")


def _add_once(items: list[str], item: str) -> None:
    if item not in items:
        items.append(item)


def _resync_cycles(config: Any) -> int:
    """``monitor_resync_cycles`` in [1, 60]; a missing or mocked value gives 3."""
    value = getattr(config, "monitor_resync_cycles", None)
    if isinstance(value, bool) or not isinstance(value, int):
        return _RESYNC_CYCLES_DEFAULT
    clamped = min(max(value, 1), 60)
    if clamped != value:
        logger.warning("SKOPAQ_MONITOR_RESYNC_CYCLES=%s is outside [1, 60]; using %s",
                       value, clamped)
    return clamped


def _journal_time(value: object) -> Optional[datetime]:
    """A journal line's ``ts`` (IST when it carries no offset), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=_IST)


class PositionMonitor:
    """Two-tier position monitoring loop.

    Tier 1 — Safety (every cycle):
        * Hard stop-loss: LTP <= entry * (1 - hard_stop_pct)
        * Trailing stop: LTP <= high_water * (1 - trailing_pct) (if enabled)
        * EOD exit: IST time >= (15:30 - eod_minutes)

    Tier 2 — AI (every ``ai_interval_cycles`` cycles):
        * Invokes the sell_analyst LLM to analyze technicals
        * SELL recommendation → execute immediately
        * HOLD → continue monitoring

    Both tiers route SELL orders through the standard executor pipeline.
    """

    def __init__(
        self,
        executor: Executor,
        client: INDstocksClient,
        router: OrderRouter,
        config: SkopaqConfig,
        llm=None,
        stop_event: Optional[asyncio.Event] = None,
        ai_enabled: bool = True,
        on_exit: Optional[Callable[[TradingSignal, Any], Awaitable[None]]] = None,
        *,
        sell_on_stop: bool = True,
        on_late_fill: Optional[LateFillFn] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        wall: Optional[Callable[[], datetime]] = None,
    ):
        """``on_exit(signal, execution)`` is awaited after each successful sell,
        e.g. to persist it; its failures are logged, never raised.

        Live only: ``sell_on_stop=False`` leaves what is still open after a stop to the
        caller (the daemon's CLOSING): no shutdown pass and no positions-left alert.
        ``on_late_fill(tracked, confirmation)`` is awaited when an order resolved later
        filled more than was reported (its failures are logged). ``sleep`` and ``wall``
        replace the poll wait and the IST clock (tests pass virtual time).
        """
        self._executor = executor
        self._on_exit = on_exit
        self._client = client
        self._router = router
        self._config = config
        self._llm = llm
        self._stop = stop_event or asyncio.Event()
        self._ai_enabled = ai_enabled and llm is not None
        self._sell_on_stop = sell_on_stop
        self._on_late_fill = on_late_fill
        self._sleep = sleep
        self._wall = wall or _now_ist
        # Live needs the router's live worker; a paper router (or a test double) keeps
        # today's loop
        self._live = config.trading_mode == "live" and live_worker(router) is not None
        self._resync_every = _resync_cycles(config)
        self._adopted: set[str] = set()   # orders taken over from the journal
        self._deferred: set[str] = set()  # journal orders an earlier process may still work
        self._unshown: list[UnshownBuy] = []   # confirmed BUYs positions do not show yet
        self._uncertain: list[str] = []   # our uncertain placements not in the book yet
        self._noted: set[str] = set()     # intraday positions already logged
        self._resumes: dict[str, asyncio.Task] = {}   # resumes running in the background

        # Config values
        self._poll_interval = config.monitor_poll_interval_seconds
        self._hard_stop_pct = config.monitor_hard_stop_pct
        self._eod_minutes = config.monitor_eod_exit_minutes_before_close
        self._ai_interval = config.monitor_ai_interval_cycles
        self._trailing_enabled = config.monitor_trailing_stop_enabled
        self._trailing_pct = config.monitor_trailing_stop_pct

        # Live: the AI tier's analysis gets at most half the time between two of its
        # turns (10–60 s), so an answer is never older than that
        try:
            self._ai_timeout_s = min(60.0, max(10.0, 0.5 * float(self._poll_interval)
                                               * float(self._ai_interval)))
        except (TypeError, ValueError):
            self._ai_timeout_s = 30.0

        # Minimum profit gate — prevents selling for tiny gains eaten by brokerage
        self._min_profit_pct = config.daemon_min_profit_threshold_pct
        self._min_profit_inr = config.daemon_min_profit_threshold_inr
        self._est_brokerage = 120.0  # ~₹60 per side for INDstocks (brokerage + GST)

    async def run(self) -> MonitorResult:
        """Main monitoring loop.  Returns when all positions are closed,
        the stop event is set (Ctrl+C), or the market closes."""
        if self._live:
            return await self._run_live()
        return await self._run_paper()

    async def _run_paper(self) -> MonitorResult:
        """Paper (and live without a live worker): the loop as it always was."""
        result = MonitorResult()

        positions = await self._discover_positions()
        if not positions:
            logger.info("No open positions to monitor")
            return result

        result.positions_monitored = len(positions)
        logger.info(
            "Monitoring %d position(s): %s",
            len(positions),
            ", ".join(p.symbol for p in positions),
        )

        cycle = 0
        while not self._stop.is_set() and positions:
            cycle += 1
            result.cycles = cycle

            for pos in list(positions):  # copy — may mutate
                # Fetch current price
                try:
                    ltp = await self._client.get_ltp(pos.scrip_code)
                except Exception:
                    logger.warning(
                        "LTP fetch failed for %s — skipping cycle",
                        pos.symbol, exc_info=True,
                    )
                    continue

                if ltp <= 0:
                    logger.debug("Zero LTP for %s — skipping", pos.symbol)
                    continue

                # Update high-water mark for trailing stop
                if ltp > pos.high_water_mark:
                    pos.high_water_mark = ltp

                pnl_pct = ((ltp - pos.entry_price) / pos.entry_price) * 100

                # ── SAFETY TIER (always runs) ──
                safety_reason = self._check_safety(pos, ltp)
                if safety_reason:
                    ok = await self._execute_sell(pos, ltp, safety_reason, result)
                    if ok:
                        positions.remove(pos)
                    continue

                # ── AI TIER (every N cycles) ──
                if self._ai_enabled and cycle % self._ai_interval == 0:
                    decision = await self._check_ai(pos, ltp, pnl_pct)
                    if decision and decision.action == "SELL":
                        # Min profit gate: don't sell for tiny gains brokerage eats
                        if pnl_pct > 0:
                            gross_profit = (ltp - pos.entry_price) * pos.quantity
                            net_profit = gross_profit - self._est_brokerage
                            if (pnl_pct < self._min_profit_pct
                                    or net_profit < self._min_profit_inr):
                                logger.info(
                                    "[%s] AI says SELL but profit too small: "
                                    "gross=₹%.2f, net=₹%.2f (threshold: %.1f%% / ₹%.0f) "
                                    "→ overriding to HOLD",
                                    pos.symbol, gross_profit, net_profit,
                                    self._min_profit_pct, self._min_profit_inr,
                                )
                                continue  # Skip this sell, keep monitoring

                        reason = f"AI SELL (confidence={decision.confidence}%): {decision.reasoning}"
                        ok = await self._execute_sell(pos, ltp, reason, result)
                        if ok:
                            positions.remove(pos)
                        continue

                # Log status
                logger.info(
                    "[%s] LTP=%.2f  entry=%.2f  P&L=%+.2f%%  HWM=%.2f",
                    pos.symbol, ltp, pos.entry_price, pnl_pct, pos.high_water_mark,
                )

            # Wait for next cycle (interruptible)
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._poll_interval,
                )
                # stop_event was set — graceful shutdown
                break
            except asyncio.TimeoutError:
                pass  # Normal — continue to next cycle

        # If we were interrupted and positions remain, sell them all (EOD safety)
        if positions and self._should_eod_exit():
            for pos in list(positions):
                try:
                    ltp = await self._client.get_ltp(pos.scrip_code)
                except Exception:
                    ltp = 0
                if ltp > 0:
                    await self._execute_sell(pos, ltp, "EOD exit (shutdown)", result)

        return result

    # ── Live loop ────────────────────────────────────────────────────────

    async def _run_live(self) -> MonitorResult:
        """Live: resync with the broker, exits as tasks, stay alive for open orders.

        Ends when nothing is held or open (confirmed by a fresh read), on a stop, or at
        15:31 IST. In-flight exits and resumes are awaited (the worker ends them by the
        shutdown deadline); stopped from the EOD exit on, a standalone monitor then sells
        what is still sellable.
        """
        result = MonitorResult()
        positions: list[MonitoredPosition] = []
        armer = asyncio.create_task(self._arm_on_stop())
        try:
            await self._resync(positions, result, first=True)
            if positions or self._watching():
                logger.info(
                    "Monitoring %d position(s): %s%s", len(positions),
                    ", ".join(p.symbol for p in positions) or "-",
                    f" (and {len(self._router.registry.unresolved())} open order(s))"
                    if self._watching() else "",
                )
            else:
                logger.info("No open positions to monitor")

            cycle = 0
            while True:
                self._reap(positions)
                if self._stop.is_set() or self._after_close():
                    break
                if not self._open_work(positions):
                    # Nothing tracked or watched any more. With no stop before the close, a
                    # fresh read confirms that first: a position dropped on a broker glitch,
                    # or a BUY positions show only now, keeps the monitor running. It never
                    # ends (inside the daemon: hands over to CLOSING, which sells at MARKET
                    # whatever the time) on reads that may have been wrong
                    await self._resync(positions, result)
                    self._reap(positions)
                    if not self._open_work(positions):
                        break
                cycle += 1
                result.cycles = cycle
                if self._resync_due(cycle, positions):
                    await self._resync(positions, result)
                await self._check_positions(positions, cycle, result)
                if await self._pause(self._poll_interval):
                    break  # stop_event was set — graceful shutdown

            if self._stop.is_set():
                self._arm_shutdown()  # idempotent: the watcher may have armed it already
            await self._settle_exits(positions)
            await self._settle_resumes(result)
            await self._abandon_exits(positions, result)  # any still running past its bound
            if (self._sell_on_stop and positions and self._should_eod_exit()
                    and self._router.deadlines.can_place()):
                await self._shutdown_pass(positions, result)
            await self._final_state(positions, result)
        finally:
            armer.cancel()
            for pos in positions:
                if pos.ai_task is not None:
                    pos.ai_task.cancel()
            for task in self._resumes.values():
                task.cancel()  # its order stays unresolved (journalled) for a later process
            await self._abandon_exits(positions)
            if self._sell_on_stop:
                # Standalone: nothing after us waits for these. Inside the daemon
                # (sell_on_stop=False) its REPORTING drains the same registry and alerter
                await self._router.registry.drain_recordings()
                await self._drain_alerts()
        return result

    async def _arm_on_stop(self) -> None:
        """Start the shutdown deadline when the stop arrives (``skopaq monitor`` has no
        daemon to do it; inside the daemon both arm it and the first wins)."""
        await self._stop.wait()
        self._arm_shutdown()

    def _arm_shutdown(self) -> None:
        self._router.arm_shutdown(shutdown_budget_seconds(self._config))

    def _watching(self) -> bool:
        """Something of ours is unresolved: an order (an unconfirmed BUY, a stuck or
        adopted order, or a journalled one an earlier process may still be working), a
        placement whose outcome is unknown, or a confirmed BUY that positions do not show
        yet (the last two within the lag window)."""
        lag = self._router.worker.settings.sell_fill_lag_window_s
        return bool(self._router.registry.unresolved() or self._deferred or self._uncertain
                    or any(u.age_s <= lag for u in self._unshown))

    def _open_work(self, positions: list[MonitoredPosition]) -> bool:
        """Something keeps the monitor running: a tracked position, something watched, or
        a confirmed BUY that positions do not show — even past the lag window (it is still
        held; ending would leave it unprotected, and a recovery ``skopaq monitor`` would
        just be started again)."""
        return bool(positions or self._watching() or self._unshown)

    def _resync_due(self, cycle: int, positions: list[MonitoredPosition]) -> bool:
        """Every ``monitor_resync_cycles`` polls, and every poll while an order is open or
        a confirmed BUY does not show in positions yet."""
        return (cycle % self._resync_every == 0 or self._watching() or bool(self._unshown)
                or any(p.pending_exit for p in positions))

    def _after_close(self) -> bool:
        return self._wall().astimezone(_IST).time() >= _AFTER_CLOSE

    async def _pause(self, seconds: float) -> bool:
        """Wait up to ``seconds`` between polls; True when the stop event fired.

        An injected ``sleep`` (virtual time in tests) is awaited whole, and the stop is
        seen when it returns.
        """
        if self._sleep is not None:
            await self._sleep(seconds)
            return self._stop.is_set()
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    async def _check_positions(self, positions: list[MonitoredPosition], cycle: int,
                               result: MonitorResult) -> None:
        """One pass of the rule tiers. A sell runs as a task: the loop keeps polling the
        other positions while it is worked."""
        for pos in list(positions):
            if self._stop.is_set():
                break  # seen between positions: no new exit after a stop
            if pos.exit_task is not None:
                continue  # its exit is being worked
            if pos.quantity <= 0 and not pos.blocked_by:
                continue  # nothing sellable now (our open SELL, or positions lagging)
            if not self._router.deadlines.can_place():
                continue  # after 15:29:55 IST (or the shutdown deadline): watch only
            try:
                ltp = await self._client.get_ltp(pos.scrip_code)
            except Exception:
                logger.warning("LTP fetch failed for %s — skipping cycle", pos.symbol,
                               exc_info=True)
                continue
            if ltp <= 0:
                logger.debug("Zero LTP for %s — skipping", pos.symbol)
                continue
            if ltp > pos.high_water_mark:
                pos.high_water_mark = ltp
            pnl_pct = ((ltp - pos.entry_price) / pos.entry_price) * 100 if pos.entry_price else 0.0

            # The rest of a partial exit goes first, without asking the AI again
            reason = pos.exit_intent or self._check_safety(pos, ltp)
            if not reason and pos.quantity > 0:
                reason = await self._ai_reason(pos, ltp, pnl_pct, cycle)
            if not reason:
                logger.info("[%s] LTP=%.2f  entry=%.2f  P&L=%+.2f%%  HWM=%.2f",
                            pos.symbol, ltp, pos.entry_price, pnl_pct, pos.high_water_mark)
                continue
            if pos.blocked_by:
                self._alert_blocked(pos, reason, result)
            if pos.quantity > 0:
                if pos.ai_task is not None:
                    pos.ai_task.cancel()  # its answer would come too late to matter
                    pos.ai_task = None
                pos.exit_task = asyncio.create_task(
                    self._execute_sell(pos, ltp, reason, result))
                await asyncio.sleep(0)  # the exit starts before the next position's work

    async def _ai_reason(self, pos: MonitoredPosition, ltp: float, pnl_pct: float,
                         cycle: int) -> str:
        """The AI tier (every N cycles): a SELL reason, or "" to hold.

        The analysis runs as its own task, bounded by ``_ai_timeout_s``: a quick answer
        is used this cycle, a slow one in the cycle it arrives. Meanwhile the loop keeps
        checking every position's stops and the exits keep being worked. None is started
        once the stop is set.
        """
        task = pos.ai_task
        if task is None:
            if not (self._ai_enabled and cycle % self._ai_interval == 0) or self._stop.is_set():
                return ""
            task = pos.ai_task = asyncio.create_task(self._bounded_ai(pos, ltp, pnl_pct))
            await asyncio.sleep(0)       # a quick answer is used this cycle
        if not task.done():
            return ""                    # a slow one when it arrives
        pos.ai_task = None
        decision = None if task.cancelled() or task.exception() else task.result()
        if not decision or decision.action != "SELL":
            return ""
        # Min profit gate: don't sell for tiny gains brokerage eats
        if pnl_pct > 0:
            gross_profit = (ltp - pos.entry_price) * pos.quantity
            net_profit = gross_profit - self._est_brokerage
            if pnl_pct < self._min_profit_pct or net_profit < self._min_profit_inr:
                logger.info(
                    "[%s] AI says SELL but profit too small: gross=₹%.2f, net=₹%.2f "
                    "(threshold: %.1f%% / ₹%.0f) → overriding to HOLD",
                    pos.symbol, gross_profit, net_profit,
                    self._min_profit_pct, self._min_profit_inr,
                )
                return ""
        return f"AI SELL (confidence={decision.confidence}%): {decision.reasoning}"

    async def _bounded_ai(self, pos: MonitoredPosition, ltp: float,
                          pnl_pct: float) -> Optional[SellDecision]:
        """The sell analyst, given at most ``_ai_timeout_s`` (then: hold)."""
        try:
            async with asyncio.timeout(self._ai_timeout_s):
                return await self._check_ai(pos, ltp, pnl_pct)
        except TimeoutError:
            logger.warning("[%s] AI sell analysis took over %.0fs — holding", pos.symbol,
                           self._ai_timeout_s)
            return None

    def _alert_blocked(self, pos: MonitoredPosition, reason: str,
                       result: MonitorResult) -> None:
        """A needed exit is blocked by open SELL orders that are not Skopaq's."""
        listed = ", ".join(f"{o.order_id} {o.status_raw or o.status}" for o in pos.blocked_by)
        get_alerter().alert(
            "CRITICAL", f"exit-blocked:{pos.symbol}:foreign-open-sell",
            f"Exit of {pos.symbol} needed ({reason}) but blocked by open SELL orders that are "
            f"not Skopaq's: {listed}. Cancel them at the broker or let them fill — the "
            "shares are still held.",
            order_ids=[o.order_id for o in pos.blocked_by], dedup_s=_DEDUP_S,
        )
        _add_once(result.exits_blocked, pos.symbol)

    def _reap(self, positions: list[MonitoredPosition]) -> None:
        """Collect finished exit tasks; a position whose exit sold everything is removed."""
        for pos in list(positions):
            task = pos.exit_task
            if task is None or not task.done():
                continue
            pos.exit_task = None
            if task.cancelled():
                continue
            if task.exception() is not None:
                logger.error("Exit of %s failed", pos.symbol, exc_info=task.exception())
                continue
            if task.result():
                self._drop(positions, pos)

    @staticmethod
    def _drop(positions: list[MonitoredPosition], pos: MonitoredPosition) -> None:
        """Stop tracking ``pos`` (and its AI analysis, if one is running)."""
        positions.remove(pos)
        if pos.ai_task is not None:
            pos.ai_task.cancel()
            pos.ai_task = None

    def _running_exits(self, positions: list[MonitoredPosition]) -> list[asyncio.Task]:
        return [p.exit_task for p in positions
                if p.exit_task is not None and not p.exit_task.done()]

    async def _settle_exits(self, positions: list[MonitoredPosition]) -> None:
        """Wait for the exits still being worked (the worker ends each by the deadline).

        Each may first wait for the symbol's SELL lock (one worst-case exit + 10 s) and
        then run its own worst case, so that is how long it gets.
        """
        tasks = self._running_exits(positions)
        if tasks:
            logger.info("Waiting for %d exit(s) still being worked", len(tasks))
            settings = self._router.worker.settings
            await asyncio.wait(tasks, timeout=2 * settings.exit_worst_case_s
                               + settings.cancel_confirm_timeout_s + 20)
        self._reap(positions)

    async def _abandon_exits(self, positions: list[MonitoredPosition],
                             result: Optional[MonitorResult] = None) -> None:
        """Cancel exits still running rather than leave them unattended: the worker
        cancels their orders at the broker, alerts, and leaves what they sold unreported
        (registry and journal). With ``result`` (the loop ended normally) those orders are
        then resumed once, each one found final has its fills recorded as soon as its own
        resume ends; an order still working, or whose resume does not end in time (and any
        after a failure), is left unresolved for a later resync, CLOSING or
        ``skopaq monitor``."""
        tasks = self._running_exits(positions)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        settings = self._router.worker.settings
        await asyncio.wait(tasks, timeout=settings.cancel_confirm_timeout_s + 5)
        self._reap(positions)
        if result is None:
            return
        try:
            result.late_fills += await resume_orders(
                self._router, self._router.registry.unresolved(), self._on_late_fill,
                timeout=settings.cancel_confirm_timeout_s + 5, sleep=self._sleep,
                wall=self._wall if self._sleep is not None else None)
        except Exception:
            logger.warning("Recording the fills of abandoned exits failed — they are "
                           "journalled for the next monitor", exc_info=True)

    async def _shutdown_pass(self, positions: list[MonitoredPosition],
                             result: MonitorResult) -> None:
        """Stopped from the EOD exit on: sell what is still sellable, concurrently, sized
        from a fresh read (positions may lag a sale that already filled)."""
        await self._resync(positions, result)

        async def sell(pos: MonitoredPosition) -> None:
            try:
                ltp = await self._client.get_ltp(pos.scrip_code)
            except Exception:
                ltp = 0
            if ltp > 0 and await self._execute_sell(pos, ltp, "EOD exit (shutdown)", result):
                self._drop(positions, pos)

        await asyncio.gather(*(sell(p) for p in list(positions)
                               if p.exit_task is None and p.quantity > 0))

    async def _final_state(self, positions: list[MonitoredPosition],
                           result: MonitorResult) -> None:
        """What is left at the end: positions still held, orders still unresolved."""
        try:
            snap = await self._snapshot()
            held = held_positions(self._router, snap, own_order_ids(self._router))
            self._unshown = unshown_buys(self._router, snap)
            # One read showing nothing does not prove a tracked position gone (I9): only a
            # sale in the book beyond those already in its quantity does
            tracked = {p.symbol for p in positions
                       if (p.quantity > 0 or p.pending_exit)
                       and not self._sold_since(p, snap)}
            result.positions_left = sorted({h.symbol for h in held} | tracked
                                           | {u.symbol for u in self._unshown})
        except Exception:
            logger.warning("Final positions read failed — reporting the last known ones",
                           exc_info=True)
            result.positions_left = sorted({p.symbol for p in positions}
                                           | {u.symbol for u in self._unshown})
        self._uncertain = self._uncertain_placements()
        order_ids = sorted({t.order_id for t in self._router.registry.unresolved()}
                           | self._deferred)
        # Placements whose outcome is unknown have no order id: listed by what they were
        result.orders_unconfirmed = order_ids + sorted(self._uncertain)
        if not (result.positions_left or result.orders_unconfirmed):
            return
        text = (f"Positions still open: {', '.join(result.positions_left) or 'none'}; "
                f"orders unconfirmed: {', '.join(result.orders_unconfirmed) or 'none'}")
        logger.error("Monitor ending with %s", text)
        # Standalone (nothing after us will sell them): alerted once a day for the same
        # state, however many `skopaq monitor` processes end with it
        if self._sell_on_stop and self._first_today(text):
            get_alerter().alert(
                "CRITICAL", f"positions-left:{self._wall().date().isoformat()}:monitor",
                f"`skopaq monitor` ended with live positions or orders left. {text}. Check "
                "the broker: delivery (CNC) positions are carried overnight.",
                order_ids=order_ids,
            )

    def _first_today(self, text: str) -> bool:
        """No `skopaq monitor` on this host alerted this end state yet today (the journal
        directory holds the marker; without a journal, every process alerts)."""
        journal = self._router.journal
        if journal is None:
            return True
        digest = hashlib.sha1(text.encode()).hexdigest()[:16]
        return journal.once_today(f"positions-left-monitor-{digest}")

    @staticmethod
    def _sold_since(pos: MonitoredPosition, snap: BrokerSnapshot) -> bool:
        """A filled SELL in the book, beyond those already in the tracked quantity,
        covers the position."""
        covered = _filled_sells(snap, pos.symbol, pos.security_id) - pos.sells_seen
        return covered > 0 and covered >= pos.quantity

    async def _drain_alerts(self) -> None:
        drain = getattr(get_alerter(), "drain", None)
        if drain is None:
            return
        try:
            await drain()
        except Exception:
            logger.debug("Draining order alerts failed", exc_info=True)

    # ── Live resync ──────────────────────────────────────────────────────

    async def _snapshot(self) -> BrokerSnapshot:
        settings = self._router.worker.settings
        return await self._router.broker_snapshot(
            extra_terminal=settings.extra_terminal_statuses)

    async def _resync(self, positions: list[MonitoredPosition], result: MonitorResult, *,
                      first: bool = False) -> None:
        """Bring the tracked positions in line with the broker.

        1. Resume the orders nobody is working any more: cancel what still works, read the
           final state, record late fills.
        2. One book-first read; adopt an uncertain placement that has since appeared.
        3. Tracked positions take the quantity that can be sold now. One is dropped only
           after a successful read shows nothing held or pending twice in a row, or a
           filled SELL in the book covers it — never on a failed read or an unreadable book.
        4. Positions still held but not tracked are adopted.

        A failed read keeps every position as it is; the first read raises instead. Any
        other error in a later resync is logged: the loop keeps protecting positions.
        """
        if first:
            await self._resync_once(positions, result, first=True)
            return
        try:
            await self._resync_once(positions, result, first=False)
        except Exception:
            logger.exception("Resync failed — keeping every tracked position as it is")

    async def _resync_once(self, positions: list[MonitoredPosition], result: MonitorResult,
                           *, first: bool) -> None:
        self._adopt_journal()
        await self._resume_watched(positions, result)
        try:
            snap = await self._snapshot()
        except Exception:
            if first:
                raise
            logger.warning("Broker read failed — keeping every tracked position as it is",
                           exc_info=True)
            return
        if snap.book_error:
            logger.warning("Order book unreadable (%s): tracked quantities may only shrink "
                           "this cycle", snap.book_error)
        own = own_order_ids(self._router)
        if not snap.book_error:
            own |= self._adopt_uncertain(snap, own)
        self._uncertain = self._uncertain_placements()
        for pos in list(positions):
            self._resync_position(pos, positions, snap, own)
        await self._adopt_untracked(positions, snap, own, result)
        self._note_unshown(snap)

    def _note_unshown(self, snap: BrokerSnapshot) -> None:
        """Confirmed BUYs positions do not show yet: "watched" (a resync every poll) for
        the lag window, then logged at ERROR; the loop keeps running for them whatever
        their age (``_open_work``), until 15:31, and reports them as still held at the end."""
        lag = self._router.worker.settings.sell_fill_lag_window_s
        before = {u.symbol for u in self._unshown if u.age_s <= lag}
        self._unshown = unshown_buys(self._router, snap)
        for u in self._unshown:
            if u.age_s <= lag and u.symbol not in before:
                logger.warning("%s: %s bought (confirmed) but not in positions yet — waiting "
                               "for them", u.symbol, u.qty)
            elif u.age_s > lag and u.symbol in before:
                logger.error("%s: %s bought (confirmed) still not in positions after %.0fs",
                             u.symbol, u.qty, lag)

    def _adopt_journal(self) -> None:
        """Take over today's orders an earlier Skopaq process left open (its journal).

        A just-placed order may still be worked by the live process that placed it, so it
        is taken over only once no execute() could still be working it; until then it is
        watched (the loop stays alive, and it counts as unconfirmed if the loop ends).
        Today's confirmed BUY fills are noted too (positions may not show them yet).
        """
        journal = self._router.journal
        if journal is None:
            return
        settings = self._router.worker.settings
        still_worked_s = settings.exit_worst_case_s + settings.timeout_s
        registry = self._router.registry
        known = registry.ids()
        now = self._wall()
        take = []
        deferred: set[str] = set()
        for entry in journal.today_unresolved():
            order_id = entry.get("order_id")
            if not isinstance(order_id, str) or not order_id or order_id in known:
                continue
            placed = _journal_time(entry.get("ts"))
            if (entry.get("event") == "placed" and placed is not None
                    and (now - placed).total_seconds() < still_worked_s):
                deferred.add(order_id)
                continue
            take.append(entry)
        self._deferred = deferred
        for entry in journal.entries():
            filled = to_decimal(entry.get("filled"))
            when = _journal_time(entry.get("ts"))
            if (entry.get("event") == "final" and entry.get("side") == "BUY"
                    and filled and filled > 0 and when is not None and entry.get("order_id")):
                registry.record_confirmed_buy(
                    str(entry.get("symbol") or ""), str(entry.get("security_id") or ""),
                    filled, order_id=str(entry["order_id"]),
                    ago_s=max(0.0, (now - when).total_seconds()))
        if take:
            self._router.registry.load_journal(take)
            ids = [e["order_id"] for e in take]
            self._adopted.update(ids)
            logger.warning("Resuming %d order(s) an earlier Skopaq process left open: %s",
                           len(ids), ", ".join(ids))

    async def _resume_watched(self, positions: list[MonitoredPosition],
                              result: MonitorResult) -> None:
        """Resume the registry's unresolved orders — except one an exit task of ours is
        working right now. Each resume runs in the background (a cancel the broker does
        not confirm keeps one busy for the whole cancel window): the rule tiers keep their
        poll cadence meanwhile. A resume that ends within a second (an order already
        final) is reaped in this resync; the others as they end. ``resume_order`` skips
        an order another process has finished, or is resuming."""
        self._reap_resumes(result)
        busy = [p for p in positions if p.exit_task is not None and not p.exit_task.done()]
        started = []
        for tracked in self._router.registry.unresolved():
            if tracked.order_id in self._resumes:
                continue  # still being resumed
            adopted = tracked.order_id in self._adopted
            if tracked.state == "working" and not adopted and any(
                    same_instrument(p.symbol, p.security_id, tracked.symbol,
                                    tracked.security_id) for p in busy):
                continue
            task = asyncio.create_task(resume_order(self._router, tracked, self._on_late_fill))
            self._resumes[tracked.order_id] = task
            started.append(task)
        if started:
            await _wait(set(started), _QUICK_RESUME_S, sleep=self._sleep,
                        wall=self._wall if self._sleep is not None else None)
            self._reap_resumes(result)

    def _reap_resumes(self, result: MonitorResult) -> None:
        for order_id, task in list(self._resumes.items()):
            if not task.done():
                continue
            del self._resumes[order_id]
            if task.cancelled():
                continue
            if task.exception() is not None:
                logger.error("Resuming order %s failed: %s", order_id, task.exception())
                continue
            result.late_fills += task.result()

    async def _settle_resumes(self, result: MonitorResult) -> None:
        """Wait for resumes still running (each is bounded by its cancel window)."""
        tasks = [t for t in self._resumes.values() if not t.done()]
        if tasks:
            settings = self._router.worker.settings
            await _wait(set(tasks), settings.cancel_confirm_timeout_s + 5, sleep=self._sleep,
                        wall=self._wall if self._sleep is not None else None)
        self._reap_resumes(result)

    def _uncertain_placements(self) -> list[str]:
        """Our placements whose outcome is unknown (this process's, and today's journal),
        younger than the lag window: they may still show in the book, so they are watched
        and count as unconfirmed. Older ones were alerted when they happened."""
        lag = self._router.worker.settings.sell_fill_lag_window_s
        now = self._wall()
        found: dict[str, str] = {}
        for p in self._router.registry.uncertain():
            if (now - p.at).total_seconds() <= lag:
                found[p.internal_id] = f"uncertain {p.side} {p.qty} {p.symbol}"
        journal = self._router.journal
        if journal is not None:
            for record in journal.unresolved_uncertain():
                internal_id = str(record.get("internal_id") or "")
                when = _journal_time(record.get("ts"))
                if (internal_id and internal_id not in found and when is not None
                        and (now - when).total_seconds() <= lag):
                    found[internal_id] = (f"uncertain {record.get('side') or ''} "
                                          f"{record.get('qty') or ''} {record.get('symbol') or ''}")
        return list(found.values())

    def _adopt_uncertain(self, snap: BrokerSnapshot, own: set[str]) -> set[str]:
        """Look for our uncertain placements (this process's, and today's journal) in the
        book. Only those younger than the lag window: older ones no longer count and were
        alerted when they happened.

        An order carrying the placement's ``remarks`` tag is it: adopted like any order of
        ours (resumed, cancelled if still working, its fills recorded) and the placement
        is resolved. An order that only looks like it (``could_be_placement``: new since
        it was sent, the same instrument, side and quantity, created about then) may be
        someone else's — a user who sold by hand after the placement-uncertain alert, say
        — so it is only watched: its fills are recorded (with a CRITICAL alert to check
        them), it is never cancelled, and the placement keeps counting against the shares
        until the lag window ends. Returns the order ids adopted or watched.
        """
        journal = self._router.journal
        registry = self._router.registry
        lag = self._router.worker.settings.sell_fill_lag_window_s
        now = self._wall()
        mine = {p.internal_id: p for p in registry.uncertain()}
        records: list[tuple[UncertainPlacement, str, str]] = [
            (p, "exit" if p.side == "SELL" else "entry", "EQUITY") for p in mine.values()]
        if journal is not None:
            for record in journal.unresolved_uncertain():
                internal_id = str(record.get("internal_id") or "")
                qty = to_decimal(record.get("qty"))
                when = _journal_time(record.get("ts"))
                if not internal_id or internal_id in mine or qty is None or when is None:
                    continue
                side = str(record.get("side") or "")
                records.append((uncertain_from_journal(record, qty, when),
                                str(record.get("purpose")
                                    or ("exit" if side == "SELL" else "entry")),
                                str(record.get("segment") or "EQUITY")))
        if not records:
            return set()
        # Placements already resolved, or already matched to an order we watch
        matched = {t.internal_id for t in map(registry.get, registry.ids())
                   if t is not None and t.internal_id}
        if journal is not None:
            matched |= {e.get("internal_id") for e in journal.entries()
                        if e.get("order_id") and e.get("internal_id")}
        adopted: set[str] = set()
        for placement, purpose, segment in records:
            if (placement.internal_id in matched
                    or (now - placement.at).total_seconds() > lag):
                continue
            tagged = [o for o in snap.orders if placement.remark
                      and o.remarks == placement.remark and o.order_id not in own]
            if len(tagged) == 1:
                order, guessed = tagged[0], False
            else:
                looks = [o for o in snap.orders if o.order_id not in adopted
                         and could_be_placement(o, placement, own)]
                if len(looks) != 1:
                    continue
                order, guessed = looks[0], True
            self._take_over(order, placement, purpose, segment, guessed=guessed)
            if placement.internal_id not in mine:
                self._adopted.add(order.order_id)   # another process's: see _resume_watched
            adopted.add(order.order_id)
            matched.add(placement.internal_id)
        return adopted

    def _take_over(self, order: OrderSnapshot, placement: UncertainPlacement, purpose: str,
                   segment: str, *, guessed: bool) -> None:
        """Track (and journal) a book order found for an uncertain placement."""
        journal = self._router.journal
        registry = self._router.registry
        side, qty, symbol = placement.side, placement.qty, placement.symbol
        registry.track(TrackedOrder(
            order_id=order.order_id, side=side, symbol=symbol,
            security_id=placement.security_id, segment=segment, requested=qty,
            purpose=purpose, state="unknown", internal_id=placement.internal_id,
            guessed=guessed,
        ))
        status = order.status_raw or order.status
        if journal is not None:
            journal.record("placed", order_id=order.order_id,
                           internal_id=placement.internal_id, symbol=symbol,
                           security_id=placement.security_id, segment=segment, side=side,
                           qty=qty, purpose=purpose, status=status, guessed=guessed,
                           note=("looks like an uncertain placement (not proven ours)"
                                 if guessed else "uncertain placement found in the order book"))
        if not guessed:
            registry.resolve_uncertain(placement.internal_id)
            logger.warning("Uncertain %s %s %s placement found in the order book: order %s "
                           "(%s) — resuming it", side, qty, symbol, order.order_id, status)
            return
        sent = placement.at.astimezone(_IST)
        lag = self._router.worker.settings.sell_fill_lag_window_s
        until = (sent + timedelta(seconds=lag)).strftime("%H:%M")
        counted = (f" The {qty} shares count as sold until {until} IST either way."
                   if side == "SELL" else "")
        get_alerter().alert(
            "CRITICAL", f"placement-match:{placement.internal_id}",
            f"{side} {qty} {symbol} sent at {sent:%H:%M:%S} IST (outcome unknown) may be order "
            f"{order.order_id} ({status}): it matches by instrument, side, quantity and time "
            "only, so it may be someone else's. Skopaq watches it and records its fills as "
            f"its own, but never cancels it.{counted} Check the order book: cancel it there "
            "if it should not stay.",
            order_ids=[order.order_id])

    def _resync_position(self, pos: MonitoredPosition, positions: list[MonitoredPosition],
                         snap: BrokerSnapshot, own: set[str]) -> None:
        if pos.exit_task is not None:
            return  # its exit task owns it until the broker's answer is final
        view = sellable_view(self._router, snap, pos.symbol, pos.security_id, own)
        net = view.position_qty
        if snap.book_error:
            pos.quantity = int(max(_ZERO, min(Decimal(pos.quantity), net)))
            return
        sold_today = _filled_sells(snap, pos.symbol, pos.security_id)
        if net <= 0 and view.pending_qty == 0:
            pos.zero_reads += 1
            # Sales already in the tracked quantity (an earlier partial exit) prove nothing
            covered = self._sold_since(pos, snap)
            if covered or pos.zero_reads >= 2:
                self._drop(positions, pos)
                why = ("a filled SELL in the order book covers it" if covered
                       else "two reads in a row show nothing held")
                done = "; its exit completed at the broker" if pos.pending_exit else ""
                get_alerter().alert(
                    "WARNING", f"position-dropped:{pos.symbol}",
                    f"{pos.symbol} is no longer held at the broker ({why}){done} — no longer "
                    "monitored", dedup_s=_DEDUP_S)
                return
        else:
            pos.zero_reads = 0
            pos.sells_seen = max(pos.sells_seen, sold_today)   # the day's sales only grow
        # What the day's position still holds after our own open, unconfirmed and
        # not-yet-shown SELLs, capped by what the account can sell: older holdings of the
        # same stock never absorb a pending exit
        pos.quantity = int(max(_ZERO, view.position_sellable))
        # An uncertain SELL of ours, or one of ours the book does not list yet, may be
        # working too
        pos.pending_exit = (view.pending_qty > 0 or view.uncertain_qty > 0
                            or view.own_open_qty > 0)
        pos.stuck_orders = ([o.order_id for o in view.pending if o.order_id in own]
                            + list(view.own_open_ids))
        pos.blocked_by = [o for o in view.pending if o.order_id not in own]

    async def _adopt_untracked(self, positions: list[MonitoredPosition], snap: BrokerSnapshot,
                               own: set[str], result: MonitorResult) -> None:
        """Monitor every CNC position still held that is not tracked yet (at the start,
        and later: a BUY that filled late, or shares an earlier exit did not sell)."""
        for held in held_positions(self._router, snap, own, noted=self._noted):
            if any(same_instrument(p.symbol, p.security_id, held.symbol, held.security_id)
                   for p in positions):
                continue
            scrip_code = await self._scrip_code(held.symbol, held.security_id)
            if scrip_code is None:
                continue
            pending = () if snap.book_error else held.view.pending
            pos = MonitoredPosition(
                symbol=held.symbol,
                scrip_code=scrip_code,
                entry_price=held.row.average_price,
                # Unreadable book: the SELL itself re-checks open orders before it is sent
                quantity=int(held.net) if snap.book_error else held.sellable,
                security_id=held.security_id or scrip_code.split("_", 1)[-1],
                product=(held.row.product or "").upper(),
                pending_exit=(bool(pending) or held.view.uncertain_qty > 0
                              or held.view.own_open_qty > 0),
                stuck_orders=([o.order_id for o in pending if o.order_id in own]
                              + list(held.view.own_open_ids)),
                blocked_by=[o for o in pending if o.order_id not in own],
                sells_seen=_filled_sells(snap, held.symbol, held.security_id),
            )
            positions.append(pos)
            result.positions_monitored += 1
            registry = self._router.registry
            if any(t is not None and t.side == "BUY"
                   and same_instrument(t.symbol, t.security_id, pos.symbol, pos.security_id)
                   for t in map(registry.get, registry.ids())):
                logger.warning("%s: held from a BUY that was not confirmed at the time — now "
                               "monitored", pos.symbol)
            logger.info("Monitoring %s: %s held, %d sellable now (entry %.2f)", pos.symbol,
                        held.net, pos.quantity, pos.entry_price)

    async def _scrip_code(self, symbol: str, security_id: str) -> Optional[str]:
        from skopaq.broker.scrip_resolver import resolve_scrip_code

        try:
            return await resolve_scrip_code(self._client, symbol)
        except Exception:
            if security_id:
                return f"NSE_{security_id}"  # the scrip-codes format of the position's id
            logger.warning("Could not resolve scrip for %s — skipping", symbol, exc_info=True)
            return None

    # ── Discovery ────────────────────────────────────────────────────────

    async def _discover_positions(self) -> list[MonitoredPosition]:
        """Fetch open positions and resolve scrip codes."""
        from skopaq.broker.scrip_resolver import resolve_scrip_code

        raw_positions = await self._router.get_positions()
        monitored = []

        for pos in raw_positions:
            if pos.quantity <= 0:
                continue
            try:
                scrip_code = await resolve_scrip_code(self._client, pos.symbol)
            except Exception:
                logger.warning(
                    "Could not resolve scrip for %s — skipping",
                    pos.symbol, exc_info=True,
                )
                continue

            monitored.append(MonitoredPosition(
                symbol=pos.symbol,
                scrip_code=scrip_code,
                entry_price=pos.average_price,
                quantity=int(pos.quantity),
            ))

        return monitored

    # ── Safety Tier ──────────────────────────────────────────────────────

    def _check_safety(self, pos: MonitoredPosition, ltp: float) -> Optional[str]:
        """Check rule-based exit conditions.  Returns reason string or None."""

        # Hard stop-loss
        stop_price = pos.entry_price * (1 - self._hard_stop_pct)
        if ltp <= stop_price:
            return (
                f"HARD STOP: LTP ₹{ltp:.2f} <= stop ₹{stop_price:.2f} "
                f"({self._hard_stop_pct:.0%} below entry)"
            )

        # Trailing stop
        if self._trailing_enabled and pos.high_water_mark > pos.entry_price:
            trail_stop = pos.high_water_mark * (1 - self._trailing_pct)
            if ltp <= trail_stop:
                return (
                    f"TRAILING STOP: LTP ₹{ltp:.2f} <= trail ₹{trail_stop:.2f} "
                    f"(HWM ₹{pos.high_water_mark:.2f})"
                )

        # EOD exit
        if self._should_eod_exit():
            return (
                f"EOD EXIT: {self._eod_minutes} minutes before market close"
            )

        return None

    def _should_eod_exit(self) -> bool:
        """Check if current IST time is past the EOD exit threshold."""
        now_ist = self._wall().time()
        close_dt = datetime.combine(datetime.today(), _MARKET_CLOSE)
        exit_dt = close_dt - timedelta(minutes=self._eod_minutes)
        return now_ist >= exit_dt.time()

    # ── AI Tier ──────────────────────────────────────────────────────────

    async def _check_ai(
        self, pos: MonitoredPosition, ltp: float, pnl_pct: float,
    ) -> Optional[SellDecision]:
        """Invoke the sell analyst LLM.  Returns SellDecision or None on error."""
        if not self._llm:
            return None

        trade_date = datetime.now(_IST).strftime("%Y-%m-%d")

        logger.info("[%s] Running AI sell analysis...", pos.symbol)
        decision = await analyze_exit(
            llm=self._llm,
            symbol=pos.symbol,
            entry_price=pos.entry_price,
            current_price=ltp,
            quantity=pos.quantity,
            position_pnl_pct=pnl_pct,
            trade_date=trade_date,
            min_profit_threshold_pct=self._min_profit_pct,
            estimated_round_trip_brokerage=self._est_brokerage,
        )

        logger.info(
            "[%s] AI decision: %s (confidence=%d%%) — %s",
            pos.symbol, decision.action, decision.confidence, decision.reasoning,
        )
        return decision

    # ── Execution ────────────────────────────────────────────────────────

    async def _record_exit(self, signal: TradingSignal, exec_result: Any) -> None:
        if self._on_exit is None:
            return
        try:
            await self._on_exit(signal, exec_result)
        except Exception:
            logger.warning("Recording the exit of %s failed", signal.symbol, exc_info=True)

    async def _execute_sell(
        self,
        pos: MonitoredPosition,
        ltp: float,
        reason: str,
        result: MonitorResult,
    ) -> bool:
        """Build a SELL signal and route through the executor pipeline.

        Returns True once the position is fully sold. Live, only the quantity the broker
        confirmed counts: after a partial exit the rest stays tracked with the reason in
        ``exit_intent`` (sold next cycle), and an exit that may still be working marks the
        position ``pending_exit`` so the resync resumes it instead of selling it blind.
        """
        logger.info(
            "SELLING %s qty=%d — %s",
            pos.symbol, pos.quantity, reason,
        )

        # MARKET: a LIMIT at the entry price never fills once a stop-loss has
        # fired below it. entry_price carries the LTP as the fill estimate.
        signal = TradingSignal(
            symbol=pos.symbol,
            action="SELL",
            confidence=80,
            entry_price=ltp,
            order_type=OrderType.MARKET,
            quantity=Decimal(pos.quantity),
            reasoning=reason,
            # Live: re-checked under the SELL lock against what the day's position still
            # holds (older holdings of the same stock are never sold by an exit)
            position_only=self._live,
        )

        try:
            # Inject quote for paper mode
            if self._config.trading_mode == "paper":
                from skopaq.broker.models import Quote
                paper_engine = self._router._paper  # noqa: SLF001
                paper_engine.update_quote(Quote(
                    symbol=pos.symbol,
                    ltp=ltp,
                    bid=ltp * 0.999,
                    ask=ltp * 1.001,
                ))

            exec_result = await self._executor.execute_signal(signal)
            # Live: what the broker confirmed; paper (and mocked results): the order
            filled = int(filled_quantity_of(exec_result, pos.quantity))

            if exec_result.success:
                if self._live:
                    # The broker confirmed it: recording it runs to the end even if this
                    # exit task is cancelled meanwhile (nothing else would record it)
                    await self._router.registry.shielded(self._record_exit(signal, exec_result))
                else:
                    await self._record_exit(signal, exec_result)
                # Live: the broker's average fill; paper: the LTP, as always
                price = (exec_result.fill_price
                         if exec_result.mode == "live" and exec_result.fill_price else ltp)
                pnl = (price - pos.entry_price) * filled
                result.sells_executed += 1
                result.total_pnl += pnl
                result.exit_reasons.append(f"{pos.symbol}: {reason}")
                logger.info(
                    "SOLD %s — fill=₹%.2f  P&L=₹%.2f",
                    pos.symbol,
                    exec_result.fill_price or ltp,
                    pnl,
                )
                # Notify via Telegram
                try:
                    from skopaq.notifications import notify_position_alert

                    alert_type = "TRAILING_STOP" if "trail" in reason.lower() else \
                                 "EOD_EXIT" if "eod" in reason.lower() else "TARGET_NEAR"
                    asyncio.get_running_loop().create_task(
                        notify_position_alert(pos.symbol, ltp, pos.entry_price, pnl, alert_type)
                    )
                except Exception:
                    pass
                before = pos.quantity
                pos.quantity = max(0, pos.quantity - filled)
                pos.sells_seen += filled   # already out of the quantity: proves no drop
                pos.exit_intent = reason if pos.quantity > 0 else ""
                if pos.quantity > 0:
                    logger.warning("PARTIAL exit of %s: sold %d of %d — the rest is sold "
                                   "next cycle", pos.symbol, filled, before)
                if is_unconfirmed(exec_result):
                    pos.pending_exit = True
                return pos.quantity <= 0
            else:
                result.sells_failed += 1
                logger.error(
                    "SELL REJECTED for %s: %s",
                    pos.symbol, exec_result.rejection_reason,
                )
                if is_unconfirmed(exec_result):
                    pos.pending_exit = True  # the resync resumes it; never re-sold blind
                if self._live and exec_result.safety_passed is False:
                    # Refused before the broker (the Executor alerted sell-refused)
                    _add_once(result.exits_blocked, pos.symbol)
                return False

        except Exception:
            result.sells_failed += 1
            logger.error(
                "SELL FAILED for %s", pos.symbol, exc_info=True,
            )
            return False
