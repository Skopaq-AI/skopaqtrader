"""Routes orders to paper engine or live broker based on trading mode.

The router is intentionally thin — it checks ``config.trading_mode`` and
dispatches to the appropriate execution backend.  Switching paper → live
is a config change, not a code change.

Live orders go through ``LiveOrderWorker``, which confirms fills with the
broker: a live result is a success only when INDstocks reports a fill, and
carries the filled quantity and average price it reported.

A live SELL is checked against the broker's open orders too: ``sell_inputs``
reads the order book before positions and holdings, and ``sell_lock`` keeps a
second Skopaq process from selling the same shares at the same time.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable, Optional

from skopaq.broker.book_snapshot import BrokerSnapshot, read_broker_snapshot
from skopaq.broker.client import INDstocksClient
from skopaq.broker.models import (
    ExecutionResult,
    Funds,
    Holding,
    OrderRequest,
    OrderResponse,
    Position,
    Side,
    TradingSignal,
)
from skopaq.broker.order_status import parse_order_book
from skopaq.broker.paper_engine import PaperEngine
from skopaq.broker.scrip_resolver import resolve_security_id
from skopaq.config import SkopaqConfig
from skopaq.execution.live_orders import (
    FillSettings,
    LiveOrderWorker,
    OrderDeadlines,
    OrderRegistry,
    own_open_sells,
    recent_exit_qty,
    uncertain_sells,
)
from skopaq.execution.order_alerts import get_alerter
from skopaq.execution.order_journal import OrderJournal
from skopaq.execution.sell_lock import OrderLock, SellLock, lock_dir_or_none
from skopaq.execution.sellable import SellContext
from skopaq.risk.calendar import now_ist

logger = logging.getLogger(__name__)

_DEDUP_S = 600.0   # sell-without-book alerts: at most one per symbol per 10 minutes
# After a stop, a book → positions → holdings read gets what is left until settle_by, but
# at least this long (the monitor's and CLOSING's last reads of what is left open)
_MIN_READ_AFTER_STOP_S = 5.0


@dataclass(frozen=True)
class SellInputs:
    """What a live SELL's no-short-sale check needs, from one book-first read."""

    positions: list[Position]
    holdings: list[Holding]
    context: SellContext


