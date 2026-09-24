"""Signal-to-execution pipeline.

Orchestrates the full flow: parse signal → (optionally) ATR-size →
build order → safety check → route to broker → log result.

This is the single entry point for all trade execution.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from skopaq.broker.models import (
    ExecutionResult,
    OrderRequest,
    OrderType,
    Product,
    Side,
    TradingSignal,
)
from skopaq.execution.order_router import OrderRouter
from skopaq.execution.safety_checker import SafetyChecker
from skopaq.risk.position_sizer import PositionSizer

logger = logging.getLogger(__name__)


class Executor:
    """Orchestrates trade execution from signal to fill.

    Pipeline::

        TradingSignal → PositionSizer (optional) → OrderRequest
            → SafetyChecker → OrderRouter → ExecutionResult

    Args:
        router: Routes orders to paper or live backend.
        safety: Validates orders against immutable safety rules.
        position_sizer: ATR-based position sizer (None = use signal's quantity).
    """

    def __init__(
        self,
        router: OrderRouter,
        safety: SafetyChecker,
        position_sizer: Optional[PositionSizer] = None,
    ) -> None:
        self._router = router
        self._safety = safety
        self._sizer = position_sizer

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

        # Step 2: Safety checks
        positions = await self._router.get_positions()
        holdings = await self._holdings_for(order)
        funds = await self._router.get_funds()
        portfolio_value = funds.total_collateral or funds.available_cash

        safety_result = self._safety.validate(
            order=order,
            signal=signal,
            positions=positions,
            funds=funds,
            portfolio_value=portfolio_value,
            holdings=holdings,
        )

        if not safety_result.passed:
            # Notify: safety rejection
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

        # Step 3: Route to execution backend
        result = await self._router.execute(order, signal)

        # Notify: trade result
        await self._notify_safe(
            "notify_trade_event",
            signal.action, signal.symbol,
            result.fill_price or signal.entry_price or 0,
            int(order.quantity),
            "FILLED" if result.success else "FAILED",
            pnl=0,
            order_id=result.order.order_id if result.order else "",
        )

        # Step 4: Record P&L for loss tracking (on fills)
        if result.success and result.fill_price and signal.action == "SELL":
            # Approximate P&L from signal entry vs fill
            entry = signal.entry_price or 0
            if entry > 0 and order.quantity:
                pnl = (result.fill_price - entry) * float(order.quantity)
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

        # Determine order type
        if signal.entry_price:
            order_type = OrderType.LIMIT
        else:
            order_type = OrderType.MARKET

        # Determine quantity
        quantity = signal.quantity or 1  # Default to 1 if not specified

        return OrderRequest(
            symbol=signal.symbol,
            exchange=signal.exchange,
            side=side,
            quantity=quantity,
            order_type=order_type,
            price=signal.entry_price,
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
