# Dual-Mode Analysis

SkopaqTrader supports two distinct analysis modes. Understanding when to use each is key to getting the best results.

## The Two Modes

### 1. API Mode (Multi-LLM Pipeline)

The traditional pipeline calls multiple LLM providers to run a LangGraph multi-agent system.

```
MCP tool: analyze_stock("RELIANCE")
                │
    ┌───────────┼───────────┐
    ▼           ▼           ▼
 Gemini 3.8  Grok 4.6    Claude Opus
 (analysts)  (social)    (risk mgr)
    │           │           │
    └───────────┼───────────┘
                ▼
         Trade Signal
```

**Key characteristics:**

| Property | Value |
|----------|-------|
| MCP tool | `analyze_stock` |
| Duration | 2-5 minutes |
| LLM calls | 12-15 separate API calls |
| Providers | Gemini, Grok, Claude, Perplexity |
| Cost | Higher (multiple provider fees) |
| Quality | Each agent uses its specialized model |

**When to use:**

- Production trading decisions where cost is secondary
- When you want genuine multi-perspective analysis from different model architectures
- Autonomous daemon mode (no human in the loop)

### 2. Claude-Native Mode (Slash Commands)

Claude itself plays all 15 agent roles, using its own reasoning over raw data fetched via MCP tools.

```
Slash command: /analyze RELIANCE
                │
                ▼
    gather_all_analysis_data (MCP)
                │
                ▼
     Claude plays 15 roles:
     ├── Market Analyst
     ├── Social Analyst
     ├── News Analyst
     ├── Fundamentals Analyst
     ├── Bull Researcher
     ├── Bear Researcher
     ├── Research Manager
     ├── Trader
     ├── Aggressive Risk Analyst
     ├── Neutral Risk Analyst
     ├── Conservative Risk Analyst
     └── Risk Manager
                │
                ▼
         Trade Signal
```

**Key characteristics:**

| Property | Value |
|----------|-------|
| Slash command | `/analyze` |
| Duration | 1-3 minutes |
| LLM calls | 1 (Claude does everything) |
| Providers | Claude only (data from MCP) |
| Cost | Lower (single Claude call) |
| Quality | Consistent reasoning across all roles |

**When to use:**

- Interactive analysis during a Claude Code session
- Quick iteration on multiple stocks
- When you want to follow Claude's reasoning step by step
- Learning and education (you see each phase unfold)

## Comparison

| Feature | API Mode | Claude-Native Mode |
|---------|----------|-------------------|
| Trigger | `analyze_stock` MCP tool | `/analyze` slash command |
| Speed | 2-5 min | 1-3 min |
| Cost | $$$ (multi-provider) | $ (single Claude call) |
| Diversity | Real multi-model opinions | Simulated perspectives |
| Transparency | Black box (returns summary) | Full reasoning visible |
| Data source | Pipeline fetches its own data | MCP gather tools |
| Memory | Uses agent memories | Uses agent memories |
| Works offline | No | Partially (with Ollama data) |

## Mixing Modes

You can use both modes in a single session:

1. Start with `/scan` (Claude-native) to find candidates
2. Run `/analyze SYMBOL` (Claude-native) for quick assessment
3. Use `analyze_stock` (API mode) for the final decision on top picks
4. Execute with `/trade SYMBOL` (Claude-native) for the actual order

!!! tip "Data gathering is shared"
    Both modes use the same underlying data sources (INDstocks, Kite Connect, yfinance). The difference is only in who reasons over the data -- multiple specialized LLMs or a single Claude instance.

## Configuration

The API mode uses the LLM tier configuration in `skopaq/llm/model_tier.py`:

```python
# Each role has a fallback chain
"market_analyst":       [("google", "gemini-3.8-flash"), ("ollama", "auto")]
"social_analyst":       [("openrouter", "x-ai/grok-4.6"), ...]
"research_manager":     [("anthropic", "claude-opus-5"), ...]
```

Claude-native mode requires no LLM configuration -- it uses Claude Code's own model. You only need valid broker credentials for data access.

## Choosing a Mode

```
Need a quick check?           → /quote SYMBOL
Want interactive analysis?    → /analyze SYMBOL (Claude-native)
Making a real trade decision? → analyze_stock (API mode)
Automated trading?            → daemon uses API mode exclusively
```
