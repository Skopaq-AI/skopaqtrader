from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_defi_analyst,
    create_fundamentals_analyst,
    create_funding_analyst,
    create_market_analyst,
    create_msg_delete,
    create_neutral_debator,
    create_news_analyst,
    create_onchain_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.state import AgentState

from .analyst_execution import build_analyst_execution_plan
from .conditional_logic import ConditionalLogic

# Every target a shared conditional router can return. Each edge driven by the
# router maps all of them, so a fall-through return (e.g. under prompt/i18n/
# refactor drift in the speaker labels) can never hit a missing path_map entry
# and crash LangGraph mid-run (#1088).
DEBATE_PATH_MAP = {
    "Bull Researcher": "Bull Researcher",
    "Bear Researcher": "Bear Researcher",
    "Research Manager": "Research Manager",
}
RISK_ANALYSIS_PATH_MAP = {
    "Aggressive Analyst": "Aggressive Analyst",
    "Conservative Analyst": "Conservative Analyst",
    "Neutral Analyst": "Neutral Analyst",
    "Portfolio Manager": "Portfolio Manager",
}


def _tools_or_clear(spec):
    """Route an analyst's turn: run its tool calls, or finish its report."""
    def route(state) -> str:
        return spec.tool_node if state["messages"][-1].tool_calls else spec.clear_node
    return route


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        conditional_logic: ConditionalLogic,
        llm_map: dict[str, Any] | None = None,
    ):
        """Initialize with required components.

        ``llm_map`` (Skopaq) optionally assigns an LLM per agent role, e.g.
        ``{"market_analyst": gemini, "portfolio_manager": claude}``. A role
        missing from the map uses ``_default``, then the quick/deep pair.
        """
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.conditional_logic = conditional_logic
        self.llm_map = llm_map or {}

    def _get_llm(self, *roles: str, deep: bool = False):
        """The LLM for the first of ``roles`` in ``llm_map``, else the default."""
        for role in roles:
            if role in self.llm_map:
                return self.llm_map[role]
        if "_default" in self.llm_map:
            return self.llm_map["_default"]
        return self.deep_thinking_llm if deep else self.quick_thinking_llm

    def setup_graph(
        self, selected_analysts=("market", "social", "news", "fundamentals")
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include. Options are:
                - "market": Market analyst
                - "social": Sentiment analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
                - "onchain" / "defi" / "funding": crypto analysts (Skopaq)
        """
        plan = build_analyst_execution_plan(selected_analysts)

        llm = self._get_llm
        analyst_factories = {
            "market": lambda: create_market_analyst(llm("market_analyst")),
            "social": lambda: create_sentiment_analyst(llm("sentiment_analyst", "social_analyst")),
            "news": lambda: create_news_analyst(llm("news_analyst")),
            "fundamentals": lambda: create_fundamentals_analyst(llm("fundamentals_analyst")),
            # Skopaq: crypto-specific analysts
            "onchain": lambda: create_onchain_analyst(llm("onchain_analyst")),
            "defi": lambda: create_defi_analyst(llm("defi_analyst")),
            "funding": lambda: create_funding_analyst(llm("funding_analyst")),
        }

        bull_researcher_node = create_bull_researcher(llm("bull_researcher"))
        bear_researcher_node = create_bear_researcher(llm("bear_researcher"))
        research_manager_node = create_research_manager(llm("research_manager", deep=True))
        trader_node = create_trader(llm("trader"))

        aggressive_analyst = create_aggressive_debator(llm("aggressive_debator"))
        neutral_analyst = create_neutral_debator(llm("neutral_debator"))
        conservative_analyst = create_conservative_debator(llm("conservative_debator"))
        # "risk_manager" is the role's name before upstream renamed it (v0.2.2).
        portfolio_manager_node = create_portfolio_manager(
            llm("portfolio_manager", "risk_manager", deep=True)
        )

        workflow = StateGraph(AgentState)

        for spec in plan.specs:
            workflow.add_node(spec.agent_node, analyst_factories[spec.key]())
            workflow.add_node(spec.clear_node, create_msg_delete())
            if spec.tools:
                workflow.add_node(spec.tool_node, ToolNode(list(spec.tools)))

        workflow.add_node("Bull Researcher", bull_researcher_node)
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        workflow.add_edge(START, plan.specs[0].agent_node)

        for i, spec in enumerate(plan.specs):
            if spec.tools:
                workflow.add_conditional_edges(
                    spec.agent_node, _tools_or_clear(spec), [spec.tool_node, spec.clear_node]
                )
                workflow.add_edge(spec.tool_node, spec.agent_node)
            else:
                workflow.add_edge(spec.agent_node, spec.clear_node)

            # The last analyst hands over to the research debate.
            following = plan.specs[i + 1].agent_node if i < len(plan.specs) - 1 else "Bull Researcher"
            workflow.add_edge(spec.clear_node, following)

        # Both research-debate edges share the complete DEBATE_PATH_MAP (#1088).
        for debate_node in ("Bull Researcher", "Bear Researcher"):
            workflow.add_conditional_edges(
                debate_node,
                self.conditional_logic.should_continue_debate,
                DEBATE_PATH_MAP,
            )
        workflow.add_edge("Research Manager", "Trader")
        workflow.add_edge("Trader", "Aggressive Analyst")
        # All three risk edges share the complete RISK_ANALYSIS_PATH_MAP (#1088).
        for risk_node in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
            workflow.add_conditional_edges(
                risk_node,
                self.conditional_logic.should_continue_risk_analysis,
                RISK_ANALYSIS_PATH_MAP,
            )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
