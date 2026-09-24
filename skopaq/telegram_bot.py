"""Telegram bot for SkopaqTrader — trade alerts, portfolio, and commands.

Run standalone::

    python -m skopaq.telegram_bot

Commands:
    /start          — Welcome message
    /quote SYMBOL   — Real-time stock quote
    /portfolio      — Positions, holdings, funds
    /status         — System health check
    /analyze SYMBOL — Quick Claude-style analysis (using data tools)
    /pnl            — Current P&L on open positions
    /login          — Send Kite Connect login link
    /help           — List commands

Scheduled jobs:
    09:00 IST — Pre-market Kite login reminder
    09:25 IST — Auto market scan (top movers)
    15:35 IST — EOD P&L summary
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import timezone
from typing import TYPE_CHECKING


def _clean_markdown(text: str) -> str:
    """Strip markdown formatting for Telegram plain text.

    Converts **bold** → BOLD, removes ``` code blocks,
    and cleans up table pipes for readable plain text.
    """
    # **bold** → text (uppercase for emphasis)
    text = re.sub(r"\*\*(.+?)\*\*", lambda m: m.group(1).upper(), text)
    # *italic* → text
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    # ```code``` → code
    text = re.sub(r"```\w*\n?", "", text)
    # `inline code` → code
    text = re.sub(r"`(.+?)`", r"\1", text)
    # Clean up excessive newlines
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

from telegram import Update

if TYPE_CHECKING:
    from skopaq.chat.session import ChatSession
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logger = logging.getLogger(__name__)


# ── Lazy infrastructure ──────────────────────────────────────────────────────

_infra_ready = False
alert_chat_ids: set[int] = set()  # Chat IDs registered for alerts/notifications


def _ensure_infra():
    """Bridge env vars on first use."""
    global _infra_ready
    if not _infra_ready:
        from skopaq.config import SkopaqConfig
        from skopaq.llm import bridge_env_vars

        config = SkopaqConfig()
        bridge_env_vars(config)
        _infra_ready = True


# ── Authorization ────────────────────────────────────────────────────────────


def _allowed_chat_ids() -> set[int]:
    """SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS plus the owner's SKOPAQ_TELEGRAM_CHAT_ID.

    Both empty: nobody may use the bot.
    """
    from skopaq.config import SkopaqConfig

    raw = SkopaqConfig().telegram_allowed_chat_ids
    raw += "," + os.environ.get("SKOPAQ_TELEGRAM_CHAT_ID", "")
    allowed = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            allowed.add(int(part))
    return allowed


def is_authorized(chat_id: int) -> bool:
    return chat_id in _allowed_chat_ids()


def authorized(handler):
    """Run *handler* only for allow-listed chats.

    The bot can place trades and shows the portfolio, so anyone else gets
    only their chat ID (to add to SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS).
    """
    import functools

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = update.effective_chat.id if update.effective_chat else None
        if chat_id is not None and is_authorized(chat_id):
            return await handler(update, context)
        logger.warning("Refused Telegram chat %s (not in the allow-list)", chat_id)
        if update.message:
            await update.message.reply_text(
                "This bot is private. To allow this chat, add its ID to "
                f"SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS: {chat_id}"
            )

    return wrapper


# ── Command Handlers ─────────────────────────────────────────────────────────


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Welcome message and register for alerts."""
    chat_id = update.message.chat.id
    alert_chat_ids.add(chat_id)

    # Register with centralized notification system
    from skopaq.notifications import register_chat
    register_chat(chat_id)
    await update.message.reply_text(
        "Welcome to SkopaqTrader! Your AI trading assistant.\n\n"
        "Commands:\n"
        "/quote SYMBOL - Live stock quote\n"
        "/portfolio - Positions & P&L\n"
        "/status - System health\n"
        "/pnl - Open position P&L\n"
        "/login - Connect to Zerodha\n"
        "/help - All commands\n\n"
        "Scheduled (auto):\n"
        "  09:00 IST - Login reminder\n"
        "  09:25 IST - Market scan\n"
        "  15:35 IST - EOD summary\n\n"
        "Or just chat naturally - I understand trading questions!"
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all commands."""
    await update.message.reply_text(
        "/quote SYMBOL — Real-time quote (LTP, OHLC, volume)\n"
        "/portfolio — Positions, holdings, funds\n"
        "/status — System health, mode, token\n"
        "/pnl — P&L on open positions\n"
        "/analyze SYMBOL — Quick technical analysis\n"
        "/halt REASON — Kill switch: stop all new BUYs everywhere\n"
        "/resume — Lift the kill switch\n"
        "/confirm, /cancel — Answer a pending trade\n"
        "/help — This message"
    )


