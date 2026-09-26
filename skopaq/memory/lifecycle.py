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

A live SELL closes only the quantity the broker confirmed filled, across the open live
BUY rows (newest first). A row sold in part is split: its remainder is saved as a new
open row before the row is closed for the sold part. A paper SELL that succeeded closes
the whole newest open paper BUY row as before; it never closes a live row, and a refused
one closes nothing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from skopaq.db.repositories import TradeRepository
    from skopaq.graph.skopaq_graph import AnalysisResult, SkopaqTradingGraph
    from skopaq.memory.store import MemoryStore

logger = logging.getLogger(__name__)

# Open BUY rows one live SELL may close: a guard against a runaway loop only (each row is
# closed once); shares left beyond it are alerted (sell-not-booked)
_MAX_LOTS = 100


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

    async def on_trade(self, result: AnalysisResult) -> bool:
        """Process a completed trade result.

        - **BUY**: No immediate action (position opens; waiting for SELL).
        - **SELL**: Find the opening BUY, compute P&L, trigger reflection.
        - **HOLD**: No-op.

        Args:
            result: The completed analysis-and-execution result.

        Returns:
            False when a live SELL closed no BUY row because the trade rows could not
            be read (nothing of it is booked, so booking it again is safe); True
            otherwise — rows that could not be closed or split are alerted instead.
        """
        if result.error or result.signal is None:
            return True

        action = result.signal.action

        if action == "HOLD":
            return True

        if action == "BUY":
            await self._handle_buy(result)
        elif action == "SELL":
            return await self._handle_sell(result)
        return True

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

    async def _handle_sell(self, result: AnalysisResult) -> bool:
        """Close a position and trigger reflection.

        Steps:
            1. Find the open BUY trade for this symbol
            2. Compute realized P&L
            3. Mark BUY as closed, link SELL to BUY
            4. Invoke reflection with P&L outcome

        Live: see ``_handle_live_sell``.
        """
        sold = _live_filled(result.execution)
        if sold is not None:
            return await self._handle_live_sell(result, sold)

        symbol = result.symbol
        execution = result.execution
        if execution is None or getattr(execution, "success", False) is not True:
            # Refused (no position, a safety rule) or never executed: nothing was sold
            logger.info("SELL %s did not execute — no BUY row closed", symbol)
            return True

        # Find the matching open BUY: a paper SELL closes paper rows only (a live row's
        # P&L feeds the live loss limits)
        try:
            open_buy = self._trade_repo.find_open_buy(
                symbol, is_paper=getattr(execution, "mode", "paper") == "paper")
        except Exception:
            logger.warning(
                "Failed to look up open BUY for %s — skipping reflection",
                symbol, exc_info=True,
            )
            return True

        if open_buy is None:
            logger.info(
                "No open BUY found for %s — SELL without matching BUY, skipping reflection",
                symbol,
            )
            return True

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

        _record_outcome(open_buy, result, buy_price, sell_price, pnl, pnl_pct, now)

        if self._graph is None:
            return True  # recording only (reflection off, or no graph built)

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
        return True

    async def _handle_live_sell(self, result: AnalysisResult, sold: Decimal) -> bool:
        """Close the confirmed quantity of a live SELL against the open live BUY rows.

        Newest row first, until the sold quantity is booked or the open rows run out;
        paper rows are never closed by a live SELL. A row sold in part is split: the remainder
        is inserted first as a new open row (``order_id`` None, the column is unique;
        ``model_signals.opened_at`` keeps when the position opened), then the row is
        closed for the sold part. If the remainder cannot be saved, the row stays open and
        the partial goes into ``model_signals.pending_partials``, counted when the row
        closes. The SELL row links to the first row with the SELL's whole P&L; one
        reflection covers the SELL.

        False when the open rows could not be read before any was closed (nothing of the
        SELL is booked); a read failing after some were closed is alerted
        (``sell-not-booked``) instead, since booking the SELL again would close those
        twice.
        """
        symbol = result.symbol
        sell_price = (
            result.execution.fill_price
            if result.execution.fill_price
            else (result.signal.entry_price if result.signal else None)
        )
        now = datetime.now(timezone.utc)
        remaining = sold
        lots: list[tuple[Any, Decimal, Decimal, Optional[Decimal], Optional[datetime]]] = []
        handled: set = set()
        while remaining > 0 and len(lots) < _MAX_LOTS:
            try:
                # Live rows only: a paper row's P&L would miss the live loss limits
                open_buy = self._trade_repo.find_open_buy(symbol, is_paper=False)
            except Exception:
                logger.warning("Failed to look up open BUY for %s — skipping reflection",
                               symbol, exc_info=True)
                if not lots:
                    return False    # nothing booked: the caller may book it again
                _alert("WARNING", f"sell-not-booked:{symbol}",
                       f"{symbol}: {remaining} sold share(s) not booked: the open trade rows "
                       "could not be read — check the trade rows")
                break
            if open_buy is None:
                if lots:
                    logger.warning("SELL %s: sold %s more than the open BUY rows hold",
                                   symbol, remaining)
                else:
                    logger.info("No open BUY found for %s — SELL without matching BUY, "
                                "skipping reflection", symbol)
                break
            if open_buy.id in handled:
                # Its close did not save: never close it again with the next lot's shares
                _alert("WARNING", f"sell-not-booked:{symbol}",
                       f"{symbol}: {remaining} sold share(s) not booked against a trade row "
                       f"(row {open_buy.id} could not be closed) — check the trade rows")
                break
            handled.add(open_buy.id)
            closed, pnl, buy_price, opened = self._close_lot(open_buy, remaining, sell_price,
                                                             now, result)
            lots.append((open_buy, closed, pnl, buy_price, opened))
            remaining -= closed
            if closed <= 0:
                break  # a row with nothing open left: never loop on it
        else:
            if remaining > 0:
                _alert("WARNING", f"sell-not-booked:{symbol}",
                       f"{symbol}: {remaining} sold share(s) not booked: the SELL spans more "
                       f"than {_MAX_LOTS} open trade rows — check the trade rows")

        if not lots:
            return True
        first, _, _, _, first_opened = lots[0]
        total_pnl = sum((pnl for _, _, pnl, _, _ in lots), start=Decimal("0"))
        cost = sum((price * qty for _, qty, _, price, _ in lots if price is not None),
                   start=Decimal("0"))
        closed_qty = sum((qty for _, qty, _, _, _ in lots), start=Decimal("0"))
        prices_known = sell_price is not None and all(p is not None for _, _, _, p, _ in lots)
        pnl_pct = total_pnl / cost * 100 if prices_known and cost else Decimal("0")
        avg_buy = cost / closed_qty if prices_known and closed_qty else None
        logger.info("Position closed: %s %s share(s) over %d row(s) → SELL@%s, P&L=%.2f "
                    "(%.2f%%)", symbol, closed_qty, len(lots), sell_price or "?", total_pnl,
                    pnl_pct)

        sell_trade_id = getattr(result, "trade_id", None)
        if sell_trade_id:
            try:
                self._trade_repo.update(sell_trade_id, {
                    "opening_trade_id": str(first.id),
                    "pnl": str(total_pnl),
                    "exit_reason": f"SELL closed (P&L: {pnl_pct:.2f}%)",
                })
            except Exception:
                logger.warning("Failed to link SELL to BUY", exc_info=True)

        if self._graph is None:
            return True  # recording only (reflection off, or no graph built)
        returns_losses = _format_returns(symbol, total_pnl, pnl_pct, avg_buy, sell_price)
        try:
            self._graph.reflect(
                returns_losses,
                symbol=symbol,
                realized_return=float(pnl_pct) / 100 if prices_known else None,
                opened_on=first_opened.date().isoformat() if first_opened else None,
            )
            logger.info("Reflection triggered for %s (P&L=%.2f)", symbol, total_pnl)
        except Exception:
            logger.warning("Reflection failed for %s — memories not updated", symbol,
                           exc_info=True)
        return True

    def _close_lot(self, open_buy, remaining: Decimal, sell_price, now: datetime,
                   result: AnalysisResult):
        """Close up to ``remaining`` shares of one open BUY row.

        Returns (shares closed, their P&L, the buy price, when the position opened). The
        row's own ``pnl`` also folds in earlier partials kept on it (``pending_partials``);
        the returned P&L, the signal tracker and reflection do not, because those were
        recorded when they happened.
        """
        signals = dict(open_buy.model_signals or {})
        pending = [p for p in signals.get("pending_partials") or [] if isinstance(p, dict)]
        pending_qty = sum((_decimal(p.get("qty")) for p in pending), start=Decimal("0"))
        pending_pnl = sum((_decimal(p.get("pnl")) for p in pending), start=Decimal("0"))
        open_qty = open_buy.quantity - pending_qty
        closed = max(Decimal("0"), min(remaining, open_qty))
        buy_price = open_buy.fill_price or open_buy.price
        known = sell_price is not None and buy_price is not None
        piece_pnl = (Decimal(str(sell_price)) - buy_price) * closed if known else Decimal("0")
        pnl_pct = ((Decimal(str(sell_price)) - buy_price) / buy_price * 100
                   if known and buy_price else Decimal("0"))
        opened = _opened_at(open_buy)

        if closed < open_qty:
            try:
                # The remainder first: if saving it fails, the row stays open
                saved = self._trade_repo.insert(
                    _remainder(open_buy, open_qty - closed, signals, opened))
            except Exception:
                logger.warning("Saving the remainder of BUY %s failed — the partial exit is "
                               "kept on the open row", open_buy.id, exc_info=True)
                partial = {"qty": str(closed), "sell_price": str(Decimal(str(sell_price)))
                           if sell_price is not None else None, "pnl": str(piece_pnl),
                           "at": now.isoformat()}
                try:
                    self._trade_repo.update(open_buy.id, {"model_signals": {
                        **signals, "pending_partials": pending + [partial]}})
                except Exception:
                    logger.warning("Recording the partial exit on BUY %s failed", open_buy.id,
                                   exc_info=True)
                _alert("WARNING", f"partial-not-split:{open_buy.id}",
                       f"{result.symbol}: sold {closed} of the {open_qty} open on trade row "
                       f"{open_buy.id}, but its remainder could not be saved; the row stays "
                       "open and the partial is counted when it closes")
                _record_outcome(open_buy, result, buy_price, sell_price, piece_pnl, pnl_pct,
                                now, opened=opened)
                return closed, piece_pnl, buy_price, opened
            closing = {
                "quantity": str(closed + pending_qty),
                "closed_at": now.isoformat(),
                "pnl": str(piece_pnl + pending_pnl),
                "exit_reason": f"Partial exit: sold {closed} of {open_qty}",
            }
            if not self._update_twice(open_buy.id, closing):
                # The row stays open in full: undo the remainder, or the open quantity
                # would count twice
                undone = self._undo_remainder(saved)
                _alert("WARNING", f"partial-not-split:{open_buy.id}",
                       f"{result.symbol}: sold {closed} of the {open_qty} open on trade row "
                       f"{open_buy.id} (P&L {piece_pnl}), but the row could not be closed for "
                       "them; " + ("its new remainder row was removed, so the row stays open "
                                   "in full — close the sold part by hand" if undone else
                                   f"its remainder row {getattr(saved, 'id', '?')} could not be "
                                   "removed either: both are open — fix the trade rows"))
                return closed, piece_pnl, buy_price, opened
        else:
            if not self._update_twice(open_buy.id, {
                "closed_at": now.isoformat(),
                "pnl": str(piece_pnl + pending_pnl),
                "exit_reason": f"Closed by SELL (P&L: {pnl_pct:.2f}%)",
            }):
                logger.warning("Failed to mark BUY %s as closed", open_buy.id)
        _record_outcome(open_buy, result, buy_price, sell_price, piece_pnl, pnl_pct, now,
                        opened=opened)
        return closed, piece_pnl, buy_price, opened

    def _update_twice(self, trade_id, fields: dict) -> bool:
        """Update a trade row, retrying once (a transient failure); True if it saved."""
        for attempt in (1, 2):
            try:
                self._trade_repo.update(trade_id, fields)
                return True
            except Exception:
                logger.warning("Updating trade row %s failed (attempt %d of 2)", trade_id,
                               attempt, exc_info=True)
        return False

    def _undo_remainder(self, saved) -> bool:
        """Remove a remainder row whose original could not be closed; True if removed."""
        remainder_id = getattr(saved, "id", None)
        if remainder_id is None:
            return False
        try:
            self._trade_repo.delete(remainder_id)
            return True
        except Exception:
            logger.warning("Removing remainder row %s failed", remainder_id, exc_info=True)
            return False


