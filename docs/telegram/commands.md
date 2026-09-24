# Telegram Commands

The SkopaqTrader Telegram bot supports both slash commands and natural language queries.

## Slash Commands

| Command | Description | Example |
|---------|-------------|---------|
| `/start` | Welcome message, register for alerts | `/start` |
| `/quote SYMBOL` | Real-time stock quote | `/quote RELIANCE` |
| `/portfolio` | Positions, holdings, and funds | `/portfolio` |
| `/pnl` | P&L on open positions | `/pnl` |
| `/status` | System health check | `/status` |
| `/analyze SYMBOL` | Quick technical analysis | `/analyze TCS` |
| `/login` | Send Kite Connect login link | `/login` |
| `/help` | List all commands | `/help` |

## Command Details

### /start

Registers your chat for notifications and displays a welcome message with available commands and scheduled job times.

```
Welcome to SkopaqTrader! Your AI trading assistant.

Commands:
/quote SYMBOL - Live stock quote
/portfolio - Positions & P&L
/status - System health
/pnl - Open position P&L
/login - Connect to Zerodha
/help - All commands

Scheduled (auto):
  09:00 IST - Login reminder
  09:25 IST - Market scan
  15:35 IST - EOD summary

Or just chat naturally - I understand trading questions!
```

### /quote SYMBOL

Fetches a real-time quote via Kite Connect (or INDstocks fallback).

```
/quote HDFCBANK
```

Response:

```
HDFCBANK (NSE)
LTP: Rs 1,567.80 (+1.23%)
Open: Rs 1,550.00
High: Rs 1,575.00
Low: Rs 1,545.00
Volume: 8,45,230
```

### /portfolio

Shows three sections: funds, open positions, and delivery holdings.

```
/portfolio
```

Response:

```
Funds:
  Cash: Rs 9,87,654
  Margin Used: Rs 1,12,346

Positions (2):
  RELIANCE: 10x @ Rs 2,400  P&L: +Rs 855
  TCS: 5x @ Rs 3,800  P&L: -Rs 250

Holdings (1):
  INFY: 20x @ Rs 1,450  P&L: +Rs 1,200
```

### /pnl

Shows P&L for open positions with individual and total figures.

### /status

Displays system health information:

```
SkopaqTrader v0.4.0
Mode: paper
Token: valid
LLMs: Gemini, Claude, Grok
Paper Capital: Rs 10,00,000
```

### /analyze SYMBOL

Runs a quick analysis using gathered data and presents key findings. This is faster than the full 15-agent pipeline.

```
/analyze INFY
```

### /login

Sends the Kite Connect OAuth login URL. If already connected, confirms the connection.

```
/login
```

Response (not connected):

```
Tap to connect Zerodha:
https://skopaq-trader.fly.dev/api/kite/login
```

Response (already connected):

```
Already connected to Zerodha!
```

## Natural Language Chat

The bot also understands plain text queries. A message that is not a command is routed to the AI chat brain.

**Examples:**

| Message | What Happens |
|---------|-------------|
| "What is TCS trading at?" | Fetches quote for TCS |
| "How is my portfolio doing?" | Shows positions and P&L |
| "Should I buy RELIANCE?" | Runs quick analysis |
| "What happened in the market today?" | Summarizes market moves |
| "analyze HDFCBANK" | Same as `/analyze HDFCBANK` |
| "trade INFY" | Explains the trade workflow |

!!! note "AI brain model"
    The natural language handler uses the `chat_brain` role from the LLM tier, which defaults to Claude Opus 5. If unavailable, it falls back to Gemini 3.8 Flash.

## Notification Messages

Beyond user-initiated commands, the bot sends automatic notifications for:

| Event | Example Message |
|-------|----------------|
| Order filled | "BUY 10x RELIANCE @ Rs 2,485 -- FILLED" |
| GTT triggered | "GTT TRIGGERED: HDFCBANK -- order executed at Rs 1,450" |
| Position alert | "NEW HIGH: TCS LTP Rs 3,900 (+2.5%)" |
| Stop warning | "STOP WARNING: INFY approaching stop-loss at Rs 1,400" |
| Market scan | "Market Scan: RELIANCE +1.5%, HDFCBANK +0.8%..." |
| EOD summary | "Market Closed -- Total P&L: Rs +2,340" |

## Message Formatting

The bot uses plain text (not Markdown) for maximum compatibility. Key formatting conventions:

- Stock symbols in UPPERCASE
- Prices prefixed with "Rs"
- Positive P&L shown with `+` prefix
- Negative P&L shown with `-` prefix
- Numbers formatted in Indian number system (lakhs/crores)

!!! tip "Register for alerts"
    Send `/start` at least once to register your chat ID. Without this, you will not receive automated notifications (scans, trade alerts, EOD summaries).