async def cmd_halt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kill switch: reject every BUY everywhere until /resume."""
    from skopaq.execution import kill_switch

    reason = " ".join(context.args or []) or "halted from Telegram"
    where = kill_switch.halt(reason, by=f"telegram:{update.message.chat.id}")
    shared = "supabase:system_flags" in where
    await update.message.reply_text(
        f"Trading HALTED: {reason}\n"
        + ("Recorded in Supabase: every process is halted." if shared else
           "Supabase unavailable: only the bot's machine is halted.")
    )


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lift the kill switch."""
    from skopaq.execution import kill_switch

    kill_switch.resume(by=f"telegram:{update.message.chat.id}")
    after = kill_switch.status(use_cache=False)
    await update.message.reply_text(
        after.describe() if after.halted else "Trading resumed."
    )


async def cmd_quote(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Get real-time stock quote."""
    if not context.args:
        await update.message.reply_text("Usage: /quote RELIANCE")
        return

    symbol = context.args[0].upper()
    await update.message.reply_text(f"Fetching {symbol}...")

    try:
        _ensure_infra()
        from skopaq.mcp_server import get_quote

        result = json.loads(await get_quote(symbol))
        if "error" in result:
            await update.message.reply_text(f"Error: {result['error']}")
            return

        msg = (
            f"*{result['symbol']}* \\({result['exchange']}\\)\n\n"
            f"LTP: Rs {result['ltp']:,.2f} \\({result['change_pct']:+.2f}%\\)\n"
            f"Open: {result['open']:,.2f} \\| High: {result['high']:,.2f}\n"
            f"Low: {result['low']:,.2f} \\| Close: {result['close']:,.2f}\n"
            f"Volume: {result['volume']:,}"
        ).replace(".", "\\.").replace("-", "\\-").replace("+", "\\+").replace("(", "\\(").replace(")", "\\)")
        # Simpler approach — just use plain text
        plain = (
            f"{result['symbol']} ({result['exchange']})\n\n"
            f"LTP: Rs {result['ltp']:,.2f} ({result['change_pct']:+.2f}%)\n"
            f"Open: {result['open']:,.2f} | High: {result['high']:,.2f}\n"
            f"Low: {result['low']:,.2f} | Close: {result['close']:,.2f}\n"
            f"Volume: {result['volume']:,}"
        )
        await update.message.reply_text(plain)

    except Exception as exc:
        logger.exception("Quote failed")
        await update.message.reply_text(f"Error: {exc}")


async def cmd_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show portfolio — positions, holdings, funds."""
    await update.message.reply_text("Fetching portfolio...")

    try:
        _ensure_infra()
        from skopaq.mcp_server import get_positions, get_holdings, get_funds

        positions = json.loads(await get_positions())
        funds = json.loads(await get_funds())

        lines = [
            f"Cash: Rs {funds['available_cash']:,.2f}",
            f"Margin Used: Rs {funds['used_margin']:,.2f}",
            "",
        ]

        if positions:
            lines.append(f"Positions ({len(positions)}):")
            for p in positions:
                pnl_str = f"{p['pnl']:+,.2f}" if p.get('pnl') else "0"
                lines.append(
                    f"  {p['symbol']} | Qty: {p['quantity']} | "
                    f"Avg: {p['avg_price']:,.2f} | PnL: {pnl_str}"
                )
        else:
            lines.append("No open positions.")

        await update.message.reply_text("\n".join(lines))

    except Exception as exc:
        logger.exception("Portfolio failed")
        await update.message.reply_text(f"Error: {exc}")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """System health check."""
    try:
        _ensure_infra()
        from skopaq.mcp_server import system_status

        result = json.loads(await system_status())
        msg = (
            f"SkopaqTrader v{result['version']}\n"
            f"Mode: {result['mode'].upper()}\n"
            f"Asset: {result['asset_class']}\n"
            f"Token: {'Valid' if result['token_valid'] else 'EXPIRED'}\n"
            f"LLMs: {', '.join(result['llms'])}\n"
            f"Capital: Rs {result['paper_capital']:,.0f}"
        )
        await update.message.reply_text(msg)

    except Exception as exc:
        await update.message.reply_text(f"Error: {exc}")


async def cmd_pnl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show P&L on open positions with live quotes."""
    try:
        _ensure_infra()
        from skopaq.config import SkopaqConfig
        from skopaq.broker.client import INDstocksClient
        from skopaq.broker.token_manager import TokenManager
        from skopaq.broker.scrip_resolver import resolve_scrip_code

        config = SkopaqConfig()
        config.trading_mode = "live"
        token_mgr = TokenManager()

        async with INDstocksClient(config, token_mgr) as client:
            positions = await client.get_positions()
            if not positions:
                await update.message.reply_text("No open positions.")
                return

            lines = ["Open Positions P&L:\n"]
            total_pnl = 0.0
            for p in positions:
                sym = p.symbol if hasattr(p, 'symbol') else p.get('name', '?')
                qty = float(p.quantity if hasattr(p, 'quantity') else p.get('net_qty', 0))
                avg = float(p.average_price if hasattr(p, 'average_price') else p.get('avg_price', 0))

                try:
                    scrip = await resolve_scrip_code(client, sym)
                    q = await client.get_quote(scrip, symbol=sym)
                    ltp = q.ltp
                except Exception:
                    ltp = avg  # fallback

                pnl = (ltp - avg) * qty
                total_pnl += pnl
                pnl_pct = ((ltp - avg) / avg * 100) if avg else 0
                emoji = "+" if pnl >= 0 else ""
                lines.append(
                    f"{sym}: {int(qty)} @ {avg:.2f} -> {ltp:.2f}\n"
                    f"  P&L: {emoji}{pnl:.2f} ({emoji}{pnl_pct:.2f}%)"
                )

            lines.append(f"\nTotal P&L: Rs {total_pnl:+,.2f}")
            await update.message.reply_text("\n".join(lines))

    except Exception as exc:
        logger.exception("PnL check failed")
        await update.message.reply_text(f"Error: {exc}")


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Quick analysis using MCP data tools."""
    if not context.args:
        await update.message.reply_text("Usage: /analyze RELIANCE")
        return

    symbol = context.args[0].upper()
    await update.message.reply_text(f"Analyzing {symbol}... (10-20 sec)")

    try:
        _ensure_infra()
        from skopaq.mcp_server import gather_market_data

        data = json.loads(await gather_market_data(symbol))
        indicators = data.get("indicators", {})

        # Extract key values
        rsi_text = indicators.get("rsi", "")
        macd_text = indicators.get("macd", "")
        atr_text = indicators.get("atr", "")

        # Parse latest values
        def extract_latest(text):
            for line in text.split("\n"):
                if "N/A" not in line and ":" in line and "##" not in line and "values" not in line.lower():
                    parts = line.split(":")
                    if len(parts) >= 2:
                        try:
                            return float(parts[-1].strip())
                        except ValueError:
                            continue
            return None

        rsi = extract_latest(rsi_text)
        macd = extract_latest(macd_text)
        atr = extract_latest(atr_text)

        # Quick signal
        signal = "NEUTRAL"
        if rsi and rsi < 35:
            signal = "OVERSOLD (potential BUY)"
        elif rsi and rsi > 65:
            signal = "OVERBOUGHT (potential SELL)"
        elif rsi and rsi < 45 and macd and macd < 0:
            signal = "BEARISH"
        elif rsi and rsi > 55 and macd and macd > 0:
            signal = "BULLISH"

        msg = (
            f"Quick Analysis: {symbol}\n\n"
            f"RSI: {rsi:.1f}\n" if rsi else f"RSI: N/A\n"
        )
        msg += f"MACD: {macd:.2f}\n" if macd else "MACD: N/A\n"
        msg += f"ATR: {atr:.2f}\n" if atr else "ATR: N/A\n"
        msg += f"\nSignal: {signal}"
        msg += "\n\nUse Claude Code /analyze for full 15-agent pipeline."

        await update.message.reply_text(msg)

    except Exception as exc:
        logger.exception("Analyze failed")
        await update.message.reply_text(f"Error: {exc}")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle natural language — route through the AI chat brain.

    This gives the Telegram bot the same intelligence as Claude Code.
    The ReAct agent can call MCP tools (quotes, portfolio, analysis)
    based on the user's natural language input.
    """
    text = update.message.text.strip()
    chat_id = update.message.chat.id

    # Show typing indicator
    await update.message.chat.send_action("typing")

    try:
        _ensure_infra()
        from skopaq.chat.session import ChatSession
        from skopaq.config import SkopaqConfig

        # Get or create session for this chat
        session = _get_chat_session(chat_id)
        session.add_user_message(text)

        agent = session.ensure_agent()
        config = session.thread_config

        result = await agent.ainvoke(
            {"messages": session.get_history()},
            config=config,
        )
        result = await _resume_until_gated(update, agent, config, result)
        if result is None:
            return  # a trade is waiting for /confirm or /cancel

        await _reply_with_result(update, session, result)

    except Exception as exc:
        logger.exception("Chat brain failed")
        await update.message.reply_text(f"Error: {exc}")


def _pending_gated_calls(agent, config) -> list[dict]:
    """Tool calls the agent is paused on that need the user's confirmation."""
    from skopaq.chat.agent import GATED_TOOLS

    state = agent.get_state(config)
    if not state.next:
        return []
    messages = state.values.get("messages", [])
    calls = getattr(messages[-1], "tool_calls", None) or [] if messages else []
    return [c for c in calls if c.get("name") in GATED_TOOLS]


async def _resume_until_gated(update, agent, config, result, max_resumes: int = 10):
    """Resume past tool interrupts, stopping before any trade.

    Returns the final result, or ``None`` when a gated tool (a trade) is
    pending: the user is asked to /confirm or /cancel it, as the terminal
    chat asks before executing.
    """
    for _ in range(max_resumes):
        if not agent.get_state(config).next:
            return result
        gated = _pending_gated_calls(agent, config)
        if gated:
            details = "\n".join(
                f"{c['name']}: " + ", ".join(f"{k}={v}" for k, v in c.get("args", {}).items() if v)
                for c in gated
            )
            await update.message.reply_text(
                f"Confirm trade?\n{details}\n\nReply /confirm to execute or /cancel."
            )
            return None
        await update.message.chat.send_action("typing")
        result = await agent.ainvoke(None, config=config)
    return result


async def _reply_with_result(update, session, result) -> None:
    """Send the agent's last message to the chat."""
    messages = result.get("messages", [])
    ai_text = ""
    for msg in reversed(messages):
        if getattr(msg, "type", None) == "ai" and msg.content:
            ai_text = msg.content if isinstance(msg.content, str) else str(msg.content)
            break

    if ai_text:
        session.add_ai_message(ai_text)
        clean = _clean_markdown(ai_text)
        # Telegram has a 4096 char limit
        if len(clean) > 4000:
            for i in range(0, len(clean), 4000):
                await update.message.reply_text(clean[i:i + 4000])
        else:
            await update.message.reply_text(clean)
    else:
        await update.message.reply_text("I processed your request but have no text response.")


async def cmd_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute the trade the agent is waiting on."""
    await _answer_pending_trade(update, approve=True)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the trade the agent is waiting on."""
    await _answer_pending_trade(update, approve=False)


async def _answer_pending_trade(update: Update, approve: bool) -> None:
    chat_id = update.message.chat.id
    session = _telegram_sessions.get(chat_id)
    if session is None:
        await update.message.reply_text("No trade is waiting for confirmation.")
        return
    agent = session.ensure_agent()
    config = session.thread_config
    gated = _pending_gated_calls(agent, config)
    if not gated:
        await update.message.reply_text("No trade is waiting for confirmation.")
        return
    try:
        if not approve:
            from langchain_core.messages import ToolMessage

            calls = agent.get_state(config).values["messages"][-1].tool_calls
            agent.update_state(config, {"messages": [
                ToolMessage(content="Trade cancelled by user.", tool_call_id=c["id"])
                for c in calls
            ]})
        await update.message.chat.send_action("typing")
        result = await agent.ainvoke(None, config=config)
        result = await _resume_until_gated(update, agent, config, result)
        if result is not None:
            await _reply_with_result(update, session, result)
    except Exception as exc:
        logger.exception("Answering the pending trade failed")
        await update.message.reply_text(f"Error: {exc}")


# ── Session Management ───────────────────────────────────────────────────────

_telegram_sessions: dict[int, "ChatSession"] = {}


def _get_chat_session(chat_id: int):
    """Get or create a ChatSession for a Telegram chat."""
    if chat_id in _telegram_sessions:
        return _telegram_sessions[chat_id]

    from skopaq.chat.session import ChatSession
    from skopaq.config import SkopaqConfig

    config = SkopaqConfig()
    session = ChatSession(config)
    _telegram_sessions[chat_id] = session
    return session


# ── Alert System ─────────────────────────────────────────────────────────────


async def send_alert(app: Application, chat_id: int, message: str) -> None:
    """Send a trade alert to a specific chat."""
    await app.bot.send_message(chat_id=chat_id, text=message)


# ── Scheduled Jobs ───────────────────────────────────────────────────────────


async def job_pre_market_login(context: ContextTypes.DEFAULT_TYPE) -> None:
    """9:00 AM IST — Send Kite login link before market opens."""
    from skopaq.broker.kite_client import get_access_token

    for chat_id in list(alert_chat_ids):
        token = get_access_token()
        if token:
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Good morning! Kite is already connected.\n"
                    "Market opens at 9:15 IST. Standing by for auto-scan at 9:25."
                ),
            )
        else:
            api_key = os.environ.get("SKOPAQ_KITE_API_KEY", "")
            login_url = f"https://skopaq-trader.fly.dev/api/kite/login"
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    "Good morning! Time to connect to Zerodha.\n\n"
                    f"Tap to login: {login_url}\n\n"
                    "After login, I'll auto-scan the market at 9:25 IST "
                    "and send you the top picks."
                ),
            )


