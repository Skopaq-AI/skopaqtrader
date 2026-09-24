# Equity Analysis

SkopaqTrader uses a 15-agent pipeline to produce BUY/SELL/HOLD recommendations. This page explains how the pipeline works and how to run an analysis.

## The 15-Agent Pipeline

The pipeline is structured in 7 phases. Each agent has a specific role and perspective.

### Phase 1: Analyst Reports (4 agents)

| Agent | Provider | What It Analyzes |
|-------|----------|-----------------|
| Market Analyst | Gemini 3.8 Flash | OHLCV data, RSI, MACD, Bollinger Bands, SMA, EMA, ATR, VWMA |
| Social Analyst | Grok 4.6 (OpenRouter) | Social media sentiment, public discussion |
| News Analyst | Gemini 3.8 Flash | Company news, global macro, insider transactions |
| Fundamentals Analyst | Gemini 3.8 Flash | Balance sheet, cash flow, income statement, company profile |

### Phase 2: Investment Debate (2 agents)

| Agent | Role |
|-------|------|
| Bull Researcher | Builds evidence-based case FOR investing |
| Bear Researcher | Builds evidence-based case AGAINST investing |

Each researcher references all four analyst reports and counters the other's arguments.

### Phase 3: Research Manager (1 agent)

The **Research Manager** (Claude Opus 5) acts as judge. It evaluates the bull/bear debate and makes a decisive BUY/SELL/HOLD recommendation. This role uses the strongest reasoning model because the decision quality matters most here.

### Phase 4: Trader (1 agent)

Translates the research manager's investment plan into a concrete trade proposal with entry, stop-loss, and target prices.

### Phase 5: Risk Debate (3 agents)

| Agent | Perspective |
|-------|-------------|
| Aggressive Risk Analyst | Champions high-reward opportunities |
| Conservative Risk Analyst | Emphasizes asset protection and stability |
| Neutral Risk Analyst | Balances both views, challenges extremes |

### Phase 6: Risk Manager (1 agent)

The **Risk Manager** (Claude Opus 5) evaluates the three-way risk debate and produces the final verdict. It outputs:

- Clear BUY/SELL/HOLD recommendation
- Confidence score (0-100)
- Refined trading plan with adjusted parameters

### Phase 7: Signal Output

The pipeline produces a `TradeSignal` with:

```
action:      BUY | SELL | HOLD
confidence:  0-100
entry_price: recommended entry
stop_loss:   protective stop
target:      profit target
reasoning:   summary of why
```

## Running an Analysis

### Via Claude Code

```
/analyze RELIANCE
```

This runs the Claude-native version where Claude plays all 15 roles.

### Via CLI

```bash
skopaq analyze RELIANCE
```

This runs the full multi-LLM pipeline (API mode).

### Via MCP Tool

Ask Claude:

> Run a full analysis on TCS

Claude calls `mcp__skopaq__analyze_stock(symbol="TCS")` which invokes the multi-LLM pipeline.

### Via Python

```python
from skopaq.graph.skopaq_graph import SkopaqTradingGraph
from skopaq.llm import build_llm_map

llm_map = build_llm_map()
graph = SkopaqTradingGraph(upstream_config, executor, selected_analysts=analysts)
result = await graph.analyze("RELIANCE", "2026-04-06")
```

## Pipeline Flow Diagram

```
                    ┌─────────────┐
                    │  Raw Data   │
                    │ (INDstocks, │
                    │  yfinance)  │
                    └──────┬──────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │  Market  │ │  News    │ │ Social/  │
        │ Analyst  │ │ Analyst  │ │ Fundmntl │
        └────┬─────┘ └────┬─────┘ └────┬─────┘
             └─────────────┼────────────┘
                           ▼
              ┌────────────┼────────────┐
              ▼                         ▼
        ┌──────────┐           ┌──────────┐
        │   Bull   │ ◄──────► │   Bear   │
        │Researcher│   debate  │Researcher│
        └────┬─────┘           └────┬─────┘
             └─────────┬────────────┘
                       ▼
              ┌─────────────────┐
              │ Research Manager │ (Judge)
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
              │     Trader      │
              └────────┬────────┘
                       ▼
         ┌─────────────┼─────────────┐
         ▼             ▼             ▼
   ┌──────────┐ ┌──────────┐ ┌──────────┐
   │Aggressive│ │ Neutral  │ │Conserv.  │
   │   Risk   │ │   Risk   │ │  Risk    │
   └────┬─────┘ └────┬─────┘ └────┬─────┘
        └─────────────┼────────────┘
                      ▼
             ┌─────────────────┐
             │  Risk Manager   │ (Final Judge)
             └────────┬────────┘
                      ▼
             ┌─────────────────┐
             │  Trade Signal   │
             │ BUY/SELL/HOLD   │
             └─────────────────┘
```

## Agent Memory

Each agent role has a persistent memory backed by Supabase. Past trade outcomes are stored as lessons. When analyzing a new stock, agents recall relevant memories using BM25 similarity search to avoid repeating past mistakes.

Memory roles: `bull_memory`, `bear_memory`, `trader_memory`, `invest_judge_memory`, `risk_manager_memory`.

!!! tip "Learning from experience"
    After a trade is closed, use `save_trade_reflection` (or the MCP tool) to generate and store a lesson. This feeds back into future analyses.

## Customizing Analysts

Select which analysts to include via config:

```bash
export SKOPAQ_SELECTED_ANALYSTS="market_analyst,news_analyst,fundamentals_analyst,social_analyst"
```

Or in `.env`:

```
SKOPAQ_SELECTED_ANALYSTS=market_analyst,news_analyst,fundamentals_analyst
```
