# Docker Deployment

SkopaqTrader ships as one Docker image for every service: API, Telegram bot, scheduler,
daemon, chatbot, MCP server and any `skopaq` CLI command. The same image is used by
`docker-compose.yml`, Fly (`fly.toml`, `fly-telegram.toml`) and Railway (`railway.toml`,
`railway-daemon.toml`).

!!! tip "Running 24/7"
    For an always-on host (a Mac mini M4 or any Linux box), follow the
    [Mac mini runbook](mac-mini.md): host setup, verification, daily operations.

## Quick Start

Build the image (native arm64 on Apple Silicon, amd64 elsewhere), then run the API
server on loopback only, the interactive chatbot, or any `skopaq` CLI command:

```bash
docker build -t skopaqtrader .
docker run -d --env-file .env -p 127.0.0.1:8000:8000 skopaqtrader api
docker run -it --env-file .env skopaqtrader chat
docker run --rm --env-file .env skopaqtrader status
```

Shell blocks on this page have no `#` comments, so they paste into macOS's default zsh.

## The Image

Built from the root `Dockerfile`:

- single stage on `python:3.14-slim-trixie`; no apt packages and no compiler (every
  dependency ships CPython 3.14 wheels for linux/arm64 and linux/amd64);
- dependencies from `pip install -e ".[deploy]"` (Telegram bot with its job queue,
  Kite Connect, Ollama, psycopg2, quantstats, langchain-community);
- runs as the non-root user `skopaq` (uid 1000) with `WORKDIR /home/skopaq`, so
  relative paths (`results/`, `.cache/`) are writable; `/app` (the code) is read-only
  for the app;
- `TZ=Asia/Kolkata`, `PYTHONUNBUFFERED=1`;
- no image-level `HEALTHCHECK`: each compose service defines its own (below), and Fly
  and Railway use their own checks.

## Available Services

`docker/entrypoint.sh` picks the service from the first argument and passes the rest
through:

| Service | Command | Description | Port |
|---------|---------|-------------|------|
| `api` | `docker run ... api` | FastAPI backend (honours `$PORT`) | 8000 |
| `telegram` | `docker run ... telegram` | Telegram bot | none |
| `scheduler` | `docker run ... scheduler` | One daemon session per NSE trading day | none |
| `chat` | `docker run -it ... chat` | Interactive chatbot | none |
| `mcp` | `docker run -i ... mcp` | MCP server (stdio; stdout carries only JSON-RPC) | none |
| `daemon` | `docker run ... daemon [--dry-run]` | Paper trading session, now | none |
| `daemon-live` | `docker run ... daemon-live` | Live trading session, now | none |
| `monitor` | `docker run ... monitor` | Position monitor | none |
| `scan` | `docker run ... scan` | One-shot market scan | none |
| `status` | `docker run ... status` | System health check | none |
| `shell` | `docker run -it ... shell` | Bash shell | none |
| any `skopaq` command | `docker run ... halt "reason"`, `resume --yes`, `token set <tok>`, `settle`, `report` | Passed to the CLI | none |
| `help` | `docker run ... help` | Lists the services and CLI commands | none |

`python ...`, `bash`, `sh` and absolute paths are executed directly (so Railway's
`python -m ...` start commands work).

## Docker Compose

`docker compose up -d --build` starts the default services **api**, **telegram** and
**scheduler**. The others are behind profiles and are meant for `docker compose run`:

| Command | What it does |
|---------|--------------|
| `docker compose up -d --build` | api + telegram + scheduler |
| `docker compose ps` | all three turn "healthy" |
| `docker compose run --rm chat` | interactive chatbot (profile: interactive) |
| `docker compose run --rm scan` | one-shot market scan (profile: tools) |
| `docker compose run --rm daemon --dry-run` | scan-only daemon session now (profile: trading) |
| `docker compose run --rm daemon` | a full paper session now |
| `docker compose run --rm -T mcp` | MCP server over stdio (profile: mcp); never `up -d` |
| `docker compose exec api skopaq halt "reason"` | any skopaq command in a running container |

