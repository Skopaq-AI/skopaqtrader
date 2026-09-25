# Mac mini (Docker)

Run SkopaqTrader unattended on a Mac mini M4 (Apple Silicon) with Docker Desktop or
OrbStack. One `docker compose up -d --build` starts the API, the Telegram bot and a
scheduler that runs one autonomous daemon session per NSE trading day. The same
`docker-compose.yml` works on any always-on Linux host.

!!! warning "Unattended trading"
    The scheduler places orders without supervision. Start in paper mode
    (`SKOPAQ_SCHEDULER_MODE=paper`, the default), paper-trade for at least a week,
    and read the logs daily. You are responsible for every order it places.

The shell blocks below contain no `#` comments, so they paste into macOS's default zsh
as they are (zsh treats `#` as text unless `setopt interactivecomments` is on).

## 1. What runs

| Service | What it does | Health check |
|---------|--------------|--------------|
| `api` | FastAPI on `127.0.0.1:8000` only (Kite OAuth, dashboard, `/health`) | `GET /health` |
| `telegram` | Telegram bot (long polling, commands, weekday jobs) | heartbeat file younger than 3 min |
| `scheduler` | One daemon session per NSE trading day | heartbeat file younger than 3 min |

The scheduler (`skopaq schedule`, `skopaq/execution/scheduler.py`):

- starts a session at **09:15 IST** on NSE trading days (weekends and the holidays in
  `skopaq/risk/calendar.py` are skipped);
- catches up until **11:30** if the Mac was down at 09:15; after that the day is
  skipped and you get a Telegram alert;
- starts at most one session per day, even across restarts (a marker file is written
  before launch). The exception: a session whose PRE_OPEN phase failed (exit code 3:
  INDstocks token missing or expiring before 15:45, broker login, LLM setup; nothing
  was traded) is started again every 5 minutes until 11:30;
- checks at **08:45** (`SKOPAQ_SCHEDULER_PREFLIGHT`) that the INDstocks token is set and
  lasts until 15:45, and alerts if not;
- after a restart, alerts once about a session that was cut off (power cut, OOM kill,
  Docker Desktop crash or update). In live mode, before 15:45, it then runs
  `skopaq monitor` until 15:45, so the open delivery (CNC) positions keep their
  stop-loss and the 15:20 EOD exit instead of being carried overnight unmanaged. Paper
  positions of that session are gone (the paper engine keeps them in memory). The same
  recovery runs when the session exits non-zero on its own before 15:45 (an error, or
  an OOM kill of the session alone); the daemon also runs its CLOSING phase when it fails
  after opening trades;
- sends SIGTERM to a session still running at **15:45** (the daemon then closes its
  positions) and SIGKILL 5 minutes later;
- runs `skopaq settle` at **18:30** as a backstop;
- alerts on Telegram (`SKOPAQ_TELEGRAM_CHAT_ID`) when a session fails, a day is
  missed or the holiday list for the year is missing, and can ping a dead-man's
  switch (`SKOPAQ_SCHEDULER_PING_URL`).

The position monitor is not scheduled separately: it runs inside each daemon session
(MONITORING phase) and exits at 15:20 IST.

State lives in two named volumes, shared by every container:

| Volume | Path in the container | Holds |
|--------|-----------------------|-------|
| `skopaq-home` | `/home/skopaq/.skopaq/token.enc`, `token.key` | INDstocks token (encrypted) and its key |
| `skopaq-home` | `/home/skopaq/.skopaq/HALT` | kill-switch file |
| `skopaq-home` | `/home/skopaq/.tradingagents/` | decision log (settled by `skopaq settle`) |
| `skopaq-home` | `/home/skopaq/results/`, `/home/skopaq/.cache/` | analysis reports, data cache |
| `skopaq-home` | `/home/skopaq/scheduler/` | scheduler markers (`daemon-YYYY-MM-DD.started`, `.rc`) |
| `skopaq-home` | `/home/skopaq/logs/daemon/` | one log per session (`daemon-YYYY-MM-DD.log`, kept 60 days) |
| `skopaq-data` | `/data/skopaq_kite_token.json` | Kite access token (written by `api`, read by `telegram`) |

Native on the Mac (not in Docker): Ollama (optional, uses the Metal GPU),
`cloudflared` (optional, for Kite), and the Claude Code MCP server (optional).

## 2. Cutover (do this first)

Only one daemon may trade the account and only one process may poll the Telegram bot.

