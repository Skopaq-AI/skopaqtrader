# MCP Tools Reference

SkopaqTrader exposes 23 MCP tools through `skopaq/mcp_server.py`. All tools are async and return JSON strings.

## Market Data

| Tool | Description | Key Args |
|------|-------------|----------|
| `get_quote` | Real-time LTP, OHLC, bid/ask, volume, change% | `symbol` |
| `get_historical` | OHLCV candles (last 20 returned) | `symbol`, `days=5`, `resolution=1` |

**Example — get a quote:**

> What is the current price of RELIANCE?

Claude calls `mcp__skopaq__get_quote(symbol="RELIANCE")` and returns:

```json
{
  "symbol": "RELIANCE",
  "ltp": 2485.50,
  "open": 2470.00,
  "high": 2498.00,
  "low": 2465.00,
  "change_pct": 0.63
}
```

**Example — historical data:**

> Show me 15-minute candles for TCS over the last 3 days.

```
mcp__skopaq__get_historical(symbol="TCS", days=3, resolution=15)
```

## Portfolio Management

| Tool | Description | Key Args |
|------|-------------|----------|
| `get_positions` | Open positions with P&L | none |
| `get_holdings` | Delivery (CNC) holdings | none |
| `get_funds` | Available cash, margin, collateral | none |
| `get_orders` | Today's orders with status | none |

These tools try Kite Connect first. If Kite is not connected, they fall back to the paper engine.

## AI Analysis

| Tool | Description | Key Args |
|------|-------------|----------|
| `analyze_stock` | Full 15-agent pipeline (2-5 min) | `symbol`, `date=""` |
| `scan_market` | Multi-model market scan | `max_candidates=5` |
| `check_safety` | Pre-trade safety validation | `symbol`, `quantity`, `price`, `side` |

!!! note "Analysis duration"
    `analyze_stock` runs the complete multi-agent pipeline with 4 analysts, bull/bear debate, risk debate, and trader decision. It takes 2-5 minutes and calls multiple LLM providers.

## Data Gathering (Claude-Native)

These tools fetch raw data for Claude to reason over directly, bypassing the multi-LLM pipeline:

| Tool | Description | Key Args |
|------|-------------|----------|
| `gather_all_analysis_data` | One-shot: market + news + fundamentals + social + memories | `symbol`, `date=""` |
| `gather_market_data` | OHLCV + RSI, MACD, Bollinger, SMA, EMA, ATR, VWMA | `symbol`, `date=""` |
| `gather_news_data` | Company news, global macro, insider transactions | `symbol`, `date=""` |
| `gather_fundamentals_data` | Profile, balance sheet, cash flow, income statement | `symbol`, `date=""` |
| `gather_social_data` | Social media sentiment and company news | `symbol`, `date=""` |
| `recall_agent_memories` | BM25 search over past trade lessons | `situation_summary` |
| `quick_decision` | Calibrated answer to a question about a text via TypeSafe Jev (~0.1 s; needs `SKOPAQ_JEV_ENABLED`) | `text`, `question`, `options=None` |
| `save_trade_reflection` | Store post-trade lesson for future reference | `symbol`, `side`, `entry_price`, `exit_price`, `pnl`, `pnl_pct` |

## Order Execution

| Tool | Description | Key Args |
|------|-------------|----------|
| `place_order` | Execute order through safety checker | `symbol`, `side="BUY"`, `quantity=1`, `price=0`, `order_type="MARKET"` |
| `system_status` | Version, mode, token health, active LLMs | none |

!!! warning "Safety First"
    Every order passes through the `SafetyChecker` before execution. Orders that violate position limits, daily loss caps, or other safety rules are rejected automatically.

## Options Trading

| Tool | Description | Key Args |
|------|-------------|----------|
| `get_option_chain` | Full chain with calls/puts, OI, volume, distance% | `symbol="NIFTY"`, `expiry_index=0` |
| `suggest_option_trade` | AI strike selection with risk metrics | `symbol="NIFTY"`, `strategy="SHORT_PUT"`, `expiry_index=0` |

**Supported strategies:**

- `SHORT_PUT` -- Sell OTM put (bullish view)
- `SHORT_CALL` -- Sell OTM call (bearish view)
- `SHORT_STRANGLE` -- Sell OTM put + call (neutral view)

## GTT Orders

| Tool | Description | Key Args |
|------|-------------|----------|
| `place_gtt_order` | GTT buy trigger or OCO sell (target + stop-loss) | `symbol`, `action`, `trigger_price`, `target_price`, `stop_loss_price`, `quantity` |
| `list_gtt_orders` | List all active GTT orders | none |
| `setup_swing_trade` | Complete CNC swing: GTT buy + planned OCO sell | `symbol`, `entry_price`, `target_price`, `stop_loss_price`, `quantity` |

!!! tip "GTT orders require Kite Connect"
    GTT (Good Till Triggered) orders are a Zerodha feature. You must be logged into Kite via `/api/kite/login` before using these tools.

## Tool Count by Category

| Category | Count |
|----------|-------|
| Market Data | 2 |
| Portfolio | 4 |
| AI Analysis | 3 |
| Data Gathering | 7 |
| Execution | 2 |
| Options | 2 |
| GTT / Swing | 3 |
| **Total** | **23** |
