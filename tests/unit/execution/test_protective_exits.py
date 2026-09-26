"""Protective exits: a stop-loss, EOD or AI SELL of a held position must go through.

Two defects kept them from it:

- exit SELLs were LIMIT orders at the position's entry price, so once the
  price fell below entry (every stop-loss) paper refused the fill and live
  left a resting order above the market while the exit was recorded at
  breakeven;
- SafetyChecker applied BUY-side risk limits to them: after a loss the
  cool-down and loss limits refused the next stop-loss, and a position that
  had grown past the size or value caps could not be sold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skopaq.broker.models import (
    Exchange,
    Funds,
    Holding,
    OrderRequest,
    OrderResponse,
    OrderType,
    Position,
    Product,
    Quote,
    Side,
    TradingSignal,
)
from skopaq.broker.paper_engine import PaperEngine
from skopaq.constants import SafetyRules
from skopaq.execution.executor import Executor
from skopaq.execution.order_router import OrderRouter
from skopaq.execution.safety_checker import SafetyChecker

RULES = SafetyRules(
    max_position_pct=0.15,
    max_daily_loss_pct=0.03,
    max_weekly_loss_pct=0.07,
    max_monthly_loss_pct=0.12,
    max_order_value_inr=100_000,
    max_lots_per_position=5,
    require_stop_loss=False,
    market_hours_only=False,
    cool_down_after_loss_minutes=15,
)
FUNDS = Funds(available_cash=500_000, available_margin=500_000, total_collateral=500_000)


def _order(side: Side, qty: int = 10, price: float | None = 2500.0) -> OrderRequest:
    return OrderRequest(
        symbol="RELIANCE", exchange=Exchange.NSE, side=side, quantity=qty,
        order_type=OrderType.LIMIT if price else OrderType.MARKET, price=price,
        product=Product.CNC,
    )


def _held(qty: int = 10) -> list[Position]:
    return [Position(symbol="RELIANCE", quantity=qty, average_price=2500.0)]


# ── SafetyChecker ────────────────────────────────────────────────────────────


class TestSafetyChecker:
    def test_stop_loss_after_a_loss_passes_the_cool_down(self):
        checker = SafetyChecker(rules=RULES)
        checker.record_pnl(-100)  # an earlier stop-loss: cool-down starts

        buy = checker.validate(_order(Side.BUY, qty=1), None, [], FUNDS, 500_000)
        sell = checker.validate(_order(Side.SELL, qty=1), None, _held(1), FUNDS, 500_000)

        assert any("Cool-down" in r for r in buy.rejections)
        assert sell.passed, sell.rejections

    @pytest.mark.parametrize("attr", ["_day_pnl", "_week_pnl", "_month_pnl"])
    def test_exits_pass_the_loss_limits(self, attr):
        checker = SafetyChecker(rules=RULES)
        setattr(checker, attr, -100_000)  # 20% of 500k: every limit is breached

        buy = checker.validate(_order(Side.BUY, qty=1), None, [], FUNDS, 500_000)
        sell = checker.validate(_order(Side.SELL, qty=1), None, _held(1), FUNDS, 500_000)

        assert any("circuit breaker" in r for r in buy.rejections)
        assert sell.passed, sell.rejections

    def test_a_grown_position_can_be_sold_whole(self):
        """Above the size (15%), value (1 lakh) and lot (5) caps that bound a BUY."""
        checker = SafetyChecker(rules=RULES)
        # 41 x 2500 = 1,02,500: 20.5% of 5 lakh, over 1 lakh, and 41 > 5 lots
        buy = checker.validate(_order(Side.BUY, qty=41), None, [], FUNDS, 500_000)
        sell = checker.validate(_order(Side.SELL, qty=41), None, _held(41), FUNDS, 500_000)

        for cap in ("Position size", "Order value", "Quantity 41 exceeds"):
            assert any(r.startswith(cap) for r in buy.rejections), buy.rejections
        assert sell.passed, sell.rejections

    def test_selling_more_than_is_held_is_still_refused(self):
        checker = SafetyChecker(rules=RULES)
        checker.record_pnl(-100_000)
        result = checker.validate(_order(Side.SELL, qty=11), None, _held(10), FUNDS, 500_000)
        assert not result.passed
        assert any("No short sales" in r for r in result.rejections)

    def test_market_hours_still_apply_to_exits(self):
        checker = SafetyChecker(rules=SafetyRules(market_hours_only=True, require_stop_loss=False))
        night = datetime(2026, 9, 25, 16, 0, tzinfo=timezone.utc)  # 21:30 IST
        with patch("skopaq.execution.safety_checker.datetime") as clock:
            clock.now.return_value = night
            result = checker.validate(_order(Side.SELL, qty=1), None, _held(1), FUNDS, 500_000)
        assert any("Outside market hours" in r for r in result.rejections)

    def test_the_order_rate_still_applies_to_exits(self):
        checker = SafetyChecker(rules=SafetyRules(
            market_hours_only=False, require_stop_loss=False, max_orders_per_minute=2))
        results = [checker.validate(_order(Side.SELL, qty=1), None, _held(10), FUNDS, 500_000)
                   for _ in range(3)]
        assert [r.passed for r in results] == [True, True, False]


# ── Executor → paper engine, end to end ──────────────────────────────────────


def _paper_executor(**rules) -> tuple[Executor, PaperEngine, SafetyChecker]:
    config = MagicMock(trading_mode="paper")
    paper = PaperEngine(initial_capital=1_000_000)
    safety = SafetyChecker(rules=SafetyRules(**{
        "market_hours_only": False, "require_stop_loss": False, **rules}))
    return Executor(OrderRouter(config, paper), safety), paper, safety


def _exit(symbol: str, ltp: float, qty: int) -> TradingSignal:
    """The SELL PositionMonitor builds when a stop fires."""
    return TradingSignal(symbol=symbol, action="SELL", confidence=80, entry_price=ltp,
                         order_type=OrderType.MARKET, quantity=Decimal(qty),
                         reasoning="HARD STOP")


@pytest.mark.asyncio
async def test_stop_loss_below_entry_fills_and_records_the_loss():
    executor, paper, safety = _paper_executor()
    paper.update_quote(Quote(symbol="TCS", ltp=100, bid=99.9, ask=100.1))
    bought = await executor.execute_signal(TradingSignal(
        symbol="TCS", action="BUY", confidence=80, entry_price=100, quantity=Decimal(5)))
    assert bought.success, bought.rejection_reason

    paper.update_quote(Quote(symbol="TCS", ltp=94, bid=93.9, ask=94.1))
    sold = await executor.execute_signal(_exit("TCS", 94, 5))

    assert sold.success, sold.rejection_reason
    assert sold.fill_price < 95
    assert paper.get_positions() == []
    assert safety._day_pnl == pytest.approx((sold.fill_price - bought.fill_price) * 5)
    assert safety._last_loss_time is not None  # the cool-down now blocks BUYs


@pytest.mark.asyncio
async def test_second_stop_loss_is_not_blocked_by_the_first_ones_cool_down():
    executor, paper, _ = _paper_executor(cool_down_after_loss_minutes=15)
    for symbol in ("TCS", "INFY"):
        paper.update_quote(Quote(symbol=symbol, ltp=100, bid=99.9, ask=100.1))
        assert (await executor.execute_signal(TradingSignal(
            symbol=symbol, action="BUY", confidence=80, entry_price=100,
            quantity=Decimal(5)))).success

    for symbol in ("TCS", "INFY"):
        paper.update_quote(Quote(symbol=symbol, ltp=94, bid=93.9, ask=94.1))
        sold = await executor.execute_signal(_exit(symbol, 94, 5))
        assert sold.success, f"{symbol}: {sold.rejection_reason}"


@pytest.mark.asyncio
async def test_a_limit_sell_at_a_price_is_still_a_limit_order():
    """Only an explicit MARKET changes the order type; a priced SELL stays LIMIT."""
    executor, paper, _ = _paper_executor()
    paper.update_quote(Quote(symbol="TCS", ltp=100, bid=99.9, ask=100.1))
    assert (await executor.execute_signal(TradingSignal(
        symbol="TCS", action="BUY", confidence=80, entry_price=100, quantity=Decimal(5)))).success

    paper.update_quote(Quote(symbol="TCS", ltp=94, bid=93.9, ask=94.1))
    limit = await executor.execute_signal(TradingSignal(
        symbol="TCS", action="SELL", confidence=80, entry_price=100, quantity=Decimal(5)))

    assert not limit.success  # a LIMIT at 100 cannot fill at 94
    assert "LIMIT" in limit.rejection_reason


def test_build_order():
    executor, _, _ = _paper_executor()
    market = executor._build_order(_exit("TCS", 94, 5))
    limit = executor._build_order(TradingSignal(
        symbol="TCS", action="SELL", confidence=80, entry_price=100, quantity=Decimal(5)))
    unpriced = executor._build_order(TradingSignal(
        symbol="TCS", action="SELL", confidence=80, quantity=Decimal(5)))

    assert (market.order_type, market.price) == (OrderType.MARKET, None)
    assert (limit.order_type, limit.price) == (OrderType.LIMIT, 100)
    assert (unpriced.order_type, unpriced.price) == (OrderType.MARKET, None)


def _mock_live_router(positions, holdings) -> MagicMock:
    """A live router mock: a SELL's positions and holdings come from ``sell_inputs``
    (read book-first, with an empty order book here), under no lock."""
    from skopaq.execution.order_router import SellInputs
    from skopaq.execution.sellable import SellContext

    router = MagicMock(mode="live")
    router.sell_lock = MagicMock(return_value=None)
    router.sell_inputs = AsyncMock(return_value=SellInputs(
        positions=positions, holdings=holdings,
        context=SellContext(orders=(), read_at=datetime(2026, 9, 25, 11, 0,
                                                        tzinfo=timezone.utc))))
    router.get_funds = AsyncMock(return_value=FUNDS)
    return router


@pytest.mark.asyncio
async def test_exit_pnl_uses_the_holding_cost_when_there_is_no_position():
    """Live: shares bought on an earlier day are delivery holdings, not positions."""
    safety = SafetyChecker(rules=SafetyRules(market_hours_only=False, require_stop_loss=False))
    router = _mock_live_router(positions=[], holdings=[
        Holding(symbol="TCS", quantity=Decimal(5), average_price=100.0)])
    router.execute = AsyncMock(side_effect=lambda order, signal: MagicMock(
        success=True, fill_price=signal.entry_price, order=None, rejection_reason=""))

    result = await Executor(router, safety).execute_signal(_exit("TCS", 94, 5))

    assert result.success
    assert safety._day_pnl == pytest.approx(-30.0)


@pytest.mark.parametrize("today", [Decimal(-5), Decimal(0)])
@pytest.mark.asyncio
async def test_exit_pnl_skips_a_position_row_with_no_long_quantity(today):
    """Live day 2+: shares sold earlier today leave a net-negative (or flat) position
    at the sale price; the holding keeps the cost the rest is measured against."""
    safety = SafetyChecker(rules=SafetyRules(market_hours_only=False, require_stop_loss=False))
    router = _mock_live_router(
        positions=[Position(symbol="TCS", quantity=today, average_price=80.0)],
        holdings=[Holding(symbol="TCS", quantity=Decimal(10), average_price=100.0)])
    router.execute = AsyncMock(side_effect=lambda order, signal: MagicMock(
        success=True, fill_price=signal.entry_price, order=None, rejection_reason=""))

    held_after_today = 10 + int(today)
    result = await Executor(router, safety).execute_signal(_exit("TCS", 94, held_after_today))

    assert result.success, result.rejection_reason
    assert safety._day_pnl == pytest.approx((94 - 100) * held_after_today)


# ── Live router: the fill price of a MARKET order ────────────────────────────


def _filled_live_client(qty: int, price: str) -> AsyncMock:
    """A live client whose order is SUCCESS on the first read (qty at price)."""
    live = AsyncMock()
    live.place_order = AsyncMock(return_value=OrderResponse(
        order_id="EQ-1", status="INITIATED", message=""))
    live.get_order = AsyncMock(return_value={
        "id": "EQ-1", "status": "SUCCESS", "txn_type": "SELL", "requested_qty": qty,
        "traded_qty": qty, "traded_price": price})
    live.get_order_book = AsyncMock(return_value=[])
    live.get_trades = AsyncMock(return_value=[])
    return live


def _live_router(live) -> OrderRouter:
    # A fixed wall clock: nothing is placed after 15:29:55 IST
    wall = lambda: datetime(2026, 9, 25, 11, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))  # noqa: E731
    return OrderRouter(MagicMock(trading_mode="live"), PaperEngine(), live_client=live, wall=wall)


@pytest.mark.asyncio
async def test_live_market_sell_reports_the_broker_fill():
    live = _filled_live_client(5, "93.80")
    order = OrderRequest(symbol="TCS", side=Side.SELL, quantity=Decimal(5),
                         order_type=OrderType.MARKET, security_id="11536")

    result = await _live_router(live).execute(order, _exit("TCS", 94, 5))

    payload_order = live.place_order.await_args.args[0]
    assert payload_order.order_type == OrderType.MARKET and payload_order.price is None
    assert (result.fill_price, result.fill_price_source) == (93.8, "order")


@pytest.mark.asyncio
async def test_live_market_sell_without_a_broker_price_uses_the_reference_price():
    live = _filled_live_client(5, "")
    order = OrderRequest(symbol="TCS", side=Side.SELL, quantity=Decimal(5),
                         order_type=OrderType.MARKET, security_id="11536")

    result = await _live_router(live).execute(order, _exit("TCS", 94, 5))

    assert result.success
    assert (result.fill_price, result.fill_price_source) == (94, "estimate")


# ── Callers ──────────────────────────────────────────────────────────────────


async def _close(positions, client):
    from skopaq.execution.daemon import TradingDaemon

    daemon = TradingDaemon(MagicMock(trading_mode="live"))
    daemon._client = client
    daemon._router = AsyncMock()
    daemon._router.get_positions = AsyncMock(return_value=positions)
    daemon._executor = AsyncMock()
    daemon._executor.execute_signal = AsyncMock(return_value=MagicMock(success=True))
    with patch.object(daemon, "_record_exit", new_callable=AsyncMock), \
         patch("skopaq.broker.scrip_resolver.resolve_scrip_code",
               new=AsyncMock(side_effect=lambda client, symbol: f"NSE_{symbol}")):
        await daemon._phase_close()
    return [c.args[0] for c in daemon._executor.execute_signal.await_args_list]


@pytest.mark.asyncio
async def test_daemon_close_sells_at_market_at_the_ltp():
    """INDstocks position rows have no last price: the close fetches the LTP."""
    client = MagicMock()
    client.get_ltp = AsyncMock(return_value=1450.0)
    signals = await _close([
        Position(symbol="TCS", quantity=Decimal(3), average_price=4000.0, last_price=3800.0),
        Position(**{"symbol": "INFY", "net_qty": "2", "avg_price": 1500.0}),  # as INDstocks sends it
    ], client)

    assert [(s.symbol, s.order_type, s.entry_price) for s in signals] == [
        ("TCS", OrderType.MARKET, 3800.0),   # paper fills last_price itself
        ("INFY", OrderType.MARKET, 1450.0),  # the broker's LTP, not the average price
    ]
    client.get_ltp.assert_awaited_once_with("NSE_INFY")


@pytest.mark.asyncio
async def test_daemon_close_without_a_price_does_not_invent_breakeven():
    client = MagicMock()
    client.get_ltp = AsyncMock(side_effect=RuntimeError("quote API down"))
    [signal] = await _close([Position(**{"symbol": "INFY", "net_qty": "2", "avg_price": 1500.0})],
                            client)

    assert (signal.order_type, signal.entry_price) == (OrderType.MARKET, None)


@pytest.mark.asyncio
async def test_live_close_records_the_loss_at_the_broker_fill():
    """Daemon CLOSE → Executor → live router: the loss reaches the safety checker."""
    from skopaq.execution.daemon import TradingDaemon

    safety = SafetyChecker(rules=SafetyRules(market_hours_only=False, require_stop_loss=False))
    live = _filled_live_client(3, "3790")
    live.get_ltp = AsyncMock(return_value=3800.0)
    positions = [Position(**{"symbol": "TCS", "net_qty": "3", "avg_price": 4000.0})]
    live.get_positions = AsyncMock(return_value=positions)
    live.get_holdings = AsyncMock(return_value=[])
    live.get_funds = AsyncMock(return_value=FUNDS)
    router = _live_router(live)

    daemon = TradingDaemon(MagicMock(trading_mode="live"))
    daemon._client, daemon._router, daemon._executor = live, router, Executor(router, safety)
    with patch.object(daemon, "_record_exit", new_callable=AsyncMock) as record, \
         patch("skopaq.broker.scrip_resolver.resolve_scrip_code", new=AsyncMock(return_value="S")), \
         patch("skopaq.execution.order_router.resolve_security_id",
               new=AsyncMock(return_value="11536")):
        await daemon._phase_close()

    signal, execution = record.await_args.args
    assert live.place_order.await_args.args[0].order_type == OrderType.MARKET
    assert execution.fill_price == 3790.0      # the broker's fill, not the LTP
    assert safety._day_pnl == pytest.approx(-630.0)
