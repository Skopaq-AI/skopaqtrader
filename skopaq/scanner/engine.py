"""Scanner engine — async background loop that screens watchlist stocks.

Multi-model scanning pipeline:
    1. Fetch quotes for all watchlist symbols
    2. Compute metrics (change%, volume ratio, gap%)
    3. Primary screen: Gemini Flash for technical candidate selection
    4. Optional: Perplexity Sonar for news-driven signals (parallel)
    5. Optional: Grok for social sentiment signals (parallel)
    6. Merge and deduplicate candidates
    7. Optional: Jev scores each candidate's reason and ranks them (dropping
       weak ones only above a configured threshold) before the slow analysis
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from skopaq.scanner.models import ScannerCandidate, ScannerMetrics
from skopaq.scanner.screen import (
    build_news_prompt,
    build_screen_prompt,
    build_social_prompt,
    parse_screen_response,
)
from skopaq.scanner.watchlist import Watchlist

logger = logging.getLogger(__name__)


class ScannerEngine:
    """Background scanner that cycles every N seconds.

    Supports multi-model screening: a primary screener (technical metrics)
    plus optional news and social screeners that run in parallel.

    Args:
        watchlist: Symbols to scan.
        cycle_seconds: Interval between scan cycles.
        max_candidates: Max candidates per cycle.
        quote_fetcher: Async callable ``(symbols) -> list[dict]`` returning
            quote dicts with keys: symbol, ltp, open, high, low, close, volume.
            Defaults to paper-mode stub if not provided.
        llm_screener: Async callable ``(prompt) -> str`` that sends the
            screening prompt to an LLM and returns the response text.
            Defaults to a no-op if not provided.
        news_screener: Optional async callable ``(prompt) -> str`` for
            news-aware screening (Perplexity Sonar recommended).
        social_screener: Optional async callable ``(prompt) -> str`` for
            social sentiment screening (Grok recommended).
        jev: Optional :class:`skopaq.llm.jev.Jev` that scores each
            candidate's reason (``catalyst``) and sets the drop threshold
            (``min_catalyst_score``).
    """

    def __init__(
        self,
        watchlist: Optional[Watchlist] = None,
        cycle_seconds: int = 30,
        max_candidates: int = 5,
        quote_fetcher: Optional[Callable] = None,
        llm_screener: Optional[Callable] = None,
        news_screener: Optional[Callable] = None,
        social_screener: Optional[Callable] = None,
        jev: Any = None,
    ) -> None:
        self.watchlist = watchlist or Watchlist()
        self.cycle_seconds = cycle_seconds
        self.max_candidates = max_candidates
        self._quote_fetcher = quote_fetcher or self._default_quote_fetcher
        self._llm_screener = llm_screener or self._default_llm_screener
        self._news_screener = news_screener
        self._social_screener = social_screener
        self._jev = jev
        self.candidate_queue: asyncio.Queue[ScannerCandidate] = asyncio.Queue()

        # State
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._cycle_count = 0
        self._last_cycle_at: Optional[datetime] = None
        self._last_candidates: list[ScannerCandidate] = []

    # ── Public API ────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._running

    @property
    def status(self) -> dict[str, Any]:
        return {
            "running": self._running,
            "cycle_count": self._cycle_count,
            "last_cycle_at": self._last_cycle_at.isoformat() if self._last_cycle_at else None,
            "watchlist_size": len(self.watchlist),
            "cycle_seconds": self.cycle_seconds,
            "queue_size": self.candidate_queue.qsize(),
            "last_candidates": [c.to_dict() for c in self._last_candidates],
            "screeners": self._screener_info(),
        }

    async def start(self) -> None:
        """Start the background scanning loop."""
        if self._running:
            logger.warning("Scanner already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "Scanner started (cycle=%ds, watchlist=%d, screeners=%s)",
            self.cycle_seconds, len(self.watchlist),
            "+".join(self._screener_info()["active"]),
        )

    async def stop(self) -> None:
        """Stop the scanner gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Scanner stopped after %d cycles", self._cycle_count)

    async def scan_once(self) -> list[ScannerCandidate]:
        """Run a single scan cycle (useful for manual/CLI triggering)."""
        return await self._scan_cycle()

    # ── Internal ──────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        """Main loop — scan, wait, repeat."""
        while self._running:
            try:
                candidates = await self._scan_cycle()
                for c in candidates:
                    await self.candidate_queue.put(c)
                    logger.debug("Queued candidate: %s — %s", c.symbol, c.reason)
            except Exception:
                logger.exception("Scanner cycle failed")

            await asyncio.sleep(self.cycle_seconds)

    async def _scan_cycle(self) -> list[ScannerCandidate]:
        """Execute one full scan cycle with multi-model screening."""
        self._cycle_count += 1
        self._last_cycle_at = datetime.now(timezone.utc)

        # 1. Fetch quotes
        symbols = self.watchlist.symbols
        quotes = await self._quote_fetcher(symbols)

        if not quotes:
            logger.debug("No quotes returned for cycle %d", self._cycle_count)
            return []

        # 2. Compute metrics
        metrics = self._compute_metrics(quotes)

        # 3. Run screeners in parallel
        all_candidates = await self._run_screeners(metrics, symbols)

        # 4. Deduplicate (same symbol from multiple screeners)
        candidates = self._deduplicate(all_candidates)

        # 5. Jev: drop weak catalysts, rank the rest
        candidates = await self._score_catalysts(candidates)
        self._last_candidates = candidates

        logger.info(
            "Scan cycle %d: %d quotes → %d candidates (from %d raw)",
            self._cycle_count, len(quotes), len(candidates), len(all_candidates),
        )
        return candidates

    async def _run_screeners(
        self,
        metrics: list[ScannerMetrics],
        symbols: list[str],
    ) -> list[ScannerCandidate]:
        """Run all active screeners in parallel and collect candidates."""
        tasks: list[asyncio.Task] = []

        # Primary: technical screening (always runs)
        tasks.append(asyncio.create_task(
            self._screen_technical(metrics),
            name="screen_technical",
        ))

        # Optional: news screening (Perplexity Sonar)
        if self._news_screener:
            tasks.append(asyncio.create_task(
                self._screen_news(symbols),
                name="screen_news",
            ))

        # Optional: social screening (Grok)
        if self._social_screener:
            tasks.append(asyncio.create_task(
                self._screen_social(symbols),
                name="screen_social",
            ))

        # Wait for all screeners (with individual error isolation)
        all_candidates: list[ScannerCandidate] = []
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for task, result in zip(tasks, results):
            if isinstance(result, Exception):
                logger.warning(
                    "Screener '%s' failed: %s", task.get_name(), result,
                )
            elif isinstance(result, list):
                all_candidates.extend(result)

        return all_candidates

    async def _screen_technical(
        self, metrics: list[ScannerMetrics],
    ) -> list[ScannerCandidate]:
        """Technical metrics screening (Gemini Flash)."""
        prompt = build_screen_prompt(metrics, self.max_candidates)
        response_text = await self._llm_screener(prompt)
        candidates = parse_screen_response(response_text)
        # Tag source
        for c in candidates:
            c.metrics["source"] = "technical"
        return candidates

    async def _screen_news(self, symbols: list[str]) -> list[ScannerCandidate]:
        """News-aware screening (Perplexity Sonar)."""
        prompt = build_news_prompt(symbols, max_candidates=3)
        response_text = await self._news_screener(prompt)
        candidates = parse_screen_response(response_text)
        for c in candidates:
            c.metrics["source"] = "news"
        return candidates

    async def _screen_social(self, symbols: list[str]) -> list[ScannerCandidate]:
        """Social sentiment screening (Grok)."""
        prompt = build_social_prompt(symbols, max_candidates=3)
        response_text = await self._social_screener(prompt)
        candidates = parse_screen_response(response_text)
        for c in candidates:
            c.metrics["source"] = "social"
        return candidates

    @staticmethod
    def _deduplicate(
        candidates: list[ScannerCandidate],
    ) -> list[ScannerCandidate]:
        """Deduplicate candidates — merge reasons if same symbol from multiple sources.

        When multiple screeners flag the same stock, the candidate is promoted
        to "high" urgency and reasons are combined.
        """
        by_symbol: dict[str, list[ScannerCandidate]] = {}
        for c in candidates:
            by_symbol.setdefault(c.symbol, []).append(c)

        merged: list[ScannerCandidate] = []
        for symbol, group in by_symbol.items():
            if len(group) == 1:
                merged.append(group[0])
            else:
                # Multi-source confluence — combine and promote
                sources = [c.metrics.get("source", "?") for c in group]
                reasons = [f"[{c.metrics.get('source', '?')}] {c.reason}" for c in group]
                merged.append(ScannerCandidate(
                    symbol=symbol,
                    reason=" | ".join(reasons)[:200],
                    urgency="high",  # Multi-source = high urgency
                    metrics={
                        "sources": sources,
                        "source": "multi",
                        "source_count": len(group),
                    },
                ))

        # Sort: multi-source first, then high urgency, then rest
        merged.sort(key=lambda c: (
            -c.metrics.get("source_count", 1),
            0 if c.urgency == "high" else 1,
        ))

        return merged

    async def _score_catalysts(
        self, candidates: list[ScannerCandidate],
    ) -> list[ScannerCandidate]:
        """Score each candidate's reason with Jev; rank them, dropping any below
        the threshold (``min_catalyst_score``, 0 by default: rank only).

        A candidate Jev could not score is kept and ranked as if it scored
        exactly the threshold, so a Jev outage changes nothing.
        """
        if self._jev is None or not candidates:
            return candidates

        threshold = self._jev.min_catalyst_score
        scores = await asyncio.gather(
            *(self._jev.catalyst(c.symbol, c.reason) for c in candidates),
            return_exceptions=True,
        )

        kept: list[ScannerCandidate] = []
        for candidate, score in zip(candidates, scores):
            if score is None or isinstance(score, BaseException):
                kept.append(candidate)
                continue
            candidate.metrics["catalyst_score"] = round(score.score, 2)
            candidate.metrics["catalyst_confidence"] = round(score.confidence, 2)
            candidate.metrics["specific_news"] = round(score.specific_news, 2)
            if score.score < threshold:
                logger.info(
                    "Jev dropped %s: catalyst %.2f < %.2f (%s)",
                    candidate.symbol, score.score, threshold, candidate.reason,
                )
                continue
            kept.append(candidate)

        dropped = len(candidates) - len(kept)
        if dropped:
            logger.warning(
                "Jev dropped %d of %d scanner candidates below catalyst score %.2f "
                "(SKOPAQ_JEV_MIN_CATALYST_SCORE)", dropped, len(candidates), threshold,
            )

        # Multi-source first (as _deduplicate does), then catalyst strength,
        # then named company news.  sort() is stable, so ties keep their order.
        kept.sort(key=lambda c: (
            -c.metrics.get("source_count", 1),
            -c.metrics.get("catalyst_score", threshold),
            -c.metrics.get("specific_news", 0.0),
        ))
        return kept

    @staticmethod
    def _compute_metrics(quotes: list[dict]) -> list[ScannerMetrics]:
        """Compute scanner metrics from raw quote dicts."""
        metrics = []
        for q in quotes:
            symbol = q.get("symbol", "")
            ltp = float(q.get("ltp", 0))
            close = float(q.get("close", 0)) or ltp
            open_price = float(q.get("open", 0)) or ltp
            volume = int(q.get("volume", 0))
            avg_volume = int(q.get("avg_volume", 0)) or 1

            change_pct = ((ltp - close) / close * 100) if close else 0
            gap_pct = ((open_price - close) / close * 100) if close else 0
            volume_ratio = volume / avg_volume if avg_volume else 0

            metrics.append(ScannerMetrics(
                symbol=symbol,
                ltp=ltp,
                change_pct=round(change_pct, 2),
                volume=volume,
                volume_ratio=round(volume_ratio, 1),
                gap_pct=round(gap_pct, 2),
            ))

        return metrics

    def _screener_info(self) -> dict[str, Any]:
        """Return info about which screeners are active."""
        active = ["technical"]
        if self._news_screener:
            active.append("news")
        if self._social_screener:
            active.append("social")
        return {"active": active, "count": len(active), "jev": self._jev is not None}

    # ── Default stubs ─────────────────────────────────────────────────

    @staticmethod
    async def _default_quote_fetcher(symbols: list[str]) -> list[dict]:
        """Paper-mode stub — returns empty quotes."""
        logger.debug("Using default quote fetcher (no real data)")
        return []

    @staticmethod
    async def _default_llm_screener(prompt: str) -> str:
        """No-op LLM screener — returns empty JSON array."""
        logger.debug("Using default LLM screener (no-op)")
        return "[]"
