"""Tests for the pre-trade safety checker.

Each safety rule gets its own test to ensure it fires at the right threshold.
"""

import pytest
from datetime import datetime, time, timezone, timedelta
from unittest.mock import patch

from skopaq.broker.models import (
    Exchange,
    Funds,
    OrderRequest,
    OrderType,
    Position,
    Product,
    Side,
    TradingSignal,
)
from skopaq.constants import SafetyRules
from skopaq.execution.safety_checker import SafetyChecker


@pytest.fixture
def rules():
    """Custom rules for testing (can override frozen defaults)."""
    return SafetyRules(
        max_position_pct=0.15,
        max_daily_loss_pct=0.03,
        max_weekly_loss_pct=0.07,
        max_monthly_loss_pct=0.12,
        max_open_positions=3,
        max_order_value_inr=100_000,
        max_orders_per_minute=5,
        require_stop_loss=True,
        min_stop_loss_pct=0.02,
        max_lots_per_position=100,  # High limit so other tests don't trip this
        no_naked_option_selling=True,
        market_hours_only=False,  # Disable for testing
        cool_down_after_loss_minutes=5,
    )


@pytest.fixture
def checker(rules):
    return SafetyChecker(rules=rules)


@pytest.fixture
def funds():
    return Funds(available_cash=500_000, available_margin=500_000, total_collateral=500_000)


@pytest.fixture
def signal_with_sl():
    return TradingSignal(
        symbol="RELIANCE", action="BUY", confidence=70,
        entry_price=2500, stop_loss=2450,
    )


@pytest.fixture
def signal_no_sl():
    return TradingSignal(
        symbol="RELIANCE", action="BUY", confidence=70,
        entry_price=2500,
    )


def _buy_order(symbol="RELIANCE", qty=10, price=2500.0):
    return OrderRequest(
        symbol=symbol, exchange=Exchange.NSE, side=Side.BUY,
        quantity=qty, order_type=OrderType.LIMIT, price=price,
        product=Product.CNC,
    )


def _held(symbol="RELIANCE", qty=10):
    """An open position, so a SELL of it is not a short sale."""
    return [Position(symbol=symbol, quantity=qty)]


def _sell_order(symbol="RELIANCE", qty=10, price=2500.0):
    return OrderRequest(
        symbol=symbol, exchange=Exchange.NSE, side=Side.SELL,
        quantity=qty, order_type=OrderType.LIMIT, price=price,
        product=Product.CNC,
    )


class TestPositionSize:
    def test_within_limit_passes(self, checker, funds, signal_with_sl):
        # 10 * 2500 = 25000 = 5% of 500,000 < 15%
        order = _buy_order(qty=10, price=2500)
        result = checker.validate(order, signal_with_sl, [], funds, 500_000)
        assert result.passed

    def test_exceeds_limit_rejected(self, checker, funds, signal_with_sl):
        # 40 * 2500 = 100,000 = 20% of 500,000 > 15%
        order = _buy_order(qty=40, price=2500)
        result = checker.validate(order, signal_with_sl, [], funds, 500_000)
        assert not result.passed
        assert any("Position size" in r for r in result.rejections)


class TestOrderValue:
    def test_within_cap_passes(self, checker, funds, signal_with_sl):
        # 10 * 2500 = 25,000 < 100,000
        order = _buy_order(qty=10, price=2500)
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert result.passed

    def test_exceeds_cap_rejected(self, checker, funds, signal_with_sl):
        # 50 * 2500 = 125,000 > 100,000
        order = _buy_order(qty=50, price=2500)
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert not result.passed
        assert any("Order value" in r for r in result.rejections)


