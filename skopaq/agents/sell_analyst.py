"""AI-powered sell analyst — evaluates whether to exit an open position.

This is a lightweight, single-agent LLM call (NOT the full multi-agent graph).
It uses Gemini 3 Flash to analyze technicals and price action, then recommends
SELL or HOLD with confidence and reasoning.

Usage::

    from skopaq.agents.sell_analyst import analyze_exit, SellDecision

    decision = await analyze_exit(
        llm=llm,
        symbol="BHARTIARTL",
        entry_price=1899.40,
        current_price=1920.00,
        quantity=1,
        position_pnl_pct=1.08,
        trade_date="2026-03-04",
    )
    if decision.action == "SELL":
        ...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.messages import HumanMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from skopaq.llm import extract_text

logger = logging.getLogger(__name__)

# Re-use upstream tool definitions — they handle vendor routing automatically
from tradingagents.agents.tools import get_indicators, get_stock_data


@dataclass
class SellDecision:
    """Result from the AI sell analyst."""

    action: Literal["SELL", "HOLD"] = "HOLD"
    confidence: int = 50  # 0-100
    reasoning: str = ""


_SYSTEM_PROMPT = """\
You are a position exit analyst for an Indian stock trading system.
You are monitoring an OPEN BUY position and must decide: SELL now or HOLD.

POSITION DETAILS:
- Symbol: {symbol}
- Entry Price: ₹{entry_price:.2f}
- Current Price: ₹{current_price:.2f}
- Quantity: {quantity}
- Unrealised P&L: {pnl_pct:+.2f}%
- Trade Date: {trade_date}

INSTRUCTIONS:
1. Use get_stock_data to fetch recent price history (last 5 trading days).
2. Use get_indicators to check RSI, MACD, and Bollinger Bands for the symbol.
3. Analyze whether the position should be SOLD or HELD based on:
   - Momentum reversal signals (RSI divergence, MACD crossover)
   - Price action relative to Bollinger Bands
   - Intraday trend direction
   - Current P&L (protect gains, cut losses)
4. Output your decision in EXACTLY this format at the END of your response:

DECISION: SELL or HOLD
CONFIDENCE: <number 0-100>
REASONING: <1-2 sentence summary>

MINIMUM PROFIT RULE:
- Do NOT recommend SELL for profit-taking if unrealised P&L is below {min_profit_pct:.1f}%
- Estimated round-trip brokerage: ₹{est_brokerage:.0f}
- Only recommend SELL for gains if net profit (after brokerage) justifies the exit
- This rule does NOT apply to cutting losses — always cut losses promptly

IMPORTANT: Be decisive. If technical signals are mixed but P&L is positive,
lean towards protecting gains. If P&L is negative and momentum is fading,
recommend SELL to cut losses early.

