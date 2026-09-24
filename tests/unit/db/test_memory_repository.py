"""MemoryRepository against a mocked Supabase client."""

from __future__ import annotations

from unittest.mock import MagicMock
from uuid import uuid4

from skopaq.db.repositories import MemoryRepository


def test_delete_by_ids_deletes_exactly_those_rows():
    client = MagicMock()
    query = client.table.return_value.delete.return_value.in_.return_value
    query.execute.return_value.data = [{"id": "a"}, {"id": "b"}]
    ids = [uuid4(), uuid4()]

    assert MemoryRepository(client).delete_by_ids(ids) == 2
    client.table.assert_called_once_with("agent_memories")
    client.table.return_value.delete.return_value.in_.assert_called_once_with(
        "id", [str(i) for i in ids])


def test_delete_by_ids_with_nothing_to_delete_sends_no_request():
    client = MagicMock()
    assert MemoryRepository(client).delete_by_ids([]) == 0
    client.table.assert_not_called()