class TestMaxPositions:
    def test_under_limit_passes(self, checker, funds, signal_with_sl):
        positions = [
            Position(symbol="TCS", exchange=Exchange.NSE, product=Product.CNC,
                     quantity=10, average_price=3000, last_price=3000, pnl=0, day_pnl=0),
            Position(symbol="INFY", exchange=Exchange.NSE, product=Product.CNC,
                     quantity=5, average_price=1500, last_price=1500, pnl=0, day_pnl=0),
        ]
        order = _buy_order("RELIANCE", qty=10, price=2500)
        result = checker.validate(order, signal_with_sl, positions, funds, 1_000_000)
        assert result.passed

    def test_at_limit_rejected(self, checker, funds, signal_with_sl):
        positions = [
            Position(symbol=s, exchange=Exchange.NSE, product=Product.CNC,
                     quantity=10, average_price=100, last_price=100, pnl=0, day_pnl=0)
            for s in ["TCS", "INFY", "HDFC"]
        ]
        order = _buy_order("RELIANCE", qty=1, price=100)  # New symbol
        result = checker.validate(order, signal_with_sl, positions, funds, 1_000_000)
        assert not result.passed
        assert any("open positions" in r for r in result.rejections)

    def test_adding_to_existing_position_passes(self, checker, funds, signal_with_sl):
        """Buying more of an existing position should NOT count as a new position."""
        positions = [
            Position(symbol=s, exchange=Exchange.NSE, product=Product.CNC,
                     quantity=10, average_price=100, last_price=100, pnl=0, day_pnl=0)
            for s in ["TCS", "INFY", "RELIANCE"]
        ]
        order = _buy_order("RELIANCE", qty=5, price=100)  # Same symbol
        result = checker.validate(order, signal_with_sl, positions, funds, 1_000_000)
        assert result.passed


class TestStopLoss:
    def test_buy_with_stop_loss_passes(self, checker, funds, signal_with_sl):
        order = _buy_order(qty=1, price=100)
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert result.passed

    def test_buy_without_stop_loss_rejected(self, checker, funds, signal_no_sl):
        order = _buy_order(qty=1, price=100)
        result = checker.validate(order, signal_no_sl, [], funds, 1_000_000)
        assert not result.passed
        assert any("stop-loss" in r.lower() for r in result.rejections)

    def test_sell_without_stop_loss_passes(self, checker, funds, signal_no_sl):
        """Sell orders don't need a stop loss."""
        order = _sell_order(qty=1, price=100)
        result = checker.validate(order, signal_no_sl, _held(), funds, 1_000_000)
        assert result.passed


class TestDailyLoss:
    def test_under_threshold_passes(self, checker, funds, signal_with_sl):
        # Record a small loss: 1% of 500k = 5000
        checker.record_pnl(-5000)
        # Clear the cool-down so we isolate the daily loss check
        checker._last_loss_time = None
        order = _buy_order(qty=1, price=100)
        result = checker.validate(order, signal_with_sl, [], funds, 500_000)
        assert result.passed  # 1% < 3%

    def test_exceeds_threshold_rejected(self, checker, funds, signal_with_sl):
        # Record a 4% loss: 20000 of 500k
        checker.record_pnl(-20_000)
        order = _buy_order(qty=1, price=100)
        result = checker.validate(order, signal_with_sl, [], funds, 500_000)
        assert not result.passed
        assert any("Daily loss" in r for r in result.rejections)


class TestOrderRate:
    def test_under_rate_passes(self, checker, funds, signal_with_sl):
        order = _buy_order(qty=1, price=100)
        for _ in range(4):
            result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
            assert result.passed

    def test_exceeds_rate_rejected(self, checker, funds, signal_with_sl):
        order = _buy_order(qty=1, price=100)
        for _ in range(5):
            checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert not result.passed
        assert any("Rate limit" in r for r in result.rejections)


class TestCoolDown:
    def test_no_loss_no_cooldown(self, checker, funds, signal_with_sl):
        order = _buy_order(qty=1, price=100)
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert result.passed

    def test_loss_triggers_cooldown(self, checker, funds, signal_with_sl):
        checker.record_pnl(-100)
        order = _buy_order(qty=1, price=100)
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert not result.passed
        assert any("Cool-down" in r for r in result.rejections)


