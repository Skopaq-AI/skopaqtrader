# Live Trading

Going live with SkopaqTrader requires Kite Connect setup, safety verification, and careful configuration. Paper mode is always the default.

!!! warning "Real Money at Risk"
    Live trading uses real money. Thoroughly test in paper mode first. SkopaqTrader is experimental software -- the authors are not responsible for trading losses.

## Prerequisites

Before going live:

- [ ] Paper traded for at least 7 days (`mandatory_paper_days_for_new_strategy`)
- [ ] Zerodha account with F&O segment activated (if trading options)
- [ ] Kite Connect API subscription (Rs 2,000/month from Zerodha)
- [ ] All unit tests passing (`python3 -m pytest tests/unit/ -x -q`)
- [ ] `.env` configured with valid Kite credentials

## Kite Connect Setup

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
skopaq trade RELIANCE --live --confirm-live
```

The `--confirm-live` flag is mandatory -- it prevents accidental live trades.

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
| `auto_shutdown_on_api_failure_minutes` | 5 | Kill switch on API failures |

!!! note "Immutable rules"
    `SafetyRules` is a frozen dataclass. These values cannot be modified at runtime by any automated process. Only a human can edit `skopaq/constants.py`.

!!! note "Exits are never blocked by the risk limits"
    A SELL can only reduce a position you hold: the no-short-sale check refuses anything more. So the limits on new risk (position size, order value, lot count, the daily/weekly/monthly loss limits and the cool-down after a loss) apply to BUYs only. Stop-loss, trailing-stop, EOD and AI exits go to the broker as MARKET orders, not as LIMIT orders at your entry price, so a stop-loss below entry is no longer held back. Market hours, the order rate limit and the no-short-sale rule still apply to SELLs.

    Two limits remain in live mode: INDstocks may price a MARKET order as a LIMIT at the live price, which can rest in a fast-falling market, and Skopaq treats an order the broker accepted as filled (the exit is recorded at the LTP it was decided at). Check the broker's order book after a volatile session.

## Pre-Live Checklist

```bash
# 1. Run all tests
python3 -m pytest tests/unit/ -x -q

# 2. Verify paper trading works
skopaq trade RELIANCE    # Should execute in paper mode

# 3. Check system health
skopaq status

# 4. Verify Kite connection
# Visit /api/kite/login and complete OAuth

# 5. Test with minimum quantity
skopaq trade RELIANCE --live --confirm-live
# This will trade 1 share as a sanity check
```

## Order Flow (Live)

```
Trade Signal
    │
    ▼
SafetyChecker ─── reject? → notification + abort
    │
    ▼ (passed)
PositionSizer ─── cap to safety limits
    │
    ▼
OrderRouter ─── route to KiteClient (live)
    │
    ▼
KiteConnect API ─── place order on NSE
    │
    ▼
Execution Result ─── fill price, slippage
    │
    ▼
Notification ─── Telegram alert
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

### Telegram Notifications

All trade events are sent via Telegram:

- Order fills
- GTT triggers
- Position alerts
- EOD summaries

## Kill Switch

If something goes wrong:

1. **Telegram**: Send `/stop` to the bot
2. **API**: `POST /api/kill-switch`
3. **Manual**: Set `SKOPAQ_TRADING_MODE=paper` and restart
4. **Zerodha**: Cancel all orders directly on Kite web/app

The daemon automatically shuts down if the API fails for 5 consecutive minutes (`auto_shutdown_on_api_failure_minutes`).
