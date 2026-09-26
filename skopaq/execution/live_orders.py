"""Live order confirmation: place, confirm the fill, cancel or re-place — never assume.

INDstocks' POST /order only acknowledges an order; whether it fills is decided at the
exchange later. ``LiveOrderWorker`` turns one ``OrderRequest`` into an ``ExecutionResult``
backed by broker evidence (the filled quantity and average price the broker reports):

- Entries (BUYs, and LIMIT SELLs) get one order. Whatever has not filled within
  ``SKOPAQ_ORDER_FILL_TIMEOUT_SECONDS`` is cancelled, and only the filled part counts.
- Protective exits (MARKET SELLs) are worked until filled: a resting order is cancelled
  and the remainder re-placed as a tick-rounded marketable LIMIT, a bounded number of
  times, within the shutdown/close deadline. Otherwise they end in a CRITICAL alert.
- An order whose outcome is unknown is never re-sent blind. An uncertain placement is
  looked up in the order book by new order ids; an order still working after its cancel
  window is registered, journalled and alerted, and resumed later by the monitor/CLOSING.

``OrderRouter`` builds the worker only in live mode with a live client, so paper orders
never reach it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from datetime import time as dtime
from decimal import ROUND_FLOOR, Decimal
from enum import StrEnum
from typing import Any, Awaitable, Callable, ClassVar, Iterable, Optional
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from skopaq.broker.book_snapshot import read_broker_snapshot
from skopaq.broker.client import BrokerError, OrderPlacementUncertain
from skopaq.broker.models import (
    CancelOrderRequest,
    ExecutionResult,
    OrderRequest,
    OrderResponse,
    OrderType,
    Side,
    TradingSignal,
)
from skopaq.broker.order_status import (
    TERMINAL_STATES,
    FillSummary,
    OrderSnapshot,
    OrderState,
    RejectKind,
    classify_rejection,
    find_order,
    is_known_status,
    normalise_status,
    parse_fills,
    parse_order_book,
    parse_order_row,
    to_decimal,
)
from skopaq.broker.scrip_resolver import cached_tick_size, resolve_tick_size
from skopaq.constants import NSE_MARKET_CLOSE
from skopaq.execution.order_alerts import get_alerter
from skopaq.execution.order_journal import OrderJournal
from skopaq.execution.sellable import (
    SKEW_ALLOWANCE_S,
    OwnOpenSell,
    SellableView,
    SellContext,
    UncertainPlacement,
    same_instrument,
    sellable_quantity,
)
from skopaq.risk.calendar import IST, now_ist

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
Wall = Callable[[], datetime]

_ZERO = Decimal("0")
_DEDUP_S = 600.0             # per-symbol exit alerts: at most one per 10 minutes
_DEADLINE = "the shutdown/close deadline"
_MIN_CALL_S = 0.5            # a broker call after a stop gets at least this long
_TICK_LOOKUP_S = 2.0         # a re-price waits at most this long for a tick-size download
_INT = TypeAdapter(int)


# ── Settings ─────────────────────────────────────────────────────────────────

# (field, config attribute, low, high, whole number); the env var is SKOPAQ_<ATTRIBUTE>
_NUMERIC_FIELDS = (
    ("timeout_s", "order_fill_timeout_seconds", 5.0, 120.0, False),
    ("poll_interval_s", "order_fill_poll_interval_seconds", 0.5, 5.0, False),
    ("cancel_confirm_timeout_s", "order_cancel_confirm_timeout_seconds", 3.0, 30.0, False),
    ("exit_attempt_timeout_s", "order_exit_attempt_timeout_seconds", 3.0, 30.0, False),
    ("exit_max_attempts", "order_exit_max_attempts", 1, 5, True),
    ("exit_reprice_buffer_pct", "order_exit_reprice_buffer_pct", 0.1, 2.0, False),
    ("reconcile_timeout_s", "order_reconcile_timeout_seconds", 5.0, 30.0, False),
    ("sell_fill_lag_window_s", "order_sell_fill_lag_window_seconds", 60.0, 1800.0, False),
)


def _number(value: object, attr: str = "") -> Optional[float]:
    """An int/float config value; None for bools, mocks, NaN/inf and anything else.

    A non-finite number is logged (naming SKOPAQ_<ATTR>) before it is ignored.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        if attr:
            logger.warning("SKOPAQ_%s=%s is not a finite number; using the default",
                           attr.upper(), value)
        return None
    return float(value)


@dataclass(frozen=True)
class FillSettings:
    """How long to wait for a fill, and how hard to work a protective exit.

    ``from_config`` clamps every value to its range and shortens exits until one exit's
    worst case fits in half the shutdown budget (an in-flight monitor exit plus one
    CLOSING pass must end before the scheduler's SIGKILL).
    """

    timeout_s: float = 30.0                 # entries: then the rest is cancelled
    poll_interval_s: float = 1.0
    cancel_confirm_timeout_s: float = 10.0  # retry the cancel and re-read this long
    exit_attempt_timeout_s: float = 10.0    # protective exits, per attempt
    exit_max_attempts: int = 3
    exit_reprice_buffer_pct: float = 0.5    # re-placed LIMIT: LTP × (1 − buf% × (attempt − 1))
    reconcile_timeout_s: float = 15.0       # look for an uncertain placement this long
    sell_fill_lag_window_s: float = 600.0
    shutdown_budget_s: float = 240.0        # shutdown_budget_seconds(config)
    extra_terminal_statuses: frozenset[str] = frozenset()
    remarks_enabled: bool = False

    CANCEL_RETRY_INTERVAL_S: ClassVar[float] = 2.0
    REPRICE_TOTAL_CAP_PCT: ClassVar[float] = 5.0
    RATE_LIMIT_BACKOFF_CAP_S: ClassVar[float] = 15.0   # per exit, across its attempts
    ATTEMPT_OVERHEAD_S: ClassVar[float] = 3.0          # sellable re-read + LTP + placement

    @property
    def exit_worst_case_s(self) -> float:
        """attempts × (attempt + cancel window + overhead) + reconcile + backoff cap."""
        return (self.exit_max_attempts * (self.exit_attempt_timeout_s
                                          + self.cancel_confirm_timeout_s
                                          + self.ATTEMPT_OVERHEAD_S)
                + self.reconcile_timeout_s + self.RATE_LIMIT_BACKOFF_CAP_S)

    @classmethod
    def from_config(cls, config: object) -> FillSettings:
        """Settings from ``SkopaqConfig`` (a MagicMock config gives the defaults)."""
        values: dict[str, Any] = {}
        for name, attr, low, high, whole in _NUMERIC_FIELDS:
            raw = _number(getattr(config, attr, None), attr)
            if raw is None:
                continue
            value = min(max(raw, low), high)
            if value != raw:
                logger.warning("SKOPAQ_%s=%s is outside [%s, %s]; using %s",
                               attr.upper(), raw, low, high, value)
            values[name] = int(value) if whole else value
        extra = getattr(config, "order_extra_terminal_statuses", "")
        if isinstance(extra, str):
            declared = [s for s in (normalise_status(part) for part in extra.split(",")) if s]
            # Only statuses Skopaq does not recognise: declaring PENDING or PARTIALLY
            # FILLED final would hide open SELLs from the no-short-sale check
            known = [s for s in declared if is_known_status(s)]
            if known:
                logger.warning("SKOPAQ_ORDER_EXTRA_TERMINAL_STATUSES: ignoring %s (statuses "
                               "Skopaq already knows the meaning of)", ", ".join(known))
            values["extra_terminal_statuses"] = frozenset(
                s for s in declared if not is_known_status(s))
        values["remarks_enabled"] = getattr(
            config, "indstocks_order_remarks_enabled", False) is True
        values["shutdown_budget_s"] = shutdown_budget_seconds(config)
        return cls(**values)._fitted()

    def _fitted(self) -> FillSettings:
        """Shorten the attempts (floor 3 s), then use fewer (floor 1), until one exit's
        worst case fits in half the shutdown budget."""
        half = self.shutdown_budget_s / 2
        if self.exit_worst_case_s <= half:
            return self
        fixed = self.reconcile_timeout_s + self.RATE_LIMIT_BACKOFF_CAP_S
        per_attempt = self.cancel_confirm_timeout_s + self.ATTEMPT_OVERHEAD_S
        attempts = self.exit_max_attempts
        attempt_s = math.floor(((half - fixed) / attempts - per_attempt) * 10) / 10
        if attempt_s < 3.0:
            attempt_s = 3.0
            while attempts > 1 and attempts * (attempt_s + per_attempt) + fixed > half:
                attempts -= 1
        fitted = dataclasses.replace(self, exit_attempt_timeout_s=attempt_s,
                                     exit_max_attempts=attempts)
        logger.warning(
            "Protective exits shortened to fit half the %.0fs shutdown budget: %d attempt(s) "
            "of %.1fs (configured %d of %.1fs); worst case %.0fs",
            self.shutdown_budget_s, attempts, attempt_s, self.exit_max_attempts,
            self.exit_attempt_timeout_s, fitted.exit_worst_case_s,
        )
        if fitted.exit_worst_case_s > half:
            logger.warning(
                "One exit's worst case (%.0fs) still exceeds half the shutdown budget (%.0fs): "
                "the deadline after a stop will cut exits short (raise "
                "SKOPAQ_SCHEDULER_KILL_AFTER_SECONDS)", fitted.exit_worst_case_s, half)
        return fitted


def shutdown_budget_seconds(config: object) -> float:
    """How long order work may continue after a stop: kill-after − margin, at least 30 s,
    and always at least 5 s before the SIGKILL (a kill-after below 35 s gets less).

    kill-after is ``SKOPAQ_SCHEDULER_KILL_AFTER_SECONDS`` parsed as the scheduler parses it
    (a whole number; invalid or missing → 300). The margin, left for REPORTING, is
    ``SKOPAQ_ORDER_SHUTDOWN_MARGIN_SECONDS`` clamped to [20, 120] (default 60); the stated
    margin holds only when kill-after is at least margin + 30 s.
    """
    kill_after = 300
    raw = getattr(config, "scheduler_kill_after_seconds", None)
    if isinstance(raw, (str, int)) and not isinstance(raw, bool):   # never a mock's __int__
        try:
            number = _INT.validate_python(raw)
        except ValidationError:
            number = 0
        if number >= 1:
            kill_after = number
    attr = "order_shutdown_margin_seconds"
    configured = _number(getattr(config, attr, None), attr)
    margin = 60.0 if configured is None else min(max(configured, 20.0), 120.0)
    if configured is not None and margin != configured:
        logger.warning("SKOPAQ_%s=%s is outside [20, 120]; using %s", attr.upper(),
                       configured, margin)
    return min(max(30.0, kill_after - margin), max(1.0, kill_after - 5.0))