class TestNakedOptions:
    def test_selling_option_rejected(self, checker, funds):
        order = OrderRequest(
            symbol="NIFTY23DEC21000CE", exchange=Exchange.NSE, side=Side.SELL,
            quantity=50, order_type=OrderType.LIMIT, price=200,
            product=Product.NRML,
        )
        result = checker.validate(order, None, [], funds, 1_000_000)
        assert not result.passed
        assert any("Naked option" in r for r in result.rejections)

    def test_buying_option_passes(self, checker, funds, signal_with_sl):
        order = OrderRequest(
            symbol="NIFTY23DEC21000CE", exchange=Exchange.NSE, side=Side.BUY,
            quantity=50, order_type=OrderType.LIMIT, price=200,
            product=Product.NRML,
        )
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert result.passed


class TestInsufficientFunds:
    def test_buy_within_margin_passes(self, checker, signal_with_sl):
        funds = Funds(available_margin=50_000)
        order = _buy_order(qty=10, price=2500)  # 25,000
        result = checker.validate(order, signal_with_sl, [], funds, 500_000)
        assert result.passed

    def test_buy_exceeds_margin_rejected(self, checker, signal_with_sl):
        funds = Funds(available_margin=10_000)
        order = _buy_order(qty=10, price=2500)  # 25,000 > 10,000
        result = checker.validate(order, signal_with_sl, [], funds, 500_000)
        assert not result.passed
        assert any("Insufficient margin" in r for r in result.rejections)


class TestMinStopLossPct:
    """Validate that stop-loss distance is at least min_stop_loss_pct from entry."""

    def test_adequate_stop_loss_passes(self, checker, funds, signal_with_sl):
        """Trigger price 3% below entry → exceeds 2% minimum → passes."""
        order = OrderRequest(
            symbol="RELIANCE", exchange=Exchange.NSE, side=Side.BUY,
            quantity=1, order_type=OrderType.LIMIT, price=2500.0,
            trigger_price=2425.0,  # 3% below entry
            product=Product.CNC,
        )
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert result.passed

    def test_tight_stop_loss_rejected(self, checker, funds, signal_with_sl):
        """Trigger price 0.5% below entry → under 2% minimum → rejected."""
        order = OrderRequest(
            symbol="RELIANCE", exchange=Exchange.NSE, side=Side.BUY,
            quantity=1, order_type=OrderType.LIMIT, price=2500.0,
            trigger_price=2487.5,  # 0.5% below
            product=Product.CNC,
        )
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert not result.passed
        assert any("Stop loss too tight" in r for r in result.rejections)

    def test_no_trigger_price_skipped(self, checker, funds, signal_with_sl):
        """No trigger price → check is skipped (handled by _check_stop_loss)."""
        order = _buy_order(qty=1, price=2500)
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        # Should pass — no trigger price to validate against min pct
        assert result.passed

    def test_sell_order_skipped(self, checker, funds, signal_with_sl):
        """Sell orders should not be checked for min stop-loss distance."""
        order = OrderRequest(
            symbol="RELIANCE", exchange=Exchange.NSE, side=Side.SELL,
            quantity=1, order_type=OrderType.LIMIT, price=2500.0,
            trigger_price=2490.0,  # Very tight — but it's a sell
            product=Product.CNC,
        )
        result = checker.validate(order, signal_with_sl, _held(), funds, 1_000_000)
        assert result.passed


class TestMaxLots:
    """Validate per-position quantity limit."""

    @pytest.fixture
    def strict_checker(self):
        """Checker with max_lots_per_position=5 for testing the lot limit."""
        rules = SafetyRules(
            max_position_pct=1.0, max_order_value_inr=10_000_000,
            require_stop_loss=False, market_hours_only=False,
            max_lots_per_position=5,
        )
        return SafetyChecker(rules=rules)

    def test_within_limit_passes(self, strict_checker, funds, signal_with_sl):
        """Quantity 5 == max_lots_per_position (5) → passes."""
        order = _buy_order(qty=5, price=100)
        result = strict_checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert result.passed

    def test_exceeds_limit_rejected(self, strict_checker, funds, signal_with_sl):
        """Quantity 6 > 5 → rejected."""
        order = _buy_order(qty=6, price=100)
        result = strict_checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert not result.passed
        assert any("Quantity 6 exceeds max 5" in r for r in result.rejections)

    def test_sell_is_bounded_by_what_is_held_not_the_lot_limit(
        self, strict_checker, funds, signal_with_sl,
    ):
        """An over-sell is refused by the no-short-sale check; a held position of
        any size can be sold (tests/unit/execution/test_protective_exits.py)."""
        over = strict_checker.validate(_sell_order(qty=10, price=100), signal_with_sl,
                                       _held(qty=6), funds, 1_000_000)
        whole = strict_checker.validate(_sell_order(qty=10, price=100), signal_with_sl,
                                        _held(qty=10), funds, 1_000_000)
        assert any("No short sales" in r for r in over.rejections)
        assert whole.passed, whole.rejections


