"""DeFi / Tokenomics Analyst — TVL, supply dynamics, protocol metrics.

Follows the same factory pattern as market_analyst.py:
  create_defi_analyst(llm) → node function (state → dict)
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.context import get_instrument_context_from_state, get_language_instruction
from tradingagents.agents.crypto_tools import (
    get_token_fundamentals,
    get_defi_tvl,
    get_chain_tvl_overview,
)

# The tools this analyst is offered; its tool node is built from the same tuple.
TOOLS = (
    get_token_fundamentals,
    get_defi_tvl,
    get_chain_tvl_overview,
)


def create_defi_analyst(llm):
    """Factory: returns a LangGraph node function for DeFi/tokenomics analysis."""

    def defi_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        system_message = (
            "You are an expert DeFi and tokenomics analyst. Your role is to analyze "
            "token supply dynamics, DeFi ecosystem health, and fundamental valuation "
            "metrics for cryptocurrencies.\n\n"
            "Key areas of analysis:\n"
            "1. **Supply Dynamics**: Circulating vs total vs max supply.\n"
            "   - FDV/MCap ratio > 2x = significant future dilution risk.\n"
            "   - Low circulating/max supply ratio = tokens still to be released.\n"
            "   - Deflationary tokenomics (burning) = supply reduction.\n"
            "2. **Market Cap & Valuation**: Market cap rank, price vs ATH/ATL.\n"
            "   - Price far below ATH = potential value or continued decline.\n"
            "   - Market cap rank changes = shifting market dynamics.\n"
            "3. **DeFi TVL (Total Value Locked)**: Capital committed to DeFi protocols.\n"
            "   - Rising TVL = growing ecosystem confidence and utility.\n"
            "   - Falling TVL = capital flight, reduced trust, or yield compression.\n"
            "4. **Chain TVL Comparison**: Where capital is flowing across ecosystems.\n"
            "   - Capital migrating to a chain = growing ecosystem.\n"
            "   - TVL concentration = ecosystem maturity.\n\n"
            "Start by calling get_token_fundamentals for supply/valuation data. "
            "Then call get_defi_tvl and/or get_chain_tvl_overview for ecosystem health. "
            "Synthesize into a detailed tokenomics and DeFi report.\n\n"
            "Write a detailed, nuanced report. Append a Markdown table summarizing "
            "key tokenomics metrics and their investment implications."
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
            "defi_report": report,
        }

    return defi_analyst_node