def _live_filled(execution) -> Optional[Decimal]:
    """A live execution's broker-confirmed quantity; None for paper (whole-row close).

    A live SELL that did not succeed, or whose filled quantity the broker did not
    confirm, sold nothing as far as the books go (0): a refusal before the broker must
    never close a BUY row at the signal's price.
    """
    if execution is None or getattr(execution, "mode", None) != "live":
        return None
    value = getattr(execution, "filled_quantity", None)
    if getattr(execution, "success", False) is not True or not isinstance(value, Decimal):
        return Decimal("0")
    return value


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except ArithmeticError:
        return Decimal("0")


def _opened_at(open_buy) -> Optional[datetime]:
    """When the position opened: a split row keeps it in ``model_signals.opened_at``."""
    raw = (open_buy.model_signals or {}).get("opened_at")
    if isinstance(raw, str) and raw:
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return open_buy.created_at


def _remainder(open_buy, quantity: Decimal, signals: dict, opened: Optional[datetime]):
    """The still-open part of a BUY row sold in part, as a new open row."""
    from skopaq.db.models import TradeRecord

    return TradeRecord(
        symbol=open_buy.symbol, exchange=open_buy.exchange, side="BUY", quantity=quantity,
        price=open_buy.price, order_type=open_buy.order_type, product=open_buy.product,
        order_id=None, status=open_buy.status, is_paper=open_buy.is_paper,
        signal_source=open_buy.signal_source, agent_decision=dict(open_buy.agent_decision or {}),
        model_signals={**signals, "split_from": str(open_buy.id),
                       "opened_at": opened.isoformat() if opened else None,
                       "pending_partials": []},
        consensus_score=open_buy.consensus_score,
        entry_reason=f"Remainder of {open_buy.id} after a partial exit",
        brokerage=Decimal("0"), fill_price=open_buy.fill_price, slippage=open_buy.slippage,
        strategy_version=open_buy.strategy_version, nifty_level=open_buy.nifty_level,
        india_vix=open_buy.india_vix,
    )


