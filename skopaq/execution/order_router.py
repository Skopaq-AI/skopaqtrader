"""Routes orders to paper engine or live broker based on trading mode.

The router is intentionally thin — it checks ``config.trading_mode`` and
dispatches to the appropriate execution backend.  Switching paper → live
is a config change, not a code change.
"""

from __future__ import annotations

import logging
from typing import Optional

from skopaq.broker.client import INDstocksClient
from skopaq.broker.models import (
    ExecutionResult,
    Funds,
    Holding,
    OrderRequest,
    OrderResponse,
    Position,
    TradingSignal,
)
from skopaq.broker.paper_engine import PaperEngine
from skopaq.broker.scrip_resolver import resolve_security_id
from skopaq.config import SkopaqConfig

logger = logging.getLogger(__name__)


class OrderRouter:
    """Routes orders to the correct execution backend.

    In ``paper`` mode all orders go through the PaperEngine.
    In ``live`` mode orders go to the INDstocks REST API.

    Args:
        config: Application configuration (determines mode).
        paper_engine: Paper trading engine instance.
        live_client: INDstocks REST client (can be None in paper-only mode).
    """

    def __init__(
        self,
        config: SkopaqConfig,
        paper_engine: PaperEngine,
        live_client: Optional[INDstocksClient] = None,
    ) -> None:
        self._mode = config.trading_mode
        self._paper = paper_engine
        self._live = live_client

    @property
    def mode(self) -> str:
        return self._mode

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
        set on the order, then places the order via the broker client.
        """
        if self._live is None:
            logger.error("Live client not configured — falling back to paper")
            return self._execute_paper(order, signal)

        try:
            # Resolve security_id if missing (executor builds orders without it)
            if not order.security_id:
                order.security_id = await resolve_security_id(
                    self._live, order.symbol, order.exchange.value,
                )
                logger.info(
                    "Resolved %s → security_id=%s",
                    order.symbol, order.security_id,
                )

            response = await self._live.place_order(order)
            return ExecutionResult(
                success=True,
                order=response,
                signal=signal,
                mode="live",
                fill_price=order.price,   # Limit price (actual fill via order book)
                brokerage=20.0,           # INDstocks flat fee estimate
            )
        except Exception as exc:
            logger.error("Live order failed: %s — NOT falling back to paper", exc)
            return ExecutionResult(
                success=False,
                signal=signal,
                mode="live",
                rejection_reason=f"Broker error: {exc}",
            )

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
        """Get today's orders from the active backend."""
        if self._mode == "live" and self._live:
            return await self._live.get_orders()
        return self._paper.get_orders()
