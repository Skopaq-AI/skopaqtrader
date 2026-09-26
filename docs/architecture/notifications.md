# Notification System

SkopaqTrader has a centralized notification system (`skopaq/notifications.py`) that routes trading events to all configured channels. Currently, Telegram is the primary channel with console logging as fallback.

## Architecture

```
Any SkopaqTrader module
        │
        │  await notify("message")
        ▼
  skopaq/notifications.py
        │
        ├── Console log (always)
        ├── Telegram Bot API (if configured)
        └── (extensible for email, Slack, etc.)
```

## Core Functions

### notify()

The primary function. Sends a message to all registered channels.

```python
from skopaq.notifications import notify

await notify("Order filled: BUY 10x RELIANCE @ Rs 2,485")
```

### Event-Specific Functions

| Function | Purpose | Key Args |
|----------|---------|----------|
| `notify_trade_event()` | Order fill/rejection | `side`, `symbol`, `price`, `quantity`, `status`, `reason` |
| `notify_order_alert()` | CRITICAL/WARNING alert about a live order | `severity`, `title`, `text`, `order_ids` |
| `notify_gtt_event()` | GTT placed/triggered/cancelled | `event`, `symbol`, `trigger_price`, `trigger_id` |
| `notify_position_alert()` | Position monitoring alerts | `symbol`, `ltp`, `entry`, `pnl`, `alert_type` |
| `notify_market_scan()` | Scanner results | `results` (list of quote dicts) |
| `notify_options_trade()` | Options recommendation | `trade_text` |
| `notify_eod_summary()` | End-of-day portfolio summary | `portfolio_text` |

## Events That Trigger Notifications

### Trade Events

| Event | When | Example Message |
|-------|------|-----------------|
| Order filled | After successful execution | "BUY 10x RELIANCE @ Rs 2,485 -- FILLED" |
| Order partly filled | Live: the broker filled part of the order, the rest was cancelled | "BUY RELIANCE -- PARTIAL, Qty: 3 ... filled 3 of 5; rest cancelled" |
| Order unconfirmed | Live: the order may still be working at the broker | "SELL TCS -- UNCONFIRMED" and why |
| Order rejected | Safety check or broker rejection | "REJECTED: exceeds max position size" |
| Paper trade | Paper mode execution | "PAPER BUY 10x TCS @ Rs 3,800" |

### Order Alerts (live)

Sent by `OrderAlerter` (`skopaq/execution/order_alerts.py`) in the background, so placing or
cancelling an order never waits for Telegram, and deduplicated by key within the process (a
blocked exit retried every 10 seconds sends one message). An alert about particular orders
ends with their broker order ids ("Orders: EQ-…").