def _alert(severity: str, key: str, text: str) -> None:
    try:
        from skopaq.execution.order_alerts import get_alerter

        get_alerter().alert(severity, key, text)
    except Exception:
        logger.warning(text)


def _record_outcome(open_buy, result, buy_price, sell_price, pnl, pnl_pct, now, *,
                    opened=None) -> None:
    """Feed the closed position to the signal tracker (skopaq/learning/tracker.py).

    The tracker's analytics (win rate by sector and hour, confidence
    calibration) back the MCP learning tools; it stores to DATABASE_URL and
    does nothing without it.
    """
    try:
        from skopaq.learning.tracker import SECTOR_MAP, SignalRecord, record_signal

        opened = opened or open_buy.created_at
        if opened is not None and opened.tzinfo is None:
            opened = opened.replace(tzinfo=timezone.utc)
        ist_hour = (opened + timedelta(hours=5, minutes=30)).hour if opened else 0
        record_signal(SignalRecord(
            symbol=result.symbol,
            signal="BUY",
            confidence=int((open_buy.agent_decision or {}).get("confidence") or 0),
            entry_price=float(buy_price or 0),
            exit_price=float(sell_price or 0),
            pnl=float(pnl),
            pnl_pct=float(pnl_pct),
            won=pnl > 0,
            sector=SECTOR_MAP.get(result.symbol, ""),
            entry_hour=ist_hour,
            holding_days=(now - opened).days if opened else 0,
            exit_reason=(result.signal.reasoning or "")[:50] if result.signal else "",
        ))
    except Exception:
        logger.debug("Signal tracker not updated", exc_info=True)


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