1. Disable the Railway daemon cron service (`railway-daemon.toml`).
2. Stop the Fly Telegram bot: `fly scale count 0 -a skopaq-telegram`.
3. Decide whether the Fly/Railway API stays up. If it does, keep its Kite redirect URL
   until your tunnel (section 12) works.
4. Apply `supabase/migrations/001`–`003` in the Supabase SQL editor. `003_system_flags.sql`
   makes the kill switch reach every container and the native MCP server.

## 3. Static egress IP for INDstocks

INDstocks only accepts API calls from a whitelisted IPv4. A Cloudflare Tunnel is
**inbound only**: it does not give the Mac a static outbound IP. Pick one:

- a static IPv4 from your ISP;
- a WireGuard or Tailscale exit node on a small VPS in Mumbai;
- an egress proxy: set `HTTPS_PROXY`, `HTTP_PROXY` and
  `NO_PROXY=localhost,127.0.0.1,host.docker.internal` in `.env`.

Register the IP with INDstocks (and with Kite, for orders), put it in
`SKOPAQ_EXPECTED_EGRESS_IP`, and let `verify.sh` confirm the containers use it.

## 4. Mac host setup

- **Power:**
  `sudo pmset -a sleep 0 disksleep 0 displaysleep 10 powernap 0 womp 1 autorestart 1 tcpkeepalive 1`,
  then check `pmset -g`.
- **Automatic login** for the user that runs Docker (System Settings > Users & Groups).
  This requires FileVault off. If you keep FileVault on, someone must unlock the Mac
  after every power loss; use `sudo fdesetup authrestart` for planned reboots.
- **Docker Desktop:** turn on "Start Docker Desktop when you sign in"; give it 4–6 CPUs,
  6–8 GB RAM (leave room for native Ollama) and 64 GB or more of disk; turn off
  Resource Saver and automatic updates (update on weekends). OrbStack works too.
- **macOS updates:** turn off automatic installation; apply updates on weekends.
- **Network:** wired Ethernet, a DHCP reservation, and a UPS for the Mac and the router.
- **Time:** keep "Set time and date automatically" on. Setting the Mac's time zone to
  Asia/Kolkata is recommended; the containers set `TZ=Asia/Kolkata` themselves.
- **Remote access:** Tailscale or SSH (System Settings > General > Sharing > Remote Login).

## 5. Install

```bash
git clone https://github.com/samuelvinay91/skopaqtrader.git
cd skopaqtrader
cp .env.example .env
```

Fill in `.env` (below). Compose treats `$` in a value as a variable, so write a literal
`$` as `$$`. Then build (natively for arm64: no `--platform` flag) and start the stack:

```bash
docker compose build
docker compose up -d
docker compose ps
```

`api`, `telegram` and `scheduler` turn "healthy" within about 2 minutes.

Minimum `.env`: an LLM key (`SKOPAQ_GOOGLE_API_KEY`), the Supabase URL and service key,
`SKOPAQ_TELEGRAM_BOT_TOKEN`, `SKOPAQ_TELEGRAM_CHAT_ID` (scheduler alerts go here),
`SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS`, and `SKOPAQ_CORS_ORIGINS`. Keep
`SKOPAQ_SCHEDULER_MODE=paper`.

## 6. Verify

| Command | What it does |
|---------|--------------|
| `scripts/macmini/verify.sh` | host, configuration and stack checks |
| `scripts/macmini/verify.sh --build --up` | build and start first |
| `scripts/macmini/verify.sh --probe-kill-switch` | halt in `api`, check the scheduler sees it, resume |
| `scripts/macmini/verify.sh --unit-tests` | run `tests/unit` inside the image, without `.env` or the stack's volumes |
| `scripts/macmini/verify.sh --dry-run-daemon` | a scan-only session now (makes LLM calls) |

Each line is `PASS`, `WARN`, `FAIL` or `INFO`; the script exits 1 if anything FAILs and
never prints secret values. Fix every FAIL. The kill-switch probe refuses to run on a
trading day between 09:00 and 15:45 IST unless you add `--force`. Expected output
(abridged):

```text
PASS  host: macOS on Apple Silicon (arm64)
PASS  engine architecture: aarch64 (native arm64 images)
PASS  api: running/healthy
PASS  api port: 127.0.0.1:8000 (loopback only)
PASS  volumes: home (.skopaq, .tradingagents, working directory) and /data are writable
PASS  schedule --check: ok
        Today:          NSE trading day
        Next session:   Mon 2026-09-28 at 09:15 IST
PASS  egress IP: 203.0.113.7 matches SKOPAQ_EXPECTED_EGRESS_IP
Summary: 27 passed, 1 warnings, 0 failed
```

