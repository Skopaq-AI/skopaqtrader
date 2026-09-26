"""TradeRepository against a mocked Supabase client."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from skopaq.db.repositories import TradeRepository


def test_find_by_order_id_queries_that_order_id():
    client = MagicMock()
    query = client.table.return_value.select.return_value.eq.return_value.limit.return_value
    trade_id = uuid4()
    query.execute.return_value.data = [
        {"id": str(trade_id), "symbol": "TCS", "side": "BUY", "quantity": 4, "order_id": "EQ-1"}]

    found = TradeRepository(client).find_by_order_id("EQ-1")

    client.table.assert_called_once_with("trades")
    client.table.return_value.select.return_value.eq.assert_called_once_with("order_id", "EQ-1")
    client.table.return_value.select.return_value.eq.return_value.limit.assert_called_once_with(1)
    assert (found.id, found.order_id) == (trade_id, "EQ-1")


def test_find_by_order_id_without_a_row_is_none():
    client = MagicMock()
    query = client.table.return_value.select.return_value.eq.return_value.limit.return_value
    query.execute.return_value.data = []
    assert TradeRepository(client).find_by_order_id("EQ-404") is None


def test_delete_removes_that_row():
    client = MagicMock()
    trade_id = uuid4()

    TradeRepository(client).delete(trade_id)

    client.table.assert_called_once_with("trades")
    client.table.return_value.delete.return_value.eq.assert_called_once_with("id", str(trade_id))
    client.table.return_value.delete.return_value.eq.return_value.execute.assert_called_once()
