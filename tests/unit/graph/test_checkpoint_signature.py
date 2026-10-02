"""The checkpoint key must not resume a pre-v0.5.2 sequential run.

Upstream v0.5.2 (#1255) made the parallel analyst graph the only layout and
pins ``analysts=parallel`` into the signature. Skopaq used to add that marker
itself behind the ``parallel_analysts`` config key; the key is gone and the
marker now comes from upstream, so a checkpoint written by the old sequential
graph cannot resume into this one.
"""

from __future__ import annotations

from types import SimpleNamespace

from tradingagents.graph.trading_graph import TradingAgentsGraph


def _signature(**config) -> str:
    graph = SimpleNamespace(
        selected_analysts=["market", "news"],
        config={"max_debate_rounds": 1, "max_risk_discuss_rounds": 1, **config},
    )
    return TradingAgentsGraph._run_signature(graph, "stock")


def test_signature_marks_the_parallel_layout():
    """Every run is the parallel layout now, so the marker is unconditional."""
    assert "analysts=parallel" in _signature()


def test_sequential_layout_gets_a_different_key():
    """A checkpoint from the pre-v0.5.2 sequential graph must not resume here."""
    sequential = (
        "analysts=market,news|debate=1|risk=1|asset=stock|portfolio=none"
    )
    assert _signature() != sequential


def test_analyst_selection_changes_the_key():
    graph = SimpleNamespace(
        selected_analysts=["market"],
        config={"max_debate_rounds": 1, "max_risk_discuss_rounds": 1},
    )
    assert TradingAgentsGraph._run_signature(graph, "stock") != _signature()


def test_graph_shaping_setting_changes_the_key():
    """max_tool_rounds reshapes the graph (upstream's per-analyst cap)."""
    assert _signature(max_tool_rounds=3) != _signature(max_tool_rounds=5)