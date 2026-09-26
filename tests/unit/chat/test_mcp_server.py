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


# ── check_safety / place_order: the SELL's open-order context (GAP 2) ────────


def _sell_inputs(positions, *, error=""):
    from datetime import datetime, timezone

    from skopaq.execution.order_router import SellInputs
    from skopaq.execution.sellable import SellContext

    return SellInputs(positions=positions, holdings=[], context=SellContext(
        orders=(), read_at=datetime(2026, 9, 25, 5, 30, tzinfo=timezone.utc), error=error))


def _tcs(qty):
    from decimal import Decimal

    from skopaq.broker.models import Position

    return Position(symbol="TCS", security_id="11536", quantity=Decimal(qty), product="CNC",
                    average_price=100.0)


def _mock_live_router(inputs, execute=None):
    from unittest.mock import AsyncMock, MagicMock

    from skopaq.broker.models import Funds

    router = MagicMock(mode="live")
    router.sell_inputs = AsyncMock(return_value=inputs)
    router.sell_lock = MagicMock(return_value=None)
    router.get_positions = AsyncMock(return_value=[])
    router.get_settled_holdings = AsyncMock(return_value=[])
    router.get_funds = AsyncMock(return_value=Funds(available_cash=500_000,
                                                    available_margin=500_000))
    router.execute = AsyncMock(return_value=execute)
    return router


def _run(tool, *, router, config, **kwargs):
    import asyncio
    import json
    from unittest.mock import patch

    from skopaq import mcp_server
    from skopaq.execution.safety_checker import SafetyChecker

    with patch.dict(mcp_server._infra_cache, {"config": config, "router": router}), \
         patch("skopaq.execution.pnl_history.seed_safety_checker"), \
         patch.object(SafetyChecker, "_check_market_hours"):
        return json.loads(asyncio.run(getattr(mcp_server, tool)(**kwargs)))


def _live_config():
    from unittest.mock import MagicMock

    return MagicMock(trading_mode="live", max_sector_concentration_pct=0.4)


def test_check_safety_checks_a_sell_against_the_routers_open_order_context():
    router = _mock_live_router(_sell_inputs([_tcs(10)], error="HTTP 503"))

    result = _run("check_safety", router=router, config=_live_config(), symbol="TCS",
                  quantity=5, side="SELL")

    assert not result["passed"]
    assert result["rejections"][0].startswith("Cannot read the broker's order book (HTTP 503)")
    router.get_positions.assert_not_called()      # the book-first read replaces them


def test_check_safety_buy_does_not_read_sell_inputs():
    router = _mock_live_router(None)
    result = _run("check_safety", router=router, config=_live_config(), symbol="TCS",
                  quantity=1, price=100, side="BUY")
    assert result["passed"], result
    router.sell_inputs.assert_not_called()


def test_place_order_refuses_a_live_sell_when_the_book_cannot_be_read(monkeypatch):
    from skopaq.execution import order_alerts
    from tests.unit.execution._fakes import AlertSpy

    spy = AlertSpy()
    monkeypatch.setattr(order_alerts, "_alerter", spy)
    router = _mock_live_router(_sell_inputs([_tcs(10)], error="HTTP 503"))

    result = _run("place_order", router=router, config=_live_config(), symbol="TCS",
                  side="SELL", quantity=5)

    assert result["success"] is False
    assert "Cannot read the broker's order book" in result["reason"]
    router.execute.assert_not_called()
    router.sell_lock.assert_called_once()
    assert spy.keys("CRITICAL") == ["sell-refused:TCS:book-unreadable"]


def test_place_order_holds_the_sell_lock_until_the_broker_answers():
    from decimal import Decimal

    from skopaq.broker.models import ExecutionResult

    events = []

    class Lock:
        async def __aenter__(self):
            events.append("lock")

        async def __aexit__(self, *exc):
            events.append("unlock")

    filled = ExecutionResult(success=True, mode="live", fill_price=95.0,
                             filled_quantity=Decimal(3), requested_quantity=Decimal(5),
                             outcome="partial", order_ids=["EQ-1"])
    router = _mock_live_router(_sell_inputs([_tcs(10)]), execute=filled)
    router.sell_lock.return_value = Lock()

    async def execute(order, signal):
        events.append("execute")
        return filled

    router.execute.side_effect = execute

    result = _run("place_order", router=router, config=_live_config(), symbol="TCS",
                  side="SELL", quantity=5)

    assert events == ["lock", "execute", "unlock"]
    assert result["success"] is True
    assert (result["filled_quantity"], result["outcome"], result["order_ids"]) == (
        3, "partial", ["EQ-1"])
    assert (result["remaining_open"], result["fill_unconfirmed"]) == (False, False)


def test_place_order_in_paper_reports_the_fill_as_before():
    from unittest.mock import MagicMock

    from skopaq.broker.paper_engine import PaperEngine
    from skopaq.execution.order_router import OrderRouter

    config = MagicMock(trading_mode="paper", max_sector_concentration_pct=0.4)
    paper = PaperEngine(initial_capital=1_000_000)
    router = OrderRouter(config, paper)
    import asyncio
    import json
    from unittest.mock import patch

    from skopaq import mcp_server

    with patch.dict(mcp_server._infra_cache, {"config": config, "router": router,
                                              "paper": paper}), \
         patch("skopaq.execution.pnl_history.seed_safety_checker"):
        bought = json.loads(asyncio.run(mcp_server.place_order(
            symbol="TCS", side="BUY", quantity=2, price=100, order_type="LIMIT")))
        refused = json.loads(asyncio.run(mcp_server.place_order(
            symbol="INFY", side="SELL", quantity=1, price=100, order_type="LIMIT")))

    assert bought["success"] is True and bought["mode"] == "paper"
    assert (bought["filled_quantity"], bought["outcome"], bought["order_ids"]) == (2, "", [])
    assert refused == {"success": False,
                       "reason": "Safety check failed: No short sales: SELL 1 INFY but only "
                                 "0 held"}
