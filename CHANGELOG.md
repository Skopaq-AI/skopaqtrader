# Changelog

All notable changes to SkopaqTrader. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may break things).

## [0.2.0] — 2026-09-26

This release moves SkopaqTrader onto TradingAgents v0.5.1. It adds calibrated
decisions with TypeSafe Jev, hardens order safety end to end, and makes the
stack run unattended on a Mac mini M4 (or any Docker host). Paper trading is
still the default everywhere.

### Upgrade notes

- **Apply `supabase/migrations/003_system_flags.sql`** in the Supabase SQL
  editor. The kill switch uses it to halt every process and machine at once.
- **Copy the new settings from `.env.example`**:
  - scheduler: `SKOPAQ_SCHEDULER_*`;
  - API security: `SKOPAQ_API_TOKEN`, `SKOPAQ_CORS_ORIGINS`;
  - URLs: `SKOPAQ_PUBLIC_BASE_URL`, `SKOPAQ_API_BASE_URL`;
  - Telegram: `SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS`;
  - Jev: `SKOPAQ_JEV_*`;
  - live order confirmation: `SKOPAQ_ORDER_*`, `SKOPAQ_MONITOR_RESYNC_CYCLES`
    and `SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK` (off by default; see below).
- **Live BUYs that don't fill within 30 seconds are cancelled**
  (`SKOPAQ_ORDER_FILL_TIMEOUT_SECONDS`). INDstocks turns MARKET orders into
  LIMITs at the live price, so some entries will not fill and are cancelled
  rather than left resting.
- **In live mode a SELL is refused if the broker's order book can't be read.**
  You get a critical alert. `SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK=true`
  overrides this; every use is logged and alerted.
- **Kite orders are real, whatever the trading mode.** With a Kite session, the
  MCP server's advanced-order tools (AMO, bracket, cover, basket, options,
  futures, mutual funds, GTT, swing trade) place real Zerodha orders even in
  paper mode, outside the safety checks. Claude Code asks your permission for
  each one. Behaviour unchanged, now documented.
- **Telegram is private by default.** Only chats in
  `SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS` (or `SKOPAQ_TELEGRAM_CHAT_ID`) get
  answers. Trades wait for `/confirm`.
- **Railway's daemon cron now runs at 09:15 IST**, up from 09:10. Disable it
  before starting the Docker scheduler, or two daemons will trade the same
  account.
- **`docker/Dockerfile` is gone.** Every deployment (compose, Fly, Railway)
  builds the root `Dockerfile`.
- **INDstocks only accepts whitelisted IPs.** An always-on host needs a static
  egress IPv4 on that whitelist. A Cloudflare Tunnel does not provide one; see
  `docs/deployment/mac-mini.md` §3.
- **LLM models moved** to Gemini 3.8 Flash, Grok 4.6 (OpenRouter) and Claude
  Opus 5.

### Added

- **TradingAgents v0.5.1 base.** It brings the append-only decision log,
  settled against NIFTY once each call's holding window has traded, and
  upstream's own test suite runs in CI against our `tradingagents/`. Every
  local modification is listed in `UPSTREAM_CHANGES.md`.
- **Always-on deployment** (`docker-compose.yml`, runbook
  `docs/deployment/mac-mini.md`, checks `scripts/macmini/verify.sh`):
  - api, telegram and a scheduler service run on persistent volumes;
  - one non-root image, built natively for linux/arm64 and amd64;
  - a health check per service.
- **Scheduler** (`skopaq schedule`):
  - runs one daemon session per NSE trading day at 09:15 IST, with catch-up
    until 11:30 and a hard stop at 15:45;
  - checks the INDstocks token at 08:45;
  - settles decisions at 18:30;
  - optional dead-man's-switch ping;
  - holds a single-instance lock.
  - In live mode it keeps positions protected with `skopaq monitor` after a
    failed, killed or interrupted session, and resumes that protection after a
    restart.
- **TypeSafe Jev**, off unless `SKOPAQ_JEV_ENABLED=true`:
  - calibrated confidence for entry signals, where a confident contradiction
    becomes HOLD;
  - the sell analyst's exit call;
  - catalyst scoring in the scanner;
  - the MCP `quick_decision` tool;
  - it can run through a TypeSafe-compatible gateway such as OpenRouter
    (`SKOPAQ_JEV_BASE_URL`).
- **Kill switch**: `skopaq halt` / `skopaq resume`, the Telegram `/halt` and
  `/resume` commands, and the MCP `halt_trading` / `resume_trading` tools.
  While halted, every BUY is rejected on every path, and SELLs still protect
  open positions.
- **Track record** (`skopaq report`, MCP `performance_report`):
  - AI calls against NIFTY;
  - closed trades: win rate, profit factor, drawdown;
  - confidence calibration.
- **`skopaq monitor` exit code**: exits 4 in live mode when positions or
  unconfirmed orders are left, so the scheduler alerts.
- **`docs/indstocks_api.md`**: the INDstocks API reference that CLAUDE.md
  already pointed to.
- **`skopaq settle`**: settles past decisions whose holding window has traded.
- **`skopaq memory legacy`**: exports or deletes pre-v0.5.1 agent memories.
- **Parallel analysts** in isolated message channels (opt-in upstream, on in
  Skopaq).