async def job_market_scan(context: ContextTypes.DEFAULT_TYPE) -> None:
    """9:25 AM IST — Auto-scan market after prices settle."""
    from skopaq.broker.kite_client import get_access_token

    if not get_access_token():
        for chat_id in list(alert_chat_ids):
            await context.bot.send_message(
                chat_id=chat_id,
                text="Kite not connected. Please login first:\nhttps://skopaq-trader.fly.dev/api/kite/login",
            )
        return

    for chat_id in list(alert_chat_ids):
        await context.bot.send_message(
            chat_id=chat_id,
            text="Market open! Scanning NIFTY 50 for opportunities...",
        )

    # Scan top stocks using Kite directly
    try:
        from skopaq.broker.kite_client import KiteClient

        api_key = os.environ.get("SKOPAQ_KITE_API_KEY", "")
        client = KiteClient(api_key=api_key, access_token=get_access_token())

        symbols = [
            "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
            "SBIN", "LT", "BHARTIARTL", "WIPRO", "NTPC",
        ]
        results = []
        for sym in symbols:
            try:
                q = await client.get_quote(f"NSE:{sym}", symbol=sym)
                results.append({
                    "symbol": q.symbol,
                    "ltp": q.ltp,
                    "change_pct": q.change_pct,
                    "volume": q.volume,
                })
            except Exception:
                pass

        # Sort by change% (top movers)
        results.sort(key=lambda x: abs(x.get("change_pct", 0)), reverse=True)

        lines = ["📊 Market Scan (09:25 IST)\n"]
        for r in results[:8]:
            emoji = "🟢" if r["change_pct"] >= 0 else "🔴"
            lines.append(
                f"{emoji} {r['symbol']}: Rs {r['ltp']:,.2f} "
                f"({r['change_pct']:+.2f}%) Vol: {r['volume']:,}"
            )

        # Identify top pick
        gainers = [r for r in results if r["change_pct"] > 0.5]
        if gainers:
            top = gainers[0]
            lines.append(f"\n💡 Top pick: {top['symbol']} (+{top['change_pct']:.2f}%)")

        lines.append("\nReply 'analyze SYMBOL' for deep analysis")
        lines.append("Reply 'trade SYMBOL' to execute")

        scan_msg = "\n".join(lines)
        for chat_id in list(alert_chat_ids):
            await context.bot.send_message(chat_id=chat_id, text=scan_msg)

    except Exception as exc:
        logger.exception("Auto-scan failed")
        for chat_id in list(alert_chat_ids):
            await context.bot.send_message(chat_id=chat_id, text=f"Scan error: {exc}")


