"""Persistent agent memory backed by Supabase.

Upstream TradingAgents (v0.2.4+) keeps its memory as an append-only
markdown decision log (``TradingMemoryLog``): every run logs its decision
as *pending*, later runs settle it with the realised return and a
reflection, and the Portfolio Manager reads recent entries as past
context. The log is a local file, so on ephemeral deploys (Railway, Fly)
it would be lost between sessions.

This store mirrors that file to the Supabase ``agent_memories`` table as
one row (role ``decision_log``, one JSONB ``documents`` element per log
entry): ``load()`` restores it into the graph's log file before a run,
``save()`` uploads it afterwards.

The per-agent BM25 memories of upstream v0.2.0 (``bull_memory`` etc.)
are retired upstream. Their rows stay searchable through
:meth:`MemoryStore.recall` until removed with ``skopaq memory legacy
--delete``, which exports them first.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skopaq.db.models import AgentMemoryRecord
from skopaq.db.repositories import MemoryRepository

if TYPE_CHECKING:
    from supabase import Client

logger = logging.getLogger(__name__)

# Row holding the mirrored decision log.
DECISION_LOG_ROLE = "decision_log"

# Per-agent memory rows written before the v0.5.1 upstream sync (read-only now).
LEGACY_MEMORY_ROLES = (
    "bull_memory",
    "bear_memory",
    "trader_memory",
    "invest_judge_memory",
    "risk_manager_memory",
)

# Entry separator used by upstream's TradingMemoryLog.
_SEPARATOR = "\n\n<!-- ENTRY_END -->\n\n"

# "[2026-09-24 | RELIANCE.NS | Buy | pending]" → date, ticker
_TAG_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2}) \| ([^|]+?) \|")


def _entry_key(entry: str) -> tuple[str, str] | None:
    match = _TAG_RE.match(entry)
    return (match.group(1), match.group(2).strip()) if match else None


def _is_pending(entry: str) -> bool:
    return entry.splitlines()[0].rstrip().endswith("| pending]")


def _split_entries(text: str) -> list[str]:
    return [e.strip() for e in text.split(_SEPARATOR) if e.strip()]


def merge_entries(*sources: list[str]) -> list[str]:
    """Union of decision-log entries, one per (date, ticker), oldest first.

    When the same decision appears twice, the settled copy wins over the
    pending one; otherwise the first source wins. Entries without a
    recognisable tag are dropped.
    """
    merged: dict[tuple[str, str], str] = {}
    for entries in sources:
        for entry in entries:
            key = _entry_key(entry)
            if key is None:
                continue
            current = merged.get(key)
            if current is None or (_is_pending(current) and not _is_pending(entry)):
                merged[key] = entry
    return [merged[key] for key in sorted(merged, key=lambda k: k[0])]


def _log_path(graph: Any) -> Path | None:
    memory_log = getattr(graph, "memory_log", None)
    return getattr(memory_log, "_log_path", None)


class MemoryStore:
    """Load and save upstream's decision log to Supabase.

    Args:
        client: Authenticated Supabase client (service-role key).
        max_entries: FIFO cap on settled entries; pending ones are always kept.
    """

    def __init__(self, client: Client, max_entries: int = 50) -> None:
        self._repo = MemoryRepository(client)
        self._max_entries = max_entries

    def load(self, graph: Any) -> int:
        """Restore the decision log from Supabase into the graph's log file.

        Entries already in the local file are kept (merged by date and
        ticker), so a long-lived machine does not lose its own history.

        Args:
            graph: An upstream ``TradingAgentsGraph`` instance.

        Returns:
            Number of entries in the log after loading.
        """
        path = _log_path(graph)
        if path is None:
            return 0

        try:
            record = self._repo.get_by_role(DECISION_LOG_ROLE)
        except Exception:
            logger.warning(
                "Failed to load decision log from Supabase — starting fresh", exc_info=True
            )
            return 0
        if record is None or not record.documents:
            return 0

        local = _split_entries(path.read_text(encoding="utf-8")) if path.exists() else []
        entries = merge_entries(record.documents, local)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_SEPARATOR.join(entries) + _SEPARATOR, encoding="utf-8")
        logger.info(
            "Decision log restored: %d entries (%d from Supabase)",
            len(entries), len(record.documents),
        )
        return len(entries)

    def save(self, graph: Any) -> int:
        """Merge the graph's decision log into the Supabase copy.

        The stored row is read first and merged with the local log, so a
        failed load or another process's newer entries are never
        overwritten by this process's partial view. If the row cannot be
        read, nothing is written.

        Args:
            graph: An upstream ``TradingAgentsGraph`` instance.

        Returns:
            Number of entries saved.
        """
        path = _log_path(graph)
        if path is None or not path.exists():
            return 0
        local = _split_entries(path.read_text(encoding="utf-8"))
        if not local:
            return 0

        try:
            record = self._repo.get_by_role(DECISION_LOG_ROLE)
        except Exception:
            logger.warning(
                "Could not read the stored decision log — not saving over it", exc_info=True
            )
            return 0
        remote = record.documents if record is not None else []
        entries = self._cap(merge_entries(remote, local))

        try:
            self._repo.upsert(AgentMemoryRecord(role=DECISION_LOG_ROLE, documents=entries))
        except Exception:
            logger.warning("Failed to save decision log to Supabase", exc_info=True)
            return 0
        logger.info("Decision log saved: %d entries", len(entries))
        return len(entries)

    def _cap(self, entries: list[str]) -> list[str]:
        """Every pending entry plus the ``max_entries`` most recent settled ones.

        Pending decisions are never pruned (as in upstream's own rotation):
        dropping one would lose it before its outcome is known.
        """
        settled = [i for i, e in enumerate(entries) if not _is_pending(e)]
        dropped = set(settled[: max(0, len(settled) - self._max_entries)])
        return [e for i, e in enumerate(entries) if i not in dropped]

    def legacy_records(self) -> list[AgentMemoryRecord]:
        """The stored per-agent memory rows from before the v0.5.1 sync."""
        return [r for r in self._repo.get_all_roles() if r.role in LEGACY_MEMORY_ROLES]

    def delete_legacy(self, records: list[AgentMemoryRecord]) -> int:
        """Delete exactly these legacy rows (by id); returns the number deleted.

        Pass the records from :meth:`legacy_records` that were exported, so
        what is deleted is what was backed up.

        Raises:
            ValueError: if a record is not a legacy role (the decision log is
                never deleted here) or has no id.
        """
        other = sorted({r.role for r in records} - set(LEGACY_MEMORY_ROLES))
        if other:
            raise ValueError(f"Not legacy memory roles: {', '.join(other)}")
        if any(r.id is None for r in records):
            raise ValueError("Cannot delete memory rows without an id")
        return self._repo.delete_by_ids([r.id for r in records])

    def recall(self, situation: str, n_matches: int = 2) -> dict[str, list[dict[str, Any]]]:
        """BM25-rank stored lessons against *situation*.

        Searches the decision log and the legacy per-agent memories.

        Returns:
            ``{role: [{"recommendation": str, "score": float}, ...]}``, best
            match first, for every role that has stored entries.
        """
        from rank_bm25 import BM25Okapi

        records = {r.role: r for r in self._repo.get_all_roles()}
        corpora: dict[str, tuple[list[str], list[str]]] = {}

        log = records.get(DECISION_LOG_ROLE)
        if log is not None and log.documents:
            corpora[DECISION_LOG_ROLE] = (log.documents, log.documents)
        for role in LEGACY_MEMORY_ROLES:
            record = records.get(role)
            if record is None or not record.documents:
                continue
            if len(record.documents) == len(record.recommendations):
                corpora[role] = (record.documents, record.recommendations)

        query = _tokenize(situation)
        results: dict[str, list[dict[str, Any]]] = {}
        for role, (documents, answers) in corpora.items():
            scores = BM25Okapi([_tokenize(d) for d in documents]).get_scores(query)
            ranked = sorted(range(len(documents)), key=lambda i: scores[i], reverse=True)
            results[role] = [
                {"recommendation": answers[i], "score": float(scores[i])}
                for i in ranked[:n_matches]
            ]
        return results


def _tokenize(text: str) -> list[str]:
    return re.findall(r"\b\w+\b", text.lower()) or [""]
