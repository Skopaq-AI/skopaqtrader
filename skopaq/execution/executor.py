"""Signal-to-execution pipeline.

Orchestrates the full flow: parse signal → (optionally) ATR-size →
build order → safety check → route to broker → log result.

This is the single entry point for all trade execution.

A live SELL holds its symbol's lock (``OrderRouter.sell_lock``) from the
order-book read until the broker's answer is final, and is checked against the
broker's open orders (``OrderRouter.sell_inputs``). Only the quantity the broker
confirmed filled reaches the notification and the loss limits.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import date
from typing import Callable, Iterable, Optional

from skopaq.broker.models import (
    ExecutionResult,
    OrderRequest,
    OrderType,
    Product,
    Side,
    TradingSignal,
    fill_status_of,
    filled_quantity_of,
    order_ids_of,
)
from skopaq.execution.order_alerts import get_alerter
from skopaq.execution.order_router import OrderRouter
from skopaq.execution.safety_checker import SafetyChecker, SafetyResult, _base_symbol
from skopaq.execution.sell_lock import SellLockBusy
from skopaq.risk.position_sizer import PositionSizer

logger = logging.getLogger(__name__)

# A refused live SELL is retried every monitor cycle (10 s): alert and notify it
# at most once per 10 minutes for the same symbol and reason
_REFUSAL_DEDUP_S = 600.0


def alert_sell_refused(order: OrderRequest, code: str, reason: str,
                       order_ids: Iterable[str] = ()) -> None:
    """Alert that a live SELL was refused before it reached the broker.

    CRITICAL for a protective exit (a MARKET SELL) or an unreadable order book or
    holdings, WARNING otherwise; deduplicated per symbol and ``code`` for 10 minutes. The
    text names the open orders that blocked it and their raw statuses.
    """
    protective = order.order_type == OrderType.MARKET
    unreadable = code in ("book-unreadable", "holdings-unreadable")
    severity = "CRITICAL" if protective or unreadable else "WARNING"
    what = "Protective SELL" if protective else "SELL"
    get_alerter().alert(severity, f"sell-refused:{order.symbol}:{code}",
                        f"{what} {order.quantity} {order.symbol} refused: {reason}",
                        order_ids=tuple(order_ids), dedup_s=_REFUSAL_DEDUP_S)


class Executor:
    """Orchestrates trade execution from signal to fill.

    Pipeline::

        TradingSignal → PositionSizer (optional) → OrderRequest
            → SafetyChecker → OrderRouter → ExecutionResult

    Args:
        router: Routes orders to paper or live backend.
        safety: Validates orders against immutable safety rules.
        position_sizer: ATR-based position sizer (None = use signal's quantity).
        clock: Monotonic clock for deduplicating refused-SELL notifications.
    """

    def __init__(
        self,
        router: OrderRouter,
        safety: SafetyChecker,
        position_sizer: Optional[PositionSizer] = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._router = router
        self._safety = safety
        self._sizer = position_sizer
        self._clock = clock
        self._refusals_notified: dict[tuple[str, str], float] = {}

    async def execute_signal(
        self,
        signal: TradingSignal,
        trade_date: Optional[str] = None,
        regime_scale: float = 1.0,
        calendar_scale: float = 1.0,
    ) -> ExecutionResult:
        """Execute a trading signal through the full pipeline.

        Steps:
            1. (Optional) Run ATR-based position sizing for BUY signals.
            2. Convert signal to an OrderRequest.
            3. Run safety checks against current portfolio.
            4. Route to paper engine or live broker.
            5. Record P&L for loss tracking.
            6. Return ExecutionResult.

        Args:
            signal: The trading signal to execute.
            trade_date: Current date (YYYY-MM-DD) for ATR lookup.
            regime_scale: Market regime multiplier (0.0–1.2).
            calendar_scale: Event calendar multiplier (0.0–1.0).
        """
        # Step 0a: Resolve entry_price if missing (upstream agents don't set it)
        if signal.action == "BUY" and not signal.entry_price:
            price = self._fetch_current_price(signal.symbol)
            if price:
                signal.entry_price = price
                logger.info("Resolved entry_price for %s: %.2f", signal.symbol, price)

        # Step 0b: ATR-based position sizing (BUY only)
        if self._sizer and signal.action == "BUY" and signal.entry_price:
            # Map confidence [0, 100] → scale [0.5, 1.0]
            confidence_scale = 0.5 + (signal.confidence / 100.0) * 0.5
            await self._apply_position_sizing(
                signal, trade_date or date.today().isoformat(),
                regime_scale, calendar_scale,
                confidence_scale=confidence_scale,
            )

        # Step 1: Build order from signal
        order = self._build_order(signal)
        if order is None:
            return ExecutionResult(
                success=False,
                signal=signal,
                mode=self._router.mode,
                safety_passed=False,
                rejection_reason=f"Cannot build order from signal: action={signal.action}",
            )

        # Steps 2–3: safety checks, then the backend. A live SELL holds its
        # symbol's lock from the order-book read until the broker's answer is
        # final, so no other Skopaq process can sell the same shares in between
        lock = self._router.sell_lock(order) if order.side == Side.SELL else None
        try:
            async with lock or contextlib.nullcontext():
                safety_result, positions, holdings, live_sell = await self._check(
                    order, signal)
                if not safety_result.passed:
                    return await self._refused(order, signal, safety_result, live_sell)
                result = await self._router.execute(order, signal)
        except SellLockBusy as exc:
            logger.warning("SELL %s not sent: %s", order.symbol, exc)
            reason = (f"Another Skopaq process is already selling {order.symbol} — "
                      "not sending a second SELL")
            alert_sell_refused(order, "lock-busy", reason)
            await self._notify_refusal(signal, reason)
            return ExecutionResult(
                success=False,
                signal=signal,
                mode=self._router.mode,
                safety_passed=False,
                rejection_reason=reason,
            )

        # Notify: trade result — the quantity the broker confirmed (paper: the
        # order's), and live, why a fill was partial, unconfirmed or failed. Live, it is
        # sent in the background: nothing may come between the broker's confirmed result
        # and the caller recording it (a caller cancelled while Telegram answered would
        # lose a fill that is final at the broker)
        status = fill_status_of(result)
        filled = filled_quantity_of(result, order.quantity)
        extra = {}
        if result.mode == "live":
            extra["reason"] = result.rejection_reason or result.broker_message
        notification = self._notify_safe(
            "notify_trade_event",
            signal.action, signal.symbol,
            result.fill_price or signal.entry_price or 0,
            int(filled if result.success else order.quantity),
            status,
            pnl=0,
            order_id=(",".join(order_ids_of(result))
                      or (result.order.order_id if result.order else "")),
            **extra,
        )
        if result.mode == "live":
            _in_background(notification)
        else:
            await notification

        # Step 4: Record P&L for loss tracking (on fills): the fill against the
        # cost basis of what was sold, for the shares actually sold, so the loss
        # limits and cool-down see it
        if result.success and result.fill_price and signal.action == "SELL":
            cost = _cost_basis(order.symbol, positions, holdings)
            if cost and filled:
                pnl = (result.fill_price - cost) * float(filled)
                self._safety.record_pnl(pnl)

        logger.info(
            "Execution %s: %s %s qty=%s mode=%s%s",
            "OK" if result.success else "FAILED",
            signal.action,
            signal.symbol,
            signal.quantity or order.quantity,
            result.mode,
            f" reason={result.rejection_reason}" if result.rejection_reason else "",
        )

        return result

    async def _check(
        self, order: OrderRequest, signal: TradingSignal,
    ) -> tuple[SafetyResult, list, list, bool]:
        """Run the safety checks; returns (result, positions, holdings, live SELL).

        A live SELL's positions and holdings come from ``router.sell_inputs``, read
        after the order book, which the check subtracts open SELLs from; a signal marked
        ``position_only`` (the monitor's and CLOSING's exits) may sell only what the day's
        position still holds. Paper and BUYs read positions and holdings as before
        (``sell_context=None``).
        """
        inputs = None
        if order.side == Side.SELL:
            # A protective exit of the day's position is re-checked against that position
            # (never older holdings), from the same book-first read, under the SELL lock
            position_only = getattr(signal, "position_only", False) is True
            inputs = (await self._router.sell_inputs(order, position_only=True)
                      if position_only else await self._router.sell_inputs(order))
        if inputs is None:
            positions = await self._router.get_positions()
            holdings = await self._holdings_for(order)
        else:
            positions, holdings = inputs.positions, inputs.holdings
        funds = await self._router.get_funds()
        portfolio_value = funds.total_collateral or funds.available_cash

        safety_result = self._safety.validate(
            order=order,
            signal=signal,
            positions=positions,
            funds=funds,
            portfolio_value=portfolio_value,
            holdings=holdings,
            sell_context=inputs.context if inputs is not None else None,
        )
        return safety_result, positions, holdings, inputs is not None

    async def _refused(
        self,
        order: OrderRequest,
        signal: TradingSignal,
        safety_result: SafetyResult,
        live_sell: bool,
    ) -> ExecutionResult:
        """Report a safety rejection (nothing reached the broker)."""
        if live_sell:
            code = safety_result.codes[0] if safety_result.codes else "safety"
            alert_sell_refused(order, code, safety_result.reason,
                               safety_result.blocking_order_ids)
            await self._notify_refusal(signal, safety_result.reason)
        else:
            await self._notify_safe(
                "notify_trade_event",
                signal.action, signal.symbol,
                signal.entry_price or 0, int(signal.quantity or 1),
                "REJECTED",
            )
        return ExecutionResult(
            success=False,
            signal=signal,
            mode=self._router.mode,
            safety_passed=False,
            rejection_reason=safety_result.reason,
        )

    async def _notify_refusal(self, signal: TradingSignal, reason: str) -> None:
        """The REJECTED notification for a live SELL, with its reason, at most once per
        10 minutes for the same symbol and reason (the monitor retries every cycle)."""
        now = self._clock()
        self._refusals_notified = {
            key: at for key, at in self._refusals_notified.items()
            if now - at < _REFUSAL_DEDUP_S
        }
        key = (signal.symbol, reason)
        if key in self._refusals_notified:
            logger.info("SELL %s refused again (not re-notified): %s", signal.symbol, reason)
            return
        self._refusals_notified[key] = now
        await self._notify_safe(
            "notify_trade_event",
            signal.action, signal.symbol,
            signal.entry_price or 0, int(signal.quantity or 1),
            "REJECTED",
            reason=reason,
        )

    async def _notify_safe(self, func_name: str, *args, **kwargs) -> None:
        """Call a notification function, silently catching errors."""
        try:
            import skopaq.notifications as notif

            fn = getattr(notif, func_name, None)
            if fn:
                await fn(*args, **kwargs)
        except Exception:
            pass  # Notifications should never break trading

    async def _apply_position_sizing(
        self,
        signal: TradingSignal,
        trade_date: str,
        regime_scale: float,
        calendar_scale: float,
        confidence_scale: float = 1.0,
    ) -> None:
        """Compute ATR-based position size and mutate the signal in place.

        Overrides signal.quantity and signal.stop_loss with risk-adjusted values.
        After computing the raw ATR-based size, caps the quantity to respect
        safety limits (max lots, max position %, max order value) so the
        downstream SafetyChecker won't reject an otherwise valid signal.

        Falls back gracefully if ATR data is unavailable.
        """
        try:
            funds = await self._router.get_funds()
            equity = funds.total_collateral or funds.available_cash

            size = self._sizer.compute_size(
                equity=equity,
                price=signal.entry_price,
                symbol=signal.symbol,
                trade_date=trade_date,
                regime_scale=regime_scale,
                calendar_scale=calendar_scale,
                confidence_scale=confidence_scale,
            )

            # Cap quantity to respect safety limits
            capped_qty = self._cap_quantity(
                size.quantity, signal.entry_price, equity,
            )

            # Mutate signal with computed values
            signal.quantity = capped_qty
            signal.stop_loss = size.stop_loss

            if capped_qty < size.quantity:
                logger.info(
                    "Position capped %s: %d → %d shares (safety limits: "
                    "max_lots=%d, max_position=%.0f%%, max_order=₹%.0f)",
                    signal.symbol, size.quantity, capped_qty,
                    self._safety._rules.max_lots_per_position,
                    self._safety._rules.max_position_pct * 100,
                    self._safety._rules.max_order_value_inr,
                )

            logger.info(
                "Position sized %s: qty=%d, stop=%.2f, risk=%.0f INR, "
                "ATR=%.2f (%s), regime=%.1f, calendar=%.1f, confidence=%.2f",
                signal.symbol, capped_qty, size.stop_loss, size.risk_amount,
                size.atr, size.atr_source, regime_scale, calendar_scale,
                confidence_scale,
            )

        except Exception:
            logger.warning(
                "Position sizing failed for %s — using signal defaults",
                signal.symbol, exc_info=True,
            )
            # Ensure stop-loss is set even when sizer fails — safety checker
            # requires it for BUY orders in live mode.
            if not signal.stop_loss and signal.entry_price:
                signal.stop_loss = round(signal.entry_price * 0.98, 2)
                logger.info(
                    "Fallback stop-loss for %s: %.2f (2%% below entry)",
                    signal.symbol, signal.stop_loss,
                )

    def _build_order(self, signal: TradingSignal) -> Optional[OrderRequest]:
        """Convert a TradingSignal into an OrderRequest."""
        if signal.action == "HOLD":
            return None

        side = Side.BUY if signal.action == "BUY" else Side.SELL

        # Determine order type: explicit (exits sell at MARKET), else LIMIT
        # at the entry price when there is one
        order_type = signal.order_type or (
            OrderType.LIMIT if signal.entry_price else OrderType.MARKET
        )

        # Determine quantity
        quantity = signal.quantity or 1  # Default to 1 if not specified

        return OrderRequest(
            symbol=signal.symbol,
            exchange=signal.exchange,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=signal.entry_price if order_type == OrderType.LIMIT else None,
            trigger_price=signal.stop_loss if side == Side.BUY else None,
            product=Product.CNC,
            tag=f"skopaq-{signal.confidence}",
        )

    async def _holdings_for(self, order: OrderRequest) -> list:
        """Delivery holdings, fetched only for SELLs (the no-short-sale check)."""
        if order.side != Side.SELL:
            return []
        try:
            return await self._router.get_settled_holdings()
        except Exception:
            logger.warning("Could not fetch holdings — SELL checked against positions only",
                           exc_info=True)
            return []

    def _cap_quantity(self, raw_qty: int, price: float, equity: float) -> int:
        """Cap raw ATR-computed quantity to respect safety limits.

        Applies three caps (takes the minimum):
        1. max_lots_per_position — absolute share limit per trade
        2. max_position_pct — order value as % of portfolio
        3. max_order_value_inr — absolute order value cap

        Returns at least 1 share.
        """
        import math

        rules = self._safety._rules
        qty = raw_qty

        # Cap 1: max lots per position
        qty = min(qty, rules.max_lots_per_position)

        # Cap 2: max position % of portfolio
        if price > 0 and equity > 0:
            max_value_by_pct = equity * rules.max_position_pct
            max_qty_by_pct = math.floor(max_value_by_pct / price)
            qty = min(qty, max_qty_by_pct)

        # Cap 3: max absolute order value
        if price > 0:
            max_qty_by_value = math.floor(rules.max_order_value_inr / price)
            qty = min(qty, max_qty_by_value)

        return max(1, qty)

    @staticmethod
    def _fetch_current_price(symbol: str) -> Optional[float]:
        """Fetch current market price via yfinance (best-effort).

        Indian stocks use the ``.NS`` suffix on Yahoo Finance.
        Returns None on any error — caller should handle gracefully.
        """
        try:
            import yfinance as yf

            # Indian stocks need .NS suffix for Yahoo Finance
            yf_symbol = f"{symbol}.NS" if not any(
                symbol.endswith(s) for s in (".NS", ".BO", "-USD", "USDT")
            ) else symbol

            ticker = yf.Ticker(yf_symbol)
            info = ticker.fast_info
            price = getattr(info, "last_price", None)
            if price and price > 0:
                return round(float(price), 2)
        except Exception:
            logger.debug("yfinance price fetch failed for %s", symbol, exc_info=True)
        return None


def _in_background(coro) -> None:
    """Send a notification as a background task (drained with the order alerts)."""
    alerter = get_alerter()
    background = getattr(alerter, "background", None)
    if callable(background):
        background(coro, "trade notification")
        return
    task = asyncio.get_running_loop().create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


_BACKGROUND: set = set()   # notifications sent without an OrderAlerter (kept referenced)


def _cost_basis(symbol: str, positions: list, holdings: list) -> Optional[float]:
    """Average price paid for *symbol*: a long position first, else the holding.

    Rows with no long quantity are skipped: live, shares sold earlier today show
    as a net-negative position while the delivery holding keeps the real cost.
    """
    base = _base_symbol(symbol)
    for item in [*positions, *holdings]:
        price = float(getattr(item, "average_price", 0) or 0)
        if (_base_symbol(getattr(item, "symbol", "")) == base
                and (getattr(item, "quantity", 0) or 0) > 0 and price > 0):
            return price
    return None
