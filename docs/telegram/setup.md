# Telegram Bot Setup

SkopaqTrader includes a Telegram bot (`@Skopaq_bot`) that provides trade alerts, portfolio monitoring, and scheduled market updates. The bot is defined in `skopaq/telegram_bot.py`.

## Create Your Bot

### Step 1: Register with BotFather

1. Open Telegram and search for `@BotFather`
2. Send `/newbot`
3. Choose a name (e.g., "SkopaqTrader")
4. Choose a username (e.g., `my_skopaq_bot`)
5. BotFather gives you a token like `7123456789:AAH-abc123def456ghi789jkl`

### Step 2: Set the Token

Add the token to your `.env`:

```bash
SKOPAQ_TELEGRAM_BOT_TOKEN=7123456789:AAH-abc123def456ghi789jkl
```

Set your chat ID. The bot is private: it answers only this chat and any
listed in `SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS`, and sends notifications here.

```bash
SKOPAQ_TELEGRAM_CHAT_ID=123456789
# Optional: more chats allowed to use the bot (comma-separated)
SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS=
```

!!! tip "Finding your chat ID"
    Send `/start` to your bot: until your chat is allowed, it replies with the chat ID to add. Alternatively, send a message to `@userinfobot`.

!!! warning "Trades need /confirm"
    When a chat message leads the agent to a trade, the bot shows the trade and waits for `/confirm` (or `/cancel`) before executing it. `/halt REASON` and `/resume` work the kill switch.

### Step 3: Configure Bot Commands (Optional)

Send these commands to `@BotFather` to set up the menu:

```
/setcommands
```

Then paste:

```
quote - Get real-time stock quote
portfolio - View positions and P&L
status - System health check
pnl - Current P&L summary
analyze - Quick stock analysis
login - Connect to Zerodha Kite
help - List all commands
```

## Running Locally

```bash
python -m skopaq.telegram_bot
```

Or via the CLI:

```bash
skopaq telegram  # If this command exists
```

The bot starts polling for messages and registers 3 scheduled jobs:

```
Starting SkopaqTrader Telegram bot...
Scheduled jobs:
  09:00 IST — Pre-market login reminder
  09:25 IST — Auto market scan
  15:35 IST — EOD P&L summary
Bot ready. Polling for messages...
```

## Deploying to Fly.io

### Step 1: Create the App

```bash
fly launch --name skopaq-telegram --region bom --no-deploy
```

### Step 2: Set Secrets

```bash
fly secrets set \
  SKOPAQ_TELEGRAM_BOT_TOKEN="your-token" \
  SKOPAQ_KITE_API_KEY="your-key" \
  SKOPAQ_KITE_API_SECRET="your-secret" \
  SKOPAQ_GOOGLE_API_KEY="your-key" \
  -a skopaq-telegram
```

### Step 3: Deploy

Use the Telegram-specific Fly config (`fly-telegram.toml`):

```bash
fly deploy --config fly-telegram.toml
```

The Telegram bot runs as a background worker (no HTTP port needed):

```toml
# fly-telegram.toml
app = 'skopaq-telegram'
primary_region = 'bom'

[processes]
  app = "telegram"

[mounts]
  source = "skopaq_data"
  destination = "/data"

[[vm]]
  memory = '512mb'
  cpu_kind = 'shared'
  cpus = 1
```

### Step 4: Create Volume for Token Persistence

```bash
fly volumes create skopaq_data --region bom --size 1 -a skopaq-telegram
```

The Kite access token is persisted to `/data/skopaq_kite_token.json` so it survives restarts.
It is used until 06:00 IST the next day, when Kite expires it; the 09:00 job then sends the login link again.

## Deploying with Docker

```bash
docker run -d \
  --name skopaq-telegram \
  --env-file .env \
  samuelvinay91/skopaq:latest \
  telegram
```

Or with Docker Compose:

```bash
docker compose up -d telegram
```

## Architecture

```
Telegram Cloud
      │
      │ (polling)
      ▼
skopaq/telegram_bot.py
      │
      ├── Command Handlers (/quote, /portfolio, etc.)
      ├── Natural Language Handler (AI chat brain)
      ├── Scheduled Jobs (login, scan, EOD)
      │
      ├── skopaq/broker/kite_client.py (market data)
      ├── skopaq/notifications.py (centralized alerts)
      └── skopaq/mcp_server.py (analysis tools)
```

## Notification System

The bot integrates with `skopaq/notifications.py`. Any part of the system can send alerts:

```python
from skopaq.notifications import notify

await notify("Order filled: BUY 10x RELIANCE @ Rs 2,485")
```

Chat IDs are registered when users send `/start`. All registered chats receive notifications.

!!! warning "Single bot per token"
    Telegram allows only one bot instance per token. If you run the bot locally and on Fly.io simultaneously, one will fail to receive messages.
