# Configuration

All configuration is via environment variables with the `SKOPAQ_` prefix, loaded from `.env`.

## Core Settings

| Variable | Default | Description |
|----------|---------|-------------|
| `SKOPAQ_TRADING_MODE` | `paper` | `paper` or `live` |
| `SKOPAQ_ASSET_CLASS` | `equity` | `equity` or `crypto` |
| `SKOPAQ_INITIAL_PAPER_CAPITAL` | `1000000` | Paper trading capital (INR) |

## LLM API Keys

| Variable | Required | Used For |
|----------|----------|----------|
| `SKOPAQ_GOOGLE_API_KEY` | Yes | Gemini Flash (all analysts) |
| `SKOPAQ_ANTHROPIC_API_KEY` | Recommended | Claude Opus (judge roles, chat brain) |
| `SKOPAQ_OPENROUTER_API_KEY` | Optional | Grok (social) + Perplexity (scanner) |

## Broker

| Variable | Description |
|----------|-------------|
| `SKOPAQ_KITE_API_KEY` | Zerodha Kite Connect API key |
| `SKOPAQ_KITE_API_SECRET` | Kite API secret |
| `SKOPAQ_INDSTOCKS_TOKEN` | INDstocks API token (alternative broker) |

## Risk Management

| Variable | Default | Description |
|----------|---------|-------------|
| `SKOPAQ_POSITION_SIZING_ENABLED` | `true` | ATR-based position sizing |
| `SKOPAQ_RISK_PER_TRADE_PCT` | `0.01` | 1% of equity per trade |
| `SKOPAQ_ATR_MULTIPLIER` | `2.0` | Stop distance in ATR units |
| `SKOPAQ_MAX_SECTOR_CONCENTRATION_PCT` | `0.40` | Max 40% in one sector |
| `SKOPAQ_MIN_CONFIDENCE_PCT` | `0` | Minimum AI confidence to trade |

## Position Monitor

| Variable | Default | Description |
|----------|---------|-------------|
| `SKOPAQ_MONITOR_POLL_INTERVAL_SECONDS` | `10` | Check interval |
| `SKOPAQ_MONITOR_HARD_STOP_PCT` | `0.04` | 4% hard stop |
| `SKOPAQ_MONITOR_EOD_EXIT_MINUTES_BEFORE_CLOSE` | `10` | EOD exit at 15:20 |
| `SKOPAQ_MONITOR_TRAILING_STOP_ENABLED` | `false` | Enable trailing stop |
| `SKOPAQ_MONITOR_TRAILING_STOP_PCT` | `0.02` | 2% trail distance |

## Daemon (Autonomous)

| Variable | Default | Description |
|----------|---------|-------------|
| `SKOPAQ_DAEMON_MAX_TRADES_PER_SESSION` | `3` | Max BUY orders/day |
| `SKOPAQ_DAEMON_MAX_CANDIDATES_TO_ANALYZE` | `5` | Top N scanner picks |
| `SKOPAQ_DAEMON_SCAN_DELAY_AFTER_OPEN_SECONDS` | `60` | Wait after 9:15 |

## Telegram

| Variable | Description |
|----------|-------------|
| `SKOPAQ_TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `SKOPAQ_TELEGRAM_CHAT_ID` | Your chat ID: notifications, and allowed to use the bot |
| `SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS` | More chat IDs allowed to use the bot (comma-separated) |

## Ollama (Local Models)

| Variable | Default | Description |
|----------|---------|-------------|
| `SKOPAQ_OLLAMA_ENABLED` | `false` | Enable local model fallback |
| `SKOPAQ_OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server |
| `SKOPAQ_OLLAMA_MODEL` | (auto-detect) | Model name |
