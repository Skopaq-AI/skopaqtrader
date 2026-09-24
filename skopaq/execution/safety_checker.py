"""Pre-trade safety validation against immutable rules.

Every order passes through ``SafetyChecker.validate()`` before reaching
the broker or paper engine.  The rules come from ``constants.SAFETY_RULES``
which is a frozen dataclass — no runtime code can modify them.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, time, timezone
from typing import Optional

from skopaq.broker.models import (
    Funds,
    Holding,
    OrderRequest,
    OrderType,
    Position,
    Side,
    TradingSignal,
)
from skopaq.constants import (
    NSE_MARKET_CLOSE,
    NSE_MARKET_OPEN,
    SAFETY_RULES,
    SafetyRules,
)
from skopaq.risk.concentration import ConcentrationChecker

logger = logging.getLogger(__name__)

# NSE option contract symbols end in a strike and CE/PE, e.g. NIFTY23DEC21000CE.
_OPTION_RE = re.compile(r"\d+(?:CE|PE)$")


@dataclass
class SafetyResult:
    """Outcome of a safety check."""

    passed: bool
    rejections: list[str]

    @property
    def reason(self) -> str:
        return "; ".join(self.rejections) if self.rejections else ""


class SafetyChecker:
    """Validates orders against immutable safety rules.

    Args:
        rules: Frozen SafetyRules instance (defaults to global singleton).
        ist_offset_hours: UTC offset for IST (default 5.5).
    """

    def __init__(
        self,
        rules: SafetyRules = SAFETY_RULES,
        ist_offset_hours: float = 5.5,
        max_sector_concentration_pct: float = 0.40,
    ) -> None:
        self._rules = rules
        self._ist_offset_hours = ist_offset_hours
        self._concentration = ConcentrationChecker(max_sector_concentration_pct)

        # Intra-day tracking (reset daily)
        self._orders_this_minute: list[datetime] = []
        self._day_pnl: float = 0.0
        self._week_pnl: float = 0.0
        self._month_pnl: float = 0.0
        self._last_loss_time: Optional[datetime] = None

    def validate(
        self,
        order: OrderRequest,
        signal: Optional[TradingSignal],
        positions: list[Position],
        funds: Funds,
        portfolio_value: float,
        holdings: Optional[list[Holding]] = None,
    ) -> SafetyResult:
        """Run all safety checks on a proposed order.

        ``holdings`` (delivery holdings) count alongside ``positions`` when
        checking that a SELL only sells what is held.

        Returns a ``SafetyResult`` with ``passed=True`` if all checks pass,
        or ``passed=False`` with a list of rejection reasons.
        """
        rejections: list[str] = []

        self._check_trading_halt(order, rejections)
        self._check_market_hours(rejections)
        self._check_no_short_sale(order, positions, holdings or [], rejections)
        self._check_position_size(order, portfolio_value, rejections)
        self._check_order_value(order, rejections)
        self._check_max_positions(order, positions, rejections)
        self._check_stop_loss(order, signal, rejections)
        self._check_min_stop_loss_pct(order, rejections)
        self._check_max_lots(order, rejections)
        self._check_sector_concentration(order, positions, portfolio_value, rejections)
        self._check_daily_loss(portfolio_value, rejections)
        self._check_weekly_loss(portfolio_value, rejections)
        self._check_monthly_loss(portfolio_value, rejections)
        self._check_order_rate(rejections)
        self._check_cool_down(rejections)
        self._check_naked_options(order, rejections)
        self._check_sufficient_funds(order, funds, rejections)
        self._check_minimum_confidence(signal, rejections)

        result = SafetyResult(passed=len(rejections) == 0, rejections=rejections)

        if not result.passed:
            logger.warning("Order REJECTED: %s %s — %s", order.side, order.symbol, result.reason)
        else:
            # Track order timestamp for rate limiting
            self._orders_this_minute.append(datetime.now(timezone.utc))

        return result

    # ── Individual checks ────────────────────────────────────────────────

    def _check_market_hours(self, rejections: list[str]) -> None:
        """Reject orders outside NSE market hours (9:15–15:30 IST)."""
        if not self._rules.market_hours_only:
            return

        now_utc = datetime.now(timezone.utc)
        # Convert to IST (UTC + 5:30)
        ist_hour = (now_utc.hour + int(self._ist_offset_hours)) % 24
        ist_minute = now_utc.minute + int((self._ist_offset_hours % 1) * 60)
        if ist_minute >= 60:
            ist_hour += 1
            ist_minute -= 60
        ist_time = time(ist_hour, ist_minute)

        if ist_time < NSE_MARKET_OPEN or ist_time > NSE_MARKET_CLOSE:
            rejections.append(
                f"Outside market hours (IST {ist_time.strftime('%H:%M')}, "
                f"market {NSE_MARKET_OPEN.strftime('%H:%M')}-{NSE_MARKET_CLOSE.strftime('%H:%M')})"
            )

    def _check_position_size(
        self, order: OrderRequest, portfolio_value: float, rejections: list[str],
    ) -> None:
        """Reject if order exceeds max position % of capital.

        Small-account exemption: buying 1 share (the minimum) is always
        allowed if the account can afford it, even when the percentage
        exceeds the limit.  The 15% rule is designed to prevent
        over-concentration in larger accounts — blocking the only
        possible trade in a small account defeats its purpose.
        """
        if portfolio_value <= 0:
            return
        price = order.price or 0
        if price <= 0:
            return  # Can't check for market orders without a price estimate
        order_value = price * float(order.quantity)
        pct = order_value / portfolio_value
        if pct > self._rules.max_position_pct:
            # Small-account exemption: allow minimum-qty orders that fit in cash
            if order.quantity <= 1 and order_value <= portfolio_value:
                logger.info(
                    "Position size %.1f%% exceeds %.0f%% limit, but allowing "
                    "minimum-qty order (small-account exemption)",
                    pct * 100, self._rules.max_position_pct * 100,
                )
                return
            rejections.append(
                f"Position size {pct:.1%} exceeds max {self._rules.max_position_pct:.0%}"
            )

    def _check_order_value(self, order: OrderRequest, rejections: list[str]) -> None:
        """Reject if order value exceeds absolute cap."""
        price = order.price or 0
        if price <= 0:
            return
        order_value = price * float(order.quantity)
        if order_value > self._rules.max_order_value_inr:
            rejections.append(
                f"Order value INR {order_value:,.0f} exceeds max INR {self._rules.max_order_value_inr:,.0f}"
            )

    def _check_trading_halt(self, order: OrderRequest, rejections: list[str]) -> None:
        """Reject every BUY while the kill switch is on (``skopaq halt``).

        SELLs stay allowed so open positions can still be protected; the
        no-short-sale check means a SELL can only reduce what is held.
        """
        if order.side != Side.BUY:
            return
        from skopaq.execution import kill_switch

        halt = kill_switch.status()
        if halt.halted:
            rejections.append(f"{halt.describe()} — run `skopaq resume` to lift")

    def _check_no_short_sale(
        self,
        order: OrderRequest,
        positions: list[Position],
        holdings: list[Holding],
        rejections: list[str],
    ) -> None:
        """Reject a SELL for more than is held — Skopaq never sells short.

        A SELL signal on a stock we do not own (e.g. a Sell rating on a
        scanner candidate) would otherwise become a short delivery sale.
        Option writes are handled by the naked-options check.

        Sellable = settled holdings + today's net positions, both signed: a
        position is negative after selling earlier holdings today, so those
        shares are no longer counted. ``holdings`` must not repeat
        ``positions`` (``OrderRouter.get_settled_holdings``).
        """
        if order.side != Side.SELL or _OPTION_RE.search(order.symbol):
            return
        symbol = _base_symbol(order.symbol)
        held = sum(
            (item.quantity for item in [*holdings, *positions]
             if _base_symbol(item.symbol) == symbol),
            start=type(order.quantity)(0),
        )
        if held < order.quantity:
            rejections.append(
                f"No short sales: SELL {order.quantity} {order.symbol} but only {held} held"
            )

    def _check_max_positions(
        self, order: OrderRequest, positions: list[Position], rejections: list[str],
    ) -> None:
        """Reject if opening a new position would exceed max concurrent positions."""
        if order.side != Side.BUY:
            return  # Sell reduces positions
        existing_symbols = {p.symbol for p in positions if p.quantity > 0}
        if order.symbol not in existing_symbols:
            if len(existing_symbols) >= self._rules.max_open_positions:
                rejections.append(
                    f"Max {self._rules.max_open_positions} open positions reached"
                )

    def _check_stop_loss(
        self,
        order: OrderRequest,
        signal: Optional[TradingSignal],
        rejections: list[str],
    ) -> None:
        """Reject BUY orders without a stop loss when required."""
        if not self._rules.require_stop_loss:
            return
        if order.side != Side.BUY:
            return

        has_sl = False
        # Check if signal has stop_loss
        if signal and signal.stop_loss and signal.stop_loss > 0:
            has_sl = True
        # Check if order itself is SL type
        if order.order_type in (OrderType.SL, OrderType.SLM):
            has_sl = True
        # Check trigger price
        if order.trigger_price and order.trigger_price > 0:
            has_sl = True

        if not has_sl:
            rejections.append("BUY order requires a stop-loss (rule: require_stop_loss=True)")

    def _check_min_stop_loss_pct(self, order: OrderRequest, rejections: list[str]) -> None:
        """Reject if stop-loss distance is below the minimum percentage.

        A stop-loss that is too tight (e.g., 0.5% from entry on a 2%+ ATR stock)
        will get triggered by normal intraday noise, defeating its purpose.
        """
        if not self._rules.require_stop_loss:
            return
        if order.side != Side.BUY:
            return
        if not order.trigger_price or not order.price:
            return  # No stop-loss price to validate
        if order.price <= 0:
            return

        sl_distance = abs(order.price - order.trigger_price) / order.price
        if sl_distance < self._rules.min_stop_loss_pct:
            rejections.append(
                f"Stop loss too tight: {sl_distance:.1%} distance < {self._rules.min_stop_loss_pct:.0%} minimum"
            )

    def _check_max_lots(self, order: OrderRequest, rejections: list[str]) -> None:
        """Reject if order quantity exceeds the per-position lot limit.

        Prevents accidentally sized orders (e.g., 50 lots from a parsing error)
        from reaching the broker.
        """
        if order.quantity > self._rules.max_lots_per_position:
            rejections.append(
                f"Quantity {order.quantity} exceeds max {self._rules.max_lots_per_position} per position"
            )

    def _check_sector_concentration(
        self,
        order: OrderRequest,
        positions: list[Position],
        portfolio_value: float,
        rejections: list[str],
    ) -> None:
        """Reject if adding this order would breach sector concentration limits.

        Delegates to ConcentrationChecker which uses a static NIFTY 50 sector map
        to classify symbols.  Unknown symbols are allowed through (cannot check).
        """
        if order.side != Side.BUY:
            return  # Only BUY orders increase exposure
        price = order.price or 0
        if price <= 0 or portfolio_value <= 0:
            return
        order_value = price * float(order.quantity)
        reason = self._concentration.check(
            symbol=order.symbol,
            order_value=order_value,
            positions=positions,
            portfolio_value=portfolio_value,
        )
        if reason:
            rejections.append(reason)

    def _check_daily_loss(self, portfolio_value: float, rejections: list[str]) -> None:
        """Reject if daily loss exceeds circuit breaker threshold."""
        if portfolio_value <= 0:
            return
        loss_pct = abs(self._day_pnl) / portfolio_value if self._day_pnl < 0 else 0
        if loss_pct >= self._rules.max_daily_loss_pct:
            rejections.append(
                f"Daily loss {loss_pct:.1%} hit circuit breaker ({self._rules.max_daily_loss_pct:.0%})"
            )

    def _check_weekly_loss(self, portfolio_value: float, rejections: list[str]) -> None:
        """Reject if weekly loss exceeds threshold."""
        if portfolio_value <= 0:
            return
        loss_pct = abs(self._week_pnl) / portfolio_value if self._week_pnl < 0 else 0
        if loss_pct >= self._rules.max_weekly_loss_pct:
            rejections.append(
                f"Weekly loss {loss_pct:.1%} hit circuit breaker ({self._rules.max_weekly_loss_pct:.0%})"
            )

    def _check_monthly_loss(self, portfolio_value: float, rejections: list[str]) -> None:
        """Reject if monthly loss exceeds threshold."""
        if portfolio_value <= 0:
            return
        loss_pct = abs(self._month_pnl) / portfolio_value if self._month_pnl < 0 else 0
        if loss_pct >= self._rules.max_monthly_loss_pct:
            rejections.append(
                f"Monthly loss {loss_pct:.1%} hit circuit breaker ({self._rules.max_monthly_loss_pct:.0%})"
            )

    def _check_order_rate(self, rejections: list[str]) -> None:
        """Reject if order rate exceeds max orders per minute."""
        now = datetime.now(timezone.utc)
        # Prune orders older than 60 seconds
        cutoff = now.timestamp() - 60
        self._orders_this_minute = [
            t for t in self._orders_this_minute if t.timestamp() > cutoff
        ]
        if len(self._orders_this_minute) >= self._rules.max_orders_per_minute:
            rejections.append(
                f"Rate limit: {len(self._orders_this_minute)} orders in last minute "
                f"(max {self._rules.max_orders_per_minute})"
            )

    def _check_cool_down(self, rejections: list[str]) -> None:
        """Reject if still in cool-down after a loss."""
        if self._last_loss_time is None:
            return
        now = datetime.now(timezone.utc)
        elapsed = (now - self._last_loss_time).total_seconds() / 60
        if elapsed < self._rules.cool_down_after_loss_minutes:
            remaining = self._rules.cool_down_after_loss_minutes - elapsed
            rejections.append(f"Cool-down active: {remaining:.0f} minutes remaining after loss")

    def _check_naked_options(self, order: OrderRequest, rejections: list[str]) -> None:
        """Reject naked option selling (placeholder — requires option chain context)."""
        if not self._rules.no_naked_option_selling:
            return
        # Full implementation requires checking if the position has a hedge.
        # For Phase 1, flag any SELL order on option symbols.
        # NSE options have a strike price (digits) before CE/PE suffix,
        # e.g. NIFTY23DEC21000CE. Use regex to avoid false positives
        # on equity symbols like RELIANCE that happen to end with "CE".
        if order.side == Side.SELL and _OPTION_RE.search(order.symbol):
            rejections.append(
                "Naked option selling is forbidden. Ensure a protective position exists."
            )

    def _check_sufficient_funds(
        self, order: OrderRequest, funds: Funds, rejections: list[str],
    ) -> None:
        """Reject BUY if insufficient margin."""
        if order.side != Side.BUY:
            return
        price = order.price or 0
        if price <= 0:
            return
        required = price * float(order.quantity)
        if required > funds.available_margin:
            rejections.append(
                f"Insufficient margin: need INR {required:,.0f}, available INR {funds.available_margin:,.0f}"
            )

    def _check_minimum_confidence(
        self, signal: Optional[TradingSignal], rejections: list[str],
    ) -> None:
        """Reject signals below minimum confidence threshold."""
        if self._rules.min_confidence_pct <= 0:
            return  # Gate disabled
        if signal is None:
            return
        if signal.confidence < self._rules.min_confidence_pct:
            rejections.append(
                f"Confidence {signal.confidence}% below minimum "
                f"{self._rules.min_confidence_pct}%"
            )

    # ── P&L tracking (called externally after fills) ─────────────────────

    def seed_realized_pnl(self, day: float, week: float, month: float) -> None:
        """Start from P&L already realized this day, week and month.

        Each CLI command, MCP call and daemon session builds a fresh checker;
        without this, the loss limits would only see trades closed inside
        the current process (``skopaq.execution.pnl_history``).
        """
        self._day_pnl = day
        self._week_pnl = week
        self._month_pnl = month

    def record_pnl(self, pnl: float) -> None:
        """Record a trade's P&L for loss tracking."""
        self._day_pnl += pnl
        self._week_pnl += pnl
        self._month_pnl += pnl
        if pnl < 0:
            self._last_loss_time = datetime.now(timezone.utc)

    def reset_daily(self) -> None:
        """Reset daily counters (call at start of each trading day)."""
        self._day_pnl = 0.0
        self._orders_this_minute.clear()
        self._last_loss_time = None

    def reset_weekly(self) -> None:
        """Reset weekly P&L counter."""
        self._week_pnl = 0.0

    def reset_monthly(self) -> None:
        """Reset monthly P&L counter."""
        self._month_pnl = 0.0


def _base_symbol(symbol: str) -> str:
    """``NSE:RELIANCE-EQ`` / ``RELIANCE.NS`` / ``reliance`` → ``RELIANCE``."""
    symbol = symbol.upper().split(":")[-1]
    for suffix in ("-EQ", "-BE", ".NS", ".BO"):
        if symbol.endswith(suffix):
            symbol = symbol[: -len(suffix)]
    return symbol
