# Live Trading

Going live with SkopaqTrader requires an INDstocks account, safety verification, and careful configuration. Skopaq's own trading pipeline — the daemon, the position monitor, `skopaq trade`, chat — sends live orders to INDstocks only (the MCP server's `place_order` goes to the paper engine). Kite Connect is optional: that pipeline uses it for market data and the Telegram `/login` flow. But once a Kite session exists, the MCP server's advanced-order tools place **real orders on your Zerodha account**, whatever `SKOPAQ_TRADING_MODE` says and outside every check on this page (see [Residual limits](#residual-limits)). Paper mode is always the default.

!!! warning "Real Money at Risk"
    Live trading uses real money. Thoroughly test in paper mode first. SkopaqTrader is experimental software -- the authors are not responsible for trading losses.

## Prerequisites

Before going live:

- [ ] Paper traded for at least 7 days (`mandatory_paper_days_for_new_strategy`)
- [ ] INDstocks account with API access; today's token set (`skopaq token set <TOKEN>`, or `SKOPAQ_INDSTOCKS_TOKEN`)
- [ ] The host's egress IPv4 whitelisted at INDstocks (orders and cancels are refused from any other IP; see the [Mac mini runbook](../deployment/mac-mini.md) §3)
- [ ] All unit tests passing (`python3 -m pytest tests/unit/ -x -q`)
- [ ] Optional: Kite Connect credentials (market data and the Telegram login for Skopaq's pipeline). A connected Kite session also lets the MCP server's Kite order tools trade that Zerodha account for real: see [Residual limits](#residual-limits)

## Kite Connect Setup (optional)

!!! warning "A Kite session lets the MCP server trade your Zerodha account"
    Skopaq's pipeline only reads market data from Kite. The MCP server's Kite tools (`place_amo_order`, `place_bracket`, `place_cover`, `place_basket`, `buy_option_contract`, `trade_future`, `invest_mutual_fund`, `place_gtt_order`, `setup_swing_trade`) place real Zerodha orders as soon as a session exists, even in paper mode and without the safety rules. Connect a funded account only if you want that.

### 1. Get API Credentials

1. Go to [Kite Connect Developer Console](https://developers.kite.trade/)
2. Create a new app
3. Note your **API Key** and **API Secret**
4. Set the redirect URL to your deployment URL + `/api/kite/callback`

### 2. Configure Environment

Add to your `.env`:

```bash
SKOPAQ_KITE_API_KEY=your_api_key
SKOPAQ_KITE_API_SECRET=your_api_secret
```

### 3. OAuth Login Flow

The login flow happens daily (Kite tokens expire at end of day):

```
User → /api/kite/login → Zerodha Login Page → /api/kite/callback
                                                     │
                                                     ▼
                                              Access token stored
                                              (memory + /data file)
```

**Via Telegram:**

```
/login
```

The bot sends a login link. After login, the token is stored and persisted.

**Via browser:**

Visit `https://your-deployment.fly.dev/api/kite/login`

### 4. Verify Connection

```
/status
```

Or check the API:

```
GET /api/kite/status
```

## Switching to Live Mode

### Via Environment

```bash
export SKOPAQ_TRADING_MODE=live
```

Or in `.env`:

```
SKOPAQ_TRADING_MODE=live
```

### Via CLI

```bash
SKOPAQ_TRADING_MODE=live skopaq trade RELIANCE
```

In live mode `skopaq trade` asks "Proceed with LIVE execution?" before it analyses and places anything; the quantity comes from the analysis and the position sizer.

### Via Daemon

```bash
skopaq daemon --once --live --confirm-live
```

## Safety Rules

Live trading enforces immutable safety rules defined in `skopaq/constants.py`:

| Rule | Value | Purpose |
|------|-------|---------|
| `max_position_pct` | 15% | Max capital per position |
| `max_daily_loss_pct` | 3% | Stop trading after 3% daily loss |
| `max_weekly_loss_pct` | 7% | Stop trading after 7% weekly loss |
| `max_monthly_loss_pct` | 12% | Stop trading after 12% monthly loss |
| `max_open_positions` | 5 | Maximum concurrent positions |
| `max_order_value_inr` | Rs 5,00,000 | Maximum single order value |
| `max_orders_per_minute` | 20 | Rate limit on orders |
| `require_stop_loss` | true | Every order must have a stop-loss |
| `min_stop_loss_pct` | 2% | Minimum stop-loss distance |
| `market_hours_only` | true | Orders only during NSE hours |
| `cool_down_after_loss_minutes` | 15 | Pause after a loss |
| `auto_shutdown_on_api_failure_minutes` | 5 | Declared, not enforced yet (see [Kill Switch](#kill-switch)) |

!!! note "Immutable rules"
    `SafetyRules` is a frozen dataclass. These values cannot be modified at runtime by any automated process. Only a human can edit `skopaq/constants.py`.

!!! note "Exits are never blocked by the risk limits"
    A SELL can only reduce a position you hold: the no-short-sale check refuses anything more. So the limits on new risk (position size, order value, lot count, the daily/weekly/monthly loss limits and the cool-down after a loss) apply to BUYs only. Stop-loss, trailing-stop, EOD and AI exits go to the broker as MARKET orders, not as LIMIT orders at your entry price, so a stop-loss below entry is no longer held back, and in live mode they are worked until filled (below). Market hours, the order rate limit and the no-short-sale rule still apply to SELLs.

## Fill confirmation (live)

Live orders go to INDstocks, whose order API only acknowledges an order: whether it fills is decided at the exchange afterwards. Skopaq books only what the broker confirms (`skopaq/execution/live_orders.py`):

- **Only confirmed fills count.** After placing an order Skopaq reads it back (every `SKOPAQ_ORDER_FILL_POLL_INTERVAL_SECONDS`) until INDstocks reports it final. The filled quantity and the average price come from the broker (the order's trades, else its `traded_price`), and trade rows, P&L, the loss limits and notifications use them. A rejected, cancelled or unconfirmed order never counts as filled.
- **Entries** (BUYs, and LIMIT SELLs) get one order. Whatever has not filled within `SKOPAQ_ORDER_FILL_TIMEOUT_SECONDS` is cancelled at the broker, and only the filled part counts: a partly filled BUY opens a smaller position.
- **Protective exits** (stop-loss, trailing-stop, EOD, AI and CLOSING SELLs, sent as MARKET) are worked until filled. INDstocks turns a MARKET order into a LIMIT at the live price, which can rest in a falling market. So an exit still resting after `SKOPAQ_ORDER_EXIT_ATTEMPT_TIMEOUT_SECONDS` is cancelled and the rest re-placed as a LIMIT a little further below the LTP (`SKOPAQ_ORDER_EXIT_REPRICE_BUFFER_PCT` more per attempt, at most 5 %), rounded down to the instrument's tick size (the tick from the last instruments CSV loaded, however old; a re-price waits at most 2 s for a download, then uses a coarse tick valid in every price band). That repeats up to `SKOPAQ_ORDER_EXIT_MAX_ATTEMPTS` attempts, never past the shutdown deadline or 15:29:55 IST, and the shares still sellable are checked again before each re-placement. A rate limit (HTTP 429) or a connection failure is retried after a short pause without using an attempt. An exit that still has not filled, or is refused, ends in a CRITICAL alert (`exit-not-filled`, `exit-partial`, `exit-rejected`), and the monitor tries again on its next cycle.
- **Nothing is re-sent blind.** If the answer to a placement is lost (a timeout, a 5xx, a failure body), Skopaq looks for the new order in the order book instead of sending it again: an order id that was not there before, for the same instrument, side and quantity, and never one of Skopaq's own orders already known (when the book could not be read before placing, a lone match created within two minutes of the placement is taken only if it is still the only one after `SKOPAQ_ORDER_RECONCILE_TIMEOUT_SECONDS`). If it cannot tell which order it was, it stops with a CRITICAL `placement-uncertain` alert naming the quantity (for an exit, the remainder that attempt was selling); an uncertain SELL then counts as an open SELL of those shares for `SKOPAQ_ORDER_SELL_FILL_LAG_WINDOW_SECONDS`, so the monitor and CLOSING do not sell them again meanwhile. An order that shows in the book later and only *looks* like it (same instrument, side and quantity, not in the book before, created about then) may be yours: the monitor watches it and records its fills with a CRITICAL `placement-match` alert to check them, but never cancels it, and the uncertain SELL keeps counting until the lag window ends. Only an order carrying the placement's `remarks` tag (`SKOPAQ_INDSTOCKS_ORDER_REMARKS_ENABLED`) is taken over as Skopaq's outright. A cancel INDstocks has not confirmed after `SKOPAQ_ORDER_CANCEL_CONFIRM_TIMEOUT_SECONDS` of retries leaves the order **stuck**: a CRITICAL `order-stuck` alert names it, and nothing is placed over it — while it is unresolved its open remainder counts against the shares even before the order book lists it (for `SKOPAQ_ORDER_SELL_FILL_LAG_WINDOW_SECONDS`), so neither the monitor nor CLOSING sells them again. A cancel answered "Position could not be found." is sent again if the order then still reads as working. A process stopped mid-order cancels the order first (`order-interrupted`, naming every order of it and what they filled); those fills are left for the monitor or CLOSING to record, so they are never lost.
- **Unresolved orders are picked up again.** Every live order is written to the order journal (`~/.skopaq/orders/<date>.jsonl`). The position monitor and the daemon's CLOSING phase re-read the ones left unresolved (stuck, uncertain or unconfirmed), retry the cancel, record late fills (the trade row is added or updated) and re-place what is left of an exit; the monitor does this in the background, so its stop-losses keep their pace. What a still-working order has filled is booked as soon as a resume reads it (a stuck BUY's shares before an exit can sell them), and counts against the shares meanwhile, so they are not sold again. A recovery `skopaq monitor` started while the process that placed an order may still be working it (a just-placed order) waits for it (it counts as unconfirmed if the monitor has to end first); a stuck or interrupted order may be resumed by either process, but only by one at a time (a per-order lock, `~/.skopaq/locks/order-<ID>.lock`). Each booking is journalled twice — `booking` before the trade rows are written, `booked` once they are — and a process reads the journal under that lock before it resumes the order, so it books only what no process has confirmed booking. Progress is booked only under the lock, only when the read gives its price (otherwise the final fill, priced from the order's trades, books it) and only once its `booking` line is written; a final fill is booked only by the process that claims it first in the journal directory, lock or no lock, from the total booked when it claims it (if the lock directory cannot be used, neither process books progress). So each share is booked once, whichever process sees it, as long as each booking finishes and the journal can be written. A booking that fails (the trade rows could not be written) or never finishes (the process died mid-write) counts as not booked: a CRITICAL `booking-unconfirmed` alert names the order and the shares ("may not be booked — check trade rows"), and the next booking of the order — more progress, the final fill, or a process that takes the order over under its lock — starts again from the last confirmed total, so shares whose rows were written just before a process died are booked twice (the alert is the cue to check). If the journal directory cannot be written, progress is not booked, and a final fill whose claim cannot be written is not booked either (CRITICAL `late-fill-unclaimed`: "not booked: journal unwritable — book by hand"); the `journal-write-failed` alert warns that a fill booked while the journal cannot be written may be booked twice. An order still working when the last Skopaq process of the day ends stays listed as unconfirmed (CRITICAL `positions-left`): what it had filled at its last read is booked (if that read gave its price), anything it fills after that only if a later `skopaq monitor` that day reads it — otherwise check the broker's trade book. The journal also tells Skopaq's own orders from yours: an open SELL you placed yourself, such as a GTT leg, is never cancelled.
- **A confirmed fill is never lost.** Once the broker has confirmed a fill, recording it (the exit, the late fill) runs to the end even if the task doing it is cancelled (the monitor ending, Ctrl-C), and the process waits for such recordings before it exits. A live trade notification is sent in the background, so Telegram never delays that.
- **A fill positions do not show yet is still watched.** INDstocks' positions can lag behind a fill. A confirmed BUY that positions do not show yet keeps the monitor running until they do (it is then monitored like any position) — until 15:31 IST if need be; still missing then, it is reported as a position left open. CLOSING cannot sell it until positions show it and sends a CRITICAL `exit-blocked:<symbol>:unshown-buy` alert. A "PARTIALLY FILLED - CANCELLED/EXPIRED" order that reports nothing traded counts as unconfirmed (and as possibly sold in full), never as unfilled.

Results say what happened: an order filled in part is reported as PARTIAL ("Filled 3 of 5"), one that may still be working as UNCONFIRMED, both with the broker's order ids.

!!! warning "Behaviour change: stale BUY LIMITs are cancelled"
    A BUY LIMIT at a price the market has moved away from will often be cancelled after 30 seconds ("Not filled within 30s — cancelled at the broker") instead of resting at the broker for the rest of the day. Raise `SKOPAQ_ORDER_FILL_TIMEOUT_SECONDS` (at most 120) to wait longer.

## Open SELL orders and the no-short-sale check

A SELL may only reduce what you hold. In live mode the check works out:

```
sellable = holdings + CNC positions
           − the open quantity of SELL orders still working at the broker
           − SELLs that filled but do not show in positions yet
           − Skopaq SELLs whose placement is uncertain (for the lag window)
           − Skopaq SELL orders still unresolved that the book does not list yet
             (for the lag window, or as long as a read finds them still working)
```

- **Open and just-filled SELLs are subtracted.** A SELL of shares that an open order (yours, Skopaq's or another process's) is already selling is refused, and the message names the blocking order ids and their statuses. Positions can lag behind a filled SELL, so fills from the last `SKOPAQ_ORDER_SELL_FILL_LAG_WINDOW_SECONDS` that positions do not show yet still count: the same shares are not sold twice. Fills another Skopaq process on this host confirmed count too (from the order journal), even while the order book does not show them yet. Sales positions show from earlier in the day (a morning exit, your own sale of holdings) never cancel out a fresh exit that neither the book nor positions show yet.
- **Protective exits sell the day's position, never your older holdings.** The monitor and CLOSING size an exit by the day's position less Skopaq's own open, unconfirmed and not-yet-shown SELLs of it, then cap it by `sellable`. So a stuck, uncertain or partly filled exit is never taken out of the delivery holdings you already had, and a second Skopaq process does not sell the same position again. The Executor re-checks that size under the SELL lock, from the same order-book read, and refuses the exit otherwise ("… older delivery holdings are not sold by an exit"). Someone else's open SELL (a GTT on your holdings) is only counted against `sellable`. An analysis SELL (`skopaq trade`) may still sell holdings.
- **Intraday positions do not count for a delivery (CNC) SELL.**
- **The same shares on the other exchange count too.** A stock has a different security id on NSE and BSE, so rows are matched by ISIN (taken from the instrument's own position or holding row), else by symbol: shares you sold on BSE are not sold again on NSE. An open SELL on the other exchange whose ISIN is unknown counts against the SELL.
- **The order book is read first**, then positions, then holdings. An order that fills between the reads is then counted twice (which only understates what can be sold), never zero times.
- **If the order book cannot be read, the SELL is refused** (after one retry) with a CRITICAL `sell-refused:<symbol>:book-unreadable` alert, at most one per symbol every 10 minutes. Holdings that cannot be read count as none; a SELL refused because of that says so (`sell-refused:<symbol>:holdings-unreadable`, CRITICAL), never "only 0 held". Without the book Skopaq cannot see open SELLs, and selling shares that are already being sold makes a short delivery, settled through the exchange's auction at a penalty; a delayed exit is the smaller risk. The monitor tries again on its next cycle.
- `SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK=true` lets such a SELL through without counting open orders. It is not recommended: it is logged at CRITICAL when the order router starts and on every use, and a CRITICAL `sell-without-book` alert is sent at most once per symbol every 10 minutes.
- **One SELL per symbol at a time.** A per-symbol lock (`~/.skopaq/locks/sell-<SYMBOL>.lock`) is held from the order-book read until the SELL is final, across the daemon, `skopaq monitor`, `skopaq trade` and chat on the same host. (The MCP server's `place_order` has no live INDstocks client: it goes to the paper engine and takes no lock. Its Kite order tools are outside all of this: see [Residual limits](#residual-limits).) A second SELL of the same symbol waits, then is refused ("Another Skopaq process is already selling …").
- A refused live SELL sends a `sell-refused` alert (CRITICAL for a protective exit). When the monitor or CLOSING finds a needed exit blocked by someone else's open SELL, it sends a CRITICAL `exit-blocked` alert: cancel the listed order at the broker if you do not want to keep it.

Paper mode checks holdings and positions as before.

## Shutdown and the close

- After a stop (the scheduler's SIGTERM at its deadline or on a restart, or Ctrl-C), live order work ends within `SKOPAQ_SCHEDULER_KILL_AFTER_SECONDS` − `SKOPAQ_ORDER_SHUTDOWN_MARGIN_SECONDS` (300 − 60 = 240 s by default), before the scheduler's SIGKILL. It gets at least 30 s but always ends at least 5 s before the SIGKILL, so the margin is kept only when kill-after is at least the margin + 30 s. A broker call still unanswered then is cut off (a placement cut off this way counts as uncertain). No new order is placed in the last `SKOPAQ_ORDER_CANCEL_CONFIRM_TIMEOUT_SECONDS` + 2 polls of that budget (so after 228 s), a BUY still being confirmed is cancelled at once, and an order still working when the budget runs out is alerted (`order-deadline`) and resumed by the next monitor. Exits are shortened automatically so that one exit's worst case fits in half the budget (a WARNING says so).
- No live order is placed after **15:29:55 IST**.
- The daemon's CLOSING phase first settles Skopaq's orders still open (stuck exits; a late BUY is cancelled), then sells what is still held, all positions at once, each sized to what is sellable. A second pass runs only if an exit fell short and there is time for one more attempt; while one of the session's own stuck exits still covers a position, passes go on (resume it, then sell) as long as an attempt fits before the deadline. An exit refused by the orders-per-minute rule is retried once the rule's one-minute window has room again, if an attempt still fits after that wait. Each exit is sized to what the day's position still holds (never older delivery holdings). Whatever is still held is listed in the session report and in a CRITICAL `positions-left` alert.
- MONITORING never hands positions to CLOSING early: before the monitor ends with nothing left to watch it confirms with a fresh read (a position dropped on a broker glitch is taken back), and if it still ends before the EOD exit with positions held and no stop, the daemon starts it again (up to 3 times) rather than selling at MARKET in the middle of the day.
- In live mode `skopaq monitor` sells from the 15:20 EOD exit and ends once nothing is held or open, or at 15:31 IST; while anything is still held (including a confirmed BUY positions do not show) it keeps running. It **exits 4** when positions are still open (including a position it still tracks that one last empty positions read does not show), an exit failed or an order is unconfirmed at the end (0 when flat), which the scheduler turns into a "check the broker" alert; its own CRITICAL `positions-left` alert goes out once a day for the same state. A recovery monitor that exits 4 before 15:30 is run again by the scheduler, since the market is still open (5 minutes later if it ended within a minute of starting). A tracked position is dropped only after two successful reads in a row show nothing held, or a new filled SELL in the book covers it; sales already taken out of its quantity prove nothing.
- The monitor's AI tier runs each analysis in the background (at most half its interval, 10–60 s), so a slow LLM call never holds up a stop-loss, a resting exit or a SIGTERM.

## Residual limits

- An unconfirmed order (stuck, uncertain or interrupted) needs a manual check of the broker's order book; the alert names it.
- A sustained order-book outage near 15:30 refuses SELLs and can carry shares overnight (with CRITICAL alerts).
- **The MCP server can place real Zerodha orders.** Once a Kite session exists (the compose `mcp` container reads it from `/data`; the native server fetches it through `SKOPAQ_API_BASE_URL`), the MCP tools `place_amo_order`, `place_bracket`, `place_cover`, `place_basket`, `buy_option_contract`, `trade_future`, `invest_mutual_fund`, `place_gtt_order` and `setup_swing_trade` place real orders on the Zerodha account through Kite, whatever `SKOPAQ_TRADING_MODE` says. They bypass the `SafetyChecker` (the safety rules and the kill switch), the no-short-sale check, the SELL locks and the order journal, and Skopaq neither confirms nor books their fills. The repo's `.claude/settings.json` does not auto-allow them, so Claude Code asks before each call (unless permissions are bypassed); leave Kite unconnected on a host where they should not trade.
- The per-symbol and per-order locks cover processes on this host only (the compose containers share them). A live Skopaq process on another machine would be guarded by the order-book check alone. (The MCP server's `place_order`, native or in its container, has no live INDstocks client: it goes to the paper engine.)
- An order that only looks like an uncertain placement is never cancelled by Skopaq: if it is Skopaq's and should not stay, cancel it at the INDstocks order book (the `placement-match` alert names it).
- Some API details are still unverified (how positions and holdings split T1 shares, the unit of the instruments' tick size, which order lookup path works): see [INDstocks API](../indstocks_api.md#to-verify-live).

## Live order configuration

All live only (paper ignores them). A value outside the range in parentheses is clamped to it, with a WARNING naming the variable. A value that does not parse (`30s`, `maybe`, empty) is ignored with a WARNING and the default is used (off, for the two switches), so a typo never stops the api, telegram or scheduler services.

| Variable | Default | What it does |
|----------|---------|--------------|
| `SKOPAQ_ORDER_FILL_TIMEOUT_SECONDS` | `30` | Entries: wait this long for a fill, then cancel the rest (5–120) |
| `SKOPAQ_ORDER_FILL_POLL_INTERVAL_SECONDS` | `1` | How often an order's status is read (0.5–5) |
| `SKOPAQ_ORDER_CANCEL_CONFIRM_TIMEOUT_SECONDS` | `10` | Retry a cancel and re-read the order this long before it counts as stuck (3–30) |
| `SKOPAQ_ORDER_EXIT_ATTEMPT_TIMEOUT_SECONDS` | `10` | Protective exits: wait per attempt before cancelling and re-placing (3–30) |
| `SKOPAQ_ORDER_EXIT_MAX_ATTEMPTS` | `3` | Protective exits: attempts at most (1–5) |
| `SKOPAQ_ORDER_EXIT_REPRICE_BUFFER_PCT` | `0.5` | Re-placed exit LIMIT = LTP × (1 − this % × (attempt − 1)), at most 5 % below (0.1–2) |
| `SKOPAQ_ORDER_RECONCILE_TIMEOUT_SECONDS` | `15` | Look for an order whose placement answer was lost in the order book this long (5–30) |
| `SKOPAQ_ORDER_SHUTDOWN_MARGIN_SECONDS` | `60` | After a stop, order work ends this long before the scheduler's kill-after (20–120) |
| `SKOPAQ_ORDER_SELL_FILL_LAG_WINDOW_SECONDS` | `600` | How long the broker may lag behind Skopaq's orders: filled SELLs positions do not show yet, uncertain SELLs and unresolved Skopaq SELLs the order book does not list yet count against a new SELL for this long; a confirmed BUY positions do not show counts as "watched" this long (a resync every poll, then an ERROR log), and the monitor keeps running for it until 15:31 whatever its age (60–1800) |
| `SKOPAQ_ORDER_EXTRA_TERMINAL_STATUSES` | `""` | Comma-separated broker statuses to treat as final (cancelled). Only statuses Skopaq does not recognise are accepted: a documented one (e.g. `PENDING`) is ignored with a WARNING |
| `SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK` | `false` | **Danger:** SELL even when the order book cannot be read (logged at CRITICAL on every use; a CRITICAL alert at most once per symbol every 10 minutes) |
| `SKOPAQ_INDSTOCKS_ORDER_REMARKS_ENABLED` | `false` | Tag orders with `remarks` to match lost placements (listed as unreleased by INDstocks) |
| `SKOPAQ_ORDER_LOCK_DIR` | `~/.skopaq/locks` | Per-symbol SELL locks and per-order resume locks (on the shared home volume); unusable: live runs without them, with a WARNING |
| `SKOPAQ_ORDER_JOURNAL_DIR` | `~/.skopaq/orders` | Per-day journal of Skopaq's own live orders; unusable: live runs without it, with a WARNING |
| `SKOPAQ_MONITOR_RESYNC_CYCLES` | `3` | Monitor: re-read the order book and positions every N polls, and every poll while an order is open (1–60) |

## Pre-Live Checklist

```bash
# 1. Run all tests
python3 -m pytest tests/unit/ -x -q

# 2. Verify paper trading works
skopaq trade RELIANCE    # Should execute in paper mode

# 3. Check system health
skopaq status

# 4. Verify the INDstocks token and the whitelisted egress IP
skopaq status              # (on the Mac mini: scripts/macmini/verify.sh)

# 5. First live trade (asks for confirmation; the quantity comes from the sizer)
SKOPAQ_TRADING_MODE=live skopaq trade RELIANCE
```

For a 1-share check of the order path itself, follow [INDstocks API](../indstocks_api.md#to-verify-live).

## Order Flow (Live)

```
Trade Signal
    │
    ▼
PositionSizer ─── cap to safety limits
    │
    ▼
SafetyChecker ─── SELL: per-symbol lock, then order book → positions → holdings
    │               reject? → notification (+ order alert) + abort
    ▼ (passed)
OrderRouter ─── live mode with an INDstocks client
    │
    ▼
LiveOrderWorker ─── place → confirm the fill → cancel / re-place (exits)
    │
    ▼
INDstocks API ─── order on NSE
    │
    ▼
Execution Result ─── filled quantity, average price, order ids
    │
    ▼
Notification + trade row ─── Telegram, Supabase
```

## Monitoring

### Position Monitor

```bash
skopaq monitor
```

Watches open positions and triggers alerts for:

- New highs
- Stop-loss warnings
- Target proximity
- Trailing stops
- End-of-day exit reminders

In live mode it also keeps in step with the broker: every `SKOPAQ_MONITOR_RESYNC_CYCLES` polls it re-reads the order book, positions and holdings, drops a position only when two successful reads agree it is gone (and takes it back when a later read shows it), resumes orders left open in the background and records their late fills. It exits 4 when positions remain open at the end (see [Shutdown and the close](#shutdown-and-the-close)).

### Telegram Notifications

All trade events are sent via Telegram:

- Order fills (FILLED, PARTIAL, UNCONFIRMED, FAILED, REJECTED)
- Order alerts, CRITICAL or WARNING: an exit that did not fill, a refused or blocked SELL, a stuck or uncertain order, positions left open (each naming the order ids to check)
- GTT triggers
- Position alerts
- EOD summaries

## Kill Switch

If something goes wrong:

1. **Halt new BUYs everywhere**: `skopaq halt "reason"`, Telegram `/halt`, the MCP `halt_trading` tool, or `SKOPAQ_TRADING_HALTED=true`. Every BUY is refused while it is on (the daemon skips scanning and trading); exits and the monitor keep running, so open positions stay protected. `skopaq resume` (or `/resume`) lifts it.
2. **Stop live sessions altogether**: set `SKOPAQ_SCHEDULER_MODE=paper` and recreate the scheduler (`docker compose up -d scheduler`). The scheduler launches its sessions with `daemon --once --live` from that setting, so `SKOPAQ_TRADING_MODE` alone does not stop them. Recreating it stops a session that is running (its CLOSING sells what it holds; see [Mac mini](../deployment/mac-mini.md)), and during a recovery `skopaq monitor` it leaves positions unmanaged until the scheduler is back, so prefer a moment when nothing is held.
3. **INDstocks**: Cancel open orders directly in the INDstocks order book (web/app); every order alert names the order ids.
4. **Zerodha (Kite)**: the MCP server's Kite order tools are not stopped by any of the above; cancel their orders in Kite, and log the Kite session out if they should not trade.

Nothing shuts the daemon down by itself when the broker API keeps failing: `auto_shutdown_on_api_failure_minutes` is declared in `SafetyRules` but not enforced. A failed broker read keeps the last known state (positions are never dropped on it), failed or unconfirmed orders are alerted, and a SELL whose order book cannot be read is refused; use the kill switch above to stop new BUYs.