| Severity | Examples |
|----------|----------|
| CRITICAL | `exit-not-filled`, `exit-partial`, `exit-rejected`, `exit-replace-blocked`, `sell-refused` (a protective exit, or the order book or holdings unreadable), `exit-blocked`, `order-stuck`, `order-deadline`, `order-interrupted`, `placement-uncertain`, `placement-match` (an order that only looks like a lost placement: watched, never cancelled), `fill-qty-unknown` (an exit), `sell-without-book` (at most once per symbol every 10 minutes), `exit-late-unpriced`, `late-fill-unpriced`, `late-fill`/`exit-late` of an order only matched by its look, `booking-unconfirmed` (a late fill's booking failed or never finished: its shares may not be booked — check trade rows), `late-fill-unclaimed` (a final late fill not booked: the journal directory is unwritable — book it by hand), `positions-left` (`skopaq monitor`: once a day for the same state) |
| WARNING | `sell-refused` (any other SELL), `entry-partial`, `fill-qty-unknown` (an entry), `fill-price-unknown`, `late-fill`, `exit-late`, `position-dropped`, `partial-not-split`, `sell-not-booked`, `sell-lock-unavailable`, `journal-write-failed` |

What each means and what to do: [Live Trading](../trading/live-trading.md#fill-confirmation-live) and the troubleshooting table in [Mac mini (Docker)](../deployment/mac-mini.md#15-troubleshooting).

### GTT Events

| Event | When | Example Message |
|-------|------|-----------------|
| `PLACED` | GTT order created | "GTT BUY set: buy 50x HDFCBANK when price hits Rs 1,450" |
| `TRIGGERED` | Price hit the trigger | "GTT TRIGGERED: HDFCBANK -- order executed!" |
| `CANCELLED` | User cancelled GTT | "GTT CANCELLED: HDFCBANK" |
| `EXPIRED` | GTT expired (1 year) | "GTT EXPIRED: RELIANCE" |
| `REJECTED` | Zerodha rejected GTT | "GTT REJECTED: insufficient margin" |

### Position Alerts

| Alert Type | Trigger | Example |
|------------|---------|---------|
| `NEW_HIGH` | Position hits new intraday high | "NEW HIGH: TCS LTP Rs 3,900 (+2.5%)" |
| `STOP_WARNING` | Price approaching stop-loss | "STOP WARNING: INFY near SL at Rs 1,400" |
| `TARGET_NEAR` | Price near target | "TARGET NEAR: RELIANCE at Rs 2,590 (target Rs 2,600)" |
| `TRAILING_STOP` | Trailing stop adjusted | "TRAILING STOP: HDFCBANK SL raised to Rs 1,520" |
| `EOD_EXIT` | End-of-day exit reminder | "EOD EXIT: Close RELIANCE before 15:20" |

### Market Scan

Sent at 09:25 IST (scheduled) or on demand:

```
Market Scan

  RELIANCE: Rs 2,485.50 (+1.23%)
  HDFCBANK: Rs 1,567.80 (+0.89%)
  SBIN: Rs 756.30 (-0.45%)

Top: RELIANCE (+1.23%)
```

## Chat Registration

Notifications are sent to all registered Telegram chat IDs. Registration happens when:

1. User sends `/start` to the Telegram bot
2. Code calls `register_chat(chat_id)`
3. The default chat ID is set via `SKOPAQ_TELEGRAM_CHAT_ID`

```python
from skopaq.notifications import register_chat, set_default_chat

register_chat(123456789)        # Register a specific chat
set_default_chat(123456789)     # Set as default + register
```

## Configuration

| Variable | Purpose |
|----------|---------|
| `SKOPAQ_TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `SKOPAQ_TELEGRAM_CHAT_ID` | Default chat ID for notifications |

If neither is set, notifications are logged to console only.

## How Telegram Delivery Works

```python
async def notify(message: str) -> None:
    logger.info("NOTIFY: %s", message[:100])  # Always log

    chat_ids = _get_chat_ids()
    if not chat_ids:
        return  # No registered chats

    token = os.environ.get("SKOPAQ_TELEGRAM_BOT_TOKEN", "")
    if not token:
        return  # No bot token

    bot = Bot(token)
    for chat_id in chat_ids:
        await bot.send_message(chat_id=chat_id, text=message)
```

Each notification creates a new `Bot` instance. Failed sends are logged but do not raise exceptions -- the trading system continues regardless of notification failures.

## Extending to New Channels

To add a new notification channel (e.g., Slack, email):

1. Add the delivery logic inside `notify()`
2. Add configuration variables to `SkopaqConfig`
3. The event-specific functions (`notify_trade_event`, etc.) all call `notify()`, so they automatically get the new channel

```python
async def notify(message: str) -> None:
    logger.info("NOTIFY: %s", message[:100])

    # Telegram
    await _send_telegram(message)

    # New: Slack
    await _send_slack(message)
```

## File Reference

| File | Purpose |
|------|---------|
| `skopaq/notifications.py` | Core notification functions |
| `skopaq/telegram_bot.py` | Telegram bot (also sends notifications) |
| `skopaq/execution/daemon.py` | Daemon sends trade/scan notifications |
| `skopaq/execution/position_monitor.py` | Position alerts |
| `skopaq/options/gtt.py` | GTT event notifications |
