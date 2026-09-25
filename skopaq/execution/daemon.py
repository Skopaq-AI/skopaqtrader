"""Autonomous Trading Daemon — Scan -> Trade -> Monitor -> Close.

Finite state machine that composes four existing subsystems into a single
trading session:

    PRE_OPEN -> SCANNING -> ANALYZING+TRADING -> MONITORING -> CLOSING -> REPORTING

Usage::

    daemon = TradingDaemon(config)
    report = await daemon.run_session()

Started once per NSE trading day at 09:15 IST (market open): by the ``skopaq schedule``
loop on an always-on host (docker compose), or by the Railway cron (``45 3 * * 1-5``
UTC). With ``--once`` PRE_OPEN starts at once and the scan follows the scan delay, so a
09:15 start scans after the open, not in the 09:00-09:15 pre-open auction.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING, Optional

from skopaq.constants import (
    DAEMON_PAPER_SAFETY_RULES,
    DAEMON_SAFETY_RULES,
    NSE_MARKET_CLOSE,
    NSE_MARKET_OPEN,
)

if TYPE_CHECKING:
    from skopaq.config import SkopaqConfig
    from skopaq.execution.position_monitor import MonitorResult
    from skopaq.scanner.models import ScannerCandidate

logger = logging.getLogger(__name__)

# IST = UTC+5:30
_IST = timezone(timedelta(hours=5, minutes=30))

# The end-of-session Telegram report may not hold up the process exit for longer.
_NOTIFY_TIMEOUT_SECONDS = 15

# `skopaq daemon` exit code when PRE_OPEN failed (token, broker session, LLM setup): nothing
# was scanned or traded, so the scheduler may start the session again later that morning.
PRE_OPEN_FAILED_EXIT_CODE = 3


class DaemonPhase(str, Enum):
    """Daemon lifecycle phases."""

    IDLE = "idle"
    PRE_OPEN = "pre_open"
    SCANNING = "scanning"
    ANALYZING = "analyzing"
    TRADING = "trading"
    MONITORING = "monitoring"
    CLOSING = "closing"
    REPORTING = "reporting"
    SHUTDOWN = "shutdown"


@dataclass
class DaemonSessionReport:
    """End-of-day session summary."""

    session_date: str = ""
    phase_times: dict[str, float] = field(default_factory=dict)
    candidates_scanned: int = 0
    candidates_analyzed: int = 0
    trades_opened: int = 0
    trades_rejected: int = 0
    holds: int = 0
    sells_executed: int = 0
    sells_failed: int = 0
    gross_pnl: float = 0.0
    decisions_settled: int = 0
    halted: str = ""  # kill switch description when the session was halted
    pre_open_failed: bool = False  # PRE_OPEN raised: nothing was scanned or traded
    # The session itself failed (an exception ended it); a candidate's failed analysis
    # only adds to errors.
    failed: bool = False
    errors: list[str] = field(default_factory=list)
    monitor_result: Optional[MonitorResult] = None


class TradingDaemon:
    """Orchestrates a full autonomous trading session.

    Phases:
        1. PRE_OPEN:   Validate token, build LLM/executor infra.
        2. SCANNING:   Wait for prices to settle, run multi-model scanner.
        3. ANALYZING:  For each candidate, run multi-agent graph.
        4. TRADING:    Execute BUY signals (up to max_trades).
        5. MONITORING: Run PositionMonitor until all positions closed or EOD.
        6. CLOSING:    Safety net — force-sell any remaining positions.
        7. REPORTING:  Compile and log session report.

    Error recovery:
        - Scanner failure -> 0 candidates -> skip to REPORTING
        - Individual analysis failure -> skip candidate, continue
        - All trades rejected -> 0 positions -> MONITORING exits immediately
        - SIGTERM -> stop opening positions, jump to MONITORING -> CLOSING
    """

    def __init__(
        self,
        config: SkopaqConfig,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        self._config = config
        self._stop = stop_event or asyncio.Event()
        self._phase = DaemonPhase.IDLE
        self._phase_times: dict[str, float] = {}

        # Daemon-specific limits
        self._max_trades = config.daemon_max_trades_per_session
        self._max_candidates = config.daemon_max_candidates_to_analyze
        self._scan_delay = config.daemon_scan_delay_after_open_seconds

        # Built during PRE_OPEN
        self._client = None       # INDstocksClient
        self._router = None       # OrderRouter
        self._executor = None     # Executor
        self._graph = None        # SkopaqTradingGraph
        self._llm_map = None      # Per-role LLM map
        self._memory_store = None # MemoryStore (optional)

    @property
    def phase(self) -> DaemonPhase:
        return self._phase

    # ── Public API ────────────────────────────────────────────────────

    async def wait_for_market_open(self) -> None:
        """Sleep until pre-open time (09:15 IST minus pre_open_minutes).

        Returns immediately if already past pre-open or stop_event is set.
        """
        pre_open_minutes = self._config.daemon_pre_open_minutes
        open_dt = datetime.combine(
            datetime.now(_IST).date(),
            NSE_MARKET_OPEN,
            tzinfo=_IST,
        )
        target = open_dt - timedelta(minutes=pre_open_minutes)
        now = datetime.now(_IST)

        if now >= target:
            logger.info("Already past pre-open time — starting immediately")
            return

        wait_seconds = (target - now).total_seconds()
        logger.info(
            "Waiting %.0f seconds until pre-open (%s IST)",
            wait_seconds,
            target.strftime("%H:%M"),
        )

        try:
            await asyncio.wait_for(
                self._stop.wait(),
                timeout=wait_seconds,
            )
            logger.info("Stop event received during wait — aborting")
        except asyncio.TimeoutError:
            pass  # Normal — time to start

    async def run_session(
        self, *, dry_run: bool = False,
    ) -> DaemonSessionReport:
        """Execute the full daemon session lifecycle.

        Args:
            dry_run: If True, scan only — print candidates but don't trade.

        Returns:
            DaemonSessionReport with full session metrics.
        """
        report = DaemonSessionReport(
            session_date=datetime.now(_IST).strftime("%Y-%m-%d"),
        )

        try:
            # Phase 1: PRE_OPEN — validate token, build infra
            await self._timed_phase(DaemonPhase.PRE_OPEN, self._phase_pre_open)

            # Kill switch: no scanning or new trades while trading is halted
            halt = self._halt_status()
            if halt.halted:
                report.halted = halt.describe()
                logger.warning("%s — skipping scan and trades", report.halted)
                return report

            # Phase 2: SCANNING — run multi-model scanner
            candidates = await self._timed_phase(
                DaemonPhase.SCANNING, self._phase_scan,
            )
            report.candidates_scanned = len(candidates)
            logger.info("Scanner returned %d candidates", len(candidates))

            if dry_run:
                logger.info("DRY RUN — skipping trade/monitor phases")
                return report

            if not candidates:
                logger.info("No candidates — nothing to trade")
                return report

            if self._stop.is_set():
                return report

            # Phase 3+4: ANALYZING + TRADING — run graph for each candidate
            trade_results = await self._timed_phase(
                DaemonPhase.ANALYZING,
                self._phase_analyze_and_trade,
                candidates,
                report,
            )

            buys_placed = sum(1 for r in trade_results if r is not None)
            logger.info(
                "Analysis complete: %d BUY(s) placed out of %d candidates",
                buys_placed,
                report.candidates_analyzed,
            )

            if self._stop.is_set() and buys_placed == 0:
                return report

            # Phase 5: MONITORING — AI + safety auto-sell loop
            if buys_placed > 0:
                monitor_result = await self._timed_phase(
                    DaemonPhase.MONITORING, self._phase_monitor,
                )
                report.monitor_result = monitor_result
                if monitor_result:
                    report.sells_executed = monitor_result.sells_executed
                    report.sells_failed = monitor_result.sells_failed
                    report.gross_pnl = monitor_result.total_pnl
            else:
                logger.info("No positions opened — skipping monitor phase")

            # Phase 6: CLOSING — safety net for any remaining positions
            await self._timed_phase(DaemonPhase.CLOSING, self._phase_close)

        except Exception as exc:
            logger.error("Daemon session failed: %s", exc, exc_info=True)
            report.errors.append(str(exc))
            report.failed = True
            report.pre_open_failed = self._phase == DaemonPhase.PRE_OPEN
            # The CLOSING safety net also runs when the session fails after opening trades
            # (e.g. the monitor could not read positions): nothing else would sell them.
            if report.trades_opened > 0 and self._phase != DaemonPhase.CLOSING:
                logger.warning("Session failed with %d trade(s) opened: running CLOSING",
                               report.trades_opened)
                try:
                    await self._timed_phase(DaemonPhase.CLOSING, self._phase_close)
                except Exception:
                    logger.error("CLOSING after the failure failed too", exc_info=True)
        finally:
            # Phase 7: REPORTING — compile metrics
            self._phase = DaemonPhase.REPORTING
            report.phase_times = dict(self._phase_times)
            # A dry run is scan-only: no LLM reflection calls or Supabase writes.
            if not dry_run:
                report.decisions_settled = await self._settle_due_decisions()

            # Clean up client session
            if self._client is not None:
                try:
                    await self._client.__aexit__(None, None, None)
                except Exception:
                    pass

            self._phase = DaemonPhase.SHUTDOWN

        msg = self._log_report(report)
        await self._notify_report(msg)
        return report

    # ── Phase implementations ─────────────────────────────────────────

    def _session_end(self, now: Optional[datetime] = None) -> datetime:
        """Until when today's session may need the broker: the NSE close or, if later, the
        scheduler deadline (a session still running then is stopped and sells)."""
        end = NSE_MARKET_CLOSE
        try:
            from skopaq.execution.scheduler import parse_hhmm

            end = max(end, parse_hhmm(self._config.scheduler_deadline))
        except (AttributeError, TypeError, ValueError):
            pass
        day = (now or datetime.now(_IST)).astimezone(_IST).date()
        return datetime.combine(day, end, tzinfo=_IST)

    async def _phase_pre_open(self) -> None:
        """Validate token, build LLM map, create executor stack."""
        from skopaq.broker.client import INDstocksClient
        from skopaq.broker.paper_engine import PaperEngine
        from skopaq.broker.token_manager import TokenManager, session_token_problem
        from skopaq.cli.main import (
            _build_upstream_config,
            _create_memory_store,
        )
        from skopaq.execution.executor import Executor
        from skopaq.execution.order_router import OrderRouter
        from skopaq.execution.safety_checker import SafetyChecker
        from skopaq.graph.skopaq_graph import SkopaqTradingGraph
        from skopaq.llm import bridge_env_vars, build_llm_map
        from skopaq.risk.position_sizer import PositionSizer

        config = self._config

        # 1. Validate INDstocks token: valid now, and still valid when the session ends
        token_mgr = TokenManager()
        health = token_mgr.get_health()
        if not health.valid:
            raise RuntimeError(
                f"INDstocks token invalid: {health.warning}. "
                "Run `skopaq token set <token>` first."
            )
        problem = session_token_problem(health, self._session_end())
        if problem:
            raise RuntimeError(problem)
        logger.info("Token valid — expires in %s", health.remaining)

        # 2. Open broker client session
        self._client = INDstocksClient(config, token_mgr)
        await self._client.__aenter__()

        # Validate token against API
        profile = await self._client.get_profile()
        logger.info(
            "Broker session open — user=%s",
            profile.name or profile.email or "unknown",
        )

        # 3. Build LLM map (env bridging + multi-model tiering)
        bridge_env_vars(config)
        self._llm_map = build_llm_map()
        logger.info("LLM map built: %d roles", len(self._llm_map))

        # 4. Build executor stack
        is_paper = config.trading_mode == "paper"
        paper = PaperEngine(initial_capital=config.initial_paper_capital)

        live_client = None if is_paper else self._client
        self._router = OrderRouter(config, paper, live_client=live_client)

        rules = DAEMON_PAPER_SAFETY_RULES if is_paper else DAEMON_SAFETY_RULES
        safety = SafetyChecker(
            rules=rules,
            max_sector_concentration_pct=config.max_sector_concentration_pct,
        )
        from skopaq.execution.pnl_history import seed_safety_checker
        seed_safety_checker(safety, config)

        sizer = None
        if config.position_sizing_enabled:
            sizer = PositionSizer(
                risk_per_trade_pct=config.risk_per_trade_pct,
                atr_multiplier=config.atr_multiplier,
                atr_period=config.atr_period,
            )

        self._executor = Executor(self._router, safety, position_sizer=sizer)

        # 5. Build analysis graph
        upstream_config = _build_upstream_config(config)
        self._memory_store = _create_memory_store(config)

        analysts = [
            a.strip()
            for a in config.selected_analysts.split(",")
            if a.strip()
        ]
        self._graph = SkopaqTradingGraph(
            upstream_config,
            self._executor,
            selected_analysts=analysts,
            memory_store=self._memory_store,
        )

        logger.info(
            "PRE_OPEN complete — mode=%s, max_trades=%d, analysts=%s",
            config.trading_mode,
            self._max_trades,
            analysts,
        )

    async def _phase_scan(self) -> list[ScannerCandidate]:
        """Wait for prices to settle, then run multi-model scanner."""
        # Delay after market open for prices to stabilise
        if self._scan_delay > 0 and not self._stop.is_set():
            logger.info(
                "Waiting %ds for prices to settle...", self._scan_delay,
            )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self._scan_delay,
                )
                return []  # Interrupted
            except asyncio.TimeoutError:
                pass  # Normal

        # Check stop again — may have been set while we weren't waiting
        if self._stop.is_set():
            return []

        # Reuse the same scanner wiring from main.py's _run_scan()
        from skopaq.cli.main import _run_scan

        try:
            candidates = await _run_scan(self._max_candidates)
        except Exception as exc:
            logger.error("Scanner failed: %s", exc, exc_info=True)
            candidates = []

        return candidates

    async def _phase_analyze_and_trade(
        self,
        candidates: list[ScannerCandidate],
        report: DaemonSessionReport,
    ) -> list:
        """Analyze each candidate and execute BUY signals.

        Processes candidates sequentially (capital reduces with each BUY).
        Stops after max_trades BUYs or when all candidates are exhausted.
        """
        from skopaq.cli.main import _compute_risk_scales, _reflection_graph, _run_lifecycle

        config = self._config
        trade_date = datetime.now(_IST).strftime("%Y-%m-%d")
        regime_scale, calendar_scale = _compute_risk_scales(config, trade_date)

        buys = []
        buys_placed = 0

        for i, candidate in enumerate(candidates[: self._max_candidates]):
            if self._stop.is_set():
                logger.info("Stop event — halting analysis")
                break

            halt = self._halt_status()
            if halt.halted:
                report.halted = halt.describe()
                logger.warning("%s — stopping analysis", report.halted)
                break

            if buys_placed >= self._max_trades:
                logger.info(
                    "Max trades (%d) reached — stopping analysis",
                    self._max_trades,
                )
                break

            report.candidates_analyzed += 1
            symbol = candidate.symbol
            logger.info(
                "Analyzing candidate %d/%d: %s (urgency=%s)",
                i + 1,
                min(len(candidates), self._max_candidates),
                symbol,
                candidate.urgency,
            )

            # For paper mode, inject a real-time quote
            if config.trading_mode == "paper":
                try:
                    from skopaq.cli.main import _inject_paper_quote
                    paper_engine = self._router._paper  # noqa: SLF001
                    await _inject_paper_quote(config, paper_engine, symbol)
                except Exception:
                    logger.warning(
                        "Quote injection failed for %s", symbol, exc_info=True,
                    )

            try:
                result = await self._graph.analyze_and_execute(
                    symbol,
                    trade_date,
                    regime_scale=regime_scale,
                    calendar_scale=calendar_scale,
                )
            except Exception as exc:
                logger.error(
                    "Analysis failed for %s: %s", symbol, exc, exc_info=True,
                )
                report.errors.append(f"Analysis error ({symbol}): {exc}")
                continue

            # Check outcome
            if result.error:
                logger.warning("Analysis returned error for %s: %s", symbol, result.error)
                report.errors.append(f"{symbol}: {result.error}")
                continue

            if result.signal is None or result.signal.action == "HOLD":
                report.holds += 1
                logger.info(
                    "[%s] Decision: HOLD (confidence=%d%%)",
                    symbol,
                    result.signal.confidence if result.signal else 0,
                )
                continue

            if result.signal.action == "BUY":
                if result.execution and result.execution.success:
                    buys_placed += 1
                    report.trades_opened += 1
                    buys.append(result)
                    logger.info(
                        "[%s] BUY EXECUTED — fill=%.2f qty=%s (%d/%d trades)",
                        symbol,
                        result.execution.fill_price or 0,
                        result.signal.quantity,
                        buys_placed,
                        self._max_trades,
                    )

                    # Post-trade lifecycle: persist to Supabase (the loss
                    # limits read realized P&L back) and reflect when enabled.
                    try:
                        await _run_lifecycle(
                            config,
                            _reflection_graph(config, self._graph, self._memory_store),
                            self._memory_store, result,
                        )
                    except Exception:
                        logger.warning(
                            "Lifecycle failed for %s", symbol,
                            exc_info=True,
                        )
                else:
                    report.trades_rejected += 1
                    reason = (
                        result.execution.rejection_reason
                        if result.execution
                        else "no execution result"
                    )
                    logger.warning(
                        "[%s] BUY REJECTED: %s", symbol, reason,
                    )

        return buys

    async def _phase_monitor(self) -> MonitorResult:
        """Run the position monitor until all positions are closed or EOD."""
        from skopaq.execution.position_monitor import PositionMonitor

        llm = None
        if self._llm_map:
            llm = self._llm_map.get(
                "sell_analyst", self._llm_map.get("_default"),
            )

        monitor = PositionMonitor(
            executor=self._executor,
            client=self._client,
            router=self._router,
            config=self._config,
            llm=llm,
            stop_event=self._stop,
            ai_enabled=llm is not None,
            on_exit=self._record_exit,
        )

        logger.info("Starting position monitor...")
        return await monitor.run()

    @staticmethod
    def _halt_status():
        from skopaq.execution import kill_switch

        return kill_switch.status(use_cache=False)

    async def _record_exit(self, signal, execution) -> None:
        """Persist a monitor or close-phase exit like an analysed trade."""
        from skopaq.cli.main import _record_exit

        await _record_exit(self._config, self._graph, self._memory_store, signal, execution)

    async def _settle_due_decisions(self) -> int:
        """Settle past decisions whose holding window has traded (all tickers)."""
        if self._graph is None or self._stop.is_set():
            return 0
        try:
            return await asyncio.to_thread(self._graph.settle_due, self._stop.is_set)
        except Exception:
            logger.warning("Settling past decisions failed", exc_info=True)
            return 0

    async def _phase_close(self) -> None:
        """Safety net — force-sell any remaining positions.

        This runs after the monitor exits (or if it crashed).
        If there are still open positions, sell them all at market.
        """
        if self._router is None:
            return

        try:
            positions = await self._router.get_positions()
            open_positions = [p for p in positions if p.quantity > 0]
        except Exception:
            logger.warning("Could not check positions for closing", exc_info=True)
            return

        if not open_positions:
            logger.info("No remaining positions — closing phase complete")
            return

        logger.warning(
            "CLOSING: %d position(s) still open — force selling",
            len(open_positions),
        )

        from decimal import Decimal
        from skopaq.broker.models import OrderType, TradingSignal

        for pos in open_positions:
            try:
                # MARKET, as the docstring says: a LIMIT at the average price
                # would not fill for a position in loss. entry_price is only the
                # fill estimate the exit is recorded at.
                signal = TradingSignal(
                    symbol=pos.symbol,
                    action="SELL",
                    confidence=100,
                    entry_price=await self._reference_price(pos),
                    order_type=OrderType.MARKET,
                    quantity=Decimal(int(pos.quantity)),
                    reasoning="DAEMON CLOSE: EOD safety net sell-all",
                )
                result = await self._executor.execute_signal(signal)
                if result.success:
                    logger.info("Force-sold %s", pos.symbol)
                    try:
                        await self._record_exit(signal, result)
                    except Exception:
                        logger.warning("Recording force-sell of %s failed", pos.symbol,
                                       exc_info=True)
                else:
                    logger.error(
                        "Force-sell REJECTED for %s: %s",
                        pos.symbol, result.rejection_reason,
                    )
            except Exception:
                logger.error(
                    "Force-sell FAILED for %s", pos.symbol, exc_info=True,
                )

    async def _reference_price(self, pos) -> Optional[float]:
        """The price a MARKET exit of *pos* is recorded at until the real fill is known.

        The last price when the backend reports one (paper), else the LTP from
        the broker (INDstocks positions carry no last price). ``None`` rather
        than the average price, which is the cost basis and would record every
        close as breakeven. Never raises: a missing estimate must not block the sell.
        """
        try:
            last = float(pos.last_price or 0)
            if last > 0:
                return last
            if self._client is None:
                return None
            from skopaq.broker.scrip_resolver import resolve_scrip_code

            ltp = float(await self._client.get_ltp(
                await resolve_scrip_code(self._client, pos.symbol)) or 0)
            return ltp if ltp > 0 else None
        except Exception:
            logger.warning("No price for %s — its close is sent without a price estimate",
                           pos.symbol, exc_info=True)
            return None

    # ── Utilities ─────────────────────────────────────────────────────

    async def _timed_phase(self, phase: DaemonPhase, fn, *args):
        """Execute a phase function while tracking timing."""
        self._phase = phase
        start = _time.monotonic()
        logger.info("=== PHASE: %s ===", phase.value.upper())

        try:
            result = await fn(*args)
        finally:
            elapsed = _time.monotonic() - start
            self._phase_times[phase.value] = round(elapsed, 1)
            logger.info(
                "Phase %s completed in %.1fs", phase.value, elapsed,
            )

        return result

    def _log_report(self, report: DaemonSessionReport) -> str:
        """Log the session report summary; returns the notification text."""
        logger.info("=" * 60)
        logger.info("DAEMON SESSION REPORT — %s", report.session_date)
        logger.info("=" * 60)
        logger.info("Candidates scanned:  %d", report.candidates_scanned)
        logger.info("Candidates analyzed: %d", report.candidates_analyzed)
        logger.info("Trades opened:       %d", report.trades_opened)
        logger.info("Trades rejected:     %d", report.trades_rejected)
        logger.info("Holds:               %d", report.holds)
        logger.info("Sells executed:      %d", report.sells_executed)
        logger.info("Sells failed:        %d", report.sells_failed)
        logger.info("Gross P&L:           %.2f", report.gross_pnl)
        logger.info("Decisions settled:   %d", report.decisions_settled)
        if report.halted:
            logger.warning("Halted:              %s", report.halted)

        if report.phase_times:
            times = "  ".join(
                f"{k}={v:.0f}s" for k, v in report.phase_times.items()
            )
            logger.info("Phase timings:       %s", times)

        total = sum(report.phase_times.values())
        logger.info("Total session time:  %.0fs (%.1f min)", total, total / 60)

        if report.errors:
            logger.warning("Errors (%d):", len(report.errors))
            for err in report.errors:
                logger.warning("  - %s", err)

        logger.info("=" * 60)

        msg = (
            f"Daemon Session Complete\n\n"
            f"Date: {report.session_date}\n"
            f"Scanned: {report.candidates_scanned} | "
            f"Analyzed: {report.candidates_analyzed}\n"
            f"Trades: {report.trades_opened} opened, "
            f"{report.trades_rejected} rejected\n"
            f"Sells: {report.sells_executed} executed\n"
            f"P&L: Rs {report.gross_pnl:+,.2f}\n"
            f"Duration: {total/60:.1f} min"
        )
        if report.errors:
            msg += f"\nErrors: {len(report.errors)}"
        return msg

    async def _notify_report(self, msg: str) -> None:
        """Send the session report via Telegram; awaited (bounded) so it is not dropped."""
        try:
            from skopaq.notifications import notify

            await asyncio.wait_for(notify(msg), timeout=_NOTIFY_TIMEOUT_SECONDS)
        except Exception:
            logger.warning("Session report notification failed or timed out", exc_info=True)