class TestSectorConcentration:
    """Validate sector concentration limits in SafetyChecker integration."""

    def test_first_position_passes(self, funds, signal_with_sl):
        checker = SafetyChecker(
            rules=SafetyRules(
                max_position_pct=1.0, max_order_value_inr=10_000_000,
                require_stop_loss=False, market_hours_only=False,
                max_lots_per_position=10000,
            ),
            max_sector_concentration_pct=0.40,
        )
        order = _buy_order("HDFCBANK", qty=10, price=1500)  # 15000 of 1M = 1.5%
        result = checker.validate(order, signal_with_sl, [], funds, 1_000_000)
        assert result.passed

    def test_exceeds_sector_limit_rejected(self, funds, signal_with_sl):
        checker = SafetyChecker(
            rules=SafetyRules(
                max_position_pct=1.0, max_order_value_inr=10_000_000,
                require_stop_loss=False, market_hours_only=False,
                max_lots_per_position=10000,
            ),
            max_sector_concentration_pct=0.30,  # Tight 30% limit
        )
        positions = [
            Position(symbol="HDFCBANK", exchange=Exchange.NSE, product=Product.CNC,
                     quantity=200, average_price=1500, last_price=1500, pnl=0, day_pnl=0),
        ]
        # Existing banking: 200*1500 = 300,000 = 30%
        order = _buy_order("SBIN", qty=100, price=500)  # +50,000 = 35% > 30%
        result = checker.validate(order, signal_with_sl, positions, funds, 1_000_000)
        assert not result.passed
        assert any("Sector" in r and "BANKING" in r for r in result.rejections)


class TestMinimumConfidence:
    """Validate the confidence gate safety check."""

    def test_below_threshold_rejected(self):
        """Signal with 30% confidence < 40% minimum → rejected."""
        rules = SafetyRules(
            min_confidence_pct=40, market_hours_only=False,
            require_stop_loss=False, max_lots_per_position=10000,
            max_order_value_inr=10_000_000, max_position_pct=1.0,
        )
        checker = SafetyChecker(rules=rules)
        signal = TradingSignal(
            symbol="RELIANCE", action="BUY", confidence=30, entry_price=2500,
        )
        order = _buy_order(qty=1, price=100)
        funds = Funds(available_margin=500_000)
        result = checker.validate(order, signal, [], funds, 500_000)
        assert not result.passed
        assert any("Confidence 30%" in r for r in result.rejections)

    def test_above_threshold_passes(self):
        """Signal with 75% confidence > 40% minimum → passes."""
        rules = SafetyRules(
            min_confidence_pct=40, market_hours_only=False,
            require_stop_loss=False, max_lots_per_position=10000,
            max_order_value_inr=10_000_000, max_position_pct=1.0,
        )
        checker = SafetyChecker(rules=rules)
        signal = TradingSignal(
            symbol="RELIANCE", action="BUY", confidence=75, entry_price=2500,
        )
        order = _buy_order(qty=1, price=100)
        funds = Funds(available_margin=500_000)
        result = checker.validate(order, signal, [], funds, 500_000)
        assert result.passed

    def test_at_threshold_passes(self):
        """Signal exactly at 40% threshold → passes (not strictly below)."""
        rules = SafetyRules(
            min_confidence_pct=40, market_hours_only=False,
            require_stop_loss=False, max_lots_per_position=10000,
            max_order_value_inr=10_000_000, max_position_pct=1.0,
        )
        checker = SafetyChecker(rules=rules)
        signal = TradingSignal(
            symbol="RELIANCE", action="BUY", confidence=40, entry_price=2500,
        )
        order = _buy_order(qty=1, price=100)
        funds = Funds(available_margin=500_000)
        result = checker.validate(order, signal, [], funds, 500_000)
        assert result.passed

    def test_disabled_when_zero(self):
        """min_confidence_pct=0 disables the gate — even 10% passes."""
        rules = SafetyRules(
            min_confidence_pct=0, market_hours_only=False,
            require_stop_loss=False, max_lots_per_position=10000,
            max_order_value_inr=10_000_000, max_position_pct=1.0,
        )
        checker = SafetyChecker(rules=rules)
        signal = TradingSignal(
            symbol="RELIANCE", action="BUY", confidence=10, entry_price=2500,
        )
        order = _buy_order(qty=1, price=100)
        funds = Funds(available_margin=500_000)
        result = checker.validate(order, signal, [], funds, 500_000)
        assert result.passed

    def test_no_signal_skipped(self):
        """When signal is None, the confidence check is skipped."""
        rules = SafetyRules(
            min_confidence_pct=40, market_hours_only=False,
            require_stop_loss=False, max_lots_per_position=10000,
            max_order_value_inr=10_000_000, max_position_pct=1.0,
        )
        checker = SafetyChecker(rules=rules)
        order = _sell_order(qty=1, price=100)
        funds = Funds(available_margin=500_000)
        result = checker.validate(order, None, _held(), funds, 500_000)
        assert result.passed


