from typing import Annotated

from langgraph.graph import MessagesState
from typing_extensions import TypedDict


# ── Reducers for parallel fan-out/fan-in ──────────────────────────
# LangGraph's default LastValue channel raises when multiple parallel branches
# converge (fan-in). Upstream runs analysts sequentially so it never hits this;
# we fan them out, so every field written by more than one branch needs an
# explicit reducer. The second Annotated arg is a docstring upstream and a
# reducer here — LangGraph treats it as a reducer when callable, so upstream's
# descriptions are kept as trailing comments rather than discarded.


def _last_str(a: str, b: str) -> str:
    """Reducer: keep latest non-empty string value."""
    return b if b else a


def _last_invest_state(a, b):
    """Reducer: keep the InvestDebateState with more progress (higher count)."""
    if isinstance(b, dict) and b.get("count", 0) >= (a.get("count", 0) if isinstance(a, dict) else 0):
        return b
    return a


def _last_risk_state(a, b):
    """Reducer: keep the RiskDebateState with more progress (higher count)."""
    if isinstance(b, dict) and b.get("count", 0) >= (a.get("count", 0) if isinstance(a, dict) else 0):
        return b
    return a



# Researcher team state
class InvestDebateState(TypedDict):
    bull_history: Annotated[
        str, "Bullish Conversation history"
    ]  # Bullish Conversation history
    bear_history: Annotated[
        str, "Bearish Conversation history"
    ]  # Bullish Conversation history
    history: Annotated[str, "Conversation history"]  # Conversation history
    current_response: Annotated[str, "Latest response"]  # Last response
    judge_decision: Annotated[str, "Final judge decision"]  # Last response
    count: Annotated[int, "Length of the current conversation"]  # Conversation length


# Risk management team state
class RiskDebateState(TypedDict):
    aggressive_history: Annotated[
        str, "Aggressive Agent's Conversation history"
    ]  # Conversation history
    conservative_history: Annotated[
        str, "Conservative Agent's Conversation history"
    ]  # Conversation history
    neutral_history: Annotated[
        str, "Neutral Agent's Conversation history"
    ]  # Conversation history
    history: Annotated[str, "Conversation history"]  # Conversation history
    latest_speaker: Annotated[str, "Analyst that spoke last"]
    current_aggressive_response: Annotated[
        str, "Latest response by the aggressive analyst"
    ]  # Last response
    current_conservative_response: Annotated[
        str, "Latest response by the conservative analyst"
    ]  # Last response
    current_neutral_response: Annotated[
        str, "Latest response by the neutral analyst"
    ]  # Last response
    judge_decision: Annotated[str, "Judge's decision"]
    count: Annotated[int, "Length of the current conversation"]  # Conversation length


class AgentState(MessagesState):
    company_of_interest: Annotated[str, _last_str]   # company being traded
    asset_type: Annotated[str, _last_str]            # upstream: stock | crypto
    instrument_context: Annotated[str, _last_str]    # upstream: resolved ticker identity
    trade_date: Annotated[str, _last_str]

    sender: Annotated[str, _last_str]

    # research step — each analyst writes its own report during parallel fan-out
    market_report: Annotated[str, _last_str]
    sentiment_report: Annotated[str, _last_str]
    news_report: Annotated[str, _last_str]
    fundamentals_report: Annotated[str, _last_str]

    # crypto-specific analyst reports (empty string when asset_class != "crypto")
    onchain_report: Annotated[str, _last_str]
    defi_report: Annotated[str, _last_str]
    funding_report: Annotated[str, _last_str]

    # researcher team discussion step
    investment_debate_state: Annotated[InvestDebateState, _last_invest_state]
    investment_plan: Annotated[str, _last_str]

    trader_investment_plan: Annotated[str, _last_str]

    # risk management team discussion step
    risk_debate_state: Annotated[RiskDebateState, _last_risk_state]
    final_trade_decision: Annotated[str, _last_str]

    # upstream: memory log context injected at run start
    past_context: Annotated[str, _last_str]
