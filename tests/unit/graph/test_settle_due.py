"""Settling due decisions for every ticker (not only the one being analyzed)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tradingagents.decision_log import TradingMemoryLog


def _graph(tmp_path, store=None):
    from skopaq.graph.skopaq_graph import SkopaqTradingGraph

    upstream = MagicMock()
    upstream.memory_log = TradingMemoryLog({"memory_log_path": str(tmp_path / "log.md")})
    decisions = (("TCS.NS", "2026-09-01"), ("INFY.NS", "2026-09-02"), ("HDFC.NS", "2026-09-22"))
    for ticker, day in decisions:
        upstream.memory_log.store_decision(ticker, day, "**Rating**: Buy")

    def settle(ticker):
        # Only decisions from before mid-September have a traded window
        for entry in upstream.memory_log.get_pending_entries():
            if entry["ticker"] == ticker and entry["date"] < "2026-09-15":
                upstream.memory_log.update_with_outcome(
                    ticker, entry["date"], 0.01, 0.0, 5, "ok", resolution_date="2026-09-10")

    upstream.settle_pending.side_effect = settle
    graph = SkopaqTradingGraph({}, MagicMock(), memory_store=store)
    graph._graph = upstream
    return graph, upstream


def test_settles_every_ticker_with_pending_decisions(tmp_path):
    store = MagicMock()
    graph, upstream = _graph(tmp_path, store)

    assert graph.settle_due() == 2

    assert sorted(c.args[0] for c in upstream.settle_pending.call_args_list) == [
        "HDFC.NS", "INFY.NS", "TCS.NS"]
    assert [e["ticker"] for e in upstream.memory_log.get_pending_entries()] == ["HDFC.NS"]
    store.save.assert_called_once_with(upstream)


def test_one_failing_ticker_does_not_stop_the_rest(tmp_path):
    graph, upstream = _graph(tmp_path)
    settle = upstream.settle_pending.side_effect

    def flaky(ticker):
        if ticker == "INFY.NS":
            raise RuntimeError("yahoo down")
        settle(ticker)

    upstream.settle_pending.side_effect = flaky
    assert graph.settle_due() == 1


def test_stop_leaves_remaining_tickers_for_next_run(tmp_path):
    store = MagicMock()
    graph, upstream = _graph(tmp_path, store)
    asked = []

    def should_stop():
        asked.append(1)
        return len(asked) > 2  # stop before the third ticker (TCS.NS)

    assert graph.settle_due(should_stop) == 1  # INFY.NS; HDFC.NS was not due
    assert sorted(c.args[0] for c in upstream.settle_pending.call_args_list) == [
        "HDFC.NS", "INFY.NS"]
    store.save.assert_called_once_with(upstream)


def test_nothing_settled_skips_the_save(tmp_path):
    store = MagicMock()
    graph, upstream = _graph(tmp_path, store)
    upstream.settle_pending.side_effect = None

    assert graph.settle_due() == 0
    store.save.assert_not_called()


@pytest.mark.asyncio
async def test_daemon_settles_at_the_end_of_a_session():
    from skopaq.execution.daemon import TradingDaemon

    daemon = TradingDaemon.__new__(TradingDaemon)
    daemon._stop = MagicMock(is_set=MagicMock(return_value=False))
    daemon._graph = MagicMock(settle_due=MagicMock(return_value=3))
    assert await daemon._settle_due_decisions() == 3
    daemon._graph.settle_due.assert_called_once_with(daemon._stop.is_set)

    daemon._graph.settle_due.side_effect = RuntimeError("boom")
    assert await daemon._settle_due_decisions() == 0

    daemon._graph = None
    assert await daemon._settle_due_decisions() == 0


def test_cli_settle_command(monkeypatch):
    from typer.testing import CliRunner

    from skopaq.cli import main

    graph = MagicMock(settle_due=MagicMock(return_value=4))
    monkeypatch.setattr(main, "_build_upstream_config", lambda config: {})
    monkeypatch.setattr(main, "_create_memory_store", lambda config: None)
    monkeypatch.setattr("skopaq.graph.skopaq_graph.SkopaqTradingGraph", lambda *a, **k: graph)

    result = CliRunner().invoke(main.app, ["settle"])

    assert result.exit_code == 0, result.output
    assert "Settled 4 past decision(s)." in result.output
