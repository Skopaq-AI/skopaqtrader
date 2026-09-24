"""MemoryRepository against a mocked Supabase client."""

from __future__ import annotations

from unittest.mock import MagicMock

from skopaq.db.repositories import MemoryRepository


def test_delete_by_role_filters_on_role_and_counts_rows():
    client = MagicMock()
    query = client.table.return_value.delete.return_value.eq.return_value
    query.execute.return_value.data = [{"role": "bull_memory"}]

    assert MemoryRepository(client).delete_by_role("bull_memory") == 1
    client.table.assert_called_once_with("agent_memories")
    client.table.return_value.delete.return_value.eq.assert_called_once_with("role", "bull_memory")


def test_delete_by_role_with_no_match_returns_zero():
    client = MagicMock()
    client.table.return_value.delete.return_value.eq.return_value.execute.return_value.data = []
    assert MemoryRepository(client).delete_by_role("bear_memory") == 0