async def job_eod_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    """3:35 PM IST — End of day P&L summary."""
    from skopaq.broker.kite_client import get_access_token, KiteClient

    if not get_access_token():
        return

    try:
        _ensure_infra()
        api_key = os.environ.get("SKOPAQ_KITE_API_KEY", "")
        client = KiteClient(api_key=api_key, access_token=get_access_token())

        positions = await client.get_positions()
        funds = await client.get_funds()

        total_pnl = sum(p.pnl for p in positions) if positions else 0

        lines = [
            "Market Closed — EOD Summary\n",
            f"Cash: Rs {funds.available_cash:,.2f}",
        ]

        if positions:
            lines.append(f"\nOpen Positions ({len(positions)}):")
            for p in positions:
                lines.append(
                    f"  {p.symbol}: {int(p.quantity)}x @ {p.average_price:.2f} "
                    f"P&L: Rs {p.pnl:+,.2f}"
                )
            lines.append(f"\nTotal Day P&L: Rs {total_pnl:+,.2f}")
        else:
            lines.append("\nNo open positions.")

        msg = "\n".join(lines)
        for chat_id in list(alert_chat_ids):
            await context.bot.send_message(chat_id=chat_id, text=msg)

    except Exception as exc:
        logger.exception("EOD summary failed")


