"""Chat session state — shared infrastructure and conversation history.

Each ``ChatSession`` owns its own subsystem instances (paper engine, order
router, safety checker, executor) so that the chatbot's state is independent
of any concurrent CLI commands.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional
from uuid import uuid4

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

    from skopaq.broker.paper_engine import PaperEngine
    from skopaq.config import SkopaqConfig
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter
    from skopaq.execution.safety_checker import SafetyChecker
    from skopaq.risk.position_sizer import PositionSizer

logger = logging.getLogger(__name__)


@dataclass
class Infrastructure:
    """Shared subsystem instances, initialised once per session."""

    config: SkopaqConfig
    paper_engine: PaperEngine
    order_router: OrderRouter
    safety_checker: SafetyChecker
    executor: Executor
    position_sizer: Optional[PositionSizer]
    llm_map: dict[str, Any]
    upstream_config: dict[str, Any] = field(default_factory=dict)


def build_infrastructure(config: SkopaqConfig) -> Infrastructure:
    """Wire up all subsystem objects from a SkopaqConfig.

    Mirrors the wiring in ``cli/main.py:_run_trade()`` but stores them
    for reuse across multiple tool calls within the same chat session.
    """
    from skopaq.broker.paper_engine import PaperEngine
    from skopaq.constants import (
        CRYPTO_PAPER_SAFETY_RULES,
        CRYPTO_SAFETY_RULES,
        PAPER_SAFETY_RULES,
        SAFETY_RULES,
    )
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter
    from skopaq.execution.safety_checker import SafetyChecker
    from skopaq.llm import bridge_env_vars, build_llm_map

    # Bridge SKOPAQ_ → standard env vars
    bridge_env_vars(config)

    is_crypto = config.asset_class == "crypto"

    # Paper engine
    if is_crypto:
        paper = PaperEngine(
            initial_capital=config.initial_paper_capital,
            brokerage_pct=0.001,
            currency_label="USDT",
        )
    else:
        paper = PaperEngine(initial_capital=config.initial_paper_capital)

    # Safety rules
    if is_crypto:
        rules = (
            CRYPTO_PAPER_SAFETY_RULES
            if config.trading_mode == "paper"
            else CRYPTO_SAFETY_RULES
        )
    else:
        rules = (
            PAPER_SAFETY_RULES
            if config.trading_mode == "paper"
            else SAFETY_RULES
        )

    # Wire live broker when in live mode (gracefully skip if unavailable)
    live_client = None
    if config.trading_mode == "live":
        try:
            from skopaq.broker.client import INDstocksClient
            from skopaq.broker.token_manager import TokenManager

            token_mgr = TokenManager()
            # Only create if token is available
            health = token_mgr.get_health()
            if health.valid:
                live_client = INDstocksClient(config, token_mgr)
            else:
                logger.info("INDstocks token unavailable — using Kite/paper fallback")
        except Exception:
            logger.info("INDstocks client init skipped — using Kite/paper fallback")

    router = OrderRouter(config, paper, live_client=live_client)
    safety = SafetyChecker(
        rules=rules,
        max_sector_concentration_pct=config.max_sector_concentration_pct,
    )

    # Position sizer
    sizer = None
    if config.position_sizing_enabled:
        from skopaq.risk.position_sizer import PositionSizer

        sizer = PositionSizer(
            risk_per_trade_pct=config.risk_per_trade_pct,
            atr_multiplier=config.atr_multiplier,
            atr_period=config.atr_period,
        )

    executor = Executor(router, safety, position_sizer=sizer)

    # LLM map (per-role model assignment)
    llm_map = build_llm_map()

    # Upstream config for TradingAgentsGraph
    upstream_config = _build_upstream_config(config, llm_map)

    return Infrastructure(
        config=config,
        paper_engine=paper,
        order_router=router,
        safety_checker=safety,
        executor=executor,
        position_sizer=sizer,
        llm_map=llm_map,
        upstream_config=upstream_config,
    )


def _build_upstream_config(config: SkopaqConfig, llm_map: dict) -> dict:
    """Build config dict for upstream TradingAgentsGraph."""
    from pathlib import Path

    project_dir = str(Path.cwd())
    is_crypto = config.asset_class == "crypto"

    upstream = {
        "results_dir": str(Path(project_dir) / "results"),
        "data_cache_dir": str(Path(project_dir) / ".cache" / "data"),
        "llm_provider": "google",
        "deep_think_llm": "gemini-3-flash-preview",
        "quick_think_llm": "gemini-3-flash-preview",
        "backend_url": None,
        "max_debate_rounds": config.max_debate_rounds,
        "max_risk_discuss_rounds": config.max_risk_discuss_rounds,
        "google_thinking_level": config.google_thinking_level or None,
        "max_recur_limit": 100,
        "asset_class": config.asset_class,
        "data_vendors": {
            "core_stock_apis": "yfinance" if is_crypto else "indstocks,yfinance",
            "technical_indicators": "yfinance",
            "fundamental_data": "yfinance",
            "news_data": "yfinance",
        },
        "yfinance_symbol_suffix": "" if is_crypto else ".NS",
        "llm_map": llm_map,
    }

    # Activate semantic LLM cache
    from skopaq.llm.cache import init_langcache

    cache = init_langcache(config)
    if cache:
        from langchain_core.globals import set_llm_cache

        set_llm_cache(cache)
        logger.info("Semantic cache enabled")

    return upstream


class ChatSession:
    """Manages conversation history and shared infrastructure for a chat session."""

    def __init__(self, config: SkopaqConfig) -> None:
        self.id = str(uuid4())
        self.config = config
        self.messages: list[BaseMessage] = []
        self.infra: Optional[Infrastructure] = None
        self.agent: Optional[CompiledStateGraph] = None
        self.checkpointer: Any = None  # MemorySaver for state persistence
        self.created_at = datetime.now(timezone.utc)

    @property
    def thread_config(self) -> dict:
        """LangGraph config with thread_id for checkpoint isolation."""
        return {"configurable": {"thread_id": self.id}}

    def ensure_infra(self) -> Infrastructure:
        """Lazily build infrastructure on first use."""
        if self.infra is None:
            self.infra = build_infrastructure(self.config)
        return self.infra

    def ensure_agent(self) -> CompiledStateGraph:
        """Lazily build the ReAct agent on first use."""
        if self.agent is None:
            from skopaq.chat.agent import create_chat_agent

            infra = self.ensure_infra()
            memory_context = self._load_memory_context()
            self.agent, self.checkpointer = create_chat_agent(
                infra, memory_context=memory_context
            )
        return self.agent

    def _load_memory_context(self) -> str:
        """Load recent trade reflections from Supabase for the system prompt."""
        config = self.config
        if not config.supabase_url or not config.supabase_service_key.get_secret_value():
            return ""

        try:
            from supabase import create_client

            from skopaq.memory.reflection import (
                format_lessons_for_prompt,
                load_recent_lessons,
            )

            client = create_client(
                config.supabase_url,
                config.supabase_service_key.get_secret_value(),
            )
            lessons = load_recent_lessons(client, limit=10)
            return format_lessons_for_prompt(lessons)
        except Exception:
            logger.debug("Could not load memory context", exc_info=True)
            return ""

    def add_user_message(self, text: str) -> None:
        self.messages.append(HumanMessage(content=text))

    def add_ai_message(self, text: str) -> None:
        self.messages.append(AIMessage(content=text))

    def get_history(self) -> list[BaseMessage]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()
