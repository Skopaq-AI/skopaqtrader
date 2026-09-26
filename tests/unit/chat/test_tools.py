"""Tests for chat tool definitions."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skopaq.chat.tools import (
    _get_infra,
    analyze_stock,
    check_safety,
    check_status,
    compute_position_size,
    get_all_tools,
    get_market_data,
    get_orders,
    get_portfolio,
    get_quote,
    init_tools,
    scan_market,
    trade_stock,
)


@pytest.fixture
def mock_infra():
    """Minimal Infrastructure mock for tool tests."""
    infra = MagicMock()
    infra.config.trading_mode = "paper"
    infra.config.asset_class = "equity"
    infra.config.initial_paper_capital = 100_000
    infra.config.position_sizing_enabled = True
    infra.config.selected_analysts = "market,social"
    infra.config.regime_detection_enabled = False
    infra.config.google_api_key = MagicMock()
    infra.config.google_api_key.get_secret_value.return_value = "test"
    infra.config.anthropic_api_key = MagicMock()
    infra.config.anthropic_api_key.get_secret_value.return_value = "test"
    infra.config.perplexity_api_key = MagicMock()
    infra.config.perplexity_api_key.get_secret_value.return_value = ""
    infra.config.xai_api_key = MagicMock()
    infra.config.xai_api_key.get_secret_value.return_value = ""
    infra.config.openrouter_api_key = MagicMock()
    infra.config.openrouter_api_key.get_secret_value.return_value = ""
    infra.config.binance_base_url = "https://api.binance.com"

    # Order router
    infra.order_router = AsyncMock()
    infra.order_router.mode = "paper"

    # Safety checker
    infra.safety_checker = MagicMock()

    # Position sizer
    infra.position_sizer = MagicMock()

    # Executor
    infra.executor = MagicMock()

    # Paper engine
    infra.paper_engine = MagicMock()

    # LLM map
    infra.llm_map = {"_default": MagicMock()}

    # Upstream config
    infra.upstream_config = {}

    init_tools(infra)
    return infra


def test_get_all_tools_returns_twelve():
    tools = get_all_tools()
    assert len(tools) == 12
    names = {t.name for t in tools}
    expected = {
        "analyze_stock",
        "trade_stock",
        "scan_market",
        "get_portfolio",
        "get_quote",
        "get_orders",
        "check_status",
        "compute_position_size",
        "check_safety",
        "get_market_data",
        "recall_memory",
        "view_past_trades",
    }
    assert names == expected


def test_init_tools_sets_infra():
    infra = MagicMock()
    init_tools(infra)
    assert _get_infra() is infra


def test_get_infra_raises_when_not_init():
    import skopaq.chat.tools as tools_mod

    old = tools_mod._infra
    try:
        tools_mod._infra = None
        with pytest.raises(RuntimeError, match="not initialised"):
            _get_infra()
    finally:
        tools_mod._infra = old


@pytest.mark.asyncio
async def test_get_portfolio_empty(mock_infra):
    mock_infra.order_router.get_positions.return_value = []
    mock_infra.order_router.get_holdings.return_value = []

    funds = MagicMock()
    funds.available_cash = 100_000
    funds.used_margin = 0
    funds.total_collateral = 100_000
    mock_infra.order_router.get_funds.return_value = funds

    result = await get_portfolio.ainvoke({})
    assert "Portfolio" in result
    assert "No open positions" in result
    assert "100,000" in result


@pytest.mark.asyncio
async def test_get_portfolio_with_positions(mock_infra):
    pos = MagicMock()
    pos.symbol = "RELIANCE"
    pos.exchange = "NSE"
    pos.quantity = Decimal("10")
    pos.average_price = 2500.0
    pos.last_price = 2600.0
    pos.pnl = 1000.0
    mock_infra.order_router.get_positions.return_value = [pos]
    mock_infra.order_router.get_holdings.return_value = []

    funds = MagicMock()
    funds.available_cash = 75_000
    funds.used_margin = 25_000
    funds.total_collateral = 100_000
    mock_infra.order_router.get_funds.return_value = funds

    result = await get_portfolio.ainvoke({})
    assert "RELIANCE" in result
    assert "P&L" in result
    assert "+₹1,000.00" in result


@pytest.mark.asyncio
async def test_get_orders_empty(mock_infra):
    mock_infra.order_router.get_orders.return_value = []
    result = await get_orders.ainvoke({})
    assert "No orders" in result


@pytest.mark.asyncio
async def test_get_orders_with_data(mock_infra):
    order = MagicMock()
    order.order_id = "ORD-123"
    order.status = "COMPLETE"
    order.message = "BUY RELIANCE 10@2500"
    mock_infra.order_router.get_orders.return_value = [order]

    result = await get_orders.ainvoke({})
    assert "ORD-123" in result
    assert "COMPLETE" in result


@pytest.mark.asyncio
async def test_check_status(mock_infra):
    with patch("skopaq.broker.token_manager.TokenManager") as MockTM:
        health = MagicMock()
        health.valid = True
        MockTM.return_value.get_health.return_value = health

        result = await check_status.ainvoke({})
        assert "System Status" in result
        assert "paper" in result
        assert "Valid" in result


@pytest.mark.asyncio
async def test_check_safety_passes(mock_infra):
    from skopaq.execution.safety_checker import SafetyResult

    mock_infra.safety_checker.validate.return_value = SafetyResult(
        passed=True, rejections=[]
    )
    mock_infra.order_router.get_positions.return_value = []
    funds = MagicMock()
    funds.available_cash = 100_000
    funds.used_margin = 0
    mock_infra.order_router.get_funds.return_value = funds

    result = await check_safety.ainvoke({
        "symbol": "RELIANCE",
        "quantity": 10,
        "price": 2500,
        "side": "BUY",
    })
    assert "PASSED" in result


@pytest.mark.asyncio
async def test_check_safety_rejects(mock_infra):
    from skopaq.execution.safety_checker import SafetyResult

    mock_infra.safety_checker.validate.return_value = SafetyResult(
        passed=False, rejections=["Exceeds position limit", "Market closed"]
    )
    mock_infra.order_router.get_positions.return_value = []
    funds = MagicMock()
    funds.available_cash = 100_000
    funds.used_margin = 0
    mock_infra.order_router.get_funds.return_value = funds

    result = await check_safety.ainvoke({
        "symbol": "RELIANCE",
        "quantity": 10,
        "price": 2500,
        "side": "BUY",
    })
    assert "FAILED" in result
    assert "Exceeds position limit" in result
    assert "Market closed" in result


@pytest.mark.asyncio
async def test_compute_position_size(mock_infra):
    from skopaq.risk.position_sizer import PositionSize

    mock_infra.position_sizer.compute_size.return_value = PositionSize(
        quantity=111,
        stop_loss=2410.0,
        risk_amount=10_000,
        atr=45.0,
        atr_source="vendor",
    )
    funds = MagicMock()
    funds.available_cash = 1_000_000
    mock_infra.order_router.get_funds.return_value = funds

    result = await compute_position_size.ainvoke({
        "symbol": "RELIANCE",
        "equity": 1_000_000,
        "price": 2500,
    })
    assert "111 shares" in result
    assert "2,410.00" in result
    assert "vendor" in result


@pytest.mark.asyncio
async def test_compute_position_size_disabled(mock_infra):
    mock_infra.position_sizer = None
    result = await compute_position_size.ainvoke({
        "symbol": "RELIANCE",
        "equity": 1_000_000,
        "price": 2500,
    })
    assert "disabled" in result


@pytest.mark.asyncio
async def test_analyze_stock_error_handling(mock_infra):
    with patch("skopaq.graph.skopaq_graph.SkopaqTradingGraph") as MockGraph:
        MockGraph.return_value.analyze = AsyncMock(side_effect=Exception("LLM timeout"))

        result = await analyze_stock.ainvoke({"symbol": "RELIANCE"})
        assert "error" in result.lower() or "Error" in result


@pytest.mark.asyncio
async def test_analyze_stock_returns_signal(mock_infra):
    with patch("skopaq.graph.skopaq_graph.SkopaqTradingGraph") as MockGraph:
        mock_result = MagicMock()
        mock_result.error = None
        mock_result.signal = MagicMock()
        mock_result.signal.action = "BUY"
        mock_result.signal.confidence = 75
        mock_result.signal.entry_price = 2500.0
        mock_result.signal.stop_loss = 2400.0
        mock_result.signal.target = 2700.0
        mock_result.signal.reasoning = "Strong momentum"
        mock_result.duration_seconds = 180.0
        mock_result.cache_hits = 2

        MockGraph.return_value.analyze = AsyncMock(return_value=mock_result)

        result = await analyze_stock.ainvoke({"symbol": "RELIANCE"})
        assert "BUY" in result
        assert "75%" in result
        assert "RELIANCE" in result
        assert "2,500.00" in result


# ── GAP 2: a SELL's open-order context; live fills in the trade output ───────


def _sell_inputs(positions, *, error=""):
    from datetime import datetime, timezone

    from skopaq.execution.order_router import SellInputs
    from skopaq.execution.sellable import SellContext

    return SellInputs(positions=positions, holdings=[], context=SellContext(
        orders=(), read_at=datetime(2026, 9, 25, 5, 30, tzinfo=timezone.utc), error=error))


def _funds(mock_infra):
    funds = MagicMock()
    funds.available_cash = 100_000
    funds.used_margin = 0
    funds.available_margin = 100_000
    mock_infra.order_router.get_funds.return_value = funds


@pytest.mark.asyncio
async def test_check_safety_sell_shows_an_unreadable_order_book(mock_infra):
    from skopaq.broker.models import Position
    from skopaq.constants import PAPER_SAFETY_RULES
    from skopaq.execution.safety_checker import SafetyChecker

    mock_infra.safety_checker = SafetyChecker(rules=PAPER_SAFETY_RULES)
    mock_infra.order_router.sell_inputs = AsyncMock(return_value=_sell_inputs(
        [Position(symbol="TCS", quantity=Decimal(10))], error="HTTP 503"))
    _funds(mock_infra)

    result = await check_safety.ainvoke({"symbol": "TCS", "quantity": 5, "side": "SELL"})

    assert "FAILED" in result
    assert "Cannot read the broker's order book (HTTP 503)" in result
    mock_infra.order_router.get_positions.assert_not_called()


@pytest.mark.asyncio
async def test_check_safety_passes_no_sell_context_in_paper(mock_infra):
    from skopaq.execution.safety_checker import SafetyResult

    mock_infra.safety_checker.validate.return_value = SafetyResult(passed=True, rejections=[])
    mock_infra.order_router.sell_inputs = AsyncMock(return_value=None)   # paper
    mock_infra.order_router.get_positions.return_value = []
    mock_infra.order_router.get_settled_holdings.return_value = []
    _funds(mock_infra)

    sell = await check_safety.ainvoke({"symbol": "TCS", "quantity": 5, "side": "SELL"})
    assert "PASSED" in sell
    assert mock_infra.safety_checker.validate.call_args.kwargs["sell_context"] is None
    mock_infra.order_router.get_positions.assert_awaited()

    mock_infra.order_router.sell_inputs.reset_mock()
    await check_safety.ainvoke({"symbol": "TCS", "quantity": 1, "price": 100, "side": "BUY"})
    mock_infra.order_router.sell_inputs.assert_not_called()
    assert mock_infra.safety_checker.validate.call_args.kwargs["sell_context"] is None


async def _trade(mock_infra, execution, mode="live"):
    mock_infra.config.trading_mode = mode
    result = MagicMock()
    result.error = None
    result.signal = MagicMock(action="SELL", confidence=80)
    result.execution = execution
    result.duration_seconds = 1.0
    with patch("skopaq.graph.skopaq_graph.SkopaqTradingGraph") as MockGraph, \
         patch("skopaq.chat.tools._compute_risk_scales", return_value=(1.0, 1.0)), \
         patch("skopaq.chat.tools._inject_paper_quote", new_callable=AsyncMock):
        MockGraph.return_value.analyze_and_execute = AsyncMock(return_value=result)
        return await trade_stock.ainvoke({"symbol": "TCS"})


@pytest.mark.asyncio
async def test_trade_stock_reports_a_partial_live_fill(mock_infra):
    from skopaq.broker.models import ExecutionResult

    out = await _trade(mock_infra, ExecutionResult(
        success=True, mode="live", fill_price=95.0, filled_quantity=Decimal(3),
        requested_quantity=Decimal(5), outcome="partial", order_ids=["EQ-1"],
        broker_message="filled 3 of 5; rest cancelled"))

    assert "- Execution: Partially filled (3 of 5) (live mode)" in out
    assert "- Orders: EQ-1" in out
    assert "filled 3 of 5; rest cancelled" in out


@pytest.mark.asyncio
async def test_trade_stock_reports_an_unconfirmed_live_order(mock_infra):
    from skopaq.broker.models import ExecutionResult

    out = await _trade(mock_infra, ExecutionResult(
        success=False, mode="live", outcome="open", order_ids=["EQ-1"], remaining_open=True,
        rejection_reason="Order EQ-1 may still be working — cancel it at the broker"))

    assert "- Execution: Unconfirmed (live mode)" in out
    assert "- Orders: EQ-1" in out
    assert "- Reason: Order EQ-1 may still be working" in out


@pytest.mark.asyncio
async def test_trade_stock_paper_output_is_unchanged(mock_infra):
    from skopaq.broker.models import ExecutionResult

    filled = await _trade(mock_infra, ExecutionResult(
        success=True, mode="paper", fill_price=2500.0, slippage=0.5), mode="paper")
    refused = await _trade(mock_infra, ExecutionResult(
        success=False, mode="paper", rejection_reason="No short sales"), mode="paper")

    assert "- Execution: Filled (paper mode)\n- Fill Price: ₹2,500.00\n- Slippage: 0.5000" \
        in filled
    assert "Orders" not in filled
    assert "- Execution: Rejected (paper mode)\n- Reason: No short sales" in refused
