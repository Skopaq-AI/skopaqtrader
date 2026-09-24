"""End to end through Executor → SafetyChecker → paper engine: no short sales."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from skopaq.broker.models import Quote, TradingSignal
from skopaq.broker.paper_engine import PaperEngine
from skopaq.constants import SafetyRules
from skopaq.execution.executor import Executor
from skopaq.execution.order_router import OrderRouter
from skopaq.execution.safety_checker import SafetyChecker


def _executor() -> tuple[Executor, PaperEngine]:
    config = MagicMock()
    config.trading_mode = "paper"
    paper = PaperEngine(initial_capital=1_000_000)
    paper.update_quote(Quote(symbol="RELIANCE", ltp=2500.0, close=2500.0))
    rules = SafetyRules(
        market_hours_only=False, require_stop_loss=False, max_lots_per_position=10000,
        max_order_value_inr=10_000_000, max_position_pct=1.0,
    )
    router = OrderRouter(config, paper)
    return Executor(router, SafetyChecker(rules=rules)), paper


def _signal(action: str) -> TradingSignal:
    return TradingSignal(symbol="RELIANCE", action=action, confidence=80,
                         entry_price=2500.0, quantity=Decimal("1"))


@pytest.mark.asyncio
async def test_sell_of_unheld_stock_is_rejected():
    executor, _ = _executor()
    result = await executor.execute_signal(_signal("SELL"))

    assert not result.success
    assert not result.safety_passed
    assert "No short sales" in result.rejection_reason


@pytest.mark.asyncio
async def test_sell_after_buy_is_allowed():
    executor, paper = _executor()
    bought = await executor.execute_signal(_signal("BUY"))
    assert bought.success, bought.rejection_reason

    sold = await executor.execute_signal(_signal("SELL"))
    assert sold.safety_passed, sold.rejection_reason


@pytest.mark.asyncio
async def test_paper_shares_are_not_counted_twice():
    """Paper holdings mirror paper positions; only one of them may count."""
    executor, paper = _executor()
    assert (await executor.execute_signal(_signal("BUY"))).success

    oversell = _signal("SELL").model_copy(update={"quantity": Decimal("2")})
    result = await executor.execute_signal(oversell)

    assert not result.safety_passed
    assert "only 1 held" in result.rejection_reason
    assert paper.get_positions()[0].quantity == 1


@pytest.mark.asyncio
async def test_live_router_returns_broker_holdings():
    config = MagicMock()
    config.trading_mode = "live"
    live = MagicMock()

    async def holdings():
        return ["from broker"]

    live.get_holdings = holdings
    router = OrderRouter(config, PaperEngine(initial_capital=1), live_client=live)

    assert await router.get_settled_holdings() == ["from broker"]