Available tools: {tool_names}
Current date: {trade_date}
Symbol for tool calls: {yf_symbol}
"""


async def analyze_exit(
    llm,
    symbol: str,
    entry_price: float,
    current_price: float,
    quantity: int,
    position_pnl_pct: float,
    trade_date: str,
    min_profit_threshold_pct: float = 0.0,
    estimated_round_trip_brokerage: float = 0.0,
) -> SellDecision:
    """Run a single-shot LLM analysis to decide whether to exit a position.

    This function calls the LLM with tool-binding so it can optionally fetch
    technical indicators before making a decision.  Falls back to HOLD on any
    error (safe default — the safety tier handles critical exits).

    Args:
        llm: LangChain ``BaseChatModel`` instance (sell_analyst role).
        symbol: Trading symbol (e.g. "BHARTIARTL").
        entry_price: Position entry price.
        current_price: Latest traded price.
        quantity: Number of shares held.
        position_pnl_pct: Unrealised P&L as percentage.
        trade_date: Current date in YYYY-MM-DD format.
        min_profit_threshold_pct: Minimum P&L% for profit-taking sells.
        estimated_round_trip_brokerage: Estimated total brokerage for buy+sell.

    Returns:
        SellDecision with action, confidence, and reasoning.
    """
    try:
        return await _invoke_sell_analyst(
            llm, symbol, entry_price, current_price,
            quantity, position_pnl_pct, trade_date,
            min_profit_threshold_pct, estimated_round_trip_brokerage,
        )
    except Exception:
        logger.warning(
            "Sell analyst failed for %s — defaulting to HOLD",
            symbol, exc_info=True,
        )
        return SellDecision(
            action="HOLD", confidence=0,
            reasoning="AI analysis unavailable — defaulting to HOLD",
        )


async def _invoke_sell_analyst(
    llm,
    symbol: str,
    entry_price: float,
    current_price: float,
    quantity: int,
    position_pnl_pct: float,
    trade_date: str,
    min_profit_threshold_pct: float = 0.0,
    estimated_round_trip_brokerage: float = 0.0,
) -> SellDecision:
    """Internal: invoke the LLM chain and parse the structured response."""
    tools = [get_stock_data, get_indicators]

    # yfinance needs .NS suffix for Indian equities
    yf_symbol = f"{symbol}.NS" if not any(
        symbol.endswith(s) for s in (".NS", ".BO", "-USD", "USDT")
    ) else symbol

    prompt = ChatPromptTemplate.from_messages([
        ("system", _SYSTEM_PROMPT),
        MessagesPlaceholder(variable_name="messages"),
    ])

    prompt = prompt.partial(
        symbol=symbol,
        entry_price=entry_price,
        current_price=current_price,
        quantity=quantity,
        pnl_pct=position_pnl_pct,
        trade_date=trade_date,
        yf_symbol=yf_symbol,
        tool_names=", ".join(t.name for t in tools),
        min_profit_pct=min_profit_threshold_pct,
        est_brokerage=estimated_round_trip_brokerage,
    )

    chain = prompt | llm.bind_tools(tools)

    # Single-shot: send one human message with the position context
    messages = [
        HumanMessage(
            content=(
                f"Analyze my open position in {symbol}. "
                f"I bought at ₹{entry_price:.2f}, current price is ₹{current_price:.2f} "
                f"({position_pnl_pct:+.2f}%). Should I SELL or HOLD?"
            )
        )
    ]

    # Invoke — may trigger tool calls; if so, we need to execute them
    result = chain.invoke({"messages": messages})

    # If the LLM requested tool calls, execute them and re-invoke
    max_tool_rounds = 3
    for _ in range(max_tool_rounds):
        if not result.tool_calls:
            break

        # Execute tool calls and collect results
        tool_map = {t.name: t for t in tools}
        tool_messages = [result]

        for tool_call in result.tool_calls:
            tool_fn = tool_map.get(tool_call["name"])
            if tool_fn is None:
                continue
            try:
                tool_result = tool_fn.invoke(tool_call["args"])
            except Exception as exc:
                tool_result = f"Tool error: {exc}"

            from langchain_core.messages import ToolMessage
            tool_messages.append(
                ToolMessage(content=str(tool_result), tool_call_id=tool_call["id"])
            )

        messages.extend(tool_messages)
        result = chain.invoke({"messages": messages})

    # Parse the final text response
    content = extract_text(result.content) if result.content else ""
    return _parse_decision(content)


def _parse_decision(text: str) -> SellDecision:
    """Parse the structured DECISION/CONFIDENCE/REASONING block from LLM output."""
    decision = SellDecision()

    # Extract DECISION
    m = re.search(r"DECISION:\s*(SELL|HOLD)", text, re.IGNORECASE)
    if m:
        decision.action = m.group(1).upper()  # type: ignore[assignment]

    # Extract CONFIDENCE
    m = re.search(r"CONFIDENCE:\s*(\d+)", text, re.IGNORECASE)
    if m:
        decision.confidence = min(100, max(0, int(m.group(1))))

    # Extract REASONING
    m = re.search(r"REASONING:\s*(.+?)(?:\n|$)", text, re.IGNORECASE | re.DOTALL)
    if m:
        decision.reasoning = m.group(1).strip()[:500]

    # If we couldn't parse a clear decision, keep HOLD as safe default
    if not re.search(r"DECISION:\s*(SELL|HOLD)", text, re.IGNORECASE):
        # Fallback: look for SELL/HOLD anywhere in text
        if "SELL" in text.upper() and "HOLD" not in text.upper():
            decision.action = "SELL"
        decision.reasoning = decision.reasoning or text[:200]

    return decision
