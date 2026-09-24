"""Wrapper around upstream TradingAgentsGraph + Skopaq execution pipeline.

This is the main entry point for running an analysis-and-trade cycle.
It calls the upstream ``propagate()`` as a black box, then routes the
decision through safety checks and order execution.

Upstream changes this relies on (llm_map, confidence) are listed in
UPSTREAM_CHANGES.md.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from skopaq.broker.models import Exchange, ExecutionResult, TradingSignal
from skopaq.execution.executor import Executor

logger = logging.getLogger(__name__)


@dataclass
class AnalysisResult:
    """Complete result of an analyze-and-execute cycle."""

    symbol: str
    trade_date: str
    signal: Optional[TradingSignal] = None
    execution: Optional[ExecutionResult] = None
    agent_state: dict[str, Any] = field(default_factory=dict)
    raw_decision: str = ""
    error: Optional[str] = None
    duration_seconds: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0
    trade_id: Optional[UUID] = None  # Set after DB persistence in _run_lifecycle
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class SkopaqTradingGraph:
    """Wraps upstream TradingAgentsGraph with Skopaq execution.

    Pipeline::

        propagate(symbol, date)  →  parse signal  →  executor.execute_signal()

    When a ``MemoryStore`` is provided, upstream's decision log is loaded
    from Supabase before the first ``propagate()`` call, and saved back
    after every analysis and reflection.

    Args:
        upstream_config: Config dict for TradingAgentsGraph (LLM keys, etc).
        executor: The Skopaq execution pipeline (safety → route → fill).
        selected_analysts: Which upstream analysts to enable.
        debug: Enable upstream debug/tracing mode.
        memory_store: Optional persistence layer for agent memories.
        jev: Optional ``skopaq.llm.jev.Jev``; defaults to ``get_jev()``.
    """

    def __init__(
        self,
        upstream_config: dict[str, Any],
        executor: Executor,
        selected_analysts: Optional[list[str]] = None,
        debug: bool = False,
        memory_store: Optional[Any] = None,
        jev: Optional[Any] = None,
    ) -> None:
        self._executor = executor
        self._upstream_config = upstream_config
        # None → the process-wide Jev from config (off unless SKOPAQ_JEV_ENABLED)
        self._jev = jev

        # Default analyst selection: base 4 + crypto-specific when asset_class == "crypto"
        if selected_analysts is not None:
            self._selected_analysts = selected_analysts
        else:
            base = ["market", "social", "news", "fundamentals"]
            if upstream_config.get("asset_class") == "crypto":
                self._selected_analysts = base + ["onchain", "defi", "funding"]
            else:
                self._selected_analysts = base
        self._debug = debug
        self._memory_store = memory_store
        self._graph: Any = None  # Lazy-init upstream graph

    def _ensure_graph(self) -> Any:
        """Lazy-import and initialise the upstream TradingAgentsGraph."""
        if self._graph is not None:
            return self._graph

        # Bridge SKOPAQ_ env vars → standard LLM env vars before upstream init
        from skopaq.llm.env_bridge import bridge_env_vars
        bridge_env_vars()

        # Import upstream at runtime to avoid import-time side effects
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.graph import TradingAgentsGraph

        # Upstream reads its required keys (results_dir, data_cache_dir, ...)
        # straight from config, so fill whatever the caller left out.
        config = {**DEFAULT_CONFIG, **self._upstream_config}
        llm_map = config.pop("llm_map", None)

        self._graph = TradingAgentsGraph(
            selected_analysts=self._selected_analysts,
            debug=self._debug,
            config=config,
            llm_map=llm_map,
        )
        logger.info(
            "Upstream TradingAgentsGraph initialised (analysts=%s, debug=%s)",
            self._selected_analysts, self._debug,
        )

        # Restore the persisted decision log before the first run reads it
        if self._memory_store is not None:
            try:
                loaded = self._memory_store.load(self._graph)
                logger.info("Decision log loaded from Supabase (%d entries)", loaded)
            except Exception:
                logger.warning("Memory load failed — agents will start with empty memory", exc_info=True)

        return self._graph

    async def _apply_jev(self, signal: Optional[TradingSignal], state: Any) -> None:
        """Calibrate the signal with Jev's reading of the final decision.

        The probability Jev gives the signal's action becomes its confidence
        (position sizing, minimum-confidence gate). When Jev confidently
        reads a different trade than the rating says, a BUY or SELL is
        downgraded to HOLD; Jev never turns a HOLD into a trade. Without Jev
        the Portfolio Manager's self-reported confidence stands.
        """
        from skopaq.llm.jev import get_jev

        jev = self._jev if self._jev is not None else get_jev()
        if jev is None or signal is None or not isinstance(state, dict):
            return
        verdict = await jev.trade_action(str(state.get("final_trade_decision") or ""))
        if verdict is None:
            return

        logger.info("[%s] %s", signal.symbol, verdict.describe())
        if (
            signal.action != "HOLD"
            and verdict.choice != signal.action
            and verdict.confidence >= jev.min_confidence
        ):
            logger.warning(
                "[%s] Decision text reads as %s, not %s — downgrading to HOLD",
                signal.symbol, verdict.choice, signal.action,
            )
            signal.action = "HOLD"
        signal.confidence = round(100 * verdict.probability(signal.action))
        signal.reasoning = f"[{verdict.describe()}]\n{signal.reasoning}"

    def _upstream_symbol(self, symbol: str) -> str:
        """The ticker as upstream expects it: exchange-qualified for NSE equities.

        Upstream resolves the company, benchmarks returns (``.NS`` → Nifty 50)
        and settles past decisions from Yahoo data, which needs ``RELIANCE.NS``
        rather than ``RELIANCE``. The INDstocks vendor strips the suffix again.
        """
        if self._upstream_config.get("asset_class") == "crypto":
            return symbol
        suffix = self._upstream_config.get("yfinance_symbol_suffix", "")
        if suffix and not symbol.upper().endswith(suffix.upper()):
            return symbol + suffix
        return symbol

    def _asset_type(self) -> str:
        return "crypto" if self._upstream_config.get("asset_class") == "crypto" else "stock"

    def _save_memory(self) -> None:
        """Persist the decision log to Supabase (no-op without a memory store)."""
        if self._memory_store is None or self._graph is None:
            return
        try:
            saved = self._memory_store.save(self._graph)
            logger.info("Decision log saved to Supabase (%d entries)", saved)
        except Exception:
            logger.warning("Memory save failed — decision log not persisted", exc_info=True)

    async def analyze(self, symbol: str, trade_date: str) -> AnalysisResult:
        """Run upstream analysis without executing a trade.

        Useful for getting the agent's recommendation without placing an order.
        """
        import time

        start = time.monotonic()
        try:
            graph = self._ensure_graph()
            state, decision = graph.propagate(
                self._upstream_symbol(symbol), trade_date, asset_type=self._asset_type(),
            )
            # propagate() appended this decision to the log; keep it
            self._save_memory()

            signal = self._parse_signal(symbol, decision, state)
            try:
                await self._apply_jev(signal, state)
            except Exception:
                logger.warning(
                    "Jev calibration failed for %s — signal unchanged", symbol, exc_info=True
                )
            duration = time.monotonic() - start

            # Read semantic cache stats (if cache is active)
            cache_hits, cache_misses = self._read_cache_stats()

            return AnalysisResult(
                symbol=symbol,
                trade_date=trade_date,
                signal=signal,
                agent_state=state if isinstance(state, dict) else {},
                raw_decision=str(decision),
                duration_seconds=round(duration, 2),
                cache_hits=cache_hits,
                cache_misses=cache_misses,
            )
        except Exception as exc:
            duration = time.monotonic() - start
            logger.exception("Analysis failed for %s", symbol)
            return AnalysisResult(
                symbol=symbol,
                trade_date=trade_date,
                error=str(exc),
                duration_seconds=round(duration, 2),
            )

    async def analyze_and_execute(
        self,
        symbol: str,
        trade_date: str,
        regime_scale: float = 1.0,
        calendar_scale: float = 1.0,
    ) -> AnalysisResult:
        """Run upstream analysis and execute the resulting signal.

        Full pipeline:
            1. ``upstream.propagate(symbol, date)``  — get agent decision
            2. Parse decision into a ``TradingSignal``
            3. ``executor.execute_signal(signal)``  — safety check → route → fill

        Args:
            symbol: Stock symbol to analyze and trade.
            trade_date: Trading date (YYYY-MM-DD).
            regime_scale: Market regime position multiplier (0.0–1.2).
            calendar_scale: Event calendar position multiplier (0.0–1.0).
        """
        result = await self.analyze(symbol, trade_date)

        if result.error:
            return result

        if result.signal is None or result.signal.action == "HOLD":
            logger.info("Signal is HOLD for %s — no execution", symbol)
            return result

        # Execute the signal through the full pipeline
        execution = await self._executor.execute_signal(
            result.signal,
            trade_date=trade_date,
            regime_scale=regime_scale,
            calendar_scale=calendar_scale,
        )
        result.execution = execution

        logger.info(
            "Cycle complete: %s %s → %s (success=%s, mode=%s)",
            result.signal.action, symbol, execution.mode,
            execution.success, execution.mode,
        )
        return result

    def reflect(
        self,
        returns_losses: Any,
        symbol: Optional[str] = None,
        realized_return: Optional[float] = None,
        opened_on: Optional[str] = None,
    ) -> None:
        """Record a closed position's outcome in upstream's decision log.

        Called after a position close (SELL). Upstream (v0.2.4+) keeps no
        per-agent memories: each decision is logged as it is made and later
        settled with its outcome and a reflection, which the Portfolio
        Manager reads on later runs.

        With ``realized_return`` (e.g. ``-0.012`` for -1.2%), the pending
        decision that opened the position — the one logged on ``opened_on``,
        else the latest before today — is settled with the realized trade
        outcome and a reflection on ``returns_losses``. Any other pending
        decision for *symbol* whose holding window has traded is then
        settled the usual upstream way.
        """
        if not symbol:
            logger.info("Reflection skipped — no symbol to settle (%s)", returns_losses)
            return
        graph = self._ensure_graph()
        ticker = self._upstream_symbol(symbol)
        if realized_return is not None:
            try:
                self._settle_realized(
                    graph, ticker, realized_return, opened_on, str(returns_losses)
                )
            except Exception:
                logger.warning(
                    "Recording the realized outcome failed for %s", symbol, exc_info=True
                )
        graph.settle_pending(ticker)
        logger.info("Settled pending decisions for %s", symbol)
        self._save_memory()

    def settle_due(self) -> int:
        """Settle every ticker's pending decisions whose holding window has traded.

        Upstream settles a ticker only when that ticker is analyzed again, so
        decisions on tickers the scanner never picks again would stay pending
        forever. Returns how many decisions were settled.
        """
        graph = self._ensure_graph()
        before = {(e["date"], e["ticker"]) for e in graph.memory_log.get_pending_entries()}
        for ticker in sorted({ticker for _, ticker in before}):
            try:
                graph.settle_pending(ticker)
            except Exception:
                logger.warning("Settling %s failed", ticker, exc_info=True)
        after = {(e["date"], e["ticker"]) for e in graph.memory_log.get_pending_entries()}
        settled = len(before - after)
        logger.info("Settled %d of %d pending decisions", settled, len(before))
        if settled:
            self._save_memory()
        return settled

    @staticmethod
    def _settle_realized(
        graph: Any,
        ticker: str,
        realized_return: float,
        opened_on: Optional[str],
        returns_losses: str,
    ) -> None:
        """Settle the decision behind a closed trade with its realized return."""
        from datetime import date

        import numpy as np

        from tradingagents.dataflows.vendors.yahoo.market import get_closes
        from tradingagents.graph.settlement import resolve_benchmark

        today = date.today().isoformat()
        pending = [e for e in graph.memory_log.get_pending_entries() if e["ticker"] == ticker]
        chosen = (
            [e for e in pending if e["date"] == opened_on]
            or [e for e in pending if e["date"] < today]
        )
        if not chosen:
            logger.info("No pending decision to settle for %s", ticker)
            return
        entry = chosen[-1]

        # Alpha against the market over the same span; the raw return alone
        # when the benchmark cannot be priced.
        benchmark = resolve_benchmark(ticker, graph.config)
        try:
            tomorrow = (date.today() + timedelta(days=1)).isoformat()
            closes = get_closes(benchmark, entry["date"], tomorrow)
            bench_return = float((closes.iloc[-1] - closes.iloc[0]) / closes.iloc[0])
        except Exception:
            bench_return, benchmark = 0.0, f"{benchmark} (unavailable, taken as 0%)"
        alpha = realized_return - bench_return
        holding_days = max(1, int(np.busday_count(entry["date"], today)))

        reflection = graph.reflector.reflect_on_final_decision(
            final_decision=f"{entry.get('decision', '')}\n\nRealized trade:\n{returns_losses}",
            raw_return=realized_return,
            alpha_return=alpha,
            benchmark_name=benchmark,
            holding_days=holding_days,
        )
        graph.memory_log.update_with_outcome(
            ticker, entry["date"], realized_return, alpha, holding_days,
            reflection, resolution_date=today,
        )
        logger.info("Settled %s decision of %s with realized return %+.2f%%",
                    ticker, entry["date"], realized_return * 100)

    # ── Cache stats ────────────────────────────────────────────────────

    @staticmethod
    def _read_cache_stats() -> tuple[int, int]:
        """Read hit/miss counters from the global LLM cache (if active).

        Returns (hits, misses) — both 0 when no cache is configured.
        """
        try:
            from langchain_core.globals import get_llm_cache
            cache = get_llm_cache()
            if cache is not None and hasattr(cache, "stats"):
                stats = cache.stats
                logger.info(
                    "Cache: %d hits, %d misses (%.1f%% hit rate, %d errors)",
                    stats.hits, stats.misses, stats.hit_rate_pct, stats.errors,
                )
                return stats.hits, stats.misses
        except Exception:
            logger.debug("Could not read cache stats", exc_info=True)
        return 0, 0

    # ── Signal parsing ───────────────────────────────────────────────────

    def _parse_signal(
        self,
        symbol: str,
        decision: Any,
        state: Any,
    ) -> Optional[TradingSignal]:
        """Convert upstream decision into a typed TradingSignal.

        The upstream ``propagate()`` returns a (state, decision) tuple where
        ``decision`` is upstream's 5-tier rating (Buy / Overweight / Hold /
        Underweight / Sell) or ``REVIEW`` when the decision had no readable
        rating, and ``state`` is a dict with all intermediate analysis.

        Reasoning is extracted from (in priority order):
        1. ``risk_debate_state.judge_decision`` — the risk manager's verdict
        2. ``final_trade_decision`` — the full unprocessed report
        3. The raw decision string (fallback, usually just one word)
        """
        if decision is None:
            return None

        decision_str = str(decision).strip().upper()
        action = _rating_to_action(decision_str)
        if decision_str == "REVIEW":
            logger.warning("No readable rating in the decision for %s — treating as HOLD", symbol)

        # Try to extract confidence from state
        confidence = 50
        reasoning = decision_str[:500]

        if isinstance(state, dict):
            # The risk management node may include a confidence indicator
            risk_state = state.get("risk_debate_state", {})
            if isinstance(risk_state, dict):
                confidence = _extract_confidence(risk_state)
                logger.debug("Signal confidence for %s: %d", symbol, confidence)
                # Judge decision has the richest reasoning
                judge = risk_state.get("judge_decision", "")
                if judge and len(str(judge).strip()) > len(action):
                    reasoning = str(judge).strip()[:2000]

            # Fall back to the full unprocessed trade decision
            if reasoning == decision_str[:500]:
                full_decision = state.get("final_trade_decision", "")
                if full_decision and len(str(full_decision).strip()) > len(action):
                    reasoning = str(full_decision).strip()[:2000]

        # Pick exchange based on asset class in upstream config
        asset_class = self._upstream_config.get("asset_class", "equity")
        exchange = Exchange.BINANCE if asset_class == "crypto" else Exchange.NSE

        return TradingSignal(
            symbol=symbol,
            exchange=exchange,
            action=action,
            confidence=confidence,
            reasoning=reasoning,
            agent_state=state if isinstance(state, dict) else {},
        )


# Upstream's 5-tier rating → Skopaq's order action. Overweight ("gradually
# increase exposure") enters like Buy; confidence scales the position size.
# Underweight ("reduce exposure, take partial profits") is not a sell: an
# order cannot trim part of a position, and for a symbol we do not hold a
# SELL would be a short sale. Exits stay with the sell analyst. REVIEW means
# the decision had no readable rating, which must never become a trade.
_RATING_ACTIONS = {
    "BUY": "BUY",
    "OVERWEIGHT": "BUY",
    "HOLD": "HOLD",
    "UNDERWEIGHT": "HOLD",
    "SELL": "SELL",
}


def _rating_to_action(decision: str) -> str:
    """Map an upstream rating (any case) to BUY / SELL / HOLD."""
    return _RATING_ACTIONS.get(decision.strip().upper(), "HOLD")


def _extract_confidence(risk_state: dict[str, Any]) -> int:
    """Extract a confidence score from the risk debate state.

    Extraction priority:
    1. Parse ``CONFIDENCE: <N>`` / ``**Confidence**: <N>`` from the judge_decision text.
    2. Check for explicit dict keys (forward-compatible).
    3. Heuristic: analyse debater agreement direction.
    4. Fallback: 50 (graceful degradation).
    """
    # --- Priority 1: Parse from judge_decision text ---
    judge_text = risk_state.get("judge_decision", "")
    if judge_text:
        # Normalise: extract_text handles Gemini list-of-dicts format
        from skopaq.llm import extract_text
        judge_text = extract_text(judge_text)

        # Tolerates the markdown bold of "**Confidence**: 72" (upstream render).
        match = re.search(r"(?i)confidence\**\s*:\s*\**\s*(\d{1,3})", judge_text)
        if match:
            val = int(match.group(1))
            clamped = max(0, min(100, val))
            logger.debug("Parsed confidence from judge text: %d (clamped: %d)", val, clamped)
            return clamped

    # --- Priority 2: Check for explicit dict keys (forward-compatible) ---
    for key in ("confidence", "score", "certainty"):
        if key in risk_state:
            try:
                val = int(float(risk_state[key]))
                return max(0, min(100, val))
            except (ValueError, TypeError):
                pass

    # --- Priority 3: Heuristic from debater agreement ---
    count = risk_state.get("count", 0)
    if count > 0:
        aggressive = risk_state.get("current_aggressive_response", "")
        conservative = risk_state.get("current_conservative_response", "")
        neutral = risk_state.get("current_neutral_response", "")
        agreement = _estimate_agreement(aggressive, conservative, neutral)
        heuristic = int(35 + agreement * 50)
        logger.debug(
            "Heuristic confidence: %d (agreement=%.2f, rounds=%d)",
            heuristic, agreement, count,
        )
        return heuristic

    # --- Priority 4: Default fallback ---
    logger.debug("No confidence signal found — using default 50")
    return 50


def _estimate_agreement(aggressive: str, conservative: str, neutral: str) -> float:
    """Estimate how much the three risk debaters agree on direction.

    Returns 0.0–1.0 mapped to confidence 35–85 by the caller.
    """
    if not (aggressive and conservative and neutral):
        return 0.5  # Insufficient data → mid-range

    responses = [aggressive.upper(), conservative.upper(), neutral.upper()]
    buy_count = sum(1 for r in responses if "BUY" in r and "SELL" not in r)
    sell_count = sum(1 for r in responses if "SELL" in r and "BUY" not in r)
    hold_count = sum(1 for r in responses if "HOLD" in r)

    max_agreement = max(buy_count, sell_count, hold_count)

    if max_agreement == 3:
        return 1.0   # Unanimous
    elif max_agreement == 2:
        return 0.6   # Majority
    else:
        return 0.3   # Full disagreement
