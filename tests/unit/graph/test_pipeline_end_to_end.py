"""SkopaqTradingGraph → upstream TradingAgents v0.5.1, end to end, offline.

Scripted chat models stand in for every LLM and every data vendor answers
locally, so this runs the real upstream graph: analysts with their tools,
both debates, the managers, the decision log, and Skopaq's signal parsing.
It pins the integration points of the upstream sync — per-role LLMs, NSE
tickers, crypto analysts and the Portfolio Manager's confidence.
"""

from __future__ import annotations

import pandas as pd
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import RunnableLambda
from pydantic import Field

from tradingagents.agents import context, crypto_tools, schemas
from tradingagents.agents.analysts import sentiment_analyst
from tradingagents.dataflows import router
from tradingagents.dataflows.vendors.yahoo import market as yahoo_market
from tradingagents.dataflows.vendors.yahoo import snapshot
from tradingagents.graph import trading_graph

TRADE_DATE = "2026-01-09"
TEXT = "Report.\n\n**Rating**: Overweight\n\nFINAL TRANSACTION PROPOSAL: **BUY**"

ARGS = {"symbol": "RELIANCE", "ticker": "RELIANCE", "curr_date": TRADE_DATE,
        "start_date": "2026-01-02", "end_date": TRADE_DATE, "indicator": "rsi",
        "topic": "RBI rate cut", "freq": "quarterly", "coin": "bitcoin", "protocol": "aave"}


def _structured(confidence: int) -> dict:
    return {
        schemas.ResearchPlan: schemas.ResearchPlan(
            recommendation=schemas.PortfolioRating.OVERWEIGHT, rationale="r",
            strategic_actions="a"),
        schemas.TraderProposal: schemas.TraderProposal(
            action=schemas.TraderAction.BUY, reasoning="r"),
        schemas.PortfolioDecision: schemas.PortfolioDecision(
            rating=schemas.PortfolioRating.OVERWEIGHT, executive_summary="s",
            investment_thesis="t", confidence=confidence),
        schemas.SentimentReport: schemas.SentimentReport(
            overall_band=schemas.SentimentBand.NEUTRAL, overall_score=5.0,
            confidence="low", narrative="n"),
    }


class ScriptedModel(BaseChatModel):
    """Calls every bound tool once, then answers with TEXT (or a structured object)."""

    name_tag: str = "model"
    confidence: int = 50
    tools: tuple = ()
    structured_calls: list = Field(default_factory=list)  # schemas answered by this model

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.model_copy(update={"tools": tuple(tools)})

    def with_structured_output(self, schema, **kwargs):
        def answer(_):
            self.structured_calls.append(schema)
            return _structured(self.confidence)[schema]
        return RunnableLambda(answer)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.tools and not isinstance(messages[-1], ToolMessage):
            calls = [{"name": t.name, "id": f"call_{i}",
                      "args": {k: v for k, v in ARGS.items()
                               if k in t.tool_call_schema.model_json_schema()["properties"]}}
                     for i, t in enumerate(self.tools)]
            message = AIMessage(content="", tool_calls=calls)
        else:
            message = AIMessage(content=TEXT)
        return ChatResult(generations=[ChatGeneration(message=message)])


class _Client:
    def __init__(self, model):
        self.model = model

    def get_llm(self):
        return self.model


@pytest.fixture
def offline(monkeypatch):
    """Every vendor answers offline; returns (method, vendor, first arg) calls."""
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    called: list[tuple[str, str]] = []
    for method, vendors in router.VENDOR_METHODS.items():
        for vendor in vendors:
            def answer(*a, _m=method, _v=vendor, **k):
                called.append((_m, _v, a[0] if a else ""))
                return f"{_m} data"
            monkeypatch.setitem(vendors, vendor, answer)
    for name in ("blockchain_stats", "address_activity", "token_fundamentals", "defi_tvl",
                 "chain_tvl_overview", "funding_rates", "open_interest", "long_short_ratio"):
        monkeypatch.setattr(crypto_tools, f"_get_{name}",
                            lambda *a, _n=name: called.append((_n, "crypto", "")) or f"{_n} data")
    prices = pd.DataFrame({
        "Date": pd.bdate_range(end=TRADE_DATE, periods=60),
        "Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.5, "Volume": 1_000_000,
    })
    monkeypatch.setattr(snapshot, "load_ohlcv", lambda *a, **k: prices.copy())
    monkeypatch.setattr(sentiment_analyst, "fetch_stocktwits_messages", lambda *a, **k: "no posts")
    monkeypatch.setattr(sentiment_analyst, "fetch_reddit_posts", lambda *a, **k: "no posts")
    monkeypatch.setattr(yahoo_market.yf, "Ticker",
                        lambda s: type("T", (), {"info": {"longName": "Reliance Industries"}})())
    context.resolve_instrument_identity.cache_clear()
    return called