Manual checks:

| Command | What it shows |
|---------|---------------|
| `docker compose logs -f scheduler` | the scheduler and today's session |
| `docker compose exec scheduler skopaq schedule --check` | today's plan and the next session |

## 7. Daily operations

- **INDstocks token**, every trading day before 08:45 IST (the pre-flight check):
  `docker compose exec api skopaq token set <TOKEN>`. It is stored on the shared home
  volume, so the scheduler's session uses it. A token lasts 24 hours from when you set
  it, and a session refuses one that expires before 15:45, so yesterday's token is not
  enough: set a fresh one every morning. Set late, the session still starts within
  5 minutes, until 11:30.
- **Kite login:** open the link the bot sends at 09:00 (`/login`), served through your
  tunnel (section 12). A Kite token expires at 06:00 IST the next day; after that the
  bot, the API and the MCP server stop using it and ask for a new login.
- **Kill switch:** `docker compose exec api skopaq halt "reason"`, Telegram `/halt`, or
  the native MCP `halt_trading`. With Supabase migration 003 applied, all of them reach
  every process; `skopaq resume` (or `/resume`) lifts it.
- **Logs:** `docker compose exec scheduler ls logs/daemon` and
  `docker compose exec scheduler cat logs/daemon/daemon-$(date +%F).log`. Container
  logs rotate at 5 x 10 MB per service.

## 8. Going live

1. Paper-trade for at least one week and review the results (`skopaq report`).
2. Confirm the egress IP is whitelisted (verify.sh `egress IP` is PASS).
3. Set `SKOPAQ_SCHEDULER_MODE=live` and `SKOPAQ_SCHEDULER_CONFIRM_LIVE=true` in `.env`.
   Without the confirmation the scheduler starts no sessions and alerts instead.
4. Outside 09:15–15:45 IST: `docker compose up -d scheduler`.

## 9. Updates

```bash
git pull && docker compose build && docker compose up -d
```

Only outside 09:15–15:45 IST on trading days. Recreating the scheduler during a session
sends it SIGTERM, and the daemon's CLOSING phase sells every open position.

## 10. Holidays

The NSE holiday list lives in `skopaq/risk/calendar.py` (`NSE_TRADING_HOLIDAYS`). Add
next year's list every December, or set `SKOPAQ_NSE_HOLIDAYS` (comma-separated
`YYYY-MM-DD`; any date of a year marks that year as known). Without a list for the
current year the scheduler refuses to trade and alerts once a day. Ad-hoc closures go
in `SKOPAQ_NSE_HOLIDAYS` too. Special sessions (Muhurat trading) are not traded.

## 11. Ollama (optional)

- Install it natively (the app, or `brew services start ollama`) so it uses Metal, and
  pull a 7–8B model (`ollama pull qwen2.5:7b`).
- Set `SKOPAQ_OLLAMA_ENABLED=true` and `SKOPAQ_OLLAMA_MODEL`. Containers reach it at
  `http://host.docker.internal:11434` (`SKOPAQ_DOCKER_OLLAMA_BASE_URL`).
- It is only used when no Gemini key is set.
- If containers get "connection refused", run Ollama with `OLLAMA_HOST=0.0.0.0` and
  block port 11434 from the LAN in the macOS firewall.

## 12. Kite and Cloudflare Tunnel (optional)

- `brew install cloudflared`, then `sudo cloudflared service install <token>` (a
  LaunchDaemon, so it runs without a login), with the tunnel pointing at
  `http://localhost:8000`.
- Put Cloudflare Access in front of every path except `/api/kite/callback` and
  `/api/kite/postback`.
- Set the Kite app's redirect URL to `https://<host>/api/kite/callback`.
- Set `SKOPAQ_PUBLIC_BASE_URL=https://<host>` (login links in Telegram) and
  `SKOPAQ_API_TOKEN` (guards `/api/chat/*` and `/api/kite/token`).
- Rotate the current Kite session: `/api/kite/token` was publicly reachable on Fly.

## 13. Claude Code MCP on the Mac (optional)

**Native** (`.claude/.mcp.json` uses python.org Python 3.14): run
`/Applications/Python 3.14/Install Certificates.command` once, then from the repo:

