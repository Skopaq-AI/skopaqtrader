"""`skopaq trade` (live) writes the trade row of a fill the broker confirmed before it closes
the client and drains the order alerts (up to 10 s on a slow Telegram): a Ctrl+C in that
window must not lose the row."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from skopaq.broker.models import ExecutionResult, OrderType, TradingSignal


@pytest.mark.asyncio
async def test_ctrl_c_while_the_alerts_drain_keeps_the_trade_row(monkeypatch, tmp_path):
    import skopaq.cli.main as main
    from skopaq.execution import order_alerts
    from skopaq.graph.skopaq_graph import AnalysisResult

    cfg = MagicMock()
    cfg.asset_class = "equity"
    cfg.trading_mode = "live"
    cfg.initial_paper_capital = 1_000_000
    cfg.position_sizing_enabled = False
    cfg.max_sector_concentration_pct = 40.0
    cfg.selected_analysts = "market"
    monkeypatch.setattr("skopaq.config.SkopaqConfig", lambda: cfg)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

    monkeypatch.setattr("skopaq.broker.client.INDstocksClient", Client)
    monkeypatch.setattr("skopaq.broker.token_manager.TokenManager", lambda: None)
    monkeypatch.setattr("skopaq.execution.pnl_history.seed_safety_checker", lambda *a: None)
    monkeypatch.setattr(main, "_compute_risk_scales", lambda *a: (1.0, 1.0))
    monkeypatch.setattr(main, "_build_upstream_config", lambda c: {})
    monkeypatch.setattr(main, "_create_memory_store", lambda c: None)

    signal = TradingSignal(symbol="TCS", action="BUY", entry_price=100.0,
                           order_type=OrderType.LIMIT, quantity=Decimal(10))
    execution = ExecutionResult(success=True, signal=signal, mode="live", fill_price=100.0,
                                filled_quantity=Decimal(10), requested_quantity=Decimal(10),
                                outcome="filled", order_ids=["EQ-1"])

    class Graph:
        def __init__(self, *args, **kwargs):
            pass

        async def analyze_and_execute(self, *args, **kwargs):
            return AnalysisResult(symbol="TCS", trade_date="2026-09-25", signal=signal,
                                  execution=execution)

    monkeypatch.setattr("skopaq.graph.skopaq_graph.SkopaqTradingGraph", Graph)
    persisted = []

    async def run_lifecycle(config, graph, store, result):
        await asyncio.sleep(0.05)                     # a Supabase write
        persisted.append(result.execution.filled_quantity)

    monkeypatch.setattr(main, "_run_lifecycle", run_lifecycle)

    class SlowAlerter:
        def alert(self, *args, **kwargs):
            pass

        async def drain(self, timeout: float = 10.0):
            await asyncio.sleep(0.3)                  # the trade notification still going

    monkeypatch.setattr(order_alerts, "_alerter", SlowAlerter())

    task = asyncio.create_task(main._run_trade("TCS", "2026-09-25"))
    await asyncio.sleep(0.02)          # the BUY is filled; the row is being written
    task.cancel()                      # Ctrl+C
    await asyncio.gather(task, return_exceptions=True)

    assert persisted == [Decimal(10)]