# ── Deadlines ────────────────────────────────────────────────────────────────


class OrderDeadlines:
    """When live order work must stop: the NSE close always, and a shutdown budget once armed.

    ``place_by``: no new order is placed after it (5 s before 15:30 IST, or earlier after a
    stop). ``settle_by``: no polling or cancelling after it; an order still working then
    is reported ``remaining_open`` with a CRITICAL alert. Monotonic seconds.
    """

    def __init__(self, *, clock: Clock = time.monotonic, wall: Wall = now_ist,
                 close: dtime = NSE_MARKET_CLOSE, close_margin_s: float = 5.0) -> None:
        self._clock = clock
        self._wall = wall
        self._close = close
        self._close_margin_s = close_margin_s
        self._settle_by: Optional[float] = None
        self._armed_place_by: Optional[float] = None

    def arm_shutdown(self, budget_s: float, reserve_s: float) -> None:
        """Start the shutdown budget now (idempotent: the first call wins).

        ``reserve_s`` is kept free before ``settle_by`` to cancel and settle the last order.
        """
        if self._settle_by is not None:
            return
        self._settle_by = self._clock() + budget_s
        self._armed_place_by = self._settle_by - reserve_s
        logger.warning("Order work stops in %.0fs (no new orders after %.0fs)",
                       budget_s, budget_s - reserve_s)

    @property
    def stopping(self) -> bool:
        return self._settle_by is not None

    def place_by(self) -> float:
        now = self._wall()
        now = now.replace(tzinfo=IST) if now.tzinfo is None else now.astimezone(IST)
        close = now.replace(hour=self._close.hour, minute=self._close.minute,
                            second=self._close.second, microsecond=0)
        close_mono = self._clock() + (close - now).total_seconds()
        armed = math.inf if self._armed_place_by is None else self._armed_place_by
        return min(close_mono - self._close_margin_s, armed)

    def settle_by(self) -> float:
        return math.inf if self._settle_by is None else self._settle_by

    def can_place(self, need_s: float = 0.0) -> bool:
        return self._clock() + need_s <= self.place_by()


# ── Results of the steps ─────────────────────────────────────────────────────


