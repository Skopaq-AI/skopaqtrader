"""The AI tier never holds up the monitor's safety work.

The sell analyst's LLM round trips and yfinance tools are synchronous: they run in a worker
thread, and the live monitor runs each analysis as its own bounded task. A slow analysis of
one position does not delay another position's stop-loss exit, nor the polling of an exit
already resting at the broker, nor a SIGTERM handler.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from skopaq.agents.sell_analyst import SellDecision, analyze_exit
from tests.unit.execution._fakes import Script
from tests.unit.execution.test_position_monitor_live import (  # noqa: F401
    INFY,
    TCS,
    Live,
    _lookups,
    alerts,
)


async def test_the_sell_analyst_runs_its_chain_and_tools_off_the_event_loop(monkeypatch):
    loop_thread = threading.get_ident()
    seen: list[tuple[str, int]] = []

    class FakeLLM:
        def bind_tools(self, tools):
            def invoke(_input):
                seen.append(("llm", threading.get_ident()))
                if len(seen) == 1:
                    return AIMessage(content="", tool_calls=[
                        {"name": "get_stock_data", "args": {"symbol": "TCS.NS"}, "id": "1"}])
                return AIMessage(content="DECISION: SELL\nCONFIDENCE: 70\nREASONING: weak")

            return RunnableLambda(invoke)

    def tool_invoke(args):
        seen.append(("tool", threading.get_ident()))
        return "prices"

    fake_tool = SimpleNamespace(name="get_stock_data", invoke=tool_invoke)
    monkeypatch.setattr("skopaq.agents.sell_analyst.get_stock_data", fake_tool)
    monkeypatch.setattr("skopaq.llm.jev.get_jev", lambda: None)

    decision = await analyze_exit(llm=FakeLLM(), symbol="TCS", entry_price=100.0,
                                  current_price=95.0, quantity=10, position_pnl_pct=-5.0,
                                  trade_date="2026-09-25")

    assert decision.action == "SELL"
    assert [what for what, _ in seen] == ["llm", "tool", "llm"]
    assert all(thread != loop_thread for _, thread in seen)


async def test_the_event_loop_runs_while_the_llm_call_blocks(monkeypatch):
    # A real 0.3 s blocking LLM call: a loop callback due after 0.02 s (a SIGTERM handler,
    # say) runs before the call returns
    finished: list[float] = []

    class SlowLLM:
        def bind_tools(self, tools):
            def invoke(_input):
                time.sleep(0.3)
                finished.append(time.monotonic())
                return AIMessage(content="DECISION: HOLD\nCONFIDENCE: 60\nREASONING: fine")

            return RunnableLambda(invoke)

    monkeypatch.setattr("skopaq.llm.jev.get_jev", lambda: None)
    analysis = asyncio.create_task(analyze_exit(
        llm=SlowLLM(), symbol="TCS", entry_price=100.0, current_price=99.0, quantity=10,
        position_pnl_pct=-1.0, trade_date="2026-09-25"))
    await asyncio.sleep(0.02)
    handled = time.monotonic()
    decision = await analysis

    assert decision.action == "HOLD"
    assert handled < finished[0]


async def test_a_slow_ai_analysis_does_not_delay_another_positions_stop(alerts):
    # INFY needs the AI tier every cycle (no rule fires); TCS is below its hard stop
    live = Live({"INFY": (5, 100.0), "TCS": (10, 100.0)}, ltps={INFY: 100.0, TCS: 90.0},
                llm=MagicMock(), monitor_ai_interval_cycles=1)
    live.broker.by_symbol["TCS"] = [Script(), Script(), Script()]   # TCS's exit rests
    analyses: list[tuple[float, float]] = []

    async def slow_analysis(**kwargs):
        start = live.clock.t
        await live.clock.sleep(25)                   # a 25 s LLM + tools round trip
        analyses.append((start, live.clock.t))
        return SellDecision(action="HOLD", confidence=60, reasoning="fine")

    polls: list[float] = []
    real_get_order = live.broker.get_order

    async def timed_get_order(order_id, segment="EQUITY"):
        polls.append(live.clock.t)
        return await real_get_order(order_id, segment)

    live.broker.get_order = timed_get_order
    with patch("skopaq.execution.position_monitor.analyze_exit", new=slow_analysis):
        await live.run(stop_at=40)

    [first_tcs, *_] = live.broker.placed_orders("TCS")
    assert first_tcs.placed_at < 1                   # not after INFY's analysis
    assert analyses and analyses[0][0] < 1
    # The resting exit was polled all through the analysis
    assert len([t for t in polls if 1 <= t < 20]) >= 10


# ── A StopIteration in the worker thread cannot hang the analysis ────────────
#
# asyncio cannot set a StopIteration on a Future, so one raised inside asyncio.to_thread
# would leave the await pending forever: the paper monitor's loop would hang on its first
# AI check (before the calls moved to a thread it defaulted to HOLD).


def _stopiteration_llm(after: list | None = None):
    responses = iter(after or [])

    class ExhaustedLLM:
        def bind_tools(self, tools):
            return RunnableLambda(lambda _input: next(responses))   # raises StopIteration

    return ExhaustedLLM()


async def test_a_stopiteration_in_the_llm_call_defaults_to_hold(monkeypatch):
    monkeypatch.setattr("skopaq.llm.jev.get_jev", lambda: None)

    decision = await asyncio.wait_for(analyze_exit(
        llm=_stopiteration_llm(), symbol="TCS", entry_price=100.0, current_price=95.0,
        quantity=10, position_pnl_pct=-5.0, trade_date="2026-09-25"), 5)

    assert (decision.action, decision.confidence) == ("HOLD", 0)


async def test_a_stopiteration_in_a_tool_is_a_tool_error(monkeypatch):
    def exhausted(args):
        raise StopIteration

    monkeypatch.setattr("skopaq.agents.sell_analyst.get_stock_data",
                        SimpleNamespace(name="get_stock_data", invoke=exhausted))
    monkeypatch.setattr("skopaq.llm.jev.get_jev", lambda: None)
    llm = _stopiteration_llm([
        AIMessage(content="", tool_calls=[
            {"name": "get_stock_data", "args": {"symbol": "TCS.NS"}, "id": "1"}]),
        AIMessage(content="DECISION: SELL\nCONFIDENCE: 70\nREASONING: weak")])

    decision = await asyncio.wait_for(analyze_exit(
        llm=llm, symbol="TCS", entry_price=100.0, current_price=95.0, quantity=10,
        position_pnl_pct=-5.0, trade_date="2026-09-25"), 5)

    assert decision.action == "SELL"


async def test_the_paper_monitor_does_not_hang_on_a_stopiteration_in_its_ai_check():
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from skopaq.broker.models import Quote, TradingSignal
    from skopaq.broker.paper_engine import PaperEngine
    from skopaq.config import SkopaqConfig
    from skopaq.constants import SafetyRules
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter
    from skopaq.execution.position_monitor import PositionMonitor
    from skopaq.execution.safety_checker import SafetyChecker

    cfg = SkopaqConfig(_env_file=None, trading_mode="paper", monitor_ai_interval_cycles=1)
    object.__setattr__(cfg, "monitor_poll_interval_seconds", 0.01)
    paper = PaperEngine(initial_capital=1_000_000)
    paper.update_quote(Quote(symbol="TCS", ltp=100.0, close=100.0, bid=99.9, ask=100.1))
    router = OrderRouter(cfg, paper)
    executor = Executor(router, SafetyChecker(rules=SafetyRules(
        market_hours_only=False, require_stop_loss=False, max_lots_per_position=10000,
        max_position_pct=1.0)))
    await executor.execute_signal(TradingSignal(symbol="TCS", action="BUY", confidence=80,
                                                entry_price=100.0, quantity=Decimal(5)))
    stop = asyncio.Event()
    polls: list[str] = []

    async def get_ltp(code):
        polls.append(code)
        if len(polls) >= 3:
            stop.set()                   # a SIGTERM-like stop after three polls
        return 100.5

    client = MagicMock()
    client.get_ltp = get_ltp
    monitor = PositionMonitor(executor, client, router, cfg, _stopiteration_llm(), stop,
                              ai_enabled=True)

    async def scrip(client, symbol, exchange="NSE"):
        return "NSE_TCS"

    with patch("skopaq.broker.scrip_resolver.resolve_scrip_code", scrip), \
            patch("skopaq.notifications.notify_trade_event", AsyncMock()), \
            patch("skopaq.llm.jev.get_jev", lambda: None):
        result = await asyncio.wait_for(monitor.run(), 5)

    assert stop.is_set() and len(polls) >= 3
    assert result.sells_executed == 0            # the AI check defaulted to HOLD
