"""On-Chain Analyst — blockchain network health, whale movements, exchange flows.

Follows the same factory pattern as market_analyst.py:
  create_onchain_analyst(llm) → node function (state → dict)
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.crypto_tools import (
    get_blockchain_stats,
    get_address_activity,
)


def create_onchain_analyst(llm):
    """Factory: returns a LangGraph node function for on-chain analysis."""

    def onchain_analyst_node(state):
        current_date = state["trade_date"]
        ticker = state["company_of_interest"]

        tools = [
            get_blockchain_stats,
            get_address_activity,
        ]

        system_message = (
            "You are an expert on-chain cryptocurrency analyst. Your role is to analyze "
            "blockchain network health, transaction patterns, and on-chain metrics to assess "
            "the fundamental strength of a cryptocurrency's network.\n\n"
            "Key areas of analysis:\n"
            "1. **Network Activity**: Transaction count, active addresses, and daily volume trends.\n"
            "   - Rising activity = growing adoption/usage.\n"
            "   - Declining activity = waning interest or consolidation.\n"
            "2. **Mining/Staking Health**: Hashrate, difficulty adjustments, and security metrics.\n"
            "   - Rising hashrate = miners are confident (bullish).\n"
            "   - Hash rate drops = miners leaving (bearish or difficulty adjustment).\n"
            "3. **Mempool & Fees**: Pending transactions and fee levels.\n"
            "   - High mempool + high fees = congestion (high demand).\n"
            "   - Low mempool + low fees = quiet network.\n"
            "4. **Whale Activity**: Large transactions and address concentration.\n"
            "   - Large outflows from exchanges = accumulation (bullish).\n"
            "   - Large inflows to exchanges = potential selling pressure.\n\n"
            "Start by calling get_blockchain_stats to get the network overview, then "
            "call get_address_activity for transaction trends. Synthesize both into a "
            "detailed on-chain health report.\n\n"
            "Write a detailed, nuanced report. Append a Markdown table summarizing "
            "key on-chain metrics and their bullish/bearish implications."
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}.\n{system_message}"
                    "For your reference, the current date is {current_date}. The cryptocurrency we are analyzing is {ticker}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([t.name for t in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(ticker=ticker)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""
        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "onchain_report": report,
        }

    return onchain_analyst_node
