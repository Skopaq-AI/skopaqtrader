"""Funding Rate / Derivatives Analyst — funding rates, open interest, long/short ratios.

Follows the same factory pattern as market_analyst.py:
  create_funding_analyst(llm) → node function (state → dict)
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.crypto_tools import (
    get_funding_rates,
    get_open_interest,
    get_long_short_ratio,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_funding_rates,
    get_open_interest,
    get_long_short_ratio,
)


def create_funding_analyst(llm):
    """Factory: returns a LangGraph node function for derivatives/funding analysis."""

    def funding_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        system_message = (
            "You are an expert cryptocurrency derivatives and funding rate analyst. "
            "Your role is to analyze futures market data to assess market sentiment, "
            "positioning, and potential contrarian signals.\n\n"
            "Key areas of analysis:\n"
            "1. **Funding Rates**: Periodic payments between longs and shorts in perpetual futures.\n"
            "   - Positive funding → longs pay shorts → market is bullish/overleveraged long.\n"
            "   - Negative funding → shorts pay longs → market is bearish/overleveraged short.\n"
            "   - Extremely positive (>0.1%) = potential long squeeze risk.\n"
            "   - Extremely negative (<-0.1%) = potential short squeeze opportunity.\n"
            "   - Annualized rate context: 0.01% per 8h ≈ 10.95% annually.\n"
            "2. **Open Interest (OI)**: Total outstanding futures contracts.\n"
            "   - Rising OI + rising price = strong bullish trend (new money entering longs).\n"
            "   - Rising OI + falling price = bearish pressure (new shorts opening).\n"
            "   - Falling OI + rising price = short covering rally (less conviction).\n"
            "   - Falling OI + falling price = long liquidation (capitulation).\n"
            "3. **Long/Short Ratio**: Proportion of accounts net long vs short.\n"
            "   - Ratio > 1.5 = crowded long → contrarian bearish signal.\n"
            "   - Ratio < 0.67 = crowded short → contrarian bullish signal.\n"
            "   - Extreme ratios often precede liquidation cascades.\n\n"
            "Start by calling get_funding_rates for rate trend analysis, then "
            "get_open_interest for positioning conviction, and get_long_short_ratio "
            "for crowd sentiment. Synthesize into a derivatives sentiment report.\n\n"
            "Write a detailed, nuanced report. Append a Markdown table summarizing "
            "key derivatives metrics and their bullish/bearish/contrarian implications."
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " Report what your tools support; another agent decides the trade."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([t.name for t in TOOLS]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(TOOLS)

        result = chain.invoke(state["messages"])

        report = ""
        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "funding_report": report,
        }

    return funding_analyst_node