def _skopaq_graph(tmp_path, monkeypatch, llm_map, **config):
    from unittest.mock import MagicMock

    from skopaq.graph.skopaq_graph import SkopaqTradingGraph

    monkeypatch.setattr("skopaq.llm.env_bridge.bridge_env_vars", lambda *a, **k: [])
    monkeypatch.setattr(trading_graph, "create_llm_client", lambda **k: _Client(ScriptedModel()))
    upstream_config = {
        "results_dir": str(tmp_path / "results"),
        "data_cache_dir": str(tmp_path / "cache"),
        "memory_log_path": str(tmp_path / "log.md"),
        "data_vendors": {"core_stock_apis": "indstocks,yfinance"},
        "yfinance_symbol_suffix": ".NS",
        "llm_map": llm_map,
        **config,
    }
    return SkopaqTradingGraph(upstream_config, MagicMock())


@pytest.mark.asyncio
async def test_equity_run_uses_role_llms_and_nse_ticker(tmp_path, monkeypatch, offline):
    gemini = ScriptedModel(name_tag="gemini", confidence=40)
    claude = ScriptedModel(name_tag="claude", confidence=77)
    graph = _skopaq_graph(tmp_path, monkeypatch, {"_default": gemini, "portfolio_manager": claude})

    result = await graph.analyze("RELIANCE", TRADE_DATE)

    assert result.error is None, result.error
    assert result.raw_decision == "Overweight"
    assert result.signal.symbol == "RELIANCE"
    assert result.signal.action == "BUY"
    # The Portfolio Manager's own confidence, from the role-assigned model
    assert result.signal.confidence == 77
    assert claude.structured_calls == [schemas.PortfolioDecision]
    assert schemas.PortfolioDecision not in gemini.structured_calls
    # INDstocks serves prices; yfinance calls get the .NS suffix added
    assert ("get_stock_data", "indstocks", "RELIANCE") in offline
    assert ("get_indicators", "yfinance", "RELIANCE.NS") in offline
    # Upstream itself ran on the exchange-qualified ticker
    assert result.agent_state["company_of_interest"] == "RELIANCE.NS"
    entries = graph._graph.memory_log.load_entries()
    assert [(e["ticker"], e["rating"]) for e in entries] == [("RELIANCE.NS", "Overweight")]


@pytest.mark.asyncio
async def test_crypto_run_includes_crypto_analysts(tmp_path, monkeypatch, offline):
    model = ScriptedModel(confidence=66)
    graph = _skopaq_graph(tmp_path, monkeypatch, {"_default": model},
                          asset_class="crypto", yfinance_symbol_suffix="")

    result = await graph.analyze("BTC-USD", TRADE_DATE)

    assert result.error is None, result.error
    assert result.signal.action == "BUY"
    for key in ("onchain_report", "defi_report", "funding_report"):
        assert result.agent_state[key].strip(), key
    crypto_calls = {name for name, vendor, _ in offline if vendor == "crypto"}
    assert crypto_calls == {
        "blockchain_stats", "address_activity", "token_fundamentals", "defi_tvl",
        "chain_tvl_overview", "funding_rates", "open_interest", "long_short_ratio",
    }


