"""Tests for MemoryStore — decision-log persistence via Supabase.

Uses upstream's real ``TradingMemoryLog`` on a temp file and a mocked
``MemoryRepository`` — no real DB calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from skopaq.db.models import AgentMemoryRecord
from skopaq.memory.store import (
    DECISION_LOG_ROLE,
    MemoryStore,
    merge_entries,
)
from tradingagents.decision_log import TradingMemoryLog

# ── Helpers ─────────────────────────────────────────────────────────────────


class FakeGraph:
    """Stub for upstream TradingAgentsGraph: only ``memory_log`` is used."""

    def __init__(self, path) -> None:
        self.memory_log = TradingMemoryLog({"memory_log_path": str(path)})


def _pending(date: str, ticker: str, rating: str = "Buy") -> str:
    return f"[{date} | {ticker} | {rating} | pending]\n\nDECISION:\n**Rating**: {rating}"


def _settled(date: str, ticker: str, rating: str = "Buy") -> str:
    return (
        f"[{date} | {ticker} | {rating} | +2.0% | +1.0% | 5d | resolved:2026-09-20]\n\n"
        f"DECISION:\n**Rating**: {rating}\n\nREFLECTION:\nThe call held up."
    )


def _store(record: AgentMemoryRecord | None = None, max_entries: int = 50) -> MemoryStore:
    store = MemoryStore(MagicMock(), max_entries=max_entries)
    store._repo = MagicMock()
    store._repo.get_by_role.return_value = record
    return store


# ── merge_entries ───────────────────────────────────────────────────────────


class TestMergeEntries:
    def test_union_sorted_by_date(self):
        merged = merge_entries(
            [_pending("2026-09-10", "TCS.NS")], [_pending("2026-09-01", "INFY.NS")]
        )
        assert [e.split("|")[1].strip() for e in merged] == ["INFY.NS", "TCS.NS"]

    def test_settled_copy_replaces_pending(self):
        merged = merge_entries(
            [_pending("2026-09-10", "TCS.NS")], [_settled("2026-09-10", "TCS.NS")]
        )
        assert len(merged) == 1
        assert "REFLECTION" in merged[0]

    def test_pending_does_not_replace_settled(self):
        merged = merge_entries(
            [_settled("2026-09-10", "TCS.NS")], [_pending("2026-09-10", "TCS.NS")]
        )
        assert "REFLECTION" in merged[0]

    def test_untagged_entries_dropped(self):
        assert merge_entries(["random text"]) == []


# ── load ────────────────────────────────────────────────────────────────────


class TestLoad:
    def test_empty_db_leaves_log_untouched(self, tmp_path):
        graph = FakeGraph(tmp_path / "log.md")
        assert _store(None).load(graph) == 0
        assert not (tmp_path / "log.md").exists()

    def test_restores_entries_readable_by_upstream(self, tmp_path):
        graph = FakeGraph(tmp_path / "log.md")
        record = AgentMemoryRecord(
            role=DECISION_LOG_ROLE,
            documents=[_settled("2026-09-10", "TCS.NS"), _pending("2026-09-20", "TCS.NS")],
        )
        assert _store(record).load(graph) == 2

        entries = graph.memory_log.load_entries()
        assert [e["pending"] for e in entries] == [False, True]
        assert "The call held up." in graph.memory_log.get_past_context("TCS.NS")

    def test_keeps_local_only_entries(self, tmp_path):
        graph = FakeGraph(tmp_path / "log.md")
        graph.memory_log.store_decision("INFY.NS", "2026-09-01", "**Rating**: Sell")
        record = AgentMemoryRecord(
            role=DECISION_LOG_ROLE, documents=[_pending("2026-09-10", "TCS.NS")]
        )

        assert _store(record).load(graph) == 2
        assert {e["ticker"] for e in graph.memory_log.load_entries()} == {"INFY.NS", "TCS.NS"}

    def test_survives_supabase_error(self, tmp_path):
        store = _store()
        store._repo.get_by_role.side_effect = RuntimeError("connection refused")
        assert store.load(FakeGraph(tmp_path / "log.md")) == 0

    def test_graph_without_memory_log(self):
        assert _store().load(object()) == 0


# ── save ────────────────────────────────────────────────────────────────────


class TestSave:
    def test_uploads_log_entries(self, tmp_path):
        graph = FakeGraph(tmp_path / "log.md")
        graph.memory_log.store_decision("TCS.NS", "2026-09-24", "**Rating**: Overweight")
        store = _store()

        assert store.save(graph) == 1
        record = store._repo.upsert.call_args.args[0]
        assert record.role == DECISION_LOG_ROLE
        assert record.documents[0].startswith("[2026-09-24 | TCS.NS | Overweight | pending]")

    def test_missing_log_saves_nothing(self, tmp_path):
        store = _store()
        assert store.save(FakeGraph(tmp_path / "log.md")) == 0
        store._repo.upsert.assert_not_called()

    def test_applies_fifo_cap(self, tmp_path):
        graph = FakeGraph(tmp_path / "log.md")
        for day in range(1, 6):
            graph.memory_log.store_decision("TCS.NS", f"2026-09-0{day}", "**Rating**: Hold")
        store = _store(max_entries=2)

        assert store.save(graph) == 2
        saved = store._repo.upsert.call_args.args[0].documents
        assert [e[1:11] for e in saved] == ["2026-09-04", "2026-09-05"]

    def test_survives_upsert_error(self, tmp_path):
        graph = FakeGraph(tmp_path / "log.md")
        graph.memory_log.store_decision("TCS.NS", "2026-09-24", "**Rating**: Buy")
        store = _store()
        store._repo.upsert.side_effect = RuntimeError("db down")
        assert store.save(graph) == 0


class TestRoundtrip:
    def test_save_then_load_on_fresh_machine(self, tmp_path):
        first = FakeGraph(tmp_path / "a" / "log.md")
        first.memory_log.store_decision("TCS.NS", "2026-09-24", "**Rating**: Buy")
        store = _store()
        store.save(first)
        store._repo.get_by_role.return_value = store._repo.upsert.call_args.args[0]

        second = FakeGraph(tmp_path / "b" / "log.md")
        assert store.load(second) == 1
        assert second.memory_log.get_pending_entries()[0]["ticker"] == "TCS.NS"


# ── recall ──────────────────────────────────────────────────────────────────


class TestRecall:
    def test_searches_decision_log_and_legacy_roles(self):
        store = _store()
        store._repo.get_all_roles.return_value = [
            AgentMemoryRecord(
                role=DECISION_LOG_ROLE,
                documents=[_settled("2026-09-10", "TCS.NS"), _pending("2026-09-20", "HDFC.NS")],
            ),
            AgentMemoryRecord(
                role="bull_memory",
                documents=["IT sector rally on weak rupee", "banking NPA concerns"],
                recommendations=["Lean long on IT exporters", "Avoid PSU banks"],
            ),
        ]
        memories = store.recall("rupee weakness helps IT exporters like TCS", n_matches=1)

        assert memories["bull_memory"][0]["recommendation"] == "Lean long on IT exporters"
        assert "TCS.NS" in memories[DECISION_LOG_ROLE][0]["recommendation"]

    def test_skips_legacy_rows_with_mismatched_lengths(self):
        store = _store()
        store._repo.get_all_roles.return_value = [
            AgentMemoryRecord(role="bear_memory", documents=["a", "b"], recommendations=["x"]),
        ]
        assert store.recall("anything") == {}