class OrderRouter:
    """Routes orders to the correct execution backend.

    In ``paper`` mode all orders go through the PaperEngine.
    In ``live`` mode orders go to the INDstocks REST API through a
    ``LiveOrderWorker``, which waits for the broker to confirm each fill,
    cancels what does not fill in time, and works protective exits until
    they fill (or alerts).

    Args:
        config: Application configuration (determines mode).
        paper_engine: Paper trading engine instance.
        live_client: INDstocks REST client (can be None in paper-only mode).
        fill_settings: Live fill timeouts (default: from ``config``).
        sleep, clock, wall: Time sources for the live worker (tests pass fakes).
    """

    def __init__(
        self,
        config: SkopaqConfig,
        paper_engine: PaperEngine,
        live_client: Optional[INDstocksClient] = None,
        *,
        fill_settings: Optional[FillSettings] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = now_ist,
    ) -> None:
        self._mode = config.trading_mode
        self._paper = paper_engine
        self._live = live_client
        self._sleep = sleep
        self._clock = clock
        self._wall = wall
        self._registry = OrderRegistry(clock=clock)
        self._deadlines = OrderDeadlines(clock=clock, wall=wall)
        self._journal: Optional[OrderJournal] = None
        self._worker: Optional[LiveOrderWorker] = None
        self._lock_dir: Optional[Path] = None
        # Live only: a paper router never builds the worker, even with a live client (nor
        # resolves the journal and lock directories, which paper never uses)
        if self._mode == "live" and live_client is not None:
            try:
                self._journal = OrderJournal.from_config(config, wall=wall)
            except (RuntimeError, OSError, ValueError) as exc:
                logger.warning("SKOPAQ_ORDER_JOURNAL_DIR cannot be used (%s); live orders are "
                               "not journalled (a restarted process will not recognise "
                               "them)", exc)
            self._lock_dir = lock_dir_or_none(config)
            self._worker = LiveOrderWorker(
                live_client,
                fill_settings or FillSettings.from_config(config),
                deadlines=self._deadlines,
                registry=self._registry,
                journal=self._journal,
                sleep=sleep,
                clock=clock,
                wall=wall,
            )
        # Only an explicit True: a mock or a string must never switch the book check off
        self._allow_without_book = (
            getattr(config, "allow_sell_without_order_book", False) is True)
        if self._allow_without_book and self._worker is not None:
            logger.critical(
                "SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK is on: a live SELL whose order-book "
                "read fails is checked WITHOUT open SELL orders (it can sell shares twice)")

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def worker(self) -> Optional[LiveOrderWorker]:
        """The live order worker; None in paper mode or without a live client."""
        return self._worker

    @property
    def registry(self) -> OrderRegistry:
        """This router's live orders (shared by its executor, monitor and CLOSING)."""
        return self._registry

    @property
    def deadlines(self) -> OrderDeadlines:
        return self._deadlines

    @property
    def journal(self) -> Optional[OrderJournal]:
        """This host's order journal (live only): lets the monitor resume orders an
        earlier Skopaq process left open, and tell them from someone else's."""
        return self._journal

    @property
    def allows_sell_without_order_book(self) -> bool:
        """SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK (default off)."""
        return self._allow_without_book

    def arm_shutdown(self, budget_s: float) -> None:
        """Stop live order work within ``budget_s`` seconds (after SIGTERM/SIGINT).

        No new order after ``budget_s`` minus room to cancel and settle the last one;
        no order work at all after ``budget_s``. Idempotent; a no-op in paper mode.
        """
        if self._worker is None:
            return
        settings = self._worker.settings
        self._deadlines.arm_shutdown(
            budget_s, settings.cancel_confirm_timeout_s + 2 * settings.poll_interval_s)

    async def execute(
        self,
        order: OrderRequest,
        signal: Optional[TradingSignal] = None,
    ) -> ExecutionResult:
        """Route an order to the appropriate backend."""
        if self._mode == "live":
            return await self._execute_live(order, signal)
        return self._execute_paper(order, signal)

    def _execute_paper(
        self,
        order: OrderRequest,
        signal: Optional[TradingSignal],
    ) -> ExecutionResult:
        """Execute via paper engine (synchronous)."""
        return self._paper.execute_order(order, signal)

    async def _execute_live(
        self,
        order: OrderRequest,
        signal: Optional[TradingSignal],
    ) -> ExecutionResult:
        """Execute via live INDstocks API.

        Resolves ``security_id`` from the instruments CSV if not already
        set on the order, then hands the order to the live worker, which
        returns only what the broker confirmed (filled quantity, average
        price, order ids) and never raises.
        """
        if self._live is None or self._worker is None:
            logger.error("Live client not configured — falling back to paper")
            return self._execute_paper(order, signal)

        try:
            await self._ensure_security_id(order)
        except Exception as exc:
            logger.error("Live order failed: %s — NOT falling back to paper", exc)
            return ExecutionResult(
                success=False,
                signal=signal,
                mode="live",
                rejection_reason=f"Broker error: {exc}",
            )
        return await self._worker.execute(order, signal)

    async def _ensure_security_id(self, order: OrderRequest) -> None:
        """Resolve ``order.security_id`` if missing (executor builds orders without it)."""
        if order.security_id:
            return
        order.security_id = await resolve_security_id(
            self._live, order.symbol, order.exchange.value,
        )
        logger.info("Resolved %s → security_id=%s", order.symbol, order.security_id)

    # ── Live SELLs: open orders and the per-symbol lock ───────────────────

    async def sell_inputs(self, order: OrderRequest, *,
                          position_only: bool = False) -> Optional[SellInputs]:
        """Positions, holdings and open orders for a SELL's check, read book-first.

        None in paper or without a live client (callers keep today's reads). Resolves
        ``order.security_id`` first, because order-book rows carry no symbol; if that
        fails, every open SELL in the book counts against the order (conservative).
        A positions error propagates. A book error becomes ``context.error`` (the SELL
        is refused), or, with SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK, ``context.override``
        plus a CRITICAL ``sell-without-book`` alert.

        ``position_only`` (a protective exit of the day's position: the monitor, CLOSING)
        limits the SELL to what that position still holds after Skopaq's own open,
        unconfirmed and not-yet-shown SELLs — never older delivery holdings.
        """
        if self._mode != "live" or self._live is None or self._worker is None:
            return None
        try:
            await self._ensure_security_id(order)
        except Exception as exc:
            logger.warning("Could not resolve %s's security id (%s): every open SELL in "
                           "the order book counts against it", order.symbol, exc)

        settings = self._worker.settings
        snap = await self._bounded(read_broker_snapshot(
            self._live, extra_terminal=settings.extra_terminal_statuses,
            sleep=self._sleep, wall=self._wall,
        ))
        lag = settings.sell_fill_lag_window_s
        own = set(self._registry.ids())
        if self._journal is not None:
            own |= self._journal.own_ids_today()
        error, override = snap.book_error, False
        if error and self._allow_without_book:
            error, override = "", True
            get_alerter().alert(
                "CRITICAL", f"sell-without-book:{order.symbol}",
                f"SELL {order.quantity} {order.symbol} checked WITHOUT the order book "
                f"({snap.book_error}) because SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK is on — "
                "open SELL orders are not counted", dedup_s=_DEDUP_S)
        context = SellContext(
            orders=() if (error or override) else snap.orders,
            read_at=snap.read_at,
            error=error,
            override=override,
            own_recent_exit_qty=recent_exit_qty(
                self._registry, self._journal, order.symbol, order.security_id, lag,
                snap.read_at),
            lag_window_s=lag,
            own_order_ids=frozenset(own),
            uncertain=uncertain_sells(self._registry, self._journal),
            holdings_error=snap.holdings_error,
            own_open=own_open_sells(self._registry, self._journal, snap.read_at),
            position_only=position_only,
        )
        return SellInputs(positions=list(snap.positions), holdings=list(snap.holdings),
                          context=context)

    def sell_lock(self, order: OrderRequest) -> Optional[SellLock]:
        """The symbol's SELL lock, to hold from ``sell_inputs`` until ``execute`` returns.

        None in paper, without a live client, or for a BUY. It waits at most as long as
        one worst-case protective exit (plus 10 s), and never past the shutdown deadline.
        """
        if self._worker is None or order.side != Side.SELL or not self._locks_usable():
            return None
        wait_s = min(self._worker.settings.exit_worst_case_s + 10,
                     max(0.0, self._deadlines.settle_by() - self._clock()))
        return SellLock(order.symbol, wait_s=wait_s, lock_dir=self._lock_dir,
                        sleep=self._sleep, clock=self._clock)

    def order_lock(self, order_id: str) -> Optional[OrderLock]:
        """The lock one Skopaq process holds while it resumes ``order_id`` and records its
        late fill (never waited for). None in paper or when locks cannot be used."""
        if self._worker is None or not self._locks_usable():
            return None
        return OrderLock(order_id, lock_dir=self._lock_dir, sleep=self._sleep,
                         clock=self._clock)

    def _locks_usable(self) -> bool:
        if self._lock_dir is not None:
            return True
        get_alerter().alert(
            "WARNING", "sell-lock-unavailable",
            "SKOPAQ_ORDER_LOCK_DIR cannot be used: live SELLs and order resumes run without "
            "the cross-process lock — the order-book check still applies")
        return False

    # ── Portfolio queries (unified interface) ─────────────────────────────

    async def get_positions(self) -> list[Position]:
        """Get positions from the active backend."""
        if self._mode == "live" and self._live:
            return await self._live.get_positions()
        return self._paper.get_positions()

    async def get_holdings(self) -> list[Holding]:
        """Get holdings from the active backend."""
        if self._mode == "live" and self._live:
            return await self._live.get_holdings()
        return self._paper.get_holdings()

    async def get_settled_holdings(self) -> list[Holding]:
        """Holdings not already counted in positions, for the no-short-sale check.

        Live: the broker's delivery holdings (earlier days' shares), which its
        positions (today's net quantities) do not include. Paper: none — the
        paper engine's positions are its whole book and its holdings only
        mirror them, so counting both would double every share.
        """
        if self._mode == "live" and self._live:
            return await self._live.get_holdings()
        return []

    async def get_funds(self) -> Funds:
        """Get funds from the active backend."""
        if self._mode == "live" and self._live:
            return await self._live.get_funds()
        return self._paper.get_funds()

    async def get_orders(self) -> list[OrderResponse]:
        """Get today's orders from the active backend.

        Live: the broker's order book (INDstocksClient has no ``get_orders``), with
        normalised statuses and the broker's reason (``extra_info``) as the message.
        """
        if self._mode == "live" and self._live:
            return [
                OrderResponse(
                    order_id=snap.order_id,
                    status=snap.status,
                    message=snap.message,
                    exchange_order_id=snap.exch_order_id or None,
                )
                for snap in parse_order_book(await self._live.get_order_book())
            ]
        return self._paper.get_orders()

    async def broker_snapshot(
        self,
        *,
        need_holdings: bool = True,
        extra_terminal: frozenset[str] = frozenset(),
    ) -> Optional[BrokerSnapshot]:
        """The broker's order book, positions and holdings, read book-first.

        None in paper mode or without a live client: the paper engine fills or
        rejects at once, so it has no open orders to count. After a stop the read is cut
        at the shutdown deadline (``_bounded``).
        """
        if self._mode != "live" or self._live is None:
            return None
        return await self._bounded(read_broker_snapshot(
            self._live, need_holdings=need_holdings, extra_terminal=extra_terminal,
            sleep=self._sleep, wall=self._wall,
        ))

    async def _bounded(self, read: Awaitable):
        """Await a broker read; once a shutdown is armed it is cut at ``settle_by`` (at
        least ``_MIN_READ_AFTER_STOP_S``) and raises ``TimeoutError``, so a slow broker
        cannot hold the monitor's or CLOSING's last reads past the scheduler's SIGKILL.
        Callers already treat a failed read as one (they keep the last known state)."""
        left = self._deadlines.settle_by() - self._clock()
        if math.isinf(left):
            return await read
        return await asyncio.wait_for(read, max(_MIN_READ_AFTER_STOP_S, left))