class OrderOutcome(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    OPEN = "open"            # still working when we stopped watching
    UNKNOWN = "unknown"      # never read, or placement uncertain
    NOT_PLACED = "not_placed"


_OUTCOME_OF_STATE = {
    OrderState.FILLED: OrderOutcome.FILLED,
    OrderState.PARTIAL_DONE: OrderOutcome.PARTIAL,
    OrderState.CANCELLED: OrderOutcome.CANCELLED,
    OrderState.REJECTED: OrderOutcome.REJECTED,
}


@dataclass
class Placement:
    """What POST /order (plus reconciliation) established."""

    order_id: Optional[str]
    status: str = ""
    message: str = ""
    rejected: bool = False
    reject_kind: Optional[RejectKind] = None
    uncertain: bool = False
    candidates: tuple[str, ...] = ()   # uncertain and ambiguous: the possible order ids
    reconciled: bool = False
    deadline: bool = False             # not placed: past place_by


@dataclass
class Confirmation:
    """One broker order's final view (or the last view of one still working)."""

    outcome: OrderOutcome
    order_id: str
    requested_qty: Decimal
    filled_qty: Optional[Decimal]      # None: the broker did not say
    avg_price: Optional[Decimal]
    price_source: str                  # trades | order | trade_book | ""
    status: str
    status_raw: str
    message: str                       # the broker's extra_info
    may_be_open: bool                  # stuck, unreadable or cut by the deadline
    exch_order_id: str = ""
    cancel_sent: bool = False          # we cancelled it (a timeout), not the broker
    note: str = ""                     # Skopaq's own reason, when there is one
    order_price: Optional[float] = None   # the order's limit price (fill estimate)


@dataclass
class TrackedOrder:
    """A live order this process placed or adopted, until it is final."""

    order_id: str
    side: str
    symbol: str
    security_id: str
    segment: str
    requested: Decimal
    filled_reported: Decimal = _ZERO   # what an ExecutionResult already reported as filled
    avg_price_reported: Optional[Decimal] = None
    purpose: str = "entry"             # entry | exit
    placed_mono: float = 0.0
    entry_deadline: Optional[float] = None   # entries: cancel once this passes
    state: str = "working"             # working | stuck | unknown | interrupted | final
    signal: Optional[TradingSignal] = None   # to record a late exit fill
    internal_id: str = ""
    price: Optional[float] = None      # its limit price (a late fill's estimate)
    # Matched to an uncertain placement of ours by its look alone (instrument, side,
    # quantity, time): maybe someone else's, so it is watched but never cancelled
    guessed: bool = False
    # A read found it final at the broker. It stays so even when ``state`` goes back to
    # "interrupted" (its fills still to be recorded): its unfilled rest is not open
    final_seen: bool = False
    # Booking its progress at this total failed here: not retried at the same total (more
    # progress, or the final fill, books it), so a write that keeps failing half-way does
    # not add a trade row every resync
    unbooked_at: Optional[Decimal] = None


class OrderRegistry:
    """This process's live orders and confirmed exits (one per router).

    The daemon's executor, monitor and CLOSING share one router, so they share this: the
    monitor resumes stuck orders, and sellable checks count this process's confirmed
    exits that the broker's positions do not show yet.
    """

    def __init__(self, *, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._orders: dict[str, TrackedOrder] = {}
        # [when, symbol, security id, qty, order id]: an order's entry holds its total
        self._exits: list[list] = []
        self._buys: list[list] = []                            # the same, for BUY fills
        self._uncertain: dict[str, UncertainPlacement] = {}   # by internal id
        self._recordings: set[asyncio.Task] = set()           # see shielded()
        self.resuming: set[str] = set()   # order ids a resume of this process is working
        # Order ids whose late fill this process is booking (it may outlive its resume)
        self.booking: set[str] = set()

    def track(self, tracked: TrackedOrder) -> None:
        self._orders[tracked.order_id] = tracked

    def get(self, order_id: str) -> Optional[TrackedOrder]:
        return self._orders.get(order_id)

    def ids(self) -> frozenset[str]:
        return frozenset(self._orders)

    def mark_stuck(self, order_id: str, conf: Confirmation) -> None:
        tracked = self._orders.get(order_id)
        if tracked is not None:
            tracked.state = "stuck" if conf.outcome is OrderOutcome.OPEN else "unknown"

    def mark_final(self, order_id: str, conf: Confirmation) -> None:
        tracked = self._orders.get(order_id)
        if tracked is not None:
            tracked.state = "final"
            tracked.final_seen = True

    def note_reported(self, order_id: str, conf: Confirmation) -> None:
        """Record what the caller was told was filled (a later fill is the difference)."""
        tracked = self._orders.get(order_id)
        if tracked is not None and conf.filled_qty is not None:
            tracked.filled_reported = conf.filled_qty
            tracked.avg_price_reported = conf.avg_price

    def unresolved(self) -> list[TrackedOrder]:
        return [t for t in self._orders.values() if t.state != "final"]

    def age_s(self, tracked: TrackedOrder) -> float:
        """Seconds since ``tracked`` was placed (or adopted) by this process."""
        return max(0.0, self._clock() - tracked.placed_mono)

    def confirmed_exit_of(self, order_id: str) -> Decimal:
        """Shares this process confirmed ``order_id`` (a SELL) sold so far."""
        return max((entry[3] for entry in self._exits if entry[4] == order_id),
                   default=_ZERO)

    def record_confirmed_exit(self, symbol: str, security_id: str, qty: Decimal,
                              at: Optional[float] = None, *, order_id: str = "") -> None:
        """Shares a SELL of ours sold. With ``order_id``, ``qty`` is that order's total so
        far (a late fill of an order already counted replaces its entry, never adds)."""
        self._record(self._exits, symbol, security_id, qty, at, order_id)

    def record_confirmed_buy(self, symbol: str, security_id: str, qty: Decimal,
                             at: Optional[float] = None, *, order_id: str = "",
                             ago_s: float = 0.0) -> None:
        """Shares a BUY of ours bought (the broker confirmed them ``ago_s`` seconds ago);
        like ``record_confirmed_exit``. The monitor and CLOSING compare these with
        positions, which can lag behind a fill."""
        if at is None and ago_s:
            at = self._clock() - ago_s
        self._record(self._buys, symbol, security_id, qty, at, order_id)

    def confirmed_buys(self) -> list[tuple[float, str, str, Decimal]]:
        """Today's confirmed BUY fills: (seconds ago, symbol, security id, quantity)."""
        now = self._clock()
        return [(now - at, sym, sid, qty) for at, sym, sid, qty, _ in self._buys]

    def _record(self, entries: list[list], symbol: str, security_id: str, qty: Decimal,
                at: Optional[float], order_id: str) -> None:
        if qty <= 0:
            return
        when = self._clock() if at is None else at
        if order_id:
            for entry in entries:
                if entry[4] == order_id:
                    entry[0], entry[3] = when, max(entry[3], qty)
                    return
        entries.append([when, symbol, security_id, qty, order_id])

    def recent_exit_qty(self, symbol: str, security_id: str, window_s: float) -> Decimal:
        """Shares of this instrument this process confirmed sold within ``window_s``."""
        return sum((qty for _, qty in self.recent_exits(symbol, security_id, window_s)),
                   start=_ZERO)

    def recent_exits(self, symbol: str, security_id: str,
                     window_s: float) -> list[tuple[str, Decimal]]:
        """(order id, shares sold) of this process's confirmed exits of the instrument
        within ``window_s`` ("" for an exit recorded without its order id)."""
        now = self._clock()
        return [(order_id, qty) for at, sym, sid, qty, order_id in self._exits
                if now - at <= window_s and same_instrument(symbol, security_id, sym, sid)]

    def record_uncertain(self, placement: UncertainPlacement) -> None:
        """An order of ours that may exist although no order id came back."""
        self._uncertain[placement.internal_id] = placement

    def resolve_uncertain(self, internal_id: str) -> None:
        """The uncertain placement was found in the order book (it is tracked now)."""
        self._uncertain.pop(internal_id, None)

    def uncertain(self) -> list[UncertainPlacement]:
        return list(self._uncertain.values())

    async def shielded(self, coro: Awaitable[Any]) -> Any:
        """Await ``coro`` to the end even if the caller is cancelled meanwhile.

        For recording a fill the broker already confirmed (a trade row, an exit): once
        the order is final in the registry nothing in this process would record it
        again, and a late fill's booking is confirmed in the journal (``booked``) only
        when it ends. A cancelled caller still gets ``CancelledError``; the recording goes
        on in the background and ``drain_recordings`` waits for it.
        """
        task = asyncio.ensure_future(coro)
        self._recordings.add(task)
        task.add_done_callback(self._recordings.discard)
        return await asyncio.shield(task)

    async def drain_recordings(self, timeout: float = 30.0) -> None:
        """Wait up to ``timeout`` seconds for recordings still running (at the end)."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        pending = [t for t in self._recordings if not t.done() and t.get_loop() is loop]
        if not pending:
            return
        _, still = await asyncio.wait(pending, timeout=timeout)
        if still:
            logger.error("%d fill recording(s) still running after %.0fs", len(still),
                         timeout)

    def load_journal(self, entries: Iterable[dict]) -> int:
        """Adopt today's own unresolved orders from the journal (``skopaq monitor``).

        The process that placed them is gone, so entries are due for cancelling at once.
        What was already reported for an order is the line's ``reported`` and
        ``reported_avg`` when it has them (an interrupted order: nothing; a resumed order
        still working: what has been recorded of it), else its ``filled`` and
        ``avg_price``. A final order is adopted only when its late fill is ``unbooked``
        (a booking was started and never confirmed): final at the broker, its fill still
        to be booked.
        Returns how many were adopted.
        """
        adopted = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            unbooked = entry.get("event") == "final" and entry.get("unbooked") is True
            if entry.get("event") == "final" and not unbooked:
                continue
            order_id = entry.get("order_id")
            if not isinstance(order_id, str) or not order_id or order_id in self._orders:
                continue
            purpose = str(entry.get("purpose") or "entry")
            if entry.get("reported") is not None:
                reported = to_decimal(entry.get("reported")) or _ZERO
                avg_reported = to_decimal(entry.get("reported_avg")) if reported else None
            else:
                reported = to_decimal(entry.get("filled")) or _ZERO
                avg_reported = to_decimal(entry.get("avg_price")) if reported else None
            self.track(TrackedOrder(
                order_id=order_id,
                side=str(entry.get("side") or ""),
                symbol=str(entry.get("symbol") or ""),
                security_id=str(entry.get("security_id") or ""),
                segment=str(entry.get("segment") or "EQUITY"),
                requested=to_decimal(entry.get("qty")) or _ZERO,
                filled_reported=reported,
                avg_price_reported=avg_reported,
                purpose=purpose,
                placed_mono=self._clock(),
                entry_deadline=self._clock() if purpose == "entry" else None,
                state=("interrupted" if unbooked
                       else "stuck" if entry.get("event") in ("stuck", "interrupted")
                       else "working"),
                internal_id=str(entry.get("internal_id") or ""),
                guessed=entry.get("guessed") is True,
                final_seen=unbooked,
            ))
            adopted += 1
        return adopted


def recent_exit_qty(registry: OrderRegistry, journal: Optional[OrderJournal], symbol: str,
                    security_id: str, window_s: float, now: datetime) -> Decimal:
    """Shares of the instrument Skopaq's confirmed SELLs sold within ``window_s``: this
    process's (its registry) and every Skopaq process's on this host (today's journal:
    each SELL order's latest line with a fill). The order book can lag behind a fill as
    positions can; without the journal a second process would sell those shares again.
    An order counted by both is counted once (the larger quantity)."""
    by_order: dict[str, Decimal] = {}
    unnamed = _ZERO
    for order_id, qty in registry.recent_exits(symbol, security_id, window_s):
        if order_id:
            by_order[order_id] = max(by_order.get(order_id, _ZERO), qty)
        else:
            unnamed += qty
    if journal is not None:
        for order_id, line in journal.latest_by_order().items():
            filled = to_decimal(line.get("filled"))
            when = _journal_time(line.get("ts"))
            if (line.get("side") != "SELL" or not filled or filled <= 0 or when is None
                    or line.get("event") not in ("final", "stuck", "interrupted")
                    or (now - when).total_seconds() > window_s
                    or not same_instrument(symbol, security_id, str(line.get("symbol") or ""),
                                           str(line.get("security_id") or ""))):
                continue
            by_order[order_id] = max(by_order.get(order_id, _ZERO), filled)
    return unnamed + sum(by_order.values(), start=_ZERO)


def uncertain_sells(registry: OrderRegistry,
                    journal: Optional[OrderJournal]) -> tuple[UncertainPlacement, ...]:
    """Our SELL placements whose outcome is still unknown: this process's, and the ones
    today's journal records without a later line naming their order."""
    found = {p.internal_id: p for p in registry.uncertain() if p.side == "SELL"}
    if journal is not None:
        for record in journal.unresolved_uncertain():
            internal_id = str(record.get("internal_id") or "")
            qty = to_decimal(record.get("qty"))
            when = _journal_time(record.get("ts"))
            if (not internal_id or internal_id in found or record.get("side") != "SELL"
                    or qty is None or when is None):
                continue
            found[internal_id] = uncertain_from_journal(record, qty, when)
    return tuple(found.values())


def own_open_sells(registry: OrderRegistry, journal: Optional[OrderJournal],
                   now: datetime) -> tuple[OwnOpenSell, ...]:
    """Our SELL orders not known to be final — this process's unresolved ones (stuck,
    unknown, interrupted or still being worked) and this host's (today's journal: the
    latest line is not ``final``) — each with its open remainder (requested less the most
    any process has seen filled) and when it was placed.

    Known to be final, and left out: an order a read of this process found final (even
    one whose exit was interrupted afterwards, ``final_seen``), and one today's journal
    says is final (another process finished it), or interrupted in a final status.

    ``sellable_quantity`` counts the ones the order book does not list yet, for the lag
    window — or whatever their age when a read of this process found them still working
    (``seen_working``): the book can lag behind an accepted order, and without them the
    next SELL would sell the same shares again.
    """
    found: dict[str, OwnOpenSell] = {}
    final_here = set()
    for order_id in registry.ids():
        tracked = registry.get(order_id)
        if tracked is None or tracked.side != "SELL":
            continue
        if tracked.state == "final" or tracked.final_seen:
            final_here.add(order_id)
            continue
        filled = max(tracked.filled_reported, registry.confirmed_exit_of(order_id))
        found[order_id] = OwnOpenSell(
            order_id=order_id, symbol=tracked.symbol, security_id=tracked.security_id,
            qty=max(_ZERO, tracked.requested - filled),
            at=now - timedelta(seconds=registry.age_s(tracked)),
            # stuck: a read found it working; interrupted (not final): a resume of it was
            # cut short, so nothing says it stopped working
            seen_working=tracked.state in ("stuck", "interrupted"))
    if journal is None:
        return tuple(found.values())
    for order_id, line in journal.latest_by_order().items():
        if _journal_final(order_id, line):
            found.pop(order_id, None)        # another process found it final
            continue
        if order_id in final_here or line.get("side") != "SELL":
            continue
        requested = to_decimal(line.get("qty"))
        when = _journal_time(line.get("first_ts") or line.get("ts"))
        if requested is None or when is None:
            continue
        filled = max(to_decimal(line.get("filled")) or _ZERO,
                     to_decimal(line.get("reported")) or _ZERO)
        remaining = max(_ZERO, requested - filled)
        known = found.get(order_id)
        if known is not None:
            # The journal's first line dates the placement (the registry's clock starts
            # when this process placed or adopted it); the most seen filled wins
            remaining = min(remaining, known.qty)
        found[order_id] = OwnOpenSell(
            order_id=order_id, symbol=str(line.get("symbol") or ""),
            security_id=str(line.get("security_id") or ""), qty=remaining, at=when,
            seen_working=known is not None and known.seen_working)
    return tuple(found.values())


def _journal_final(order_id: str, line: dict) -> bool:
    """The order's latest journal line says it is final: ``final``, or ``interrupted``
    in a final status (an exit interrupted after that order had ended)."""
    event = line.get("event")
    if event == "final":
        return True
    if event != "interrupted" or not line.get("status"):
        return False
    return parse_order_row({"id": order_id,
                            "status": str(line.get("status"))}).state in TERMINAL_STATES


def uncertain_from_journal(record: dict, qty: Decimal, when: datetime) -> UncertainPlacement:
    """The UncertainPlacement an ``uncertain`` journal line describes."""
    before = record.get("before_ids")
    return UncertainPlacement(
        internal_id=str(record.get("internal_id") or ""),
        symbol=str(record.get("symbol") or ""),
        security_id=str(record.get("security_id") or ""), qty=qty, at=when,
        side=str(record.get("side") or "SELL"),
        before_ids=(frozenset(str(i) for i in before) if isinstance(before, list) else None),
        remark=str(record.get("remark") or ""))


def _journal_time(value: object) -> Optional[datetime]:
    """A journal line's ``ts`` (IST when it carries no offset), or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


# ── Ticks ────────────────────────────────────────────────────────────────────


def tick_for(price: Decimal, csv_tick: Optional[Decimal]) -> Decimal:
    """The instruments CSV tick when plausible (0 < tick ≤ 0.2 % of the price), else a
    coarse tick valid in every NSE price band (each a multiple of every smaller tick)."""
    if csv_tick is not None and _ZERO < csv_tick <= price * Decimal("0.002"):
        return csv_tick
    if price < 1000:
        return Decimal("0.05")
    return Decimal("1.00") if price < 20000 else Decimal("5.00")


def round_down_to_tick(price: Decimal, tick: Decimal) -> Decimal:
    """Floor ``price`` to a multiple of ``tick`` (at least one tick): a SELL limit
    rounded down stays marketable."""
    steps = (price / tick).to_integral_value(rounding=ROUND_FLOOR)
    return max(steps * tick, tick)


# ── The worker ───────────────────────────────────────────────────────────────


class _BookCache:
    """One order-book read shared by every order the worker is watching (0.5 s TTL).

    The id snapshot before a placement, the status fallback and reconciliation all read
    through it, so concurrent orders stay far below the 15 req/s order-history limit.
    """

    TTL_S = 0.5

    def __init__(self, client: Any, *, clock: Clock, extra_terminal: frozenset[str],
                 call: Optional[Callable[..., Awaitable[Any]]] = None) -> None:
        self._client = client
        self._clock = clock
        self._extra = extra_terminal
        self._call = call               # the worker's deadline-bounded broker call
        self._rows: Optional[tuple[OrderSnapshot, ...]] = None
        self._at = 0.0
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None

    def _loop_lock(self) -> asyncio.Lock:
        """A lock for the running loop (a CLI may reuse the router across asyncio.run calls)."""
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock, self._lock_loop = asyncio.Lock(), loop
        return self._lock

    async def rows(self) -> tuple[OrderSnapshot, ...]:
        """Today's order book (raises when it cannot be read)."""
        async with self._loop_lock():
            if self._rows is not None and self._clock() - self._at < self.TTL_S:
                return self._rows
            if self._call is not None:
                raw = await self._call(self._client.get_order_book)
            else:
                raw = await self._client.get_order_book()
            if not isinstance(raw, list):
                raise BrokerError(f"order book is a {type(raw).__name__}, not a list",
                                  kind="bad_payload")
            self._rows = parse_order_book(raw, extra_terminal=self._extra)
            self._at = self._clock()
            return self._rows

    async def ids(self) -> Optional[frozenset[str]]:
        """Every order id in the book, or None when it cannot be read."""
        try:
            return frozenset(s.order_id for s in await self.rows())
        except Exception as exc:
            logger.warning("Order book unreadable before placing (%s); an uncertain "
                           "placement would be matched by time instead", exc)
            return None

    def invalidate(self) -> None:
        """Forget the cached book (after a placement, reads must be newer than it)."""
        self._rows = None


@dataclass
class _Run:
    """One execute() call: the order working right now, and the pieces done so far."""

    internal_id: str
    active_id: Optional[str] = None     # an order that may be working (cancel it if interrupted)
    active_qty: Decimal = _ZERO
    uncertain: bool = False             # a placement whose outcome is unknown
    # The request being placed right now — sent with no answer yet, or being looked up in
    # the book (an exit's later attempt is a new request: its own quantity and internal
    # id). Interrupted then, this is the order that may exist at the broker.
    placing: Optional[OrderRequest] = None
    placing_wall: Optional[datetime] = None
    placing_entry: bool = True
    placing_before: Optional[frozenset[str]] = None   # the book's order ids before it
    pieces: list[Confirmation] = field(default_factory=list)

    def placed(self) -> None:
        """The broker answered (an order id, or a refusal), or the placement is recorded."""
        self.placing = self.placing_wall = self.placing_before = None


class LiveOrderWorker:
    """Places a live order and reports only what the broker confirms (see module docstring)."""

    def __init__(
        self,
        client: Any,
        settings: FillSettings,
        *,
        deadlines: OrderDeadlines,
        registry: OrderRegistry,
        journal: Optional[OrderJournal] = None,
        alerter: Any = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = time.monotonic,
        wall: Wall = now_ist,
    ) -> None:
        self._client = client
        self._settings = settings
        self._deadlines = deadlines
        self._registry = registry
        self._journal = journal
        self._alerter = alerter
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._extra = settings.extra_terminal_statuses
        self._book = _BookCache(client, clock=clock, extra_terminal=self._extra,
                                call=self._call)
        self._status_source: Optional[str] = None   # "order" | "book", sticky once one answers

    @property
    def settings(self) -> FillSettings:
        return self._settings

    @property
    def deadlines(self) -> OrderDeadlines:
        return self._deadlines

    @property
    def registry(self) -> OrderRegistry:
        return self._registry

    # ── Entry points ─────────────────────────────────────────────────────

    async def execute(self, order: OrderRequest,
                      signal: Optional[TradingSignal]) -> ExecutionResult:
        """Place ``order`` and return what the broker confirmed. Never raises, except
        ``asyncio.CancelledError`` — re-raised after the working order is cancelled."""
        run = _Run(internal_id=str(order.internal_id))
        try:
            if self._is_protective(order):
                return await self._execute_exit(order, signal, run)
            return await self._execute_single(order, signal, run)
        except asyncio.CancelledError:
            await self._interrupted(order, run)
            raise
        except Exception as exc:
            logger.exception("Live order worker error on %s %s %s", order.side.value,
                             order.quantity, order.symbol)
            try:
                return self._crashed(order, signal, run, exc)
            except Exception:
                logger.exception("Live order worker could not report its error")
                placed = [p.order_id for p in run.pieces if p.order_id]
                if run.active_id and run.active_id not in placed:
                    placed.append(run.active_id)
                return ExecutionResult(
                    success=False, signal=signal, mode="live",
                    rejection_reason=(f"Order worker error ({exc}); orders {placed} may still "
                                      "be working — check the broker"),
                    filled_quantity=_ZERO, requested_quantity=order.quantity,
                    outcome=OrderOutcome.UNKNOWN.value, order_ids=placed,
                    remaining_open=bool(placed) or run.uncertain,
                )

    async def resume(self, tracked: TrackedOrder, *, cancel: bool,
                     on_open: Optional[Callable[[Confirmation], Awaitable[None]]] = None,
                     ) -> Confirmation:
        """Re-read an order placed earlier (for the monitor and CLOSING); never places.

        A working order is cancelled and settled when ``cancel`` is set or its entry
        deadline has passed — unless it was only guessed to be ours (``guessed``): that
        one is only read. The caller records any fill beyond ``filled_reported``;
        ``on_open`` gets the first read's view of an order still working before any cancel
        is sent, so the caller can record its progress at once.
        """
        segment = tracked.segment or "EQUITY"
        try:
            snap = await self._read_status(tracked.order_id, segment)
            if snap is not None and snap.state in TERMINAL_STATES:
                return await self._terminal(snap, segment, tracked.requested)
            view = self._open_view(tracked.order_id, tracked.requested, snap)
            if tracked.guessed:
                return view  # maybe someone else's order: watched, never cancelled
            overdue = (tracked.entry_deadline is not None
                       and self._clock() >= tracked.entry_deadline)
            if cancel or overdue:
                if on_open is not None:
                    await on_open(view)
                # Still working afterwards: its journal line keeps what has been recorded
                # for it so far (``reported``)
                return await self._cancel_and_settle(
                    tracked.order_id, segment, tracked.requested, view,
                    reported=tracked.filled_reported,
                    reported_avg=tracked.avg_price_reported)
            return view
        except Exception as exc:
            logger.exception("Resuming order %s failed", tracked.order_id)
            return Confirmation(OrderOutcome.UNKNOWN, tracked.order_id, tracked.requested, None,
                                None, "", "", "", str(exc), may_be_open=True)

    @staticmethod
    def _is_protective(order: OrderRequest) -> bool:
        return order.side == Side.SELL and order.order_type == OrderType.MARKET

    # ── Entries: one order ───────────────────────────────────────────────

    async def _execute_single(self, order: OrderRequest, signal: Optional[TradingSignal],
                              run: _Run) -> ExecutionResult:
        segment = order.segment.value
        placement = await self._place(order, signal, run, entry=True)
        if placement.order_id is None:
            run.pieces.append(self._unplaced_piece(order, placement))
            return self._finish(order, signal, run, protective=False)
        conf = await self._confirm(placement.order_id, segment, order.quantity,
                                   self._settings.timeout_s, entry=True)
        if conf.may_be_open:
            conf = await self._cancel_and_settle(placement.order_id, segment, order.quantity,
                                                 conf)
        self._settled(run, conf, order)
        if order.side == Side.BUY and conf.filled_qty:
            # Positions can lag behind it: the monitor and CLOSING watch for that
            self._registry.record_confirmed_buy(order.symbol, order.security_id,
                                                conf.filled_qty, order_id=conf.order_id)
        if conf.filled_qty is None and not conf.may_be_open:
            self._alert("WARNING", f"fill-qty-unknown:{conf.order_id}",
                        f"{order.side.value} {order.symbol}: order {conf.order_id} is "
                        f"{conf.status_raw or conf.status} but the broker did not report how "
                        "much filled; it is counted as unconfirmed",
                        order_ids=[conf.order_id])
        return self._finish(order, signal, run, protective=False)

    # ── Protective exits: worked until filled ────────────────────────────

    async def _execute_exit(self, order: OrderRequest, signal: Optional[TradingSignal],
                            run: _Run) -> ExecutionResult:
        s = self._settings
        segment = order.segment.value
        target = order.quantity
        filled = _ZERO
        attempt = 0
        backoff_used = 0.0
        force_limit = False
        stop_reason = ""
        current = order
        while filled < target and attempt < s.exit_max_attempts:
            if not self._deadlines.can_place(min(s.ATTEMPT_OVERHEAD_S, s.exit_attempt_timeout_s)):
                stop_reason = _DEADLINE
                break
            attempt += 1
            remaining = target - filled
            if attempt > 1:
                # Re-place only what a fresh book-first read says can still be sold (an
                # exit of the day's position: what that position still holds)
                sellable, view, why = await self._sellable_now(
                    order, remaining,
                    position_only=getattr(signal, "position_only", False) is True)
                if sellable is None or sellable < remaining:
                    stop_reason = self._replace_blocked(order, remaining, sellable, view, why)
                    break
                current = await self._reprice(order, attempt, remaining)
                if force_limit and current.order_type == OrderType.MARKET:
                    stop_reason = "market orders are blocked and no LTP to price a LIMIT"
                    self._alert("CRITICAL", f"exit-rejected:{order.symbol}",
                                f"Protective SELL {order.symbol}: {stop_reason}",
                                dedup_s=_DEDUP_S)
                    break

            placement, backoff_used = await self._place_with_backoff(
                current, signal, run, backoff_used)
            if (placement.rejected and placement.reject_kind is RejectKind.PRICE
                    and current.order_type == OrderType.LIMIT):
                # Tick size, circuit or price band: MARKET in the same attempt
                current = current.model_copy(update={
                    "order_type": OrderType.MARKET, "price": None, "internal_id": uuid4()})
                placement, backoff_used = await self._place_with_backoff(
                    current, signal, run, backoff_used)
            if (placement.rejected and placement.reject_kind is RejectKind.MARKET_BLOCKED
                    and current.order_type == OrderType.MARKET):
                run.pieces.append(self._unplaced_piece(current, placement))
                force_limit = True                  # the next attempt is a LIMIT
                continue
            if placement.order_id is None:
                run.pieces.append(self._unplaced_piece(current, placement))
                if placement.deadline:
                    stop_reason = _DEADLINE
                elif placement.rejected:
                    self._alert("CRITICAL", f"exit-rejected:{order.symbol}",
                                f"Protective SELL {remaining} {order.symbol} refused by the "
                                f"broker: {placement.message}", dedup_s=_DEDUP_S)
                break                               # uncertain: never re-send (I4)

            window = max(1.0, min(s.exit_attempt_timeout_s,
                                  self._deadlines.settle_by() - self._clock()
                                  - s.cancel_confirm_timeout_s))
            conf = await self._confirm(placement.order_id, segment, remaining, window,
                                       entry=False)
            if conf.may_be_open:
                conf = await self._cancel_and_settle(placement.order_id, segment, remaining,
                                                     conf)
            self._settled(run, conf, current)
            if conf.may_be_open:
                if conf.filled_qty:
                    filled += conf.filled_qty
                    self._registry.record_confirmed_exit(order.symbol, order.security_id,
                                                         conf.filled_qty,
                                                         order_id=conf.order_id)
                break                               # stuck: never re-place over it (I3)
            if conf.filled_qty is None:
                self._alert("CRITICAL", f"fill-qty-unknown:{conf.order_id}",
                            f"Protective SELL {order.symbol}: order {conf.order_id} is "
                            f"{conf.status_raw or conf.status} but the broker did not report "
                            "how much filled — not re-placing; check the order book",
                            order_ids=[conf.order_id])
                break
            filled += conf.filled_qty
            if conf.filled_qty > 0:
                self._registry.record_confirmed_exit(order.symbol, order.security_id,
                                                     conf.filled_qty, order_id=conf.order_id)
        return self._finish(order, signal, run, protective=True, stop_reason=stop_reason)

    async def _place_with_backoff(self, order: OrderRequest, signal: Optional[TradingSignal],
                                  run: _Run, backoff_used: float) -> tuple[Placement, float]:
        """Place; on a 429 or a request that never left, back off 1, 2, 4 s… within the
        per-exit cap and the deadline. A backoff does not use up an attempt."""
        cap = self._settings.RATE_LIMIT_BACKOFF_CAP_S
        placement = await self._place(order, signal, run, entry=False)
        step = 0
        while (placement.rejected
               and placement.reject_kind in (RejectKind.RATE_LIMITED, RejectKind.NOT_SENT)
               and backoff_used < cap and self._deadlines.can_place()):
            delay = min(2.0 ** step, cap - backoff_used)
            logger.warning("SELL %s not accepted (%s); retrying in %.0fs", order.symbol,
                           placement.message, delay)
            await self._sleep(delay)
            backoff_used += delay
            step += 1
            placement = await self._place(order, signal, run, entry=False)
        return placement, backoff_used

    def _replace_blocked(self, order: OrderRequest, remaining: Decimal,
                         sellable: Optional[Decimal], view: Optional[SellableView],
                         why: str) -> str:
        """Alert that the remainder cannot be re-placed; returns the reason."""
        order_ids: list[str] = []
        if sellable is None:
            reason = f"cannot re-check what is left to sell ({why})"
        else:
            reason = f"only {sellable} left to sell for the remaining {remaining}"
            if view is not None and view.pending:
                order_ids = [o.order_id for o in view.pending]
                reason += " — open SELL orders: " + ", ".join(
                    f"{o.order_id} {o.status_raw or o.status}" for o in view.pending)
            if view is not None and view.unshown_fill_qty:
                reason += f"; {view.unshown_fill_qty} sold but not yet in positions"
            if view is not None and view.uncertain_qty:
                reason += (f"; {view.uncertain_qty} in a SELL whose placement is uncertain "
                           "(not in the order book yet)")
            if view is not None and view.own_open_qty:
                order_ids += list(view.own_open_ids)
                reason += (f"; {view.own_open_qty} in Skopaq SELL orders not listed in the "
                           f"order book yet ({', '.join(view.own_open_ids)})")
        self._alert("CRITICAL", f"exit-replace-blocked:{order.symbol}",
                    f"Protective SELL {order.symbol} not re-placed: {reason}",
                    order_ids=order_ids, dedup_s=_DEDUP_S)
        return f"not re-placed: {reason}"

    async def _sellable_now(self, order: OrderRequest, remaining: Decimal, *,
                            position_only: bool = False,
                            ) -> tuple[Optional[Decimal], Optional[SellableView], str]:
        """What can still be sold, from a fresh book → positions → holdings read (with
        ``position_only``, what the day's position still holds: ``position_sellable``).

        (None, None, why) when the book or positions cannot be read: never re-place blind.
        """
        try:
            snap = await self._call(read_broker_snapshot, self._client,
                                    extra_terminal=self._extra, sleep=self._sleep,
                                    wall=self._wall)
        except Exception as exc:
            logger.error("Cannot re-check %s before re-placing: %s", order.symbol, exc)
            return None, None, f"positions unreadable: {exc}"
        if snap.book_error:
            return None, None, f"order book unreadable: {snap.book_error}"
        lag = self._settings.sell_fill_lag_window_s
        own = set(self._registry.ids())
        if self._journal is not None:
            own |= self._journal.own_ids_today()
        context = SellContext(
            orders=snap.orders, read_at=snap.read_at,
            own_recent_exit_qty=recent_exit_qty(self._registry, self._journal, order.symbol,
                                                order.security_id, lag, snap.read_at),
            lag_window_s=lag, own_order_ids=frozenset(own),
            uncertain=uncertain_sells(self._registry, self._journal),
            own_open=own_open_sells(self._registry, self._journal, snap.read_at),
        )
        view = sellable_quantity(
            symbol=order.symbol, security_id=order.security_id, product=order.product.value,
            positions=snap.positions, holdings=snap.holdings, context=context,
            order_qty=remaining, exchange=order.exchange.value,
        )
        sellable = view.position_sellable if position_only else view.sellable
        if snap.holdings_error and sellable < remaining:
            return None, view, f"holdings unreadable: {snap.holdings_error}"
        return sellable, view, ""

    async def _reprice(self, order: OrderRequest, attempt: int,
                       remaining: Decimal) -> OrderRequest:
        """The remainder as a marketable LIMIT: LTP less a growing buffer, rounded down to
        the tick. MARKET when there is no LTP."""
        s = self._settings
        try:
            ltp = to_decimal(await self._call(
                self._client.get_ltp, f"{order.exchange.value}_{order.security_id}"))
        except Exception as exc:
            logger.warning("No LTP for %s (%s); re-placing at MARKET", order.symbol, exc)
            ltp = None
        if ltp is None or ltp <= 0:
            return order.model_copy(update={"quantity": remaining, "order_type": OrderType.MARKET,
                                            "price": None, "internal_id": uuid4()})
        buffer_pct = min(s.exit_reprice_buffer_pct * (attempt - 1), s.REPRICE_TOTAL_CAP_PCT)
        csv_tick = await self._tick_size(order)
        tick = tick_for(ltp, csv_tick)
        price = round_down_to_tick(ltp * (1 - Decimal(str(buffer_pct)) / 100), tick)
        logger.info("Re-placing SELL %s %s as LIMIT %s (LTP %s, buffer %.1f%%, tick %s)",
                    remaining, order.symbol, price, ltp, buffer_pct, tick)
        return order.model_copy(update={"quantity": remaining, "order_type": OrderType.LIMIT,
                                        "price": float(price), "internal_id": uuid4()})

    async def _tick_size(self, order: OrderRequest) -> Optional[Decimal]:
        """The instruments CSV tick: the cached one, however old (never a download while
        the rest of an exit waits); else a lookup given at most ``_TICK_LOOKUP_S`` on the
        worker's clock. None (the coarse fallback tick) when neither answers."""
        cached = cached_tick_size(order.symbol, order.exchange.value)
        if cached is not None:
            return cached
        lookup = asyncio.ensure_future(self._call(resolve_tick_size, self._client,
                                                  order.symbol, order.exchange.value))
        end = self._clock() + _TICK_LOOKUP_S
        try:
            while not lookup.done() and self._clock() < end:
                await self._sleep(min(0.25, end - self._clock()))
        finally:
            if not lookup.done():
                lookup.cancel()
        if not lookup.done() or lookup.cancelled():
            logger.warning("No tick size for %s within %.0fs; using the fallback",
                           order.symbol, _TICK_LOOKUP_S)
            return None
        if lookup.exception() is not None:        # only a deadline: it never raises itself
            logger.warning("No tick size for %s (%s); using the fallback", order.symbol,
                           lookup.exception())
            return None
        return lookup.result()

    # ── Placing ──────────────────────────────────────────────────────────

    async def _place(self, order: OrderRequest, signal: Optional[TradingSignal], run: _Run,
                     *, entry: bool) -> Placement:
        if not self._deadlines.can_place():
            logger.error("Not placing %s %s %s: past %s", order.side.value, order.quantity,
                         order.symbol, _DEADLINE)
            return Placement(None, rejected=True, reject_kind=RejectKind.OTHER,
                             message=f"Not placed: {_DEADLINE}", deadline=True)
        before_ids = await self._book.ids()
        if not self._deadlines.can_place():
            # The book read took long enough to pass the deadline: still nothing placed
            logger.error("Not placing %s %s %s: past %s", order.side.value, order.quantity,
                         order.symbol, _DEADLINE)
            return Placement(None, rejected=True, reject_kind=RejectKind.OTHER,
                             message=f"Not placed: {_DEADLINE}", deadline=True)
        placed_wall = self._wall()
        run.uncertain = True             # until the broker answers, the order may exist
        run.placing, run.placing_wall = order, placed_wall
        run.placing_entry, run.placing_before = entry, before_ids
        try:
            response = await self._call(self._client.place_order, order)
        except OrderPlacementUncertain as exc:
            self._book.invalidate()
            return await self._resolve_uncertain(order, signal, run, entry, before_ids,
                                                 placed_wall, str(exc))
        except TimeoutError:
            # Cut by the shutdown deadline after it was sent: it may exist
            self._book.invalidate()
            return await self._resolve_uncertain(order, signal, run, entry, before_ids,
                                                 placed_wall,
                                                 f"no answer before {_DEADLINE}")
        except BrokerError as exc:
            run.uncertain = False
            run.placed()
            kind = classify_rejection(exc.status_code, str(exc), exc.kind)
            logger.warning("%s %s %s refused (%s): %s", order.side.value, order.quantity,
                           order.symbol, kind.value, exc)
            return Placement(None, rejected=True, reject_kind=kind,
                             message=f"Broker rejected: {exc}")
        except Exception as exc:
            run.uncertain = False
            run.placed()
            logger.error("%s %s %s failed: %s", order.side.value, order.quantity,
                         order.symbol, exc)
            return Placement(None, rejected=True, reject_kind=RejectKind.OTHER,
                             message=f"Broker error: {exc}")
        self._book.invalidate()
        order_id = getattr(response, "order_id", "")
        if not isinstance(order_id, str) or not order_id.strip():
            return await self._resolve_uncertain(order, signal, run, entry, before_ids,
                                                 placed_wall, "the answer had no order id")
        status = normalise_status(getattr(response, "status", ""))
        self._adopt(order, signal, run, entry, order_id.strip(), status)
        message = getattr(response, "message", "")
        return Placement(order_id.strip(), status=status,
                         message=message if isinstance(message, str) else "")

    def _adopt(self, order: OrderRequest, signal: Optional[TradingSignal], run: _Run,
               entry: bool, order_id: str, status: str) -> None:
        """Track a placed (or reconciled) order as this run's working order."""
        run.active_id, run.active_qty, run.uncertain = order_id, order.quantity, False
        run.placed()
        tracked = TrackedOrder(
            order_id=order_id, side=order.side.value, symbol=order.symbol,
            security_id=order.security_id, segment=order.segment.value,
            # "entry": one attempt, cancelled after its deadline (BUYs, LIMIT SELLs);
            # "exit": a protective SELL, worked until filled
            requested=order.quantity, purpose="entry" if entry else "exit",
            placed_mono=self._clock(),
            entry_deadline=self._clock() + self._settings.timeout_s if entry else None,
            signal=signal, internal_id=str(order.internal_id), price=order.price,
        )
        self._registry.track(tracked)
        self._journal_event("placed", order_id, status=status)

    async def _resolve_uncertain(self, order: OrderRequest, signal: Optional[TradingSignal],
                                 run: _Run, entry: bool, before_ids: Optional[frozenset[str]],
                                 placed_wall: datetime, reason: str) -> Placement:
        logger.error("Placement of %s %s %s uncertain (%s); looking for it in the order book",
                     order.side.value, order.quantity, order.symbol, reason)
        placement = await self._reconcile(order, before_ids, placed_wall, reason)
        if placement.order_id:
            logger.warning("Uncertain placement found in the order book: %s",
                           placement.order_id)
            self._adopt(order, signal, run, entry, placement.order_id, placement.status)
            return placement
        candidates = ", ".join(placement.candidates)
        self._record_uncertain(order, entry, placed_wall, note=candidates,
                               before_ids=before_ids)
        run.placed()
        found = (f"{len(placement.candidates)} orders could be it ({candidates})"
                 if placement.candidates else "no matching order appeared in the order book")
        if order.side == Side.SELL:
            minutes = self._settings.sell_fill_lag_window_s / 60
            resend = (f"Not re-sent: for {minutes:.0f} min it counts as an open SELL of these "
                      "shares, so nothing sells them again meanwhile — check the order book.")
        else:
            resend = "Not re-sent — check the order book."
        self._alert("CRITICAL", f"placement-uncertain:{run.internal_id}",
                    f"{order.side.value} {order.quantity} {order.symbol}: the broker's answer "
                    f"was unclear ({reason}) and {found}. {resend}",
                    order_ids=placement.candidates)
        return placement

    def _remark(self, order: OrderRequest) -> str:
        """The ``remarks`` tag the client sends with ``order`` ("" when not enabled)."""
        return (f"skopaq-{order.internal_id.hex[:24]}" if self._settings.remarks_enabled
                else "")

    def _record_uncertain(self, order: OrderRequest, entry: bool, placed_wall: datetime, *,
                          note: str = "",
                          before_ids: Optional[frozenset[str]] = None) -> None:
        """Register and journal a placement whose outcome is unknown, with the book's order
        ids from just before it was sent (none of them can be it), so a resync can watch
        an order that looks like it; a SELL counts against the shares for the lag window."""
        self._registry.record_uncertain(UncertainPlacement(
            internal_id=str(order.internal_id), symbol=order.symbol,
            security_id=order.security_id, qty=order.quantity, at=placed_wall,
            side=order.side.value, before_ids=before_ids, remark=self._remark(order)))
        if self._journal is not None:
            self._journal.record(
                "uncertain", internal_id=str(order.internal_id), symbol=order.symbol,
                security_id=order.security_id, segment=order.segment.value,
                side=order.side.value, qty=order.quantity,
                purpose="entry" if entry else "exit", note=note, before_ids=before_ids,
                remark=self._remark(order))

    async def _reconcile(self, order: OrderRequest, before_ids: Optional[frozenset[str]],
                         placed_wall: datetime, reason: str) -> Placement:
        """Find an uncertain placement: an order id that was not in the book before, for
        the same instrument, side and quantity, and not an order we already know (this
        run's earlier attempts, this process's and this host's other orders).

        Only when that snapshot could not be read, a time window (allowing for clock
        skew) stands in for it; then a lone candidate is adopted only if it is still the
        only one when the reconcile window ends.
        """
        s = self._settings
        end = min(self._clock() + s.reconcile_timeout_s, self._deadlines.settle_by())
        remark = self._remark(order)
        earliest = placed_wall - timedelta(seconds=SKEW_ALLOWANCE_S)
        known = self._known_ids()
        seen: dict[str, OrderSnapshot] = {}      # time-window candidates across the reads
        while True:
            try:
                rows: Optional[tuple[OrderSnapshot, ...]] = await self._book.rows()
            except Exception as exc:
                logger.debug("Order book read failed while reconciling: %s", exc)
                rows = None
            if rows is not None:
                if remark:
                    mine = [r for r in rows if r.remarks == remark]
                    if len(mine) == 1:
                        return Placement(mine[0].order_id, status=mine[0].status,
                                         reconciled=True)
                found = [r for r in rows
                         if self._could_be(r, order, before_ids, earliest, known)]
                if before_ids is not None and len(found) == 1:
                    return Placement(found[0].order_id, status=found[0].status, reconciled=True)
                seen.update((r.order_id, r) for r in found)
                if len(found) > 1 or len(seen) > 1:
                    return Placement(None, uncertain=True, message=reason,
                                     candidates=tuple(seen))
            if self._clock() >= end:
                if len(seen) == 1:
                    only = next(iter(seen.values()))
                    return Placement(only.order_id, status=only.status, reconciled=True)
                return Placement(None, uncertain=True, message=reason)
            await self._sleep(min(s.poll_interval_s, max(0.0, end - self._clock())))

    def _known_ids(self) -> frozenset[str]:
        """Order ids that are already accounted for: this process's and this host's."""
        known = set(self._registry.ids())
        if self._journal is not None:
            known |= self._journal.own_ids_today()
        return frozenset(known)

    @staticmethod
    def _could_be(row: OrderSnapshot, order: OrderRequest,
                  before_ids: Optional[frozenset[str]], earliest: datetime,
                  known: frozenset[str]) -> bool:
        if (row.order_id in known or row.security_id != order.security_id
                or row.side != order.side.value or row.requested_qty != order.quantity):
            return False
        if before_ids is not None:
            return row.order_id not in before_ids
        # Created within the clock-skew allowance either side of when it was sent
        return (row.created_at is not None and earliest <= row.created_at
                <= earliest + timedelta(seconds=2 * SKEW_ALLOWANCE_S))

    # ── Watching an order ────────────────────────────────────────────────

    async def _read_status(self, order_id: str, segment: str) -> Optional[OrderSnapshot]:
        """GET /order, else the shared book row (the source that answered last goes first)."""
        sources = ("book", "order") if self._status_source == "book" else ("order", "book")
        for source in sources:
            try:
                if source == "order":
                    snap = parse_order_row(
                        await self._call(self._client.get_order, order_id, segment),
                        extra_terminal=self._extra)
                else:
                    snap = find_order(await self._book.rows(), order_id)
            except Exception as exc:
                logger.debug("Order %s: %s read failed: %s", order_id, source, exc)
                continue
            if snap is not None and snap.order_id == order_id:
                self._status_source = source
                return snap
        return None

    async def _confirm(self, order_id: str, segment: str, requested: Decimal, window_s: float,
                       *, entry: bool) -> Confirmation:
        """Poll until the order is final or ``window_s`` passes (an entry stops at once
        when a shutdown is armed)."""
        end = min(self._clock() + window_s, self._deadlines.settle_by())
        last: Optional[OrderSnapshot] = None
        while True:
            snap = await self._read_status(order_id, segment)
            if snap is not None:
                if last is None or (snap.status, snap.traded_qty) != (last.status,
                                                                      last.traded_qty):
                    logger.info("Order %s %s traded=%s/%s", order_id,
                                snap.status_raw or snap.status, snap.traded_qty,
                                snap.requested_qty)
                last = snap
                if snap.state in TERMINAL_STATES:
                    return await self._terminal(snap, segment, requested)
            if self._clock() >= end or (entry and self._deadlines.stopping):
                break
            await self._sleep(min(self._settings.poll_interval_s, max(0.0, end - self._clock())))
        return self._open_view(order_id, requested, last)

    async def _cancel_and_settle(self, order_id: str, segment: str, requested: Decimal,
                                 last: Optional[Confirmation], *,
                                 reported: Optional[Decimal] = None,
                                 reported_avg: Optional[Decimal] = None) -> Confirmation:
        """Cancel (retried every 2 s) and re-read until the order is final.

        A cancel races the order filling, so the final state decides — it can be SUCCESS.
        A cancel answered "Position could not be found." (the OMS may not have registered
        a just-placed order yet) is sent again while later reads show the order working.
        An order still working after the cancel window (or at ``settle_by``) is stuck:
        registered, journalled (with ``reported``, when given: what was recorded for it
        so far) and alerted, and never re-sent over.
        """
        s = self._settings
        start = self._clock()
        settle_by = self._deadlines.settle_by()
        end = min(start + s.cancel_confirm_timeout_s, settle_by)
        cut_by_deadline = settle_by < start + s.cancel_confirm_timeout_s
        next_cancel = start
        cancelling = True
        not_found = False       # the last cancel was answered "could not be found"
        snap: Optional[OrderSnapshot] = None
        while True:
            if cancelling and self._clock() >= next_cancel:
                answer = await self._send_cancel(order_id, segment)
                cancelling, not_found = answer == "retry", answer == "not_found"
                next_cancel = self._clock() + s.CANCEL_RETRY_INTERVAL_S
            read = await self._read_status(order_id, segment)
            if read is not None:
                snap = read
                if read.state in TERMINAL_STATES:
                    conf = await self._terminal(read, segment, requested)
                    return dataclasses.replace(conf, cancel_sent=True)
                if not_found and read.state in (OrderState.WORKING, OrderState.UNRECOGNISED):
                    # "Not found", yet it reads working: the OMS had not registered it
                    # then — cancel again at the retry cadence
                    cancelling, not_found = True, False
            if self._clock() >= end:
                break
            await self._sleep(min(s.poll_interval_s, max(0.0, end - self._clock())))

        view = (self._open_view(order_id, requested, snap) if snap is not None
                else last or self._open_view(order_id, requested, None))
        status = view.status_raw or view.status or "status unreadable"
        traded = "unknown" if view.filled_qty is None else str(view.filled_qty)
        view = dataclasses.replace(
            view, cancel_sent=True,
            note=(f"Order {order_id} may still be working at the broker ({status}, traded "
                  f"{traded} of {requested}): the cancel was not confirmed — check it"))
        self._registry.mark_stuck(order_id, view)
        self._journal_event("stuck", order_id, conf=view, reported=reported,
                            reported_avg=reported_avg)
        tracked = self._registry.get(order_id)
        what = f"{tracked.side} {tracked.symbol}" if tracked else "order"
        if cut_by_deadline:
            self._alert("CRITICAL", f"order-deadline:{order_id}",
                        f"{what} order {order_id} still working at {_DEADLINE} ({status}, "
                        f"traded {traded} of {requested}); cancel it at the broker",
                        order_ids=[order_id])
        else:
            self._alert("CRITICAL", f"order-stuck:{order_id}",
                        f"{what} order {order_id} may still be working ({status}, traded "
                        f"{traded} of {requested}): the cancel was not confirmed within "
                        f"{s.cancel_confirm_timeout_s:g}s — cancel it at the broker",
                        order_ids=[order_id])
        return view

    async def _send_cancel(self, order_id: str, segment: str) -> str:
        """Send one cancel. "retry": send it again in 2 s if the order still reads working;
        "not_found": the broker could not find it (again only if a read shows it working);
        "stop": refused — only the order's final state is read from then on."""
        try:
            await self._call(self._client.cancel_order,
                             CancelOrderRequest(order_id=order_id, segment=segment))
            logger.info("Cancel sent for order %s", order_id)
            return "retry"
        except BrokerError as exc:
            text = str(exc).lower()
            if "position could not be found" in text:
                # The order is gone or complete — or not registered yet: its reads tell
                logger.info("Cancel of %s: %s — reading its state", order_id, exc)
                return "not_found"
            if (exc.kind in ("not_sent", "transport", "bad_payload") or exc.status_code == 429
                    or exc.status_code >= 500 or "already pending" in text):
                logger.warning("Cancel of %s not accepted yet (%s); retrying", order_id, exc)
                return "retry"
            logger.warning("Cancel of %s refused (%s); reading its final state", order_id, exc)
            return "stop"
        except Exception as exc:
            logger.warning("Cancel of %s failed (%s); retrying", order_id, exc)
            return "retry"

    async def _terminal(self, snap: OrderSnapshot, segment: str,
                        requested: Decimal) -> Confirmation:
        """The final view of a finished order: filled quantity and average price."""
        filled, price, source = await self._fill_details(snap, segment, requested)
        outcome = _OUTCOME_OF_STATE[snap.state]
        if filled is not None:
            if requested > 0 and filled >= requested:
                outcome = OrderOutcome.FILLED
            elif filled > 0:
                outcome = OrderOutcome.PARTIAL
        conf = Confirmation(outcome, snap.order_id, requested, filled, price, source,
                            snap.status, snap.status_raw, snap.message, may_be_open=False,
                            exch_order_id=snap.exch_order_id)
        logger.info("Order %s final: %s, filled %s of %s at %s (%s)", snap.order_id,
                    snap.status_raw or snap.status, filled, requested, price, source or "-")
        self._registry.mark_final(snap.order_id, conf)
        self._journal_event("final", snap.order_id, conf=conf)
        return conf

    async def _fill_details(self, snap: OrderSnapshot, segment: str, requested: Decimal
                            ) -> tuple[Optional[Decimal], Optional[Decimal], str]:
        """(filled, average price, source). Filled is the larger of ``traded_qty`` and the
        sum of the order's fills, or None when neither is known."""
        if (snap.state in (OrderState.CANCELLED, OrderState.REJECTED)
                and snap.traded_qty is not None and snap.traded_qty == 0):
            return _ZERO, None, ""
        try:
            fills = parse_fills(await self._call(self._client.get_trades, snap.order_id,
                                                 segment))
        except Exception as exc:
            logger.warning("Fills of order %s unreadable: %s", snap.order_id, exc)
            fills = FillSummary(_ZERO, None, 0)
        known = [q for q in (snap.filled_qty, fills.qty if fills.count else None)
                 if q is not None]
        if known:
            filled: Optional[Decimal] = min(max(known), requested)
        elif snap.state is OrderState.FILLED:
            filled = requested
        else:
            filled = None
        if snap.state is OrderState.FILLED and filled == 0:
            logger.warning("Order %s is %s but reports nothing traded; fill unknown",
                           snap.order_id, snap.status_raw)
            filled = None
        if not filled:
            return filled, None, ""
        if fills.count and fills.vwap is not None and fills.qty == filled:
            return filled, fills.vwap, "trades"
        if snap.traded_price is not None and snap.traded_price > 0:
            return filled, snap.traded_price, "order"
        if snap.exch_order_id:
            try:
                book = await self._call(self._client.get_trade_book, segment)
                mine = [r for r in book if isinstance(r, dict)
                        and str(r.get("exch_order_id", "")).strip() == snap.exch_order_id]
                from_book = parse_fills(mine)
                if from_book.count and from_book.vwap is not None:
                    return filled, from_book.vwap, "trade_book"
            except Exception as exc:
                logger.warning("Trade book unreadable for order %s: %s", snap.order_id, exc)
        if fills.vwap is not None:
            logger.warning("Order %s: fills cover %s of %s filled; using their VWAP",
                           snap.order_id, fills.qty, filled)
            return filled, fills.vwap, "trades"
        return filled, None, ""

    def _open_view(self, order_id: str, requested: Decimal,
                   snap: Optional[OrderSnapshot]) -> Confirmation:
        """An order not (known to be) final: may still be working."""
        if snap is None:
            return Confirmation(OrderOutcome.UNKNOWN, order_id, requested, None, None, "", "",
                                "", "", may_be_open=True)
        price = snap.traded_price if snap.traded_price and snap.traded_price > 0 else None
        return Confirmation(OrderOutcome.OPEN, order_id, requested, snap.filled_qty, price,
                            "order" if price else "", snap.status, snap.status_raw,
                            snap.message, may_be_open=True, exch_order_id=snap.exch_order_id)

    # ── Results ──────────────────────────────────────────────────────────

    def _settled(self, run: _Run, conf: Confirmation, order: OrderRequest) -> None:
        conf = dataclasses.replace(conf, order_price=order.price)
        run.pieces.append(conf)
        if not conf.may_be_open:
            run.active_id = None
        self._registry.note_reported(conf.order_id, conf)

    @staticmethod
    def _unplaced_piece(order: OrderRequest, placement: Placement) -> Confirmation:
        """A placement that produced no order to watch: refused, too late, or uncertain."""
        if placement.uncertain:
            note = (f"Order placement uncertain ({placement.message}) — not re-sent; check "
                    "the order book")
            if placement.candidates:
                note += f" (possible orders: {', '.join(placement.candidates)})"
            return Confirmation(OrderOutcome.UNKNOWN, "", order.quantity, None, None, "", "",
                                "", placement.message, may_be_open=True, note=note)
        not_placed = placement.deadline or placement.reject_kind is RejectKind.NOT_SENT
        return Confirmation(OrderOutcome.NOT_PLACED if not_placed else OrderOutcome.REJECTED,
                            "", order.quantity, _ZERO, None, "", "", "", placement.message,
                            may_be_open=False, note=placement.message, order_price=order.price)

    def _finish(self, order: OrderRequest, signal: Optional[TradingSignal], run: _Run, *,
                protective: bool, stop_reason: str = "") -> ExecutionResult:
        """One ExecutionResult from the pieces (and the alerts it calls for)."""
        pieces = run.pieces
        target = order.quantity
        attempts = max(1, len(pieces))
        filled = sum((p.filled_qty for p in pieces if p.filled_qty), start=_ZERO)
        open_pieces = [p for p in pieces if p.may_be_open]
        unreported = [p for p in pieces if p.filled_qty is None and not p.may_be_open]
        with_id = [p for p in pieces if p.order_id]
        last = pieces[-1] if pieces else None
        fill_price, source = self._average_price(order, signal, pieces)

        if target > 0 and filled >= target:
            outcome = OrderOutcome.FILLED.value
        elif filled > 0:
            outcome = OrderOutcome.PARTIAL.value
        elif open_pieces:
            outcome = open_pieces[-1].outcome.value
        elif unreported:
            outcome = OrderOutcome.UNKNOWN.value
        elif last is None:
            outcome = OrderOutcome.NOT_PLACED.value
        else:
            outcome = last.outcome.value

        success = filled > 0
        reason = "" if success else self._failure_reason(
            protective, attempts, stop_reason, last, open_pieces, unreported)
        if outcome == OrderOutcome.PARTIAL.value:
            if protective:
                message = f"sold {filled} of {target} after {attempts} attempt(s)"
            elif open_pieces:
                message = (f"filled {filled} of {target}; the rest may still be working at "
                           "the broker")
            else:
                message = f"filled {filled} of {target}; rest cancelled"
        else:
            message = (last.message or last.note) if last else ""

        if protective and filled < target:
            detail = stop_reason or reason or (open_pieces[-1].note if open_pieces else "")
            ids = [p.order_id for p in open_pieces if p.order_id]
            if filled > 0:
                self._alert("CRITICAL", f"exit-partial:{order.symbol}",
                            f"Protective SELL {order.symbol}: sold {filled} of {target} after "
                            f"{attempts} attempt(s); {target - filled} still held"
                            + (f" — {detail}" if detail else "") + ". Check the broker.",
                            order_ids=ids, dedup_s=_DEDUP_S)
            else:
                self._alert("CRITICAL", f"exit-not-filled:{order.symbol}",
                            f"Protective SELL {target} {order.symbol} NOT filled: {detail}. "
                            "The shares are still held — check the broker.",
                            order_ids=ids, dedup_s=_DEDUP_S)
        elif not protective and outcome == OrderOutcome.PARTIAL.value and not open_pieces:
            order_id = with_id[-1].order_id if with_id else run.internal_id
            self._alert("WARNING", f"entry-partial:{order_id}",
                        f"{order.side.value} {order.symbol}: filled {filled} of {target}; "
                        "the rest was cancelled", order_ids=[order_id])

        slippage = 0.0
        if fill_price is not None and signal is not None and signal.entry_price:
            slippage = (fill_price - signal.entry_price if order.side == Side.BUY
                        else signal.entry_price - fill_price)
        last_order = with_id[-1] if with_id else None
        return ExecutionResult(
            success=success,
            order=OrderResponse(
                order_id=last_order.order_id, status=last_order.status,
                message=last_order.message, exchange_order_id=last_order.exch_order_id or None,
            ) if last_order else None,
            signal=signal,
            mode="live",
            rejection_reason=reason,
            fill_price=fill_price,
            slippage=round(slippage, 4),
            brokerage=20.0 * len([p for p in pieces if p.filled_qty]),   # INDstocks flat fee
            filled_quantity=filled,
            requested_quantity=target,
            outcome=outcome,
            order_ids=[p.order_id for p in with_id],
            remaining_open=bool(open_pieces),
            fill_unconfirmed=bool(unreported),
            fill_price_source=source,
            broker_message=message,
        )

    def _failure_reason(self, protective: bool, attempts: int, stop_reason: str,
                        last: Optional[Confirmation], open_pieces: list[Confirmation],
                        unreported: list[Confirmation]) -> str:
        if open_pieces:
            piece = open_pieces[-1]
            return piece.note or (f"Order {piece.order_id} may still be working at the "
                                  "broker — check it")
        if unreported:
            piece = unreported[-1]
            return (f"Order {piece.order_id} is {piece.status_raw or piece.status} but the "
                    "broker did not report how much filled — check the order book")
        if protective and stop_reason:
            if last is None:
                return f"Exit not placed: {stop_reason}"
            return f"Exit not filled after {attempts} attempt(s): {stop_reason}"
        if last is None:
            return "Order not placed"
        if last.note:
            return last.note
        if last.outcome is OrderOutcome.CANCELLED and last.cancel_sent:
            if protective:
                return f"Exit not filled after {attempts} attempt(s) — cancelled at the broker"
            return (f"Not filled within {self._settings.timeout_s:g}s — cancelled at the "
                    "broker")
        return f"Broker {last.status_raw or last.status}: {last.message}".rstrip(": ")

    def _average_price(self, order: OrderRequest, signal: Optional[TradingSignal],
                       pieces: list[Confirmation]) -> tuple[Optional[float], str]:
        """VWAP over the pieces that filled; a piece without a broker price uses its limit
        price or the signal's reference price, and the source becomes "estimate"."""
        value = _ZERO
        qty = _ZERO
        sources: list[str] = []
        for piece in pieces:
            if not piece.filled_qty:
                continue
            price, source = piece.avg_price, piece.price_source
            if price is None or price <= 0:
                estimate = piece.order_price or (signal.entry_price if signal else None)
                price, source = to_decimal(estimate), "estimate"
                if piece.order_id:
                    self._alert("WARNING", f"fill-price-unknown:{piece.order_id}",
                                f"{order.side.value} {order.symbol}: order {piece.order_id} "
                                f"filled {piece.filled_qty} but the broker gave no price; "
                                f"using {price if price else 'no'} estimate",
                                order_ids=[piece.order_id])
            sources.append(source)
            if price is None or price <= 0:
                continue
            value += piece.filled_qty * price
            qty += piece.filled_qty
        # Report the least precise source used
        source = next((s for s in ("estimate", "trade_book", "order", "trades") if s in sources),
                      "")
        return (float(value / qty) if qty else None), source

    def _crashed(self, order: OrderRequest, signal: Optional[TradingSignal], run: _Run,
                 exc: Exception) -> ExecutionResult:
        """An unexpected error: an order that may be working is reported open (I7)."""
        protective = self._is_protective(order)
        if run.active_id or run.uncertain:
            order_id = run.active_id or ""
            piece = Confirmation(
                OrderOutcome.UNKNOWN, order_id, run.active_qty or order.quantity, None, None,
                "", "", "", str(exc), may_be_open=True,
                note=(f"Order worker error after placing {order_id or 'the order'} ({exc}) — "
                      "it may still be working; check the broker"))
            run.pieces.append(piece)
            if order_id:
                self._registry.mark_stuck(order_id, piece)
                self._journal_event("stuck", order_id, conf=piece, note=str(exc))
            elif run.placing is not None:
                self._record_uncertain(run.placing, run.placing_entry,
                                       run.placing_wall or self._wall(),
                                       note=f"worker error while placing: {exc}",
                                       before_ids=run.placing_before)
                run.placed()
            self._alert("CRITICAL",
                        f"order-stuck:{order_id}" if order_id
                        else f"placement-uncertain:{run.internal_id}",
                        f"{order.side.value} {order.quantity} {order.symbol}: {piece.note}",
                        order_ids=[order_id] if order_id else [])
        elif not run.pieces:
            run.pieces.append(Confirmation(
                OrderOutcome.NOT_PLACED, "", order.quantity, _ZERO, None, "", "", "", str(exc),
                may_be_open=False, note=f"Order worker error: {exc}"))
        return self._finish(order, signal, run, protective=protective,
                            stop_reason=f"worker error: {exc}" if protective else "")

    async def _interrupted(self, order: OrderRequest, run: _Run) -> None:
        """The caller was cancelled mid-order: cancel the working order (shielded, bounded),
        leave every fill of this run to be recorded, and alert.

        The caller never sees a result, so it records nothing: each order of the run that
        filled (or may still be working) goes back into the registry as unresolved with
        nothing reported, and is journalled the same way, so a resync, CLOSING or a later
        ``skopaq monitor`` records its fills as late fills. An interrupted placement whose
        outcome is unknown is registered and journalled as ``uncertain``.
        """
        final: Optional[Confirmation] = None
        if run.active_id:
            try:
                final = await asyncio.shield(asyncio.wait_for(
                    self._cancel_and_settle(run.active_id, order.segment.value,
                                            run.active_qty, None),
                    self._settings.cancel_confirm_timeout_s))
            except BaseException:            # never mask the cancellation being handled
                pass
        views: dict[str, Confirmation] = {p.order_id: p for p in run.pieces if p.order_id}
        if run.active_id:
            views[run.active_id] = final or self._open_view(run.active_id, run.active_qty, None)
        unreported = {oid: v for oid, v in views.items()
                      if v.may_be_open or v.filled_qty is None or v.filled_qty > 0}
        for order_id, view in unreported.items():
            tracked = self._registry.get(order_id)
            if tracked is not None:
                tracked.filled_reported, tracked.avg_price_reported = _ZERO, None
                if tracked.state == "final":
                    tracked.state = "interrupted"
            self._journal_event("interrupted", order_id, conf=view, reported=_ZERO)
        # The request in flight, not the one execute() was called with: an exit's later
        # attempt has its own quantity (the remainder) and internal id
        placing = run.placing if not run.active_id else None
        if placing is not None:
            self._record_uncertain(placing, run.placing_entry, run.placing_wall or self._wall(),
                                   note="interrupted while placing",
                                   before_ids=run.placing_before)
            run.placed()
        if not (unreported or placing is not None or run.active_id):
            return
        sold = sum((v.filled_qty for v in unreported.values() if v.filled_qty), start=_ZERO)
        parts = []
        for order_id, view in views.items():
            if order_id not in unreported and order_id != run.active_id:
                continue
            if view.may_be_open:
                parts.append(f"{order_id} may still be working (its cancel was not confirmed)")
            elif view.filled_qty is None:
                parts.append(f"{order_id} is {view.status_raw or view.status}, fill unknown")
            else:
                parts.append(f"{order_id} filled {view.filled_qty}")
        if placing is not None:
            parts.append(f"a {placing.side.value} of {placing.quantity} whose placement outcome "
                         "is unknown may exist at the broker"
                         + (f" (counted as sold for "
                            f"{self._settings.sell_fill_lag_window_s / 60:.0f} min)"
                            if placing.side == Side.SELL else ""))
        self._alert("CRITICAL", f"order-interrupted:{run.active_id or run.internal_id}",
                    f"{order.side.value} {order.quantity} {order.symbol} was interrupted: "
                    f"{'; '.join(parts)}. Confirmed filled so far: {sold}"
                    + ("; not recorded yet — the monitor/CLOSING records these fills when it "
                       "resumes the orders" if unreported else "")
                    + ". Check the order book.",
                    order_ids=[oid for oid in views
                               if oid in unreported or oid == run.active_id])

    # ── Side channels ────────────────────────────────────────────────────

    async def _call(self, fn: Callable[..., Awaitable[Any]], *args: Any, **kwargs: Any) -> Any:
        """One broker call. Once a shutdown is armed it is cut at ``settle_by`` (at least
        0.5 s), so a slow broker cannot hold order work past the SIGKILL; a cut call
        raises ``TimeoutError``."""
        left = self._deadlines.settle_by() - self._clock()
        if math.isinf(left):
            return await fn(*args, **kwargs)
        return await asyncio.wait_for(fn(*args, **kwargs), max(_MIN_CALL_S, left))

    def _alert(self, severity: str, key: str, text: str, *, order_ids: Iterable[str] = (),
               dedup_s: float = 0.0) -> None:
        alerter = self._alerter if self._alerter is not None else get_alerter()
        alerter.alert(severity, key, text, order_ids=tuple(order_ids), dedup_s=dedup_s)

    def _journal_event(self, event: str, order_id: str, *, conf: Optional[Confirmation] = None,
                       status: str = "", note: str = "",
                       reported: Optional[Decimal] = None,
                       reported_avg: Optional[Decimal] = None) -> None:
        if self._journal is None:
            return
        tracked = self._registry.get(order_id)
        self._journal.record(
            event, order_id=order_id,
            internal_id=tracked.internal_id if tracked else "",
            symbol=tracked.symbol if tracked else "",
            security_id=tracked.security_id if tracked else "",
            segment=tracked.segment if tracked else "",
            side=tracked.side if tracked else "",
            qty=tracked.requested if tracked else None,
            purpose=tracked.purpose if tracked else "",
            filled=conf.filled_qty if conf else None,
            avg_price=conf.avg_price if conf else None,
            status=(conf.status_raw or conf.status) if conf else status,
            note=note,
            reported=reported,
            reported_avg=reported_avg,
            guessed=bool(tracked and tracked.guessed),
        )
