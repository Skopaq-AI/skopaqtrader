"""Tests for the MCP server tool registration."""

from __future__ import annotations


def test_mcp_server_imports():
    from skopaq.mcp_server import mcp

    assert mcp is not None
    assert mcp._tool_manager is not None


def test_mcp_server_has_all_tools():
    from skopaq.mcp_server import mcp

    tool_names = {t.name for t in mcp._tool_manager._tools.values()}
    assert len(tool_names) == 36  # Total tool count

    # Verify key tools exist by category
    assert "get_quote" in tool_names  # Market data
    assert "get_positions" in tool_names  # Portfolio
    assert "analyze_stock" in tool_names  # Analysis
    assert "place_order" in tool_names  # Execution
    assert "place_gtt_order" in tool_names  # GTT
    assert "get_option_chain" in tool_names  # Options
    assert "suggest_option_trade" in tool_names  # Options AI
    assert "place_amo_order" in tool_names  # AMO
    assert "place_bracket" in tool_names  # Bracket
    assert "place_cover" in tool_names  # Cover
    assert "place_basket" in tool_names  # Basket
    assert "buy_option_contract" in tool_names  # Options buying
    assert "trade_future" in tool_names  # Futures
    assert "invest_mutual_fund" in tool_names  # Mutual funds
    assert "list_mutual_funds" in tool_names  # MF holdings
    assert "gather_all_analysis_data" in tool_names  # Data pipeline
    assert "recall_agent_memories" in tool_names  # Memory
    assert "system_status" in tool_names  # System


def test_mcp_server_name():
    from skopaq.mcp_server import mcp

    assert mcp.name == "SkopaqTrader"


def test_mcp_server_has_instructions():
    from skopaq.mcp_server import mcp

    assert "trading" in mcp.instructions.lower()


def test_scan_market_returns_candidates_as_json():
    """scan_market used to read attributes ScannerCandidate does not have."""
    import asyncio
    import json
    from unittest.mock import MagicMock, patch

    from skopaq import mcp_server
    from skopaq.scanner.models import ScannerCandidate

    found = [
        ScannerCandidate("TCS", "Order win", "high",
                         metrics={"source": "news", "catalyst_score": 2.4}),
        ScannerCandidate("INFY", "Volume spike", metrics={"source": "technical"}),
        ScannerCandidate("WIPRO", "Gap up", metrics={"source": "technical"}),
    ]

    async def scan_once(self):
        return found

    with patch.object(mcp_server, "_get_config", return_value=MagicMock()), \
         patch("skopaq.llm.build_llm_map", return_value={}), \
         patch("skopaq.llm.jev.get_jev", return_value=None), \
         patch("skopaq.scanner.ScannerEngine.scan_once", scan_once):
        result = json.loads(asyncio.run(mcp_server.scan_market(max_candidates=2)))

    assert result == [
        {"symbol": "TCS", "reason": "Order win", "urgency": "high",
         "source": "news", "catalyst_score": 2.4},
        {"symbol": "INFY", "reason": "Volume spike", "urgency": "normal",
         "source": "technical", "catalyst_score": None},
    ]
