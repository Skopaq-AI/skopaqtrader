"""Tests for the MCP server tool registration."""

from __future__ import annotations


def test_mcp_server_imports():
    from skopaq.mcp_server import mcp

    assert mcp is not None
    assert mcp._tool_manager is not None


def test_mcp_server_has_all_tools():
    from skopaq.mcp_server import mcp

    tool_names = {t.name for t in mcp._tool_manager._tools.values()}
    assert len(tool_names) == 40  # Total tool count

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
    assert "quick_decision" in tool_names  # Jev
    assert {"halt_trading", "resume_trading"} <= tool_names  # Kill switch
    assert "performance_report" in tool_names  # Track record
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


class _FakeJev:
    """Stands in for skopaq.llm.jev.Jev in quick_decision tests."""

    model, endpoint, last_error = "jev-1.13.0", "https://api.typesafe.ai", ""

    def __init__(self, verdict=None, noul=None):
        self.verdict, self._noul = verdict, noul
        self.calls = []

    async def ask(self, state, question):
        self.calls.append((state, question))
        return self.verdict

    async def noul(self, state, instructions):
        self.calls.append((state, instructions))
        return self._noul


def _quick(jev, **kwargs):
    import asyncio
    import json
    from unittest.mock import patch

    from skopaq import mcp_server

    with patch("skopaq.llm.jev.get_jev", return_value=jev):
        return json.loads(asyncio.run(mcp_server.quick_decision(**kwargs)))


def test_quick_decision_choice():
    from skopaq.llm.jev import JevVerdict

    jev = _FakeJev(verdict=JevVerdict(
        "SELL", 0.71, {"BUY": 0.05, "HOLD": 0.1, "SELL": 0.85}, "jev-1.13.0"))
    result = _quick(jev, text="Cut to Sell on margin pressure", question="What does it recommend?",
                    options=["BUY", "HOLD", "SELL", "SELL"])

    assert result == {"answer": "SELL", "confidence": 0.71,
                      "probabilities": {"BUY": 0.05, "HOLD": 0.1, "SELL": 0.85},
                      "model": "jev-1.13.0"}
    state, question = jev.calls[0]
    assert state == {"text": "Cut to Sell on margin pressure"}
    assert question["type"] == "choice"
    assert question["instructions"] == "About `text`: What does it recommend?"
    assert list(question["criteria"]) == ["BUY", "HOLD", "SELL"]  # duplicates dropped


def test_quick_decision_yes_no():
    jev = _FakeJev(noul=(0.12, "jev-1.13.0"))
    result = _quick(jev, text="Q2 results in line", question="Does it announce a buyback?")

    assert result == {"answer": "no", "probability_yes": 0.12, "model": "jev-1.13.0"}
    assert jev.calls == [({"text": "Q2 results in line"},
                          "About `text`: Does it announce a buyback?")]


def test_quick_decision_needs_jev():
    result = _quick(None, text="anything", question="Is it bullish?")
    assert "SKOPAQ_JEV_ENABLED" in result["error"]


def test_quick_decision_validates_options_before_calling_jev():
    jev = _FakeJev()
    assert "options" in _quick(jev, text="t", question="q", options=["BUY"])["error"]
    assert "required" in _quick(jev, text=" ", question="q")["error"]
    assert jev.calls == []


def test_quick_decision_reports_jev_failure():
    assert _quick(_FakeJev(), text="t", question="q?")["error"] == "Jev request failed"


def test_quick_decision_failure_says_where_and_why(monkeypatch):
    """A gateway rejecting the model: the answer names the endpoint, model and HTTP error."""
    import httpx2

    from skopaq.llm.jev import Jev

    monkeypatch.delenv("TYPESAFE_BASE_URL", raising=False)
    jev = Jev(api_key="k", model="jev-1.13.0", base_url="https://openrouter.ai/api",
              transport=httpx2.MockTransport(
                  lambda request: httpx2.Response(404, json={"error": "model not found"})))

    result = _quick(jev, text="t", question="Is it bullish?")

    assert result["error"] == "Jev request failed"
    assert result["endpoint"] == "https://openrouter.ai/api"
    assert result["model"] == "jev-1.13.0"
    assert "404" in result["reason"]
