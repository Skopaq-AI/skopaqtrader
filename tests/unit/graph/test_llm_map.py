"""Per-role LLM assignment (Skopaq modification of upstream GraphSetup)."""

from unittest.mock import MagicMock

import pytest

from tradingagents.graph.conditional_logic import ConditionalLogic
from tradingagents.graph.setup import GraphSetup


@pytest.fixture
def llms():
    return {"quick": MagicMock(name="quick"), "deep": MagicMock(name="deep")}


def _setup(llms, llm_map=None):
    return GraphSetup(llms["quick"], llms["deep"], ConditionalLogic(), llm_map=llm_map)


def test_role_in_map_wins(llms):
    claude = MagicMock(name="claude")
    setup = _setup(llms, {"portfolio_manager": claude})
    assert setup._get_llm("portfolio_manager", deep=True) is claude


def test_legacy_role_name_resolves(llms):
    claude = MagicMock(name="claude")
    setup = _setup(llms, {"risk_manager": claude})
    assert setup._get_llm("portfolio_manager", "risk_manager", deep=True) is claude


def test_default_entry_used_for_missing_role(llms):
    gemini = MagicMock(name="gemini")
    assert _setup(llms, {"_default": gemini})._get_llm("trader") is gemini


def test_no_map_falls_back_to_quick_and_deep(llms):
    setup = _setup(llms)
    assert setup._get_llm("trader") is llms["quick"]
    assert setup._get_llm("research_manager", deep=True) is llms["deep"]


def test_trading_graph_keeps_llm_map_out_of_config(monkeypatch, tmp_path):
    """The data layer deep-copies config on every read; live LLMs must not be in it."""
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph import trading_graph

    client = MagicMock()
    monkeypatch.setattr(trading_graph, "create_llm_client", lambda **_: client)
    marker = MagicMock(name="market_llm")
    config = {
        **DEFAULT_CONFIG,
        "results_dir": str(tmp_path / "results"),
        "data_cache_dir": str(tmp_path / "cache"),
        "memory_log_path": str(tmp_path / "memory.md"),
        "llm_map": {"market_analyst": marker},
    }
    graph = trading_graph.TradingAgentsGraph(selected_analysts=["market"], config=config)

    assert "llm_map" not in graph.config
    assert graph.graph_setup.llm_map == {"market_analyst": marker}