Every service shares the same image, `.env`, volumes, `TZ`, `init: true` (signal
forwarding) and log rotation (5 x 10 MB). The API is published on `127.0.0.1:8000`
only; expose it publicly only through a Cloudflare Tunnel with Access.

## Volumes and State

| Volume | Path | Holds |
|--------|------|-------|
| `skopaq-home` | `/home/skopaq/.skopaq/` | INDstocks token (`token.enc`, `token.key`), kill-switch `HALT` file |
| `skopaq-home` | `/home/skopaq/.tradingagents/` | decision log |
| `skopaq-home` | `/home/skopaq/results/`, `.cache/` | analysis reports, data cache |
| `skopaq-home` | `/home/skopaq/scheduler/`, `logs/daemon/` | scheduler markers, one log per session |
| `skopaq-data` | `/data/` | Kite access token (shared by `api` and `telegram`) |

New named volumes start out owned by `skopaq` (the image creates the directories).
If a volume was created by an older image and is root-owned:

```bash
docker compose run --rm --no-deps --user root api chown -R skopaq:skopaq /home/skopaq /data
```

## Health Checks

| Service | Check |
|---------|-------|
| `api` | `python -m skopaq.healthcheck api` (`GET /health` on `$PORT`, status `ok`) |
| `telegram` | `python -m skopaq.healthcheck heartbeat /tmp/skopaq-telegram.heartbeat 180` |
| `scheduler` | `python -m skopaq.healthcheck heartbeat /tmp/skopaq-scheduler.heartbeat 180` |

The bot and the scheduler touch their heartbeat file (`SKOPAQ_HEARTBEAT_FILE`) every
minute or less.

## Environment Variables

```bash
cp .env.example .env
```

Then fill it in. Compose treats `$` in a value as a variable: write a literal `$` as `$$`.

Essentials: an LLM key (`SKOPAQ_GOOGLE_API_KEY`), Supabase, `SKOPAQ_TELEGRAM_BOT_TOKEN`
and `SKOPAQ_TELEGRAM_CHAT_ID`, `SKOPAQ_TRADING_MODE=paper`. Settings added for the
always-on stack:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SKOPAQ_SCHEDULER_ENABLED` | `true` | `false` keeps the scheduler up but idle |
| `SKOPAQ_SCHEDULER_MODE` | `paper` | `live` also needs `SKOPAQ_SCHEDULER_CONFIRM_LIVE=true` |
| `SKOPAQ_SCHEDULER_START` / `_LAST_START` / `_DEADLINE` / `_SETTLE_AT` | `09:15` / `11:30` / `15:45` / `18:30` | IST times |
| `SKOPAQ_SCHEDULER_PREFLIGHT` | `08:45` | IST; alert if the INDstocks token is missing or expires before the session ends |
| `SKOPAQ_SCHEDULER_PING_URL` | empty | dead-man's switch |
| `SKOPAQ_NSE_HOLIDAYS` | empty | extra NSE closures (`YYYY-MM-DD,...`) |
| `SKOPAQ_DOCKER_OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Ollama on the host |
| `SKOPAQ_PUBLIC_BASE_URL` | empty | public HTTPS URL of the API (Kite login links) |
| `SKOPAQ_API_BASE_URL` | empty | where a process without the `/data` volume (another machine, the native Mac MCP server) fetches the Kite token; never set it on `api` |
| `SKOPAQ_API_TOKEN` | empty | Bearer token for `/api/chat/*` and `/api/kite/token` |
| `SKOPAQ_CORS_ORIGINS` | `*` | browser origins allowed to call the API |

!!! warning "Never commit .env"
    The `.env` file contains secrets and is gitignored and excluded from the image
    build context (`.dockerignore`). Pass it at runtime with `env_file` / `--env-file`.

## File Reference

| File | Purpose |
|------|---------|
| `Dockerfile` | The one image (compose, Fly, Railway) |
| `docker-compose.yml` | Always-on stack plus profile tools |
| `docker/entrypoint.sh` | Service routing and CLI passthrough |
| `skopaq/healthcheck.py` | Per-service health checks |
| `scripts/macmini/verify.sh` | Host, config and stack readiness checks |
| `.env.example` | Environment variable template |
