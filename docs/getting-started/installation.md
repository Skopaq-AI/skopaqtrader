# Installation

## Prerequisites

- Python 3.11+
- At least one LLM API key (Google Gemini recommended as minimum)
- Zerodha Kite Connect account (for live trading)

## Install from Source

```bash
git clone https://github.com/samuelvinay91/skopaqtrader.git
cd skopaqtrader
pip install -e .
```

## Install via Docker

```bash
docker pull samuelvinay91/skopaq:latest
```

## API Keys

Create a `.env` file from the template:

```bash
cp .env.example .env
```

### Minimum Setup (Paper Trading)

```bash
# At least one LLM provider
SKOPAQ_GOOGLE_API_KEY=your-gemini-key
```

### Full Setup (Live Trading)

```bash
# LLM Providers
SKOPAQ_GOOGLE_API_KEY=...          # Gemini 3.8 Flash (analysts)
SKOPAQ_ANTHROPIC_API_KEY=...       # Claude Opus (research/risk manager)
SKOPAQ_OPENROUTER_API_KEY=...      # Grok + Perplexity (social + news)

# Broker
SKOPAQ_KITE_API_KEY=...            # Zerodha Kite Connect
SKOPAQ_KITE_API_SECRET=...         # Kite API secret

# Telegram Bot
SKOPAQ_TELEGRAM_BOT_TOKEN=...      # From @BotFather
SKOPAQ_TELEGRAM_CHAT_ID=...        # Your chat ID

# Trading Mode
SKOPAQ_TRADING_MODE=paper          # Start with paper, switch to live later
```

### Optional Services

```bash
# Supabase (agent memory persistence)
SKOPAQ_SUPABASE_URL=...
SKOPAQ_SUPABASE_SERVICE_KEY=...

# Ollama (local model fallback)
SKOPAQ_OLLAMA_ENABLED=true
SKOPAQ_OLLAMA_BASE_URL=http://localhost:11434
```

## Verify Installation

```bash
skopaq status    # System health check
skopaq --help    # All commands
```

## Model Tiering

| Role | Primary | Fallback | Local |
|------|---------|----------|-------|
| Analysts | Gemini 3.8 Flash | — | Ollama |
| Social Analyst | Grok 4.6 | Gemini | Ollama |
| Research/Risk Manager | Claude Opus | Gemini | — |
| Chat Brain | Claude Opus | Gemini | Ollama |
| Scanner | Gemini + Grok + Perplexity | — | — |