class TestReset:
    def test_daily_reset_clears_pnl(self, checker, funds, signal_with_sl):
        checker.record_pnl(-20_000)
        checker.reset_daily()
        order = _buy_order(qty=1, price=100)
        result = checker.validate(order, signal_with_sl, [], funds, 500_000)
        assert result.passed  # Daily loss reset


class TestNoShortSale:
    """A SELL may only sell what is held (positions + delivery holdings)."""

    def _checker(self):
        return SafetyChecker(rules=SafetyRules(
            market_hours_only=False, require_stop_loss=False, max_lots_per_position=10000,
            max_order_value_inr=10_000_000, max_position_pct=1.0,
        ))

    def _validate(self, order, positions=(), holdings=None):
        funds = Funds(available_margin=1_000_000)
        return self._checker().validate(
            order, None, list(positions), funds, 1_000_000, holdings=holdings
        )

    def test_sell_without_position_rejected(self):
        result = self._validate(_sell_order(qty=1))
        assert not result.passed
        assert any("No short sales" in r for r in result.rejections)

    def test_sell_more_than_held_rejected(self):
        result = self._validate(_sell_order(qty=20), positions=_held(qty=10))
        assert any("only 10 held" in r for r in result.rejections)

    def test_sell_of_held_position_passes(self):
        assert self._validate(_sell_order(qty=10), positions=_held(qty=10)).passed

    def test_delivery_holding_counts(self):
        from skopaq.broker.models import Holding

        holdings = [Holding(symbol="RELIANCE", quantity=5)]
        assert self._validate(_sell_order(qty=5), holdings=holdings).passed

    def test_symbol_forms_match(self):
        positions = [Position(symbol="NSE:RELIANCE-EQ", quantity=3)]
        assert self._validate(_sell_order(symbol="reliance.ns", qty=3), positions=positions).passed

    def test_other_symbol_does_not_count(self):
        result = self._validate(_sell_order(symbol="TCS", qty=1), positions=_held("RELIANCE"))
        assert not result.passed

    def test_buy_is_unaffected(self):
        assert self._validate(_buy_order(qty=1, price=100)).passed

    def test_holding_sold_earlier_today_no_longer_counts(self):
        """Holdings still say 10 until settlement; today's net position is -10."""
        from skopaq.broker.models import Holding

        holdings = [Holding(symbol="RELIANCE", quantity=10)]
        result = self._validate(_sell_order(qty=10), positions=_held(qty=-10), holdings=holdings)
        assert any("only 0 held" in r for r in result.rejections)

    def test_holding_plus_todays_buy_is_sellable(self):
        from skopaq.broker.models import Holding

        holdings = [Holding(symbol="RELIANCE", quantity=10)]
        assert self._validate(_sell_order(qty=15), positions=_held(qty=5), holdings=holdings).passed
