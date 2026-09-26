"""An in-memory TradeRepository with Supabase's behaviour for what the lifecycle uses.

``memrepo`` (a fixture: import it into the test module) routes ``_run_lifecycle`` and
``_record_late_fill`` to it, so tests can check the trade rows the real recorders write.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from skopaq.db.models import TradeRecord

_tick = itertools.count()


class MemRepo:
    def __init__(self):
        self.rows: dict = {}

    def _now(self):
        return datetime(2026, 9, 25, 5, 30, tzinfo=timezone.utc) + timedelta(seconds=next(_tick))

    def insert(self, trade: TradeRecord) -> TradeRecord:
        if trade.order_id and any(r.order_id == trade.order_id for r in self.rows.values()):
            raise RuntimeError("duplicate key value violates unique constraint "
                               "trades_order_id_key")
        assert trade.quantity > 0 and trade.quantity == int(trade.quantity), trade.quantity
        row = trade.model_copy(update={"id": uuid4(), "created_at": self._now()})
        self.rows[row.id] = row
        return row

    def update(self, trade_id, updates):
        row = self.rows[trade_id]
        fields = {}
        for key, value in updates.items():
            if key in ("quantity", "pnl", "fill_price", "price"):
                value = Decimal(str(value))
            if key == "closed_at":
                value = datetime.fromisoformat(value)
            fields[key] = value
        self.rows[trade_id] = row.model_copy(update=fields)
        return self.rows[trade_id]

    def delete(self, trade_id):
        del self.rows[trade_id]

    def find_by_order_id(self, order_id):
        return next((r for r in self.rows.values() if r.order_id == order_id), None)

    def find_open_buy(self, symbol, is_paper=None):
        rows = [r for r in self.rows.values()
                if r.symbol == symbol and r.side == "BUY" and r.closed_at is None
                and (is_paper is None or r.is_paper == is_paper)]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[0] if rows else None

    def get_closed_since(self, since, is_paper):
        return [r for r in self.rows.values() if r.side == "BUY" and r.is_paper == is_paper
                and r.closed_at is not None]

    # ── For assertions ───────────────────────────────────────────────────

    def open_buy_qty(self, symbol, *, is_paper=None):
        total = Decimal(0)
        for r in self.rows.values():
            if (r.symbol == symbol and r.side == "BUY" and r.closed_at is None
                    and (is_paper is None or r.is_paper == is_paper)):
                pending = sum(Decimal(str(p["qty"])) for p in (r.model_signals or {}).get(
                    "pending_partials", []) or [])
                total += r.quantity - pending
        return total

    def sold(self, symbol):
        return sum((r.quantity for r in self.rows.values()
                    if r.symbol == symbol and r.side == "SELL"), start=Decimal(0))

    def realized(self, *, is_paper=False):
        return sum((r.pnl for r in self.get_closed_since(None, is_paper) if r.pnl is not None),
                   start=Decimal(0))


def cli_config(mode: str = "live"):
    cfg = MagicMock()
    cfg.supabase_url = "https://x.supabase.co"
    cfg.supabase_service_key = SecretStr("k")
    cfg.reflection_enabled = False
    cfg.asset_class = "equity"
    cfg.trading_mode = mode
    return cfg


@pytest.fixture
def memrepo(monkeypatch):
    repo = MemRepo()
    monkeypatch.setattr("skopaq.db.repositories.TradeRepository", lambda client: repo)
    monkeypatch.setattr("supabase.create_client", lambda url, key: MagicMock())
    monkeypatch.setattr("skopaq.cli.main._trade_repository", lambda config: repo)
    return repo