- **CI on every PR**:
  - unit tests on Python 3.11, 3.12 and 3.14;
  - a lint gate for syntax errors and undefined names;
  - upstream TradingAgents' suite;
  - a Docker image build with the compose stack brought up and health-checked.
- **API access control**: `SKOPAQ_API_TOKEN` guards the endpoints that run
  tools or hand out credentials, and CORS origins are configurable.

### Changed

- **Protective exits are MARKET orders.** This covers the monitor's hard stop,
  trailing stop, EOD exit and AI SELL, and the daemon's closing sell-all. Exit
  P&L is measured against the cost basis of the shares sold.
- **Risk limits on new positions apply to BUYs only**: position size, order
  value, lot count, the daily/weekly/monthly loss limits and the cool-down
  after a loss. A SELL can only reduce shares you hold. Market hours, the
  order rate limit and the no-short-sale rule still apply to SELLs.
- **Loss limits hold across processes.** Daily, weekly and monthly limits are
  seeded from P&L already realised (Supabase), not only from trades in the
  current process.
- **Daemon exit codes**: 1 when a session fails, 3 when pre-open fails, 0
  otherwise (one stock's failed analysis no longer counts as a failure).
- **Kite**: a saved access token expires at 06:00 IST, and login links use
  `SKOPAQ_PUBLIC_BASE_URL` instead of a hard-coded Fly host.

### Fixed

- **Stop-losses below entry never executed.** Exits were LIMIT orders at the
  entry price.
  - Paper refused the fill on every cycle.
  - Live would have left a resting order above the market while recording the
    exit at breakeven.
  - Separately, after one loss, the cool-down and the loss limits blocked the
    next stop-loss.
- **Live orders counted as filled when merely accepted.** An order now counts
  only when INDstocks confirms it, at the real average price and filled
  quantity.
  - A rejected, cancelled or unconfirmed order never counts.
  - A protective exit that rests is re-priced to the instrument's tick size and
    re-placed, within a deadline that finishes before the scheduler's shutdown.
    If it still doesn't fill, you get a critical alert.
  - An unfilled entry is cancelled, and only the filled quantity reaches trade
    rows, P&L and the monitor.
  - Stuck and late orders are journalled, so a recovery `skopaq monitor` picks
    them up, and their fills are booked exactly once.
- **The no-short-sale check** ignored today's sales, counted paper shares
  twice, and did not count pending SELL orders. It now reads the order book
  before positions.
  - It subtracts open SELLs, and filled SELLs that positions don't show yet.
  - It also subtracts Skopaq's own stuck exits.
  - A per-symbol lock stops two processes from selling the same shares.
  - Protective exits never dip into older delivery holdings.
- **Paper SELLs could close live trade rows**, even when refused, so phantom
  P&L reached the live loss limits.
- **Live broker data**: holdings parsed as zero, a null field crashed
  positions, and the client read the wrong order-status field.
- **The Docker image**:
  - the container user could not write its own state, so every analysis
    failed;
  - unquoted `>=` specifiers were shell redirections, so version pins were
    dropped;
  - secrets could reach the image;
  - the MCP stdio server was corrupted by a startup banner.
- **Compose**: containers that first-mounted a fresh volume together could
  fail with `mkdir ...: file exists`.
- **The scheduler**:
  - a full disk could freeze a live session;
  - a mistyped setting crash-looped every container;
  - a second scheduler treated the running session as interrupted.
- **Tests**: with a real `.env` loaded, the suite wrote a halt to production
  Supabase. Tests could also read the host's Kite session file.
- **Telegram**: the bot answered anyone who found it. It now serves only the
  allow-list, and trades wait for `/confirm`.
- **Other fixes**:
  - INDstocks instruments were downloaded on every lookup;
  - a stale token file shadowed a valid env token;
  - the checkpoint key ignored parallel mode;
  - three undefined-name bugs were found by the new lint gate.

### Known limits

- INDstocks turns MARKET orders into LIMITs at the live price. Exits are
  re-priced and retried, but a very fast fall can still leave one unfilled
  until the next attempt.
- The Kite MCP order tools act outside Skopaq's safety checks (see Upgrade
  notes).
- Holiday lists must be updated each December (`skopaq/risk/calendar.py` or
  `SKOPAQ_NSE_HOLIDAYS`). Without them the scheduler fails closed and alerts.

## [0.1.0] — 2026-04-08

The initial platform, built on TradingAgents v0.2.0:

- the multi-agent analysis pipeline;
- INDstocks and Kite Connect brokers, with a paper engine;
- the autonomous daemon, with position monitoring and an AI sell analyst;
- the multi-model scanner;
- options selling, GTT orders and CNC swing trading;
- an MCP server with 31 tools;
- the Telegram bot, with scheduled jobs and notifications;
- backtesting and strategy refinement;
- Fly.io, Railway and Vercel deployments;
- the MkDocs documentation site.

[0.2.0]: https://github.com/Skopaq-AI/skopaqtrader/compare/4f6dd89...main
[0.1.0]: https://github.com/Skopaq-AI/skopaqtrader/tree/4f6dd89