```bash
/Library/Frameworks/Python.framework/Versions/3.14/bin/python3 -m pip install -e ".[deploy]"
```

The native server has no `/data` volume, so it cannot read the Kite session that `api`
stores there. Without the step below, `get_quote` and the other Kite-first tools quietly
fall back to INDstocks, which works only from the whitelisted egress IP. Give the server
`SKOPAQ_API_BASE_URL` in the `env` block of its entry in `.claude/.mcp.json`, so it asks
the local API for the token:

```json
{
  "mcpServers": {
    "skopaq": {
      "command": "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3",
      "args": ["-m", "skopaq.mcp_server"],
      "env": { "SKOPAQ_API_BASE_URL": "http://127.0.0.1:8000" }
    }
  }
}
```

Set it there, never in `.env`: every container reads `.env`, and `api` would then call
itself. `SKOPAQ_API_TOKEN`, if you set one, is read from `.env` like the other keys (the
server runs from the repo); pick one without `$`, because compose turns `$$` into `$` but
the native server reads `.env` literally. The container server below mounts `/data` and
needs none of this.

**Container** alternative, in your MCP config:

```json
{
  "mcpServers": {
    "skopaq": {
      "command": "docker",
      "args": ["compose", "-f", "/path/to/skopaqtrader/docker-compose.yml", "run", "--rm", "-T", "mcp"]
    }
  }
}
```

Each has its own paper-engine state; the kill switch is shared through Supabase.

## 14. Backups

```bash
mkdir -p "$HOME/SkopaqBackups"
docker compose run --rm --no-deps -v "$HOME/SkopaqBackups:/backup" api shell -c \
  'tar czf /backup/home-$(date +%F).tgz -C /home/skopaq . && tar czf /backup/data-$(date +%F).tgz -C /data .'
```

Keep `.env` in a password manager, rely on Supabase's backups for the database, and
run `docker system prune -f && docker builder prune -f` monthly.

## 15. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `PermissionError` on `/home/skopaq` or `/data` | `docker compose run --rm --no-deps --user root api chown -R skopaq:skopaq /home/skopaq /data` |
| Telegram `Conflict: terminated by other getUpdates request` (HTTP 409) | Another poller uses the token: `fly scale count 0 -a skopaq-telegram`, stop other copies |
| `scheduler` unhealthy | `docker compose logs scheduler`; `docker compose exec scheduler skopaq schedule --check` |
| INDstocks 401/403 | Today's token not set, or the egress IP is not whitelisted (verify.sh `egress IP`) |
| Ollama unreachable from containers | Section 11 (`OLLAMA_HOST=0.0.0.0`) |
| A value with `$` is cut short | Write `$$` in `.env` (compose interpolates `$`) |
| No session on a weekday | `schedule --check`: holiday, missing holiday list, live not confirmed, or `SKOPAQ_SCHEDULER_ENABLED=false` |
| Alert "PRE_OPEN failed" or "pre-flight" | Usually the INDstocks token: set it; the session is retried every 5 min until 11:30 |
| Alert "session ... was interrupted" | The Mac or Docker died mid-session. Check open positions at the broker (in live mode the scheduler runs `skopaq monitor` until 15:45) |
| Alert "daemon exited rc=N" | The session failed or was killed: read `logs/daemon/daemon-<date>.log`. In live mode before 15:45 the scheduler runs `skopaq monitor` until 15:45; otherwise check open positions at the broker |
| Native MCP quotes come from INDstocks, not Kite | Section 13: `SKOPAQ_API_BASE_URL` in the MCP server's `env` block |

## 16. Services and host requirements

| Service | Needed for | Notes |
|---------|------------|-------|
| INDstocks | market data, orders | daily token; static egress IPv4 whitelisted |
| Kite Connect (Zerodha) | optional broker, Telegram scans | public HTTPS callback (tunnel); daily login |
| Supabase | kill switch across processes, P&L history, memory | apply migrations 001–003 |
| Upstash / LangCache | optional semantic LLM cache | |
| Telegram | bot, scheduler alerts | one poller per token |
| LLM providers | analysis (Gemini, Anthropic, OpenRouter, Perplexity) | keys in `.env` |
| TypeSafe / Jev | optional post screening and decisions | `SKOPAQ_TYPESAFE_API_KEY` |
| Data vendors | yfinance, etc. | outbound HTTPS |
| Vercel | optional dashboard | set `SKOPAQ_CORS_ORIGINS` to its origin |
| Ollama | optional local fallback | native on the Mac |
