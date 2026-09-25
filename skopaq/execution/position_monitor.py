"""Position Monitor — two-tier auto-sell worker.

Safety tier (every poll):  hard stop-loss, trailing stop, EOD exit.
AI tier (every N polls):   Gemini 3 Flash sell analyst for intelligent exits.

Usage::

    monitor = PositionMonitor(executor, client, router, config, llm, stop_event)
    result = await monitor.run()
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Optional

from skopaq.agents.sell_analyst import SellDecision, analyze_exit
from skopaq.broker.models import OrderType, TradingSignal

if TYPE_CHECKING:
    from skopaq.broker.client import INDstocksClient
    from skopaq.config import SkopaqConfig
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter

logger = logging.getLogger(__name__)

# IST = UTC+5:30
_IST = timezone(timedelta(hours=5, minutes=30))
_MARKET_CLOSE = time(15, 30)


@dataclass
class MonitoredPosition:
    """Per-position tracking state."""

    symbol: str
    scrip_code: str
    entry_price: float
    quantity: int
    high_water_mark: float = 0.0  # For trailing stop

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
    ):
        """``on_exit(signal, execution)`` is awaited after each successful sell,
        e.g. to persist it; its failures are logged, never raised."""
        self._executor = executor
        self._on_exit = on_exit
        self._client = client
        self._router = router
        self._config = config
        self._llm = llm
        self._stop = stop_event or asyncio.Event()
        self._ai_enabled = ai_enabled and llm is not None

        # Config values
        self._poll_interval = config.monitor_poll_interval_seconds
        self._hard_stop_pct = config.monitor_hard_stop_pct
        self._eod_minutes = config.monitor_eod_exit_minutes_before_close
        self._ai_interval = config.monitor_ai_interval_cycles
        self._trailing_enabled = config.monitor_trailing_stop_enabled
        self._trailing_pct = config.monitor_trailing_stop_pct

        # Minimum profit gate — prevents selling for tiny gains eaten by brokerage
        self._min_profit_pct = config.daemon_min_profit_threshold_pct
        self._min_profit_inr = config.daemon_min_profit_threshold_inr
        self._est_brokerage = 120.0  # ~₹60 per side for INDstocks (brokerage + GST)

    async def run(self) -> MonitorResult:
        """Main monitoring loop.  Returns when all positions are closed,
        the stop event is set (Ctrl+C), or the market closes."""
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
        now_ist = datetime.now(_IST).time()
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

        Returns True if the sell was executed successfully.
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

            if exec_result.success:
                await self._record_exit(signal, exec_result)
                pnl = (ltp - pos.entry_price) * pos.quantity
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
                return True
            else:
                result.sells_failed += 1
                logger.error(
                    "SELL REJECTED for %s: %s",
                    pos.symbol, exec_result.rejection_reason,
                )
                return False

        except Exception:
            result.sells_failed += 1
            logger.error(
                "SELL FAILED for %s", pos.symbol, exc_info=True,
            )
            return False
