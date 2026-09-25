# Fly.io Deployment

SkopaqTrader runs on [Fly.io](https://fly.io/) with two apps: the API server and the Telegram bot. Both are deployed in the Mumbai region (`bom`) for lowest latency to Indian exchanges.

## Apps

| App | Purpose | Config | Type |
|-----|---------|--------|------|
| `skopaq-trader` | FastAPI backend (REST API + Kite OAuth) | `fly.toml` | HTTP service |
| `skopaq-telegram` | Telegram bot (polling + scheduled jobs) | `fly-telegram.toml` | Background worker |

Both apps build the shared root `Dockerfile` (non-root uid 1000, `WORKDIR /home/skopaq`,
`TZ=Asia/Kolkata`). `fly.toml` sets `SKOPAQ_PUBLIC_BASE_URL` (the Kite login link the bot
sends); `fly-telegram.toml` also sets `SKOPAQ_API_BASE_URL`, so the bot fetches the Kite
token from the API app. If you set `SKOPAQ_API_TOKEN` on the API app, set the same value
as a secret on the bot.

!!! tip "Moving to a Mac mini"
    When the [Mac mini stack](mac-mini.md) takes over, stop the Fly bot first
    (`fly scale count 0 -a skopaq-telegram`): only one process may poll a bot token.

## Initial Setup

### Step 1: Install Fly CLI

```bash
# macOS
brew install flyctl

# Linux
curl -L https://fly.io/install.sh | sh
```

### Step 2: Login

```bash
fly auth login
```

### Step 3: Create Apps

```bash
# API server
fly launch --name skopaq-trader --region bom --no-deploy

# Telegram bot
fly launch --name skopaq-telegram --region bom --no-deploy
```

### Step 4: Create Volumes

Volumes persist data across deploys (Kite tokens, logs):

```bash
fly volumes create skopaq_data --region bom --size 1 -a skopaq-trader
fly volumes create skopaq_data --region bom --size 1 -a skopaq-telegram
```

Fly volumes mount root-owned, and the image runs as uid 1000. Run this once per app
after the first deploy, or the Kite token file cannot be saved to `/data`:

```bash
fly ssh console -a skopaq-trader -C 'chown 1000:1000 /data'
fly ssh console -a skopaq-telegram -C 'chown 1000:1000 /data'
```

### Step 5: Set Secrets

```bash
# API server secrets
fly secrets set \
  SKOPAQ_KITE_API_KEY="..." \
  SKOPAQ_KITE_API_SECRET="..." \
  SKOPAQ_GOOGLE_API_KEY="..." \
  SKOPAQ_ANTHROPIC_API_KEY="..." \
  SKOPAQ_SUPABASE_URL="..." \
  SKOPAQ_SUPABASE_SERVICE_KEY="..." \
  -a skopaq-trader

# Telegram bot secrets
fly secrets set \
  SKOPAQ_TELEGRAM_BOT_TOKEN="..." \
  SKOPAQ_KITE_API_KEY="..." \
  SKOPAQ_KITE_API_SECRET="..." \
  SKOPAQ_GOOGLE_API_KEY="..." \
  -a skopaq-telegram
```

## Deploy

### API Server

```bash
fly deploy --config fly.toml
```

Configuration in `fly.toml`:

```toml
app = 'skopaq-trader'
primary_region = 'bom'

[build]
  dockerfile = 'Dockerfile'

[env]
  PYTHONUNBUFFERED = '1'
  SKOPAQ_PUBLIC_BASE_URL = 'https://skopaq-trader.fly.dev'

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = 'stop'
  auto_start_machines = true
  min_machines_running = 1

[mounts]
  source = "skopaq_data"
  destination = "/data"

[[vm]]
  memory = '1gb'
  cpu_kind = 'shared'
  cpus = 1
```

The API server:

- Runs on port 8000 with HTTPS
- Auto-stops when idle, auto-starts on requests
- Keeps at least 1 machine running
- Mounts a 1 GB volume at `/data` for token persistence

### Telegram Bot

```bash
fly deploy --config fly-telegram.toml
```

Configuration in `fly-telegram.toml`:

```toml
app = 'skopaq-telegram'
primary_region = 'bom'

[build]
  dockerfile = 'Dockerfile'

[env]
  PYTHONUNBUFFERED = '1'
  SKOPAQ_PUBLIC_BASE_URL = 'https://skopaq-trader.fly.dev'
  SKOPAQ_API_BASE_URL = 'https://skopaq-trader.fly.dev'

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

The Telegram bot:

- Runs as a background worker (no HTTP port)
- Uses 512 MB RAM (lighter than API)
- Shares the same volume mount for Kite token access

## URLs

After deployment:

| Service | URL |
|---------|-----|
| API | `https://skopaq-trader.fly.dev` |
| Health check | `https://skopaq-trader.fly.dev/health` |
| API docs | `https://skopaq-trader.fly.dev/docs` |
| Kite login | `https://skopaq-trader.fly.dev/api/kite/login` |
| Kite callback | `https://skopaq-trader.fly.dev/api/kite/callback` |

!!! note "Kite redirect URL"
    Set `https://skopaq-trader.fly.dev/api/kite/callback` as the redirect URL in your Kite Connect developer console.

## Monitoring

### Logs

```bash
# API logs
fly logs -a skopaq-trader

# Telegram bot logs
fly logs -a skopaq-telegram
```

### Status

```bash
fly status -a skopaq-trader
fly status -a skopaq-telegram
```

### SSH

```bash
fly ssh console -a skopaq-trader
```

## Scaling

```bash
# Scale API to 2 machines
fly scale count 2 -a skopaq-trader

# Increase memory
fly scale memory 2048 -a skopaq-trader
```

## Cost

Rough monthly estimates (Fly.io shared-cpu pricing):

| Service | Spec | Estimated Cost |
|---------|------|---------------|
| API | 1 shared CPU, 1 GB RAM | ~$5/month |
| Telegram | 1 shared CPU, 512 MB RAM | ~$3/month |
| Volumes (2x 1 GB) | Persistent storage | ~$0.30/month |
| **Total** | | **~$8/month** |

!!! tip "Free tier"
    Fly.io offers a free tier that covers small apps. Check current pricing at [fly.io/pricing](https://fly.io/pricing).

## Troubleshooting

| Issue | Solution |
|-------|----------|
| Deploy fails | Check `fly logs` for build errors |
| Token not persisting | Verify volume is mounted (`fly ssh console` then `ls -ld /data`) and owned by uid 1000 (see Step 4) |
| Kite login fails | Verify redirect URL matches in Kite developer console |
| Bot not responding | Check `fly status -a skopaq-telegram` and logs |
| Out of memory | Scale up: `fly scale memory 2048 -a skopaq-trader` |

## CI/CD

Deploy automatically on push to main:

```yaml
# .github/workflows/deploy.yml
name: Deploy to Fly.io
on:
  push:
    branches: [main]
jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: superfly/flyctl-actions/setup-flyctl@master
      - run: flyctl deploy --config fly.toml --remote-only
        env:
          FLY_API_TOKEN: ${{ secrets.FLY_API_TOKEN }}
```
