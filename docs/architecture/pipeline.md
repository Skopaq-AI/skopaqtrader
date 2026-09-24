# Multi-Agent Pipeline

The core of SkopaqTrader is a 15-agent LangGraph pipeline that produces trading decisions through structured debate and multi-perspective analysis. This page describes the architecture in detail.

## Overview

The pipeline is built on the vendored [TradingAgents v0.2.0](https://github.com/TauricResearch/TradingAgents) framework (Apache 2.0), extended by `skopaq/graph/skopaq_graph.py`.

```
Raw Data → 4 Analysts → Bull/Bear Debate → Research Manager
    → Trader → 3-Way Risk Debate → Risk Manager → Trade Signal
```

Total agents: 15. Total LLM calls: 12-15 (some agents use the same model).

## LangGraph State Machine

The pipeline is a LangGraph directed graph where each node is an agent. State flows forward through the graph, accumulating analyst reports, debate arguments, and decisions.

```python
# Simplified graph structure
graph = StateGraph(AnalysisState)
graph.add_node("market_analyst", market_analyst_fn)
graph.add_node("social_analyst", social_analyst_fn)
graph.add_node("news_analyst", news_analyst_fn)
graph.add_node("fundamentals_analyst", fundamentals_analyst_fn)
graph.add_node("bull_researcher", bull_fn)
graph.add_node("bear_researcher", bear_fn)
graph.add_node("research_manager", judge_fn)
graph.add_node("trader", trader_fn)
graph.add_node("aggressive_debator", agg_fn)
graph.add_node("conservative_debator", cons_fn)
graph.add_node("neutral_debator", neut_fn)
graph.add_node("risk_manager", risk_judge_fn)
```

## Phase Details

### Phase 1: Data Gathering

Before agents run, raw data is fetched via the dataflow layer:

| Data Type | Source | Module |
|-----------|--------|--------|
| OHLCV prices | INDstocks / yfinance | `tradingagents/dataflows/` |
| Technical indicators | Computed (RSI, MACD, etc.) | `tradingagents/dataflows/` |
| Company news | News APIs | `tradingagents/dataflows/` |
| Insider transactions | Financial APIs | `tradingagents/dataflows/` |
| Social sentiment | News/social APIs | `tradingagents/dataflows/` |
| Fundamentals | yfinance | `tradingagents/dataflows/` |

### Phase 2: Analyst Reports

Four analysts run concurrently, each producing a detailed report:

**Market Analyst** -- Selects 8 most relevant technical indicators, provides fine-grained trend analysis (not just "mixed"), appends a summary table.

**Social Analyst** -- Analyzes social media posts, company news, public sentiment. Reports implications for traders.

**News Analyst** -- Covers company-specific news, global macro trends, and insider transactions.

**Fundamentals Analyst** -- Deep dive into balance sheet, cash flow, income statement, and company profile.

### Phase 3: Bull/Bear Debate

Two researchers take opposing positions:

- **Bull Researcher**: Growth potential, competitive advantages, positive indicators
- **Bear Researcher**: Risks, challenges, negative indicators, counterpoints to bull

The debate runs for `max_debate_rounds` rounds (configurable, default 1).

### Phase 4: Research Manager

The judge role (Claude Opus 5) evaluates the debate and makes a definitive decision. It is instructed to NOT default to HOLD -- it must commit to a stance backed by the strongest arguments.

### Phase 5: Trader

Translates the research manager's recommendation into a concrete trade with entry, stop-loss, and target prices.

### Phase 6: Risk Debate

Three risk analysts with different philosophies debate the trader's plan:

- **Aggressive**: Emphasizes upside potential, questions conservative caution
- **Conservative**: Emphasizes protection, questions aggressive optimism
- **Neutral**: Balances both, challenges extremes

The debate runs for `max_risk_discuss_rounds` rounds (configurable, default 1).

### Phase 7: Risk Manager

The final judge (Claude Opus 5) produces the ultimate decision with:

- BUY/SELL/HOLD recommendation
- Confidence score (0-100)
- Refined trading plan

## Model Assignment

| Agent | Provider | Model | Why |
|-------|----------|-------|-----|
| Market Analyst | Google | gemini-3.8-flash | Fast, cost-effective for data analysis |
| Social Analyst | OpenRouter | x-ai/grok-4.6 | Strong at social sentiment |
| News Analyst | Google | gemini-3.8-flash | Handles news well |
| Fundamentals Analyst | Google | gemini-3.8-flash | Good with financial data |
| Bull/Bear Researchers | Google | gemini-3.8-flash | Fast for debate |
| Research Manager | Anthropic | claude-opus-5 | Strongest reasoning for judge role |
| Trader | Google | gemini-3.8-flash | Action-oriented |
| Risk Debaters (3) | Google | gemini-3.8-flash | Fast for multi-round debate |
| Risk Manager | Anthropic | claude-opus-5 | Strongest reasoning for final decision |

Model assignments are configured in `skopaq/llm/model_tier.py`. Each role has a fallback chain -- if the primary provider is unavailable, it falls back to the next option.

## Agent Memory

Each agent role has persistent memory backed by Supabase using BM25 similarity search. Five memory roles store lessons from past trades:

- `bull_memory` -- Lessons for the bull researcher
- `bear_memory` -- Lessons for the bear researcher
- `trader_memory` -- Lessons for the trader
- `invest_judge_memory` -- Lessons for the research manager
- `risk_manager_memory` -- Lessons for the risk manager

Before each analysis, relevant past lessons are retrieved and injected into the agent prompts.

## Entry Points

| Method | Module | Description |
|--------|--------|-------------|
| `SkopaqTradingGraph.analyze()` | `skopaq/graph/skopaq_graph.py` | Primary entry point |
| `analyze_stock` MCP tool | `skopaq/mcp_server.py` | MCP-accessible |
| `skopaq analyze` CLI | `skopaq/cli/main.py` | Command line |
| `/analyze` Telegram | `skopaq/telegram_bot.py` | Telegram bot |

## Configuration

```bash
# Number of bull/bear debate rounds
SKOPAQ_MAX_DEBATE_ROUNDS=1

# Number of risk debate rounds
SKOPAQ_MAX_RISK_DISCUSS_ROUNDS=1

# Which analysts to include
SKOPAQ_SELECTED_ANALYSTS=market_analyst,news_analyst,fundamentals_analyst,social_analyst
```

## Upstream Modifications

Changes to the vendored `tradingagents/` code are minimal and documented in `UPSTREAM_CHANGES.md`. The `skopaq/` layer wraps the upstream pipeline without modifying its core logic.
