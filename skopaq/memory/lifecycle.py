"""Trade lifecycle manager — links BUY → SELL and triggers reflection.

When a SELL trade closes a position, this manager:
1. Finds the original BUY trade via ``TradeRepository.find_open_buy()``
2. Computes realized P&L = (sell_price - buy_price) * quantity
3. Marks the BUY trade as closed (``closed_at`` + ``opening_trade_id`` on SELL)
4. Asks the graph to settle that symbol's pending decisions in upstream's
   decision log (with a reflection on each settled one)
5. Persists the decision log via ``MemoryStore.save()``

This is the mechanism that provides memory-augmented learning:
each closed position generates lessons that inform future decisions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from skopaq.db.repositories import TradeRepository
    from skopaq.graph.skopaq_graph import AnalysisResult, SkopaqTradingGraph
    from skopaq.memory.store import MemoryStore

logger = logging.getLogger(__name__)


class TradeLifecycleManager:
    """Tracks BUY → SELL position lifecycle and triggers auto-reflection.

    Args:
        trade_repo: Repository for trade CRUD (find_open_buy, update).
        graph: The Skopaq trading graph (for calling reflect()); ``None``
            records the trade and its P&L without a reflection.
        memory_store: Persistence layer for agent memories.
    """

    def __init__(
        self,
        trade_repo: TradeRepository,
        graph: Optional[SkopaqTradingGraph],
        memory_store: Optional[MemoryStore] = None,
    ) -> None:
        self._trade_repo = trade_repo
        self._graph = graph
        self._memory_store = memory_store

    async def on_trade(self, result: AnalysisResult) -> None:
        """Process a completed trade result.

        - **BUY**: No immediate action (position opens; waiting for SELL).
        - **SELL**: Find the opening BUY, compute P&L, trigger reflection.
        - **HOLD**: No-op.

        Args:
            result: The completed analysis-and-execution result.
        """
        if result.error or result.signal is None:
            return

        action = result.signal.action

        if action == "HOLD":
            return

        if action == "BUY":
            await self._handle_buy(result)
        elif action == "SELL":
            await self._handle_sell(result)

    async def _handle_buy(self, result: AnalysisResult) -> None:
        """Record context for a BUY trade.

        We don't trigger reflection on BUY — there's no P&L outcome yet.
        The trade has already been persisted to Supabase by ``_run_lifecycle()``
        in ``main.py``, so ``find_open_buy()`` will find it when the matching
        SELL arrives.
        """
        logger.info(
            "BUY persisted for %s (trade_id=%s) — awaiting SELL to trigger reflection",
            result.symbol,
            getattr(result, "trade_id", None),
        )

    async def _handle_sell(self, result: AnalysisResult) -> None:
        """Close a position and trigger reflection.

        Steps:
            1. Find the open BUY trade for this symbol
            2. Compute realized P&L
            3. Mark BUY as closed, link SELL to BUY
            4. Invoke reflection with P&L outcome
        """
        symbol = result.symbol

        # Find the matching open BUY
        try:
            open_buy = self._trade_repo.find_open_buy(symbol)
        except Exception:
            logger.warning(
                "Failed to look up open BUY for %s — skipping reflection",
                symbol, exc_info=True,
            )
            return

        if open_buy is None:
            logger.info(
                "No open BUY found for %s — SELL without matching BUY, skipping reflection",
                symbol,
            )
            return

        # Compute realized P&L
        sell_price = (
            result.execution.fill_price
            if result.execution and result.execution.fill_price
            else (result.signal.entry_price if result.signal else None)
        )
        buy_price = open_buy.fill_price or open_buy.price

        prices_known = sell_price is not None and buy_price is not None
        if prices_known:
            pnl = (Decimal(str(sell_price)) - buy_price) * open_buy.quantity
            pnl_pct = ((Decimal(str(sell_price)) - buy_price) / buy_price * 100) if buy_price else Decimal("0")
        else:
            pnl = Decimal("0")
            pnl_pct = Decimal("0")

        logger.info(
            "Position closed: %s BUY@%.2f → SELL@%s, P&L=%.2f (%.2f%%)",
            symbol,
            buy_price or 0,
            sell_price or "?",
            pnl,
            pnl_pct,
        )

        # Mark the BUY trade as closed with realized P&L
        now = datetime.now(timezone.utc)
        try:
            self._trade_repo.update(
                open_buy.id,
                {
                    "closed_at": now.isoformat(),
                    "pnl": str(pnl),
                    "exit_reason": f"Closed by SELL (P&L: {pnl_pct:.2f}%)",
                },
            )
        except Exception:
            logger.warning("Failed to mark BUY %s as closed", open_buy.id, exc_info=True)

        # Update SELL trade with opening_trade_id link + realized P&L
        sell_trade_id = getattr(result, "trade_id", None)
        if sell_trade_id:
            try:
                self._trade_repo.update(
                    sell_trade_id,
                    {
                        "opening_trade_id": str(open_buy.id),
                        "pnl": str(pnl),
                        "exit_reason": f"SELL closed (P&L: {pnl_pct:.2f}%)",
                    },
                )
            except Exception:
                logger.warning("Failed to link SELL to BUY", exc_info=True)
        else:
            logger.debug("No trade_id on result — SELL/BUY linkage skipped")

        if self._graph is None:
            return  # recording only (reflection off, or no graph built)

        # Trigger reflection with P&L outcome
        returns_losses = _format_returns(symbol, pnl, pnl_pct, buy_price, sell_price)
        try:
            self._graph.reflect(
                returns_losses,
                symbol=symbol,
                realized_return=float(pnl_pct) / 100 if prices_known else None,
                opened_on=open_buy.created_at.date().isoformat() if open_buy.created_at else None,
            )
            logger.info("Reflection triggered for %s (P&L=%.2f)", symbol, pnl)
        except Exception:
            logger.warning(
                "Reflection failed for %s — memories not updated",
                symbol, exc_info=True,
            )


def _format_returns(
    symbol: str,
    pnl: Decimal,
    pnl_pct: Decimal,
    buy_price: Optional[Decimal],
    sell_price: Any,
) -> str:
    """Format P&L data into a human-readable summary for ``graph.reflect()``."""
    return (
        f"Symbol: {symbol}\n"
        f"Entry Price: {buy_price}\n"
        f"Exit Price: {sell_price}\n"
        f"Realized P&L: {pnl:.2f} INR ({pnl_pct:.2f}%)\n"
        f"Outcome: {'PROFIT' if pnl > 0 else 'LOSS' if pnl < 0 else 'BREAKEVEN'}"
    )
