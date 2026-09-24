"""The checkpoint key tells the parallel graph apart from the sequential one."""

from __future__ import annotations

from types import SimpleNamespace

from tradingagents.graph.trading_graph import TradingAgentsGraph


def _signature(**config) -> str:
    graph = SimpleNamespace(
        selected_analysts=["market", "news"],
        config={"max_debate_rounds": 1, "max_risk_discuss_rounds": 1, **config},
    )
    return TradingAgentsGraph._run_signature(graph, "stock")


def test_parallel_runs_get_their_own_checkpoint():
    assert _signature(parallel_analysts=True) != _signature(parallel_analysts=False)


def test_sequential_signature_matches_upstream():
    assert _signature() == (
        "analysts=market,news|debate=1|risk=1|asset=stock|portfolio=none"
    )
    assert _signature(parallel_analysts=False) == _signature()
