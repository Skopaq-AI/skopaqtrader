"""Tests for crypto-specific graph wiring — state fields, analyst plan, propagation."""

import pytest


class TestAgentStateFields:
    """Verify the 3 new crypto report fields exist in AgentState."""

    def test_state_has_onchain_report(self):
        from tradingagents.agents.state import AgentState
        assert "onchain_report" in AgentState.__annotations__

    def test_state_has_defi_report(self):
        from tradingagents.agents.state import AgentState
        assert "defi_report" in AgentState.__annotations__

    def test_state_has_funding_report(self):
        from tradingagents.agents.state import AgentState
        assert "funding_report" in AgentState.__annotations__


class TestPropagationInitialState:
    """Verify initial state includes empty crypto report fields."""

    def test_initial_state_has_crypto_fields(self):
        from tradingagents.graph.propagation import Propagator

        prop = Propagator()
        state = prop.create_initial_state(
            company_name="BTCUSDT",
            trade_date="2024-01-15",
        )

        assert state["onchain_report"] == ""
        assert state["defi_report"] == ""
        assert state["funding_report"] == ""


class TestAnalystExecutionPlan:
    """Verify the crypto analysts are registered in upstream's analyst plan."""

    @pytest.mark.parametrize("key,report_key,tools", [
        ("onchain", "onchain_report", {"get_blockchain_stats", "get_address_activity"}),
        ("defi", "defi_report", {"get_token_fundamentals", "get_defi_tvl", "get_chain_tvl_overview"}),
        ("funding", "funding_report", {"get_funding_rates", "get_open_interest", "get_long_short_ratio"}),
    ])
    def test_spec_registered(self, key, report_key, tools):
        from tradingagents.graph.analyst_execution import ANALYST_NODE_SPECS

        spec = ANALYST_NODE_SPECS[key]
        assert spec.report_key == report_key
        assert {t.name for t in spec.tools} == tools
        assert spec.tool_node == f"tools_{key}"

    def test_crypto_plan_builds(self):
        from tradingagents.graph.analyst_execution import build_analyst_execution_plan

        plan = build_analyst_execution_plan(
            ["market", "social", "news", "fundamentals", "onchain", "defi", "funding"]
        )
        assert [s.key for s in plan.specs][-3:] == ["onchain", "defi", "funding"]


class TestGraphCompiles:
    """The full crypto graph compiles with the per-role LLM map."""

    def test_crypto_graph_compiles_with_llm_map(self, monkeypatch):
        from unittest.mock import MagicMock

        from tradingagents.graph import setup as setup_module
        from tradingagents.graph.conditional_logic import ConditionalLogic

        received = {}
        real_factory = setup_module.create_onchain_analyst

        def spy(llm):
            received["onchain"] = llm
            return real_factory(llm)

        monkeypatch.setattr(setup_module, "create_onchain_analyst", spy)
        quick, deep, onchain_llm = MagicMock(), MagicMock(), MagicMock()
        setup = setup_module.GraphSetup(
            quick, deep, ConditionalLogic(), llm_map={"onchain_analyst": onchain_llm}
        )
        graph = setup.setup_graph(
            ["market", "social", "news", "fundamentals", "onchain", "defi", "funding"]
        ).compile()

        for node in ("Onchain Analyst", "tools_onchain", "Defi Analyst", "Funding Analyst"):
            assert node in graph.nodes
        assert received["onchain"] is onchain_llm


class TestCryptoReportsSection:
    """Crypto reports reach the debate prompts only when a crypto analyst ran."""

    def test_empty_for_equity(self):
        from tradingagents.agents.context import crypto_reports_section

        assert crypto_reports_section({"market_report": "x"}) == ""

    def test_includes_reports_and_marks_missing(self):
        from tradingagents.agents.context import crypto_reports_section

        section = crypto_reports_section({"onchain_report": "Hashrate rising"})
        assert "On-Chain Network Analysis: Hashrate rising" in section
        assert "DeFi/Tokenomics Analysis: (No" in section


class TestSkopaqWrapperAnalystSelection:
    """Verify Skopaq auto-selects crypto analysts when asset_class is crypto."""

    def _make_wrapper(self, asset_class="equity", selected_analysts=None):
        """Create a SkopaqTradingGraph with minimal deps (no graph init)."""
        from unittest.mock import MagicMock
        from skopaq.graph.skopaq_graph import SkopaqTradingGraph

        executor = MagicMock()
        config = {"asset_class": asset_class}
        return SkopaqTradingGraph(
            upstream_config=config,
            executor=executor,
            selected_analysts=selected_analysts,
        )

    def test_equity_gets_4_base_analysts(self):
        wrapper = self._make_wrapper(asset_class="equity")
        assert wrapper._selected_analysts == ["market", "social", "news", "fundamentals"]

    def test_crypto_gets_7_analysts(self):
        wrapper = self._make_wrapper(asset_class="crypto")
        assert len(wrapper._selected_analysts) == 7
        assert "onchain" in wrapper._selected_analysts
        assert "defi" in wrapper._selected_analysts
        assert "funding" in wrapper._selected_analysts

    def test_explicit_override_respected(self):
        wrapper = self._make_wrapper(
            asset_class="crypto",
            selected_analysts=["market", "news"],
        )
        assert wrapper._selected_analysts == ["market", "news"]

    def test_no_asset_class_defaults_to_base(self):
        from unittest.mock import MagicMock
        from skopaq.graph.skopaq_graph import SkopaqTradingGraph

        wrapper = SkopaqTradingGraph(
            upstream_config={},
            executor=MagicMock(),
        )
        assert wrapper._selected_analysts == ["market", "social", "news", "fundamentals"]
