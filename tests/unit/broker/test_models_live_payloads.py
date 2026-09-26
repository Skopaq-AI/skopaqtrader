"""Broker models against the payloads INDstocks actually sends (holdings with
total_qty/avg_price, positions with null day_* values) and the ExecutionResult
fields and readers used for live fills."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from skopaq.broker.models import (
    ExecutionResult,
    Holding,
    Position,
    filled_quantity_of,
    is_remaining_open,
    is_unconfirmed,
    order_ids_of,
    outcome_of,
)

# The documented GET /portfolio/holdings row, verbatim.
DOC_HOLDING = {
    "security_id": "18520",
    "symbol": "CUPID",
    "isin": "INE509F01029",
    "total_qty": 1,
    "used_qty": 0,
    "avg_price": 217.3,
    "t1_qty": 1,
    "t1_avg_price": 217.3,
    "dp_qty": 0,
    "dp_avg_price": 0,
}

# The documented equity GET /portfolio/positions row, with the nullable day_* values null.
DOC_POSITION = {
    "position_id": "86016462",
    "security_id": "1521",
    "symbol": "INDIAGLYCO",
    "segment": "EQUITY",
    "product": "CNC",
    "exchange": "NSE",
    "isin": "INE560A01023",
    "drv_instrument": "",
    "net_qty": 0,
    "avg_price": 1146.85,
    "buy_qty": 1,
    "buy_avg": 1149.4,
    "sell_qty": 1,
    "sell_avg": 1146.85,
    "realized_profit": -2.55,
    "day_buy_qty": None,
    "day_buy_val": None,
    "day_sell_qty": None,
    "day_sell_val": None,
    "cf_buy_qty": None,
    "cf_buy_val": None,
    "cf_sell_qty": None,
    "cf_sell_val": None,
}


# ── M1: holdings ────────────────────────────────────────────────────────────


def test_holding_from_the_documented_row():
    h = Holding(**DOC_HOLDING)
    assert h.symbol == "CUPID"
    assert h.security_id == "18520"
    assert h.quantity == Decimal("1")
    assert h.average_price == 217.3
    assert h.used_quantity == Decimal("0")          # kept, not subtracted
    assert h.model_extra["t1_qty"] == 1


def test_holding_used_qty_kept():
    h = Holding(**{**DOC_HOLDING, "total_qty": 10, "used_qty": 4})
    assert h.quantity == Decimal("10")
    assert h.used_quantity == Decimal("4")


def test_holding_older_shape():
    h = Holding(**{"trading_symbol": "TCS", "quantity": 5, "average_price": 3800.0})
    assert (h.symbol, h.quantity, h.average_price) == ("TCS", Decimal("5"), 3800.0)


def test_holding_by_field_name():
    """PaperEngine and KiteClient build holdings by field name."""
    h = Holding(symbol="RELIANCE", exchange="NSE", quantity=Decimal("3"), average_price=1400.0,
                last_price=1410.0, pnl=30.0)
    assert (h.symbol, h.quantity, h.average_price) == ("RELIANCE", Decimal("3"), 1400.0)
    assert h.pnl == 30.0


def test_holding_null_numbers_are_zero():
    h = Holding(**{"symbol": "X", "total_qty": None, "avg_price": "", "used_qty": "null",
                   "security_id": 2885})
    assert (h.quantity, h.average_price, h.used_quantity) == (Decimal("0"), 0.0, Decimal("0"))
    assert h.security_id == "2885"


# ── Positions ───────────────────────────────────────────────────────────────


def test_position_with_null_day_values():
    p = Position(**DOC_POSITION)
    assert p.symbol == "INDIAGLYCO"
    assert p.quantity == Decimal("0")
    assert p.average_price == 1146.85
    assert p.sell_quantity == Decimal("1")
    assert (p.buy_value, p.sell_value) == (0.0, 0.0)
    assert p.day_sell_quantity == Decimal("0")
    assert p.pnl == -2.55


def test_position_day_sell_quantity():
    p = Position(**{**DOC_POSITION, "day_sell_qty": 3, "day_sell_val": 3440.55})
    assert p.day_sell_quantity == Decimal("3")
    assert p.sell_value == 3440.55


@pytest.mark.parametrize("blank", [None, "", "null", "NULL"])
def test_position_blank_numbers_are_zero(blank):
    p = Position(**{**DOC_POSITION, "net_qty": blank, "avg_price": blank, "sell_qty": blank})
    assert (p.quantity, p.average_price, p.sell_quantity) == (Decimal("0"), 0.0, Decimal("0"))


def test_position_by_field_name():
    """PaperEngine builds positions by field name."""
    p = Position(symbol="RELIANCE", exchange="NSE", product="CNC", quantity=Decimal("10"),
                 average_price=1400.0, pnl=5.0, buy_value=14000.0, sell_value=0.0)
    assert (p.quantity, p.average_price, p.pnl, p.buy_value) == (Decimal("10"), 1400.0, 5.0,
                                                                 14000.0)


def test_position_wrapper_row_names():
    """Older docs wrap rows in net_positions with net_quantity / trading_symbol."""
    p = Position(**{"trading_symbol": "TCS", "net_quantity": 4, "average_price": 3800.0})
    assert (p.symbol, p.quantity, p.average_price) == ("TCS", Decimal("4"), 3800.0)


def test_position_product_can_be_overwritten():
    p = Position(**DOC_POSITION)
    p.product = "INTRADAY"
    assert p.product == "INTRADAY"


# ── ExecutionResult and its readers ─────────────────────────────────────────


def test_execution_result_defaults_unchanged_for_paper():
    r = ExecutionResult(success=True)
    assert r.filled_quantity is None
    assert r.requested_quantity is None
    assert r.outcome == ""
    assert r.order_ids == []
    assert r.remaining_open is False
    assert r.fill_unconfirmed is False
    assert r.fill_price_source == ""
    assert r.broker_message == ""
    assert r.brokerage == 5.0


def test_readers_on_a_live_result():
    r = ExecutionResult(success=True, mode="live", filled_quantity=Decimal("3"),
                        requested_quantity=Decimal("5"), outcome="partial",
                        order_ids=["EQ-1", "EQ-2"], remaining_open=True)
    assert filled_quantity_of(r, 5) == Decimal("3")
    assert outcome_of(r) == "partial"
    assert order_ids_of(r) == ["EQ-1", "EQ-2"]
    assert is_remaining_open(r) is True
    assert is_unconfirmed(r) is True
    assert is_unconfirmed(ExecutionResult(success=False, fill_unconfirmed=True)) is True


def test_readers_on_a_paper_result():
    r = ExecutionResult(success=True)
    assert filled_quantity_of(r, Decimal("5")) == Decimal("5")
    assert filled_quantity_of(r, 7) == Decimal("7")
    assert isinstance(filled_quantity_of(r, 7), Decimal)
    assert outcome_of(r) == ""
    assert order_ids_of(r) == []
    assert is_remaining_open(r) is False
    assert is_unconfirmed(r) is False


def test_readers_on_a_mock_result():
    """Tests pass MagicMock results whose attributes are truthy mocks."""
    m = MagicMock()
    assert filled_quantity_of(m, 4) == Decimal("4")
    assert outcome_of(m) == ""
    assert order_ids_of(m) == []
    assert is_remaining_open(m) is False
    assert is_unconfirmed(m) is False
    m.filled_quantity = 2
    m.order_ids = ["EQ-9", MagicMock()]
    assert filled_quantity_of(m, 4) == Decimal("2")
    assert order_ids_of(m) == []
    m.filled_quantity = True                        # a bool is not a quantity
    assert filled_quantity_of(m, 4) == Decimal("4")
    assert filled_quantity_of(None, 1) == Decimal("1")


def test_null_exchange_and_isin_do_not_break_a_row():
    from skopaq.broker.models import Holding, Position

    position = Position(**{"symbol": "SENSEX", "security_id": 823580, "exchange": None,
                           "isin": None, "net_qty": 0})
    holding = Holding(**{"symbol": "CUPID", "security_id": "18520", "isin": "INE509F01029",
                         "total_qty": 1})
    assert (position.exchange, position.isin, position.security_id) == ("", "", "823580")
    assert holding.isin == "INE509F01029"
