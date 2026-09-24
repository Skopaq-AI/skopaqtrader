"""Data access layer for Supabase tables.

Each repository class provides typed CRUD operations for a single table.
All methods accept and return Pydantic models — the rest of the codebase
never touches raw dicts from Supabase.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID

from supabase import Client

from skopaq.db.models import (
    AgentMemoryRecord,
    DailySnapshotRecord,
    HealingEventRecord,
    ModelPredictionRecord,
    StrategyVersionRecord,
    TradeRecord,
)

logger = logging.getLogger(__name__)


def _clean_for_insert(record: dict[str, Any]) -> dict[str, Any]:
    """Remove None values and auto-generated fields before insert.

    Converts non-JSON-serializable types:
    - UUID, date, datetime → str
    - Decimal → float  (Supabase NUMERIC columns accept floats)
    """
    from decimal import Decimal

    skip = {"id", "created_at", "updated_at"}
    result = {}
    for k, v in record.items():
        if k in skip or v is None:
            continue
        if isinstance(v, (UUID, date, datetime)):
            result[k] = str(v)
        elif isinstance(v, Decimal):
            f = float(v)
            result[k] = int(f) if f == int(f) else f
        else:
            result[k] = v
    return result


# ── Trades ───────────────────────────────────────────────────────────────────


class TradeRepository:
    """CRUD operations for the ``trades`` table."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self._table = "trades"

    def insert(self, trade: TradeRecord) -> TradeRecord:
        """Insert a new trade and return the created record."""
        data = _clean_for_insert(trade.model_dump())
        result = (
            self._client.table(self._table)
            .insert(data)
            .execute()
        )
        row = result.data[0] if result.data else {}
        return TradeRecord(**row)

    def update(self, trade_id: UUID, updates: dict[str, Any]) -> Optional[TradeRecord]:
        """Update a trade by ID."""
        result = (
            self._client.table(self._table)
            .update(updates)
            .eq("id", str(trade_id))
            .execute()
        )
        if result.data:
            return TradeRecord(**result.data[0])
        return None

    def get_by_id(self, trade_id: UUID) -> Optional[TradeRecord]:
        """Fetch a single trade by ID."""
        result = (
            self._client.table(self._table)
            .select("*")
            .eq("id", str(trade_id))
            .limit(1)
            .execute()
        )
        if result.data:
            return TradeRecord(**result.data[0])
        return None

    def get_recent(self, limit: int = 50, is_paper: Optional[bool] = None) -> list[TradeRecord]:
        """Fetch recent trades, optionally filtered by paper/live."""
        query = (
            self._client.table(self._table)
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if is_paper is not None:
            query = query.eq("is_paper", is_paper)
        result = query.execute()
        return [TradeRecord(**row) for row in (result.data or [])]

    def get_by_symbol(self, symbol: str, limit: int = 20) -> list[TradeRecord]:
        """Fetch recent trades for a specific symbol."""
        result = (
            self._client.table(self._table)
            .select("*")
            .eq("symbol", symbol)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [TradeRecord(**row) for row in (result.data or [])]

    def get_today(self) -> list[TradeRecord]:
        """Fetch all trades from today."""
        today = datetime.now(timezone.utc).date().isoformat()
        result = (
            self._client.table(self._table)
            .select("*")
            .gte("created_at", f"{today}T00:00:00Z")
            .order("created_at", desc=True)
            .execute()
        )
        return [TradeRecord(**row) for row in (result.data or [])]

    def find_open_buy(self, symbol: str) -> Optional[TradeRecord]:
        """Find the most recent BUY trade for *symbol* that hasn't been closed.

        Used by ``TradeLifecycleManager`` to link a SELL trade back to its
        opening BUY for P&L calculation and reflection triggering.
        """
        result = (
            self._client.table(self._table)
            .select("*")
            .eq("symbol", symbol)
            .eq("side", "BUY")
            .is_("closed_at", "null")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return TradeRecord(**result.data[0])
        return None


# ── Strategy Versions ────────────────────────────────────────────────────────


class StrategyRepository:
    """CRUD for the ``strategy_versions`` table."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self._table = "strategy_versions"

    def insert(self, strategy: StrategyVersionRecord) -> StrategyVersionRecord:
        """Insert a new strategy version."""
        data = _clean_for_insert(strategy.model_dump())
        result = self._client.table(self._table).insert(data).execute()
        return StrategyVersionRecord(**result.data[0]) if result.data else strategy

    def get_active(self) -> Optional[StrategyVersionRecord]:
        """Get the currently active strategy version."""
        result = (
            self._client.table(self._table)
            .select("*")
            .eq("active", True)
            .limit(1)
            .execute()
        )
        if result.data:
            return StrategyVersionRecord(**result.data[0])
        return None

    def set_active(self, version: str) -> None:
        """Activate a strategy version (deactivates all others)."""
        # Deactivate all
        self._client.table(self._table).update({"active": False}).eq("active", True).execute()
        # Activate the target
        self._client.table(self._table).update({"active": True}).eq("version", version).execute()

    def get_history(self, limit: int = 20) -> list[StrategyVersionRecord]:
        """Get version history ordered by creation time."""
        result = (
            self._client.table(self._table)
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return [StrategyVersionRecord(**row) for row in (result.data or [])]


# ── Model Predictions ────────────────────────────────────────────────────────


class PredictionRepository:
    """CRUD for the ``model_predictions`` table."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self._table = "model_predictions"

    def insert(self, prediction: ModelPredictionRecord) -> ModelPredictionRecord:
        """Insert a new model prediction."""
        data = _clean_for_insert(prediction.model_dump())
        result = self._client.table(self._table).insert(data).execute()
        return ModelPredictionRecord(**result.data[0]) if result.data else prediction

    def get_by_trade(self, trade_id: UUID) -> list[ModelPredictionRecord]:
        """Get all predictions for a trade."""
        result = (
            self._client.table(self._table)
            .select("*")
            .eq("trade_id", str(trade_id))
            .execute()
        )
        return [ModelPredictionRecord(**row) for row in (result.data or [])]

    def get_model_accuracy(self, model_name: str, limit: int = 100) -> dict[str, Any]:
        """Calculate accuracy stats for a model over recent predictions."""
        result = (
            self._client.table(self._table)
            .select("correct")
            .eq("model_name", model_name)
            .not_.is_("correct", "null")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        rows = result.data or []
        total = len(rows)
        correct = sum(1 for r in rows if r.get("correct"))
        return {
            "model": model_name,
            "total": total,
            "correct": correct,
            "accuracy": round(correct / total, 4) if total > 0 else 0.0,
        }


# ── Healing Events ───────────────────────────────────────────────────────────


class HealingRepository:
    """CRUD for the ``healing_events`` table."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self._table = "healing_events"

    def insert(self, event: HealingEventRecord) -> HealingEventRecord:
        """Log a healing event."""
        data = _clean_for_insert(event.model_dump())
        result = self._client.table(self._table).insert(data).execute()
        return HealingEventRecord(**result.data[0]) if result.data else event

    def get_recent(self, limit: int = 50, component: Optional[str] = None) -> list[HealingEventRecord]:
        """Get recent healing events."""
        query = (
            self._client.table(self._table)
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if component:
            query = query.eq("component", component)
        result = query.execute()
        return [HealingEventRecord(**row) for row in (result.data or [])]

    def get_unresolved(self) -> list[HealingEventRecord]:
        """Get all unresolved healing events."""
        result = (
            self._client.table(self._table)
            .select("*")
            .eq("resolved", False)
            .order("created_at", desc=True)
            .execute()
        )
        return [HealingEventRecord(**row) for row in (result.data or [])]


# ── Daily Snapshots ──────────────────────────────────────────────────────────


class SnapshotRepository:
    """CRUD for the ``daily_snapshots`` table."""

    def __init__(self, client: Client) -> None:
        self._client = client
        self._table = "daily_snapshots"

    def upsert(self, snapshot: DailySnapshotRecord) -> DailySnapshotRecord:
        """Insert or update a daily snapshot (unique on user_id + date)."""
        data = _clean_for_insert(snapshot.model_dump())
        result = (
            self._client.table(self._table)
            .upsert(data, on_conflict="user_id,date")
            .execute()
        )
        return DailySnapshotRecord(**result.data[0]) if result.data else snapshot

    def get_range(
        self, start_date: date, end_date: date,
    ) -> list[DailySnapshotRecord]:
        """Get snapshots for a date range."""
        result = (
            self._client.table(self._table)
            .select("*")
            .gte("date", start_date.isoformat())
            .lte("date", end_date.isoformat())
            .order("date", desc=False)
            .execute()
        )
        return [DailySnapshotRecord(**row) for row in (result.data or [])]

    def get_latest(self) -> Optional[DailySnapshotRecord]:
        """Get the most recent daily snapshot."""
        result = (
            self._client.table(self._table)
            .select("*")
            .order("date", desc=True)
            .limit(1)
            .execute()
        )
        if result.data:
            return DailySnapshotRecord(**result.data[0])
        return None


# ── Agent Memories ──────────────────────────────────────────────────────────


class MemoryRepository:
    """CRUD for the ``agent_memories`` table.

    Each row stores one agent role's full memory (documents + recommendations)
    as JSONB arrays.  Uses ``UPSERT`` on ``(user_id, role)`` so every save
    atomically overwrites the previous snapshot.
    """

    def __init__(self, client: Client) -> None:
        self._client = client
        self._table = "agent_memories"

    def upsert(self, record: AgentMemoryRecord) -> AgentMemoryRecord:
        """Insert or update a memory record for a role.

        Uses the ``UNIQUE(user_id, role)`` constraint for atomic upsert.
        """
        data = _clean_for_insert(record.model_dump())
        result = (
            self._client.table(self._table)
            .upsert(data, on_conflict="user_id,role")
            .execute()
        )
        return AgentMemoryRecord(**result.data[0]) if result.data else record

    def get_all_roles(self) -> list[AgentMemoryRecord]:
        """Fetch all memory records for the current user."""
        result = (
            self._client.table(self._table)
            .select("*")
            .execute()
        )
        return [AgentMemoryRecord(**row) for row in (result.data or [])]

    def get_by_role(self, role: str) -> Optional[AgentMemoryRecord]:
        """Fetch a single role's memory."""
        result = (
            self._client.table(self._table)
            .select("*")
            .eq("role", role)
            .limit(1)
            .execute()
        )
        if result.data:
            return AgentMemoryRecord(**result.data[0])
        return None

    def delete_by_ids(self, ids: list[UUID]) -> int:
        """Delete exactly these memory rows; returns the number deleted.

        By id rather than role: the service-role key bypasses row-level
        security, so a delete by role would reach every user's rows.
        """
        if not ids:
            return 0
        result = (
            self._client.table(self._table)
            .delete()
            .in_("id", [str(i) for i in ids])
            .execute()
        )
        return len(result.data or [])