class RecordingModel(ScriptedModel):
    """ScriptedModel that records which tool results each analyst call saw."""

    seen: list = Field(default_factory=list)  # (bound tool names, tool-result names)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.tools:
            self.seen.append((
                {t.name for t in self.tools},
                {m.name for m in messages if isinstance(m, ToolMessage)},
            ))
        return super()._generate(messages, stop, run_manager, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("parallel", [True, False], ids=["parallel", "sequential"])
async def test_each_analyst_sees_only_its_own_tool_results(
    tmp_path, monkeypatch, offline, parallel
):
    model = RecordingModel(confidence=60)
    graph = _skopaq_graph(tmp_path, monkeypatch, {"_default": model},
                          asset_class="crypto", yfinance_symbol_suffix="",
                          parallel_analysts=parallel)

    result = await graph.analyze("BTC-USD", TRADE_DATE)

    assert result.error is None, result.error
    assert result.signal.action == "BUY"
    analyst_calls = [(bound, results) for bound, results in model.seen if results]
    assert len(analyst_calls) == 6  # every tool-using analyst reported after its tools ran
    for bound, results in analyst_calls:
        assert results <= bound, f"an analyst saw another analyst's tool results: {results - bound}"


def test_parallel_graph_fans_out_and_joins(monkeypatch):
    from unittest.mock import MagicMock

    from tradingagents.graph.conditional_logic import ConditionalLogic
    from tradingagents.graph.setup import GraphSetup

    setup = GraphSetup(MagicMock(), MagicMock(), ConditionalLogic())
    graph = setup.setup_graph(["market", "social", "news", "fundamentals"], parallel=True).compile()

    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    for analyst in ("Market Analyst", "Sentiment Analyst", "News Analyst", "Fundamentals Analyst"):
        assert ("__start__", analyst) in edges
        assert (analyst, "Bull Researcher") in edges
    assert not any(node.startswith(("Msg Clear", "tools_")) for node in graph.nodes)


@pytest.mark.asyncio
async def test_analysis_to_paper_fill_with_jev(tmp_path, monkeypatch, offline):
    """Agents → Portfolio Manager → Jev → Executor → SafetyChecker → paper fill."""
    import json
    from decimal import Decimal
    from unittest.mock import MagicMock

    import httpx2

    from skopaq.broker.models import Quote
    from skopaq.broker.paper_engine import PaperEngine
    from skopaq.constants import SafetyRules
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter
    from skopaq.execution.safety_checker import SafetyChecker
    from skopaq.graph.skopaq_graph import SkopaqTradingGraph
    from skopaq.llm.jev import Jev

    asked = []

    def jev_api(request: httpx2.Request) -> httpx2.Response:
        asked.append(json.loads(request.content))
        return httpx2.Response(200, json={
            "model": "jev-1.13.0",
            "answers": {"answer": {"type": "choice", "choice": "BUY", "confidence": 0.6,
                                   "probabilities": {"BUY": 0.81, "HOLD": 0.15, "SELL": 0.04}}},
            "usage": {"input_tokens": 200, "output_tokens": 3},
        })

    paper = PaperEngine(initial_capital=1_000_000)
    paper.update_quote(Quote(symbol="RELIANCE", ltp=2500.0, close=2500.0))
    config = MagicMock()
    config.trading_mode = "paper"
    executor = Executor(OrderRouter(config, paper), SafetyChecker(rules=SafetyRules(
        market_hours_only=False, require_stop_loss=False, max_lots_per_position=10000,
        max_order_value_inr=10_000_000, max_position_pct=1.0,
    )))
    monkeypatch.setattr(Executor, "_fetch_current_price", staticmethod(lambda s: 2500.0))

    graph = _skopaq_graph(tmp_path, monkeypatch, {"_default": ScriptedModel(confidence=77)})
    graph = SkopaqTradingGraph(
        graph._upstream_config, executor,
        jev=Jev(api_key="test", transport=httpx2.MockTransport(jev_api)),
    )

    result = await graph.analyze_and_execute("RELIANCE", TRADE_DATE)

    assert result.error is None, result.error
    # Jev read the Portfolio Manager's decision; its probability is the confidence
    assert "**Rating**: Overweight" in asked[0]["state"]["decision"]
    assert result.signal.action == "BUY"
    assert result.signal.confidence == 81
    assert result.signal.reasoning.startswith("[Jev jev-1.13.0: BUY")
    # ... and the signal filled on the paper engine through the safety checker
    assert result.execution.success, result.execution.rejection_reason
    assert result.execution.mode == "paper"
    held = paper.get_positions()[0].quantity
    assert held > 0

    # Selling more than was bought is refused by the no-short-sale check
    oversell = result.signal.model_copy(
        update={"action": "SELL", "quantity": Decimal(held) + 1})
    refused = await executor.execute_signal(oversell)
    assert not refused.safety_passed
    assert "No short sales" in refused.rejection_reason
