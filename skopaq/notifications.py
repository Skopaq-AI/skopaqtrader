"""Centralized notification system for SkopaqTrader.

All trading events (order fills, GTT triggers, position changes, alerts)
route through this module. It sends to all configured channels:
    - Telegram (primary)
    - Console log (always)

Usage::

    from skopaq.notifications import notify

    await notify("Order filled: BUY 1x TCS @ Rs 2,503")
    await notify_trade_event("BUY", "TCS", 2503.0, 1, "FILLED")
    await notify_gtt_event("PLACED", "HDFCBANK", 756.0, trigger_id=314196971)
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Sequence

logger = logging.getLogger(__name__)

# Registered chat IDs for notifications (populated from Telegram bot /start)
_chat_ids: set[int] = set()

# Known chat ID (set explicitly or loaded from env)
_default_chat_id: int = 0


def register_chat(chat_id: int) -> None:
    """Register a Telegram chat for notifications."""
    _chat_ids.add(chat_id)


def set_default_chat(chat_id: int) -> None:
    """Set the default chat ID for notifications."""
    global _default_chat_id
    _default_chat_id = chat_id
    _chat_ids.add(chat_id)


def _get_chat_ids() -> set[int]:
    """Get all registered chat IDs, including from env."""
    global _default_chat_id
    if not _default_chat_id:
        env_id = os.environ.get("SKOPAQ_TELEGRAM_CHAT_ID", "")
        if env_id:
            _default_chat_id = int(env_id)
            _chat_ids.add(_default_chat_id)
    return _chat_ids


async def notify(message: str) -> None:
    """Send a notification to all registered channels."""
    logger.info("NOTIFY: %s", message[:100])

    chat_ids = _get_chat_ids()
    if not chat_ids:
        return

    token = os.environ.get("SKOPAQ_TELEGRAM_BOT_TOKEN", "")
    if not token:
        return

    try:
        from telegram import Bot

        bot = Bot(token)
        for chat_id in chat_ids:
            try:
                await bot.send_message(chat_id=chat_id, text=message)
            except Exception as exc:
                logger.warning("Telegram send to %s failed: %s", chat_id, exc)
    except ImportError:
        pass


async def notify_trade_event(
    action: str,
    symbol: str,
    price: float,
    quantity: int,
    status: str,
    pnl: float = 0,
    order_id: str = "",
    reason: str = "",
) -> None:
    """Send a trade event notification.

    ``status`` is FILLED, PARTIAL (a live order filled in part), UNCONFIRMED (a live
    order may still be working at the broker), FAILED or REJECTED; ``reason`` says why.
    """
    emoji = {"BUY": "🟢", "SELL": "🔴", "FILLED": "✅", "FAILED": "❌", "REJECTED": "🚫",
             "PARTIAL": "🟡", "UNCONFIRMED": "❓"}

    status_emoji = emoji.get(status, "📋")
    action_emoji = emoji.get(action, "📋")

    lines = [f"{action_emoji} {action} {symbol} — {status_emoji} {status}"]
    lines.append(f"Qty: {quantity} @ Rs {price:,.2f}")
    if order_id:
        lines.append(f"Order: {order_id}")
    if pnl:
        lines.append(f"P&L: Rs {pnl:+,.2f}")
    if reason:
        lines.append(reason)

    await notify("\n".join(lines))


async def notify_order_alert(
    severity: str,
    title: str,
    text: str,
    *,
    order_ids: Sequence[str] = (),
) -> None:
    """Send a CRITICAL/WARNING alert about a live order (from ``OrderAlerter``).

    Used when an order may still be working at the broker, a protective exit did not
    fill, or a SELL was refused; ``order_ids`` names the orders to check. Never raises.
    """
    head = ("🚨 CRITICAL ORDER ALERT" if severity.upper() == "CRITICAL"
            else "⚠️ WARNING ORDER ALERT")
    lines = [head, title, text]
    ids = [i for i in order_ids if i]
    if ids:
        lines.append("Orders: " + ", ".join(ids))
    try:
        await notify("\n".join(lines))
    except Exception as exc:
        logger.warning("Order alert %s not sent: %s", title, exc)


async def notify_gtt_event(
    event: str,
    symbol: str,
    trigger_price: float,
    target_price: float = 0,
    stop_loss_price: float = 0,
    trigger_id: int = 0,
    quantity: int = 1,
) -> None:
    """Send a GTT order notification."""
    emoji = {
        "PLACED": "🎯", "TRIGGERED": "🔔", "CANCELLED": "🚫",
        "EXPIRED": "⏰", "REJECTED": "❌",
    }

    e = emoji.get(event, "📋")
    lines = [f"{e} GTT {event}: {symbol}"]
    lines.append(f"Trigger: Rs {trigger_price:,.2f}")

    if target_price and stop_loss_price:
        lines.append(f"Target: Rs {target_price:,.2f} | Stop: Rs {stop_loss_price:,.2f}")

    lines.append(f"Qty: {quantity}")

    if trigger_id:
        lines.append(f"ID: {trigger_id}")

    if event == "PLACED":
        lines.append("\nZerodha watches 24/7. You'll be notified when triggered.")
    elif event == "TRIGGERED":
        lines.append("\nOrder executed automatically by Zerodha!")

    await notify("\n".join(lines))


async def notify_position_alert(
    symbol: str,
    ltp: float,
    entry: float,
    pnl: float,
    alert_type: str,
) -> None:
    """Send a position alert (new high, stop warning, etc.)."""
    emoji = {
        "NEW_HIGH": "📈", "STOP_WARNING": "⚠️", "TARGET_NEAR": "🎯",
        "TRAILING_STOP": "📉", "EOD_EXIT": "⏰",
    }

    e = emoji.get(alert_type, "📋")
    pnl_pct = ((ltp - entry) / entry) * 100

    lines = [f"{e} {alert_type.replace('_', ' ')}: {symbol}"]
    lines.append(f"LTP: Rs {ltp:,.2f} | Entry: Rs {entry:,.2f}")
    lines.append(f"P&L: Rs {pnl:+,.2f} ({pnl_pct:+.2f}%)")

    await notify("\n".join(lines))


async def notify_market_scan(results: list[dict]) -> None:
    """Send market scan results."""
    if not results:
        return

    lines = ["📊 Market Scan\n"]
    for r in results[:8]:
        emoji = "🟢" if r.get("change_pct", 0) >= 0 else "🔴"
        lines.append(
            f"{emoji} {r['symbol']}: Rs {r['ltp']:,.2f} "
            f"({r['change_pct']:+.2f}%)"
        )

    gainers = [r for r in results if r.get("change_pct", 0) > 0.5]
    if gainers:
        lines.append(f"\n💡 Top: {gainers[0]['symbol']} (+{gainers[0]['change_pct']:.2f}%)")

    await notify("\n".join(lines))


async def notify_options_trade(trade_text: str) -> None:
    """Send an options trade recommendation."""
    await notify(f"📊 Options Recommendation\n\n{trade_text}")


async def notify_eod_summary(portfolio_text: str) -> None:
    """Send end-of-day summary."""
    await notify(f"📋 EOD Summary\n\n{portfolio_text}")