async def cmd_login(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send Kite login link."""
    alert_chat_ids.add(update.message.chat.id)
    login_url = "https://skopaq-trader.fly.dev/api/kite/login"

    from skopaq.broker.kite_client import get_access_token
    if get_access_token():
        await update.message.reply_text("Already connected to Zerodha!")
    else:
        await update.message.reply_text(
            f"Tap to connect Zerodha:\n{login_url}"
        )


# ── Entry Point ──────────────────────────────────────────────────────────────


def main() -> None:
    """Start the Telegram bot with scheduled jobs."""
    from datetime import time as dt_time

    token = os.environ.get("SKOPAQ_TELEGRAM_BOT_TOKEN", "")
    if not token:
        from skopaq.config import SkopaqConfig

        config = SkopaqConfig()
        token = getattr(config, "telegram_bot_token", "")
        if hasattr(token, "get_secret_value"):
            token = token.get_secret_value()

    if not token:
        print("Error: SKOPAQ_TELEGRAM_BOT_TOKEN not set")
        return

    allowed = _allowed_chat_ids()
    if not allowed:
        print("Warning: SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS is empty — the bot answers "
              "nobody. Send it /start to learn your chat ID.")
    # Allow-listed chats get scheduled alerts without re-sending /start after a restart.
    alert_chat_ids.update(allowed)

    print("Starting SkopaqTrader Telegram bot...")

    app = Application.builder().token(token).build()

    # Register command handlers
    # Every handler is limited to SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS.
    for name, handler in (
        ("start", cmd_start), ("help", cmd_help), ("quote", cmd_quote),
        ("portfolio", cmd_portfolio), ("status", cmd_status), ("pnl", cmd_pnl),
        ("analyze", cmd_analyze), ("login", cmd_login), ("halt", cmd_halt),
        ("resume", cmd_resume), ("confirm", cmd_confirm), ("cancel", cmd_cancel),
    ):
        app.add_handler(CommandHandler(name, authorized(handler)))

    # Plain text → AI chat brain
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, authorized(handle_text)))

    # ── Scheduled jobs (IST = UTC+5:30) ──────────────────────────
    # IST 9:00 = UTC 3:30
    app.job_queue.run_daily(
        job_pre_market_login,
        time=dt_time(hour=3, minute=30, tzinfo=timezone.utc),
        name="pre_market_login",
    )

    # IST 9:25 = UTC 3:55
    app.job_queue.run_daily(
        job_market_scan,
        time=dt_time(hour=3, minute=55, tzinfo=timezone.utc),
        name="market_scan",
    )

    # IST 15:35 = UTC 10:05
    app.job_queue.run_daily(
        job_eod_summary,
        time=dt_time(hour=10, minute=5, tzinfo=timezone.utc),
        name="eod_summary",
    )

    print("Scheduled jobs:")
    print("  09:00 IST — Pre-market login reminder")
    print("  09:25 IST — Auto market scan")
    print("  15:35 IST — EOD P&L summary")
    print("Bot ready. Polling for messages...")

    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
