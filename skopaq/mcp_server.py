"""MCP Server for SkopaqTrader — exposes broker and analysis tools.

Run as a standalone MCP server via stdio transport::

    python -m skopaq.mcp_server

Or add to Claude Desktop / Claude Code config::

    {
      "mcpServers": {
        "skopaq": {
          "command": "python3",
          "args": ["-m", "skopaq.mcp_server"]
        }
      }
    }

Any MCP-compatible client (Claude Desktop, Cursor, Windsurf) will
automatically discover these tools and make them available to the AI.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import pandas as pd

from mcp.server import FastMCP

logger = logging.getLogger(__name__)

mcp = FastMCP(
    name="SkopaqTrader",
    instructions=(
        "SkopaqTrader MCP server for Indian equity and crypto trading. "
        "Provides real-time quotes, historical data, portfolio management, "
        "AI-powered stock analysis, market scanning, and trade execution. "
        "Paper trading mode is the default — safe for experimentation."
    ),
)

# ── Lazy infrastructure ──────────────────────────────────────────────────────
# Built on first tool call to avoid import-time overhead.

_infra_cache: dict = {}


def _get_config():
    if "config" not in _infra_cache:
        from skopaq.config import SkopaqConfig
        from skopaq.llm import bridge_env_vars

        config = SkopaqConfig()
        bridge_env_vars(config)
        _infra_cache["config"] = config
    return _infra_cache["config"]


def _get_router():
    if "router" not in _infra_cache:
        from skopaq.broker.paper_engine import PaperEngine
        from skopaq.execution.order_router import OrderRouter

        config = _get_config()
        paper = PaperEngine(initial_capital=config.initial_paper_capital)
        router = OrderRouter(config, paper)
        _infra_cache["router"] = router
        _infra_cache["paper"] = paper
    return _infra_cache["router"]


# ── Market Data Tools ────────────────────────────────────────────────────────


def _get_kite():
    """Return a connected KiteClient if available.

    Always reads the latest token from file/API — never caches stale tokens.
    This ensures the MCP server (long-running process) picks up new tokens
    after daily Kite login without restart.
    """
    try:
        from skopaq.broker.kite_client import KiteClient
        import skopaq.broker.kite_client as _kmod

        # Force re-read from file/API every time (don't trust memory cache)
        _kmod._access_token = ""  # Clear memory cache

        from skopaq.broker.kite_client import get_access_token
        token = get_access_token()
        if not token:
            return None
        config = _get_config()
        if not config.kite_api_key:
            return None
        return KiteClient(api_key=config.kite_api_key, access_token=token)
    except Exception:
        return None


@mcp.tool()
async def get_quote(symbol: str) -> str:
    """Get a real-time market quote for a stock symbol.

    Returns LTP, OHLC, bid/ask, volume, and change %.

    Args:
        symbol: Stock symbol (e.g. RELIANCE, TCS, INFY).
    """
    config = _get_config()

    # Try Kite Connect first (no IP whitelist issues)
    kite = _get_kite()
    if kite:
        q = await kite.get_quote(f"NSE:{symbol}", symbol=symbol)
    else:
        from skopaq.broker.client import INDstocksClient
        from skopaq.broker.scrip_resolver import resolve_scrip_code
        from skopaq.broker.token_manager import TokenManager

        token_mgr = TokenManager()
        async with INDstocksClient(config, token_mgr) as client:
            scrip_code = await resolve_scrip_code(client, symbol)
            q = await client.get_quote(scrip_code, symbol=symbol)

    return json.dumps({
        "symbol": q.symbol,
        "exchange": q.exchange,
        "ltp": q.ltp,
        "open": q.open,
        "high": q.high,
        "low": q.low,
        "close": q.close,
        "bid": q.bid,
        "ask": q.ask,
        "volume": q.volume,
        "change_pct": q.change_pct,
    })


@mcp.tool()
async def get_historical(
    symbol: str,
    days: int = 5,
    resolution: int = 1,
) -> str:
    """Fetch historical OHLCV candles for a symbol.

    Args:
        symbol: Stock symbol (e.g. RELIANCE).
        days: Number of days of history (default 5).
        resolution: Candle resolution in minutes (1, 5, 15, 60).
    """
    config = _get_config()

    from skopaq.broker.client import INDstocksClient
    from skopaq.broker.scrip_resolver import resolve_scrip_code
    from skopaq.broker.token_manager import TokenManager

    now = datetime.now(timezone.utc)
    from_ts = int((now.timestamp() - days * 86400) * 1000)
    to_ts = int(now.timestamp() * 1000)

    # Map numeric resolution to INDstocks interval string
    interval_map = {1: "1minute", 5: "5minute", 15: "15minute", 30: "30minute", 60: "60minute"}
    interval = interval_map.get(resolution, f"{resolution}minute")

    token_mgr = TokenManager()
    async with INDstocksClient(config, token_mgr) as client:
        scrip_code = await resolve_scrip_code(client, symbol)
        candles = await client.get_historical(
            scrip_code, interval=interval, start_time=from_ts, end_time=to_ts,
        )

    return json.dumps({
        "symbol": symbol,
        "count": len(candles),
        "resolution_min": resolution,
        "candles": [
            {"ts": c.timestamp.isoformat(), "o": c.open, "h": c.high,
             "l": c.low, "c": c.close, "v": c.volume}
            for c in candles[-20:]  # Last 20 candles
        ],
    })


# ── Portfolio Tools ──────────────────────────────────────────────────────────


@mcp.tool()
async def get_positions() -> str:
    """Get open positions with P&L."""
    kite = _get_kite()
    if kite:
        positions = await kite.get_positions()
    else:
        router = _get_router()
        positions = await router.get_positions()
    return json.dumps([
        {
            "symbol": p.symbol,
            "exchange": p.exchange,
            "quantity": str(p.quantity),
            "avg_price": p.average_price,
            "ltp": p.last_price,
            "pnl": p.pnl,
        }
        for p in positions
    ])


@mcp.tool()
async def get_holdings() -> str:
    """Get delivery holdings."""
    kite = _get_kite()
    if kite:
        holdings = await kite.get_holdings()
    else:
        router = _get_router()
        holdings = await router.get_holdings()
    return json.dumps([
        {
            "symbol": h.symbol,
            "quantity": str(h.quantity),
            "avg_price": h.average_price,
            "ltp": h.last_price,
            "pnl": h.pnl,
        }
        for h in holdings
    ])


@mcp.tool()
async def get_funds() -> str:
    """Get available cash, margin, and collateral."""
    kite = _get_kite()
    if kite:
        funds = await kite.get_funds()
    else:
        router = _get_router()
        funds = await router.get_funds()
    return json.dumps({
        "available_cash": funds.available_cash,
        "used_margin": funds.used_margin,
        "available_margin": funds.available_margin,
        "total_collateral": funds.total_collateral,
    })


@mcp.tool()
async def get_orders() -> str:
    """Get today's orders with status."""
    router = _get_router()
    orders = await router.get_orders()
    return json.dumps([
        {"order_id": o.order_id, "status": o.status, "message": o.message}
        for o in orders
    ])


# ── Analysis Tools ───────────────────────────────────────────────────────────


@mcp.tool()
async def analyze_stock(symbol: str, date: str = "") -> str:
    """Run full multi-agent AI analysis on a stock symbol.

    Returns BUY/SELL/HOLD recommendation with confidence, entry/stop/target
    prices, and reasoning. Takes 2-5 minutes.

    Args:
        symbol: Stock symbol (e.g. RELIANCE, TCS).
        date: Trade date YYYY-MM-DD (defaults to today).
    """
    config = _get_config()

    from skopaq.broker.paper_engine import PaperEngine
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter
    from skopaq.execution.safety_checker import SafetyChecker
    from skopaq.graph.skopaq_graph import SkopaqTradingGraph
    from skopaq.llm import build_llm_map

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    paper = PaperEngine(initial_capital=config.initial_paper_capital)
    router = OrderRouter(config, paper)
    safety = SafetyChecker(
        max_sector_concentration_pct=config.max_sector_concentration_pct
    )
    executor = Executor(router, safety)

    llm_map = build_llm_map()
    upstream = {
        "llm_provider": "google",
        "deep_think_llm": "gemini-3-flash-preview",
        "quick_think_llm": "gemini-3-flash-preview",
        "llm_map": llm_map,
        "max_debate_rounds": config.max_debate_rounds,
        "max_risk_discuss_rounds": config.max_risk_discuss_rounds,
        "asset_class": config.asset_class,
    }

    analysts = [a.strip() for a in config.selected_analysts.split(",") if a.strip()]
    graph = SkopaqTradingGraph(upstream, executor, selected_analysts=analysts)
    result = await graph.analyze(symbol, date)

    if result.error:
        return json.dumps({"error": result.error})

    signal = result.signal
    output = {
        "symbol": symbol,
        "date": date,
        "duration_seconds": result.duration_seconds,
    }
    if signal:
        output.update({
            "action": signal.action,
            "confidence": signal.confidence,
            "entry_price": signal.entry_price,
            "stop_loss": signal.stop_loss,
            "target": signal.target,
            "reasoning": signal.reasoning[:500] if signal.reasoning else "",
        })
    else:
        output["raw_decision"] = result.raw_decision[:500]

    return json.dumps(output)


@mcp.tool()
async def scan_market(max_candidates: int = 5) -> str:
    """Scan the market for top trading candidates using multi-model AI.

    Args:
        max_candidates: Maximum candidates to return (default 5).
    """
    config = _get_config()

    from langchain_core.messages import HumanMessage

    from skopaq.llm import build_llm_map, extract_text
    from skopaq.scanner import ScannerEngine, Watchlist

    llm_map = build_llm_map()
    watchlist = Watchlist()

    async def quote_fetcher(symbols):
        from skopaq.broker.client import INDstocksClient
        from skopaq.broker.scrip_resolver import resolve_scrip_code
        from skopaq.broker.token_manager import TokenManager

        token_mgr = TokenManager()
        async with INDstocksClient(config, token_mgr) as client:
            resolved = []
            for sym in symbols:
                try:
                    code = await resolve_scrip_code(client, sym)
                    resolved.append((sym, code))
                except ValueError:
                    pass
            if not resolved:
                return []
            syms, codes = zip(*resolved)
            raw = await client.get_quotes(list(codes), symbols=list(syms))
            return [
                {"symbol": q.symbol, "ltp": q.ltp, "open": q.open,
                 "high": q.high, "low": q.low, "close": q.close, "volume": q.volume}
                for q in raw
            ]

    async def _invoke(role, prompt):
        llm = llm_map.get(role, llm_map.get("_default"))
        if not llm:
            return "[]"
        resp = await llm.ainvoke([HumanMessage(content=prompt)])
        return extract_text(resp.content)

    scanner = ScannerEngine(
        watchlist=watchlist,
        max_candidates=max_candidates,
        quote_fetcher=quote_fetcher,
        llm_screener=lambda p: _invoke("market_analyst", p),
        news_screener=lambda p: _invoke("news_analyst", p),
        social_screener=lambda p: _invoke("social_analyst", p),
    )
    candidates = await scanner.scan_once()

    return json.dumps([
        {"symbol": c.symbol, "score": c.score, "reason": c.reason, "screener": c.screener}
        for c in candidates
    ])


@mcp.tool()
async def check_safety(
    symbol: str,
    quantity: int = 1,
    price: float = 0,
    side: str = "BUY",
) -> str:
    """Validate a hypothetical order against safety rules.

    Returns pass/fail with rejection reasons.

    Args:
        symbol: Stock symbol.
        quantity: Number of shares.
        price: Order price in INR.
        side: BUY or SELL.
    """
    from decimal import Decimal

    from skopaq.broker.models import OrderRequest, Side, TradingSignal

    router = _get_router()

    from skopaq.constants import PAPER_SAFETY_RULES
    from skopaq.execution.safety_checker import SafetyChecker

    safety = SafetyChecker(rules=PAPER_SAFETY_RULES)

    order = OrderRequest(
        symbol=symbol,
        side=Side(side.upper()),
        quantity=Decimal(str(quantity)),
        price=price if price else None,
    )
    signal = TradingSignal(
        symbol=symbol, action=side.upper(), confidence=50,
        entry_price=price if price else None, reasoning="Safety check",
    )
    positions = await router.get_positions()
    funds = await router.get_funds()

    result = safety.validate(
        order, signal, positions, funds,
        funds.available_cash + funds.used_margin,
    )

    return json.dumps({
        "passed": result.passed,
        "rejections": result.rejections,
    })


# ── System Tools ─────────────────────────────────────────────────────────────


@mcp.tool()
async def system_status() -> str:
    """Check SkopaqTrader system health: version, mode, token status, LLMs."""
    from skopaq import __version__
    from skopaq.broker.token_manager import TokenManager

    config = _get_config()
    health = TokenManager().get_health()

    llms = []
    for name, key_attr in [
        ("Gemini", "google_api_key"), ("Claude", "anthropic_api_key"),
        ("Perplexity", "perplexity_api_key"), ("Grok", "xai_api_key"),
    ]:
        if getattr(config, key_attr).get_secret_value():
            llms.append(name)

    return json.dumps({
        "version": __version__,
        "mode": config.trading_mode,
        "asset_class": config.asset_class,
        "token_valid": health.valid,
        "llms": llms,
        "paper_capital": config.initial_paper_capital,
    })


# ── Order Execution ──────────────────────────────────────────────────────────


@mcp.tool()
async def place_order(
    symbol: str,
    side: str = "BUY",
    quantity: int = 1,
    price: float = 0,
    order_type: str = "MARKET",
) -> str:
    """Place a paper or live order through the safety checker and order router.

    Paper mode is the default. Live mode requires explicit config change.
    All orders pass through the SafetyChecker before execution.

    Args:
        symbol: Stock symbol (e.g. RELIANCE).
        side: BUY or SELL.
        quantity: Number of shares.
        price: Limit price (0 = market order).
        order_type: MARKET or LIMIT.
    """
    from decimal import Decimal

    from skopaq.broker.models import (
        OrderRequest,
        OrderType,
        Side,
        TradingSignal,
    )
    from skopaq.constants import PAPER_SAFETY_RULES, SAFETY_RULES
    from skopaq.execution.safety_checker import SafetyChecker

    config = _get_config()
    router = _get_router()

    rules = PAPER_SAFETY_RULES if config.trading_mode == "paper" else SAFETY_RULES
    safety = SafetyChecker(
        rules=rules,
        max_sector_concentration_pct=config.max_sector_concentration_pct,
    )

    # Inject a quote for paper fill simulation
    if config.trading_mode == "paper" and price > 0:
        from skopaq.broker.models import Quote

        _infra_cache["paper"].update_quote(
            Quote(symbol=symbol, ltp=price, open=price, high=price,
                  low=price, close=price, bid=price, ask=price)
        )

    order = OrderRequest(
        symbol=symbol,
        side=Side(side.upper()),
        quantity=Decimal(str(quantity)),
        price=price if price > 0 else None,
        order_type=OrderType(order_type.upper()),
    )
    signal = TradingSignal(
        symbol=symbol,
        action=side.upper(),
        confidence=70,
        entry_price=price if price > 0 else None,
        quantity=Decimal(str(quantity)),
        reasoning="Placed via Claude Code MCP",
    )

    # Safety check first
    positions = await router.get_positions()
    funds = await router.get_funds()
    safety_result = safety.validate(
        order, signal, positions, funds,
        funds.available_cash + funds.used_margin,
    )

    if not safety_result.passed:
        return json.dumps({
            "success": False,
            "reason": f"Safety check failed: {safety_result.reason}",
        })

    # Execute
    result = await router.execute(order, signal)

    return json.dumps({
        "success": result.success,
        "mode": result.mode,
        "fill_price": result.fill_price,
        "slippage": result.slippage,
        "rejection_reason": result.rejection_reason or "",
    })


# ── Options Tools ─────────────────────────────────────────────────────────────


@mcp.tool()
async def get_option_chain(
    symbol: str = "NIFTY",
    expiry_index: int = 0,
) -> str:
    """Fetch the option chain for NIFTY, BANKNIFTY, or any stock.

    Returns all calls and puts with strike, premium, OI, volume,
    and distance from spot. Used by the AI for options strategy selection.

    Args:
        symbol: Underlying (NIFTY, BANKNIFTY, RELIANCE, etc.)
        expiry_index: 0 = nearest expiry, 1 = next week, etc.
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected. Login first."})

    try:
        from skopaq.options.chain import fetch_option_chain

        chain = await fetch_option_chain(kite, symbol, expiry_index)

        return json.dumps({
            "symbol": chain.symbol,
            "spot_price": chain.spot_price,
            "expiry": str(chain.expiry),
            "lot_size": chain.lot_size,
            "calls_count": len(chain.calls),
            "puts_count": len(chain.puts),
            "calls": [
                {
                    "strike": c.strike, "ltp": c.ltp, "oi": c.oi,
                    "volume": c.volume, "distance_pct": round(c.distance_pct, 2),
                    "tradingsymbol": c.tradingsymbol,
                }
                for c in chain.calls if c.ltp > 0
            ][:15],  # Top 15
            "puts": [
                {
                    "strike": p.strike, "ltp": p.ltp, "oi": p.oi,
                    "volume": p.volume, "distance_pct": round(p.distance_pct, 2),
                    "tradingsymbol": p.tradingsymbol,
                }
                for p in chain.puts if p.ltp > 0
            ][:15],
        })

    except Exception as exc:
        logger.exception("Option chain fetch failed")
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def suggest_option_trade(
    symbol: str = "NIFTY",
    strategy: str = "SHORT_PUT",
    expiry_index: int = 0,
) -> str:
    """AI-powered option trade suggestion. Analyzes the chain and recommends
    the optimal strike to sell with full risk metrics.

    Strategies:
    - SHORT_PUT: Sell OTM put (bullish view, profit from theta)
    - SHORT_CALL: Sell OTM call (bearish view, profit from theta)
    - SHORT_STRANGLE: Sell OTM put + call (neutral view, max theta)

    Args:
        symbol: Underlying (NIFTY, BANKNIFTY, RELIANCE, etc.)
        strategy: SHORT_PUT, SHORT_CALL, or SHORT_STRANGLE
        expiry_index: 0 = nearest expiry, 1 = next week
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected. Login first."})

    try:
        from skopaq.options.chain import fetch_option_chain
        from skopaq.options.strategy import (
            select_short_put,
            select_short_call,
            select_short_strangle,
            format_trade_for_telegram,
        )

        chain = await fetch_option_chain(kite, symbol, expiry_index)

        if strategy == "SHORT_PUT":
            trade = select_short_put(chain)
        elif strategy == "SHORT_CALL":
            trade = select_short_call(chain)
        elif strategy == "SHORT_STRANGLE":
            trade = select_short_strangle(chain)
        else:
            return json.dumps({"error": f"Unknown strategy: {strategy}"})

        if not trade:
            return json.dumps({"error": f"No suitable {strategy} trade found for {symbol}"})

        return format_trade_for_telegram(trade)

    except Exception as exc:
        logger.exception("Option trade suggestion failed")
        return json.dumps({"error": str(exc)})


# ── GTT (Good Till Triggered) Orders ─────────────────────────────────────────


@mcp.tool()
async def place_gtt_order(
    symbol: str,
    action: str = "BUY",
    trigger_price: float = 0,
    target_price: float = 0,
    stop_loss_price: float = 0,
    quantity: int = 1,
) -> str:
    """Place a GTT order that triggers automatically when the price is hit.

    For BUY: single trigger — executes when price drops to trigger_price (buy at support).
    For SELL with target + stop_loss: OCO order — whichever hits first executes.

    GTT orders live on Zerodha's server — zero monitoring needed.

    Args:
        symbol: Stock symbol (e.g., RELIANCE).
        action: BUY or SELL.
        trigger_price: Price at which to trigger (for single-trigger BUY).
        target_price: Upper sell trigger (for OCO SELL).
        stop_loss_price: Lower sell trigger (for OCO SELL).
        quantity: Number of shares.
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        from skopaq.options.gtt import place_gtt_buy, place_gtt_oco_sell

        if action.upper() == "BUY" and trigger_price > 0:
            result = await place_gtt_buy(
                kite, symbol, trigger_price, trigger_price, quantity,
            )
            return json.dumps({
                "success": True,
                "type": "GTT_BUY",
                "symbol": symbol,
                "trigger": trigger_price,
                "quantity": quantity,
                "gtt_id": result.get("trigger_id", ""),
                "message": f"GTT BUY set: buy {quantity}x {symbol} when price hits Rs {trigger_price}",
            })

        elif action.upper() == "SELL" and target_price > 0 and stop_loss_price > 0:
            result = await place_gtt_oco_sell(
                kite, symbol, target_price, stop_loss_price, quantity,
            )
            return json.dumps({
                "success": True,
                "type": "GTT_OCO_SELL",
                "symbol": symbol,
                "target": target_price,
                "stop_loss": stop_loss_price,
                "quantity": quantity,
                "gtt_id": result.get("trigger_id", ""),
                "message": (
                    f"GTT OCO SELL set: sell {quantity}x {symbol} at "
                    f"Rs {target_price} (target) or Rs {stop_loss_price} (stop-loss)"
                ),
            })
        else:
            return json.dumps({
                "error": "For BUY: provide trigger_price. For SELL: provide target_price + stop_loss_price.",
            })

    except Exception as exc:
        logger.exception("GTT order failed")
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def list_gtt_orders() -> str:
    """List all active GTT (Good Till Triggered) orders."""
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        from skopaq.options.gtt import list_gtts, format_gtt_for_telegram

        gtts = await list_gtts(kite)
        if not gtts:
            return "No active GTT orders."

        lines = [f"Active GTT Orders ({len(gtts)})\n"]
        for g in gtts:
            lines.append(format_gtt_for_telegram(g))
            lines.append("")

        return "\n".join(lines)

    except Exception as exc:
        logger.exception("List GTT failed")
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def setup_swing_trade(
    symbol: str,
    entry_price: float,
    target_price: float,
    stop_loss_price: float,
    quantity: int = 1,
) -> str:
    """Set up a complete CNC swing trade with automated exit via GTT.

    Places a GTT BUY at the entry price, then after fill, sets up a
    GTT OCO SELL with target + stop-loss. Fully automated — zero monitoring.

    Args:
        symbol: Stock symbol.
        entry_price: Buy trigger price (support level).
        target_price: Sell target price (resistance level).
        stop_loss_price: Stop-loss price.
        quantity: Number of shares.
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        from skopaq.options.gtt import place_gtt_buy

        # Step 1: Place GTT BUY at entry
        buy_result = await place_gtt_buy(
            kite, symbol, entry_price, entry_price, quantity,
        )

        risk = entry_price - stop_loss_price
        reward = target_price - entry_price
        rr = reward / risk if risk > 0 else 0

        return json.dumps({
            "success": True,
            "type": "SWING_TRADE_SETUP",
            "symbol": symbol,
            "entry": entry_price,
            "target": target_price,
            "stop_loss": stop_loss_price,
            "quantity": quantity,
            "risk_reward": f"1:{rr:.1f}",
            "buy_gtt_id": buy_result.get("trigger_id", ""),
            "message": (
                f"Swing trade set for {symbol}:\n"
                f"  BUY trigger: Rs {entry_price:,.2f} (GTT active)\n"
                f"  Target: Rs {target_price:,.2f} (+{((target_price-entry_price)/entry_price)*100:.1f}%)\n"
                f"  Stop Loss: Rs {stop_loss_price:,.2f} (-{((entry_price-stop_loss_price)/entry_price)*100:.1f}%)\n"
                f"  R:R = 1:{rr:.1f}\n\n"
                f"After BUY fills, set OCO SELL via: "
                f"place_gtt_order SELL {symbol} target={target_price} stop_loss={stop_loss_price}"
            ),
        })

    except Exception as exc:
        logger.exception("Swing trade setup failed")
        return json.dumps({"error": str(exc)})


# ── Advanced Order Types ─────────────────────────────────────────────────────


@mcp.tool()
async def place_amo_order(
    symbol: str,
    side: str = "BUY",
    quantity: int = 1,
    price: float = 0,
) -> str:
    """Place an After Market Order — executes at next day's open (9:15 AM).

    Can be placed between 3:30 PM and 9:00 AM. Perfect for evening analysis.

    Args:
        symbol: Stock symbol (e.g., RELIANCE).
        side: BUY or SELL.
        quantity: Number of shares.
        price: Limit price.
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        from skopaq.trading.advanced_orders import place_amo

        result = await place_amo(kite, symbol, side, quantity, price)
        return json.dumps({"success": True, **result,
            "message": f"AMO {side} {quantity}x {symbol} @ Rs {price} — executes at 9:15 AM"})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def place_bracket(
    symbol: str,
    side: str = "BUY",
    quantity: int = 1,
    price: float = 0,
    stoploss_points: float = 10,
    target_points: float = 20,
    trailing_sl: float = 0,
) -> str:
    """Place a Bracket Order — entry + target + stop-loss in one order (intraday).

    All three legs managed by exchange. Auto-squared at 3:20 PM.

    Args:
        symbol: Stock symbol.
        side: BUY or SELL.
        quantity: Number of shares.
        price: Entry limit price.
        stoploss_points: Stop-loss distance in points from entry.
        target_points: Target distance in points from entry.
        trailing_sl: Trailing stop-loss distance (0 = disabled).
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        from skopaq.trading.advanced_orders import place_bracket_order

        result = await place_bracket_order(
            kite, symbol, side, quantity, price,
            stoploss_points, target_points, trailing_sl,
        )
        return json.dumps({"success": True, **result})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def place_cover(
    symbol: str,
    side: str = "BUY",
    quantity: int = 1,
    price: float = 0,
    stoploss_price: float = 0,
) -> str:
    """Place a Cover Order — entry + mandatory stop-loss (intraday, reduced margin).

    Args:
        symbol: Stock symbol.
        side: BUY or SELL.
        quantity: Number of shares.
        price: Entry limit price.
        stoploss_price: Stop-loss trigger price (absolute).
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        from skopaq.trading.advanced_orders import place_cover_order

        result = await place_cover_order(kite, symbol, side, quantity, price, stoploss_price)
        return json.dumps({"success": True, **result})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def place_basket(orders_json: str) -> str:
    """Place multiple orders as a basket — execute the scanner's top picks at once.

    Args:
        orders_json: JSON array of orders, each with: symbol, side, quantity, price.
            Example: [{"symbol":"TCS","side":"BUY","quantity":1,"price":2500}]
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        orders = json.loads(orders_json)
        from skopaq.trading.advanced_orders import place_basket_orders

        results = await place_basket_orders(kite, orders)
        return json.dumps({"success": True, "orders": results})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def buy_option_contract(
    tradingsymbol: str,
    quantity: int = 1,
    price: float = 0,
) -> str:
    """Buy an option contract for directional trades (defined risk = premium paid).

    Use get_option_chain first to find the right contract.

    Args:
        tradingsymbol: Full option symbol (e.g., NIFTY2641323200CE).
        quantity: Number of lots x lot_size.
        price: Limit price (0 = market).
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        from skopaq.trading.advanced_orders import buy_option

        result = await buy_option(kite, tradingsymbol, quantity, price)
        return json.dumps({"success": True, **result})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def trade_future(
    tradingsymbol: str,
    side: str = "BUY",
    quantity: int = 1,
    price: float = 0,
) -> str:
    """Trade futures contracts (NIFTY/BANKNIFTY/stock futures).

    Args:
        tradingsymbol: Futures symbol (e.g., NIFTY26APRFUT).
        side: BUY or SELL.
        quantity: Number of lots x lot_size.
        price: Limit price (0 = market).
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        from skopaq.trading.advanced_orders import trade_futures

        result = await trade_futures(kite, tradingsymbol, side, quantity, price)
        return json.dumps({"success": True, **result})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def invest_mutual_fund(
    tradingsymbol: str,
    amount: float,
    sip: bool = False,
    frequency: str = "monthly",
) -> str:
    """Invest in mutual funds — lumpsum or SIP.

    Args:
        tradingsymbol: MF tradingsymbol (e.g., INF846K01DP8).
        amount: Investment amount in INR.
        sip: True for SIP, False for lumpsum.
        frequency: SIP frequency (monthly, weekly) — only for SIP.
    """
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        if sip:
            from skopaq.trading.advanced_orders import place_mf_sip

            result = await place_mf_sip(kite, tradingsymbol, amount, frequency)
        else:
            from skopaq.trading.advanced_orders import place_mf_order

            result = await place_mf_order(kite, tradingsymbol, amount)

        return json.dumps({"success": True, **result})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


@mcp.tool()
async def list_mutual_funds() -> str:
    """List mutual fund holdings and active SIPs."""
    kite = _get_kite()
    if not kite:
        return json.dumps({"error": "Kite not connected"})

    try:
        from skopaq.trading.advanced_orders import list_mf_holdings, list_mf_sips

        holdings = await list_mf_holdings(kite)
        sips = await list_mf_sips(kite)

        return json.dumps({
            "holdings": [
                {
                    "fund": h.get("tradingsymbol", ""),
                    "units": h.get("quantity", 0),
                    "avg_price": h.get("average_price", 0),
                    "ltp": h.get("last_price", 0),
                    "pnl": h.get("pnl", 0),
                }
                for h in holdings
            ],
            "sips": [
                {
                    "fund": s.get("tradingsymbol", ""),
                    "amount": s.get("instalment_amount", 0),
                    "frequency": s.get("frequency", ""),
                    "status": s.get("status", ""),
                    "next_date": str(s.get("next_instalment_date", "")),
                }
                for s in sips
            ],
        })
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ── Data Gathering Tools (for Claude-native analysis) ────────────────────────
# These tools fetch raw data that the multi-agent pipeline analysts need.
# Claude Code reasons with this data directly — no separate LLM calls.


def _setup_dataflow_config():
    """Ensure the upstream dataflow config is set for route_to_vendor."""
    from tradingagents.dataflows.config import get_config, set_config

    config = _get_config()
    is_crypto = config.asset_class == "crypto"
    upstream = {
        "data_vendors": {
            "core_stock_apis": "yfinance" if is_crypto else "indstocks,yfinance",
            "technical_indicators": "yfinance",
            "fundamental_data": "yfinance",
            "news_data": "yfinance",
        },
        "yfinance_symbol_suffix": "" if is_crypto else ".NS",
    }
    set_config(upstream)
    return upstream


@mcp.tool()
async def gather_market_data(symbol: str, date: str = "") -> str:
    """Gather OHLCV price data and all key technical indicators for a symbol.

    Returns stock data + RSI, MACD, Bollinger Bands, SMA, EMA, ATR, VWMA.
    This is the data the Market Analyst uses for technical analysis.

    Args:
        symbol: Stock symbol (e.g. RELIANCE, TCS).
        date: Trading date YYYY-MM-DD (defaults to today).
    """
    import asyncio

    from tradingagents.dataflows.router import route_to_vendor

    _setup_dataflow_config()

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    start_date = (
        datetime.now(timezone.utc) - __import__("datetime").timedelta(days=60)
    ).strftime("%Y-%m-%d")

    # Fetch OHLCV data
    try:
        ohlcv = await asyncio.to_thread(
            route_to_vendor, "get_stock_data", symbol, start_date, date
        )
    except Exception as e:
        ohlcv = f"Error fetching stock data: {e}"

    # Fetch all standard indicators
    indicators = {}
    indicator_names = [
        "rsi", "macd", "macds", "macdh",
        "boll", "boll_ub", "boll_lb", "atr",
        "close_50_sma", "close_200_sma", "close_10_ema", "vwma",
    ]
    for ind in indicator_names:
        try:
            val = await asyncio.to_thread(
                route_to_vendor, "get_indicators", symbol, ind, date, 30
            )
            indicators[ind] = val
        except Exception as e:
            indicators[ind] = f"Error: {e}"

    return json.dumps({
        "symbol": symbol,
        "date": date,
        "ohlcv": ohlcv[:3000] if isinstance(ohlcv, str) else str(ohlcv)[:3000],
        "indicators": indicators,
    })


@mcp.tool()
async def gather_news_data(symbol: str, date: str = "") -> str:
    """Gather company-specific news and global macroeconomic news.

    Returns news articles from the past 7 days. This is the data the
    News Analyst and Social Analyst use.

    Args:
        symbol: Stock symbol (e.g. RELIANCE, TCS).
        date: Trading date YYYY-MM-DD (defaults to today).
    """
    import asyncio

    from tradingagents.dataflows.router import route_to_vendor

    _setup_dataflow_config()

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    start_date = (
        datetime.now(timezone.utc) - __import__("datetime").timedelta(days=7)
    ).strftime("%Y-%m-%d")

    try:
        company_news = await asyncio.to_thread(
            route_to_vendor, "get_news", symbol, start_date, date
        )
    except Exception as e:
        company_news = f"Error: {e}"

    try:
        global_news = await asyncio.to_thread(
            route_to_vendor, "get_global_news", date, 7, 5
        )
    except Exception as e:
        global_news = f"Error: {e}"

    try:
        insider = await asyncio.to_thread(
            route_to_vendor, "get_insider_transactions", symbol
        )
    except Exception as e:
        insider = f"Error: {e}"

    return json.dumps({
        "symbol": symbol,
        "company_news": company_news[:3000] if isinstance(company_news, str) else str(company_news)[:3000],
        "global_news": global_news[:3000] if isinstance(global_news, str) else str(global_news)[:3000],
        "insider_transactions": insider[:2000] if isinstance(insider, str) else str(insider)[:2000],
    })


@mcp.tool()
async def gather_fundamentals_data(symbol: str, date: str = "") -> str:
    """Gather company profile, balance sheet, cash flow, and income statement.

    This is the data the Fundamentals Analyst uses for financial evaluation.

    Args:
        symbol: Stock symbol (e.g. RELIANCE, TCS).
        date: Trading date YYYY-MM-DD (defaults to today).
    """
    import asyncio

    from tradingagents.dataflows.router import route_to_vendor

    _setup_dataflow_config()

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    results = {}
    for method in ["get_fundamentals", "get_balance_sheet", "get_cashflow", "get_income_statement"]:
        try:
            if method == "get_fundamentals":
                val = await asyncio.to_thread(route_to_vendor, method, symbol, date)
            else:
                val = await asyncio.to_thread(route_to_vendor, method, symbol, "annual", date)
            results[method] = val[:3000] if isinstance(val, str) else str(val)[:3000]
        except Exception as e:
            results[method] = f"Error: {e}"

    return json.dumps({
        "symbol": symbol,
        "fundamentals": results.get("get_fundamentals", ""),
        "balance_sheet": results.get("get_balance_sheet", ""),
        "cash_flow": results.get("get_cashflow", ""),
        "income_statement": results.get("get_income_statement", ""),
    })


@mcp.tool()
async def gather_social_data(symbol: str, date: str = "") -> str:
    """Gather social media sentiment and company news for sentiment analysis.

    This is the data the Social Media Analyst uses.

    Args:
        symbol: Stock symbol (e.g. RELIANCE, TCS).
        date: Trading date YYYY-MM-DD (defaults to today).
    """
    import asyncio

    from tradingagents.dataflows.router import route_to_vendor

    _setup_dataflow_config()

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    start_date = (
        datetime.now(timezone.utc) - __import__("datetime").timedelta(days=7)
    ).strftime("%Y-%m-%d")

    try:
        social_news = await asyncio.to_thread(
            route_to_vendor, "get_news", symbol, start_date, date
        )
    except Exception as e:
        social_news = f"Error: {e}"

    return json.dumps({
        "symbol": symbol,
        "social_sentiment_news": social_news[:4000] if isinstance(social_news, str) else str(social_news)[:4000],
    })


@mcp.tool()
async def recall_agent_memories(situation_summary: str) -> str:
    """Retrieve past lessons from agent memory using BM25 similarity search.

    Searches the decision log (each past decision and, once settled, its
    outcome and reflection) plus the per-agent lessons (bull, bear, trader,
    invest_judge, risk_manager) recorded before the upstream v0.5.1 sync.

    Args:
        situation_summary: Description of current market situation
            (typically a summary of the analyst reports).
    """
    config = _get_config()

    if not config.supabase_url or not config.supabase_service_key.get_secret_value():
        return json.dumps({"memories": {}, "note": "Supabase not configured"})

    try:
        from skopaq.memory.store import MemoryStore
        from supabase import create_client

        client = create_client(
            config.supabase_url,
            config.supabase_service_key.get_secret_value(),
        )
        store = MemoryStore(client, max_entries=config.reflection_max_memory_entries)
        memories = store.recall(situation_summary, n_matches=2)

        return json.dumps({
            "memories": memories,
            "total_loaded": sum(len(v) for v in memories.values()),
        })

    except Exception as e:
        logger.warning("Memory recall failed: %s", e)
        return json.dumps({"memories": {}, "error": str(e)})


@mcp.tool()
async def gather_all_analysis_data(symbol: str, date: str = "") -> str:
    """One-shot data gathering for the full multi-agent analysis pipeline.

    Fetches market data, news, fundamentals, and social data in parallel,
    plus agent memories. Returns a complete data bundle. Takes 10-30 seconds.

    Args:
        symbol: Stock symbol (e.g. RELIANCE, TCS).
        date: Trading date YYYY-MM-DD (defaults to today).
    """
    import asyncio

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Fetch all data in parallel
    market_task = gather_market_data(symbol, date)
    news_task = gather_news_data(symbol, date)
    fundamentals_task = gather_fundamentals_data(symbol, date)
    social_task = gather_social_data(symbol, date)

    market, news, fundamentals, social = await asyncio.gather(
        market_task, news_task, fundamentals_task, social_task,
        return_exceptions=True,
    )

    # Convert exceptions to error strings
    for name, val in [("market", market), ("news", news), ("fundamentals", fundamentals), ("social", social)]:
        if isinstance(val, Exception):
            locals()[name] = json.dumps({"error": str(val)})

    # Retrieve agent memories using a brief summary
    summary = f"Analyzing {symbol} on {date}"
    try:
        memories = await recall_agent_memories(summary)
    except Exception:
        memories = json.dumps({"memories": {}})

    return json.dumps({
        "symbol": symbol,
        "date": date,
        "market_data": json.loads(market) if isinstance(market, str) else {"error": str(market)},
        "news_data": json.loads(news) if isinstance(news, str) else {"error": str(news)},
        "fundamentals_data": json.loads(fundamentals) if isinstance(fundamentals, str) else {"error": str(fundamentals)},
        "social_data": json.loads(social) if isinstance(social, str) else {"error": str(social)},
        "agent_memories": json.loads(memories) if isinstance(memories, str) else {"error": str(memories)},
    })


@mcp.tool()
async def save_trade_reflection(
    symbol: str,
    side: str,
    entry_price: float,
    exit_price: float,
    pnl: float,
    pnl_pct: float,
    entry_reason: str = "",
    exit_reason: str = "",
) -> str:
    """Generate and store a post-trade reflection to update agent memory.

    This creates a lesson learned from the trade outcome so future
    analyses benefit from past experience.

    Args:
        symbol: Stock symbol.
        side: BUY or SELL.
        entry_price: Entry fill price.
        exit_price: Exit fill price.
        pnl: Realized P&L in INR.
        pnl_pct: P&L as percentage.
        entry_reason: Why the entry signal was generated.
        exit_reason: Why the position was closed.
    """
    config = _get_config()

    if not config.supabase_url or not config.supabase_service_key.get_secret_value():
        return json.dumps({"saved": False, "reason": "Supabase not configured"})

    try:
        from supabase import create_client
        from skopaq.memory.reflection import generate_reflection, store_reflection
        from skopaq.llm import build_llm_map

        llm_map = build_llm_map()
        llm = llm_map.get("sell_analyst", llm_map.get("_default"))

        reflection = await generate_reflection(
            llm,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_reason=entry_reason,
            exit_reason=exit_reason,
        )

        client = create_client(
            config.supabase_url,
            config.supabase_service_key.get_secret_value(),
        )
        store_reflection(
            client,
            symbol=symbol,
            reflection=reflection,
            pnl=pnl,
            pnl_pct=pnl_pct,
        )

        return json.dumps({"saved": True, "reflection": reflection})

    except Exception as e:
        logger.warning("Reflection save failed: %s", e)
        return json.dumps({"saved": False, "error": str(e)})


# ── Strategy Refinement ──────────────────────────────────────────────────────


@mcp.tool()
async def backtest_strategy(
    symbol: str,
    days: int = 365,
    stop_loss_pct: float = 3.0,
    target_pct: float = 6.0,
) -> str:
    """Backtest the AI trading strategy on historical data.

    Runs signals against OHLCV history with realistic slippage and commission.
    Returns Sharpe, Sortino, Calmar, max drawdown, win rate, and profit factor.

    Args:
        symbol: Stock symbol (e.g., RELIANCE, TCS).
        days: Days of history to backtest (default 365).
        stop_loss_pct: Stop-loss percentage (default 3%).
        target_pct: Target percentage (default 6%).
    """
    import asyncio

    try:
        from tradingagents.dataflows.router import route_to_vendor
        from skopaq.backtest.engine import BacktestConfig, run_backtest, format_backtest_report

        _setup_dataflow_config()

        # Fetch historical data
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start_date = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")

        ohlcv_text = await asyncio.to_thread(
            route_to_vendor, "get_stock_data", symbol, start_date, end_date
        )

        # Parse CSV to DataFrame
        import io
        lines = [l for l in ohlcv_text.strip().split("\n") if not l.startswith("#")]
        df = pd.read_csv(io.StringIO("\n".join(lines)))

        if len(df) < 20:
            return f"Insufficient data for {symbol} ({len(df)} bars)"

        # Generate RSI mean-reversion signals
        # Buy when oversold (RSI < 35), sell when overbought (RSI > 65)
        df["RSI"] = _compute_rsi(df["Close"], 14)
        df["SMA_20"] = df["Close"].rolling(20).mean()

        signals = pd.DataFrame({
            "date": df["Date"],
            "signal": 0,
            "confidence": 50,
        })

        for i in range(20, len(df)):
            rsi = df.iloc[i]["RSI"]
            if pd.isna(rsi):
                continue
            if rsi < 35:
                signals.iloc[i, signals.columns.get_loc("signal")] = 1
                signals.iloc[i, signals.columns.get_loc("confidence")] = int(70 + (35 - rsi))
            elif rsi > 65:
                signals.iloc[i, signals.columns.get_loc("signal")] = -1

        config = BacktestConfig(
            stop_loss_pct=stop_loss_pct / 100,
            target_pct=target_pct / 100,
        )

        result = run_backtest(signals, df, config, symbol)
        return format_backtest_report(result)

    except Exception as exc:
        logger.exception("Backtest failed")
        return f"Backtest error: {exc}"


@mcp.tool()
async def run_monte_carlo_test(
    symbol: str,
    days: int = 365,
    simulations: int = 1000,
) -> str:
    """Run Monte Carlo simulation on a strategy's historical trades.

    Shuffles trade order 1000+ times to test if the edge is real or
    just luck from sequence. Shows worst-case drawdown and probability of ruin.

    Args:
        symbol: Stock symbol.
        days: Days of history.
        simulations: Number of scenarios (default 1000).
    """
    try:
        # First run backtest to get trades
        import asyncio, io
        from tradingagents.dataflows.router import route_to_vendor
        from skopaq.backtest.engine import BacktestConfig, run_backtest
        from skopaq.backtest.monte_carlo import run_monte_carlo, format_monte_carlo_report

        _setup_dataflow_config()

        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start_date = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")

        ohlcv_text = await asyncio.to_thread(
            route_to_vendor, "get_stock_data", symbol, start_date, end_date
        )

        lines = [l for l in ohlcv_text.strip().split("\n") if not l.startswith("#")]
        df = pd.read_csv(io.StringIO("\n".join(lines)))

        if len(df) < 20:
            return f"Insufficient data for {symbol}"

        # Generate signals + run backtest
        df["SMA_20"] = df["Close"].rolling(20).mean()
        df["RSI"] = _compute_rsi(df["Close"], 14)

        signals = pd.DataFrame({"date": df["Date"], "signal": 0, "confidence": 50})
        for i in range(20, len(df)):
            if df.iloc[i]["RSI"] < 35:
                signals.iloc[i, signals.columns.get_loc("signal")] = 1
            elif df.iloc[i]["RSI"] > 65:
                signals.iloc[i, signals.columns.get_loc("signal")] = -1

        bt_result = run_backtest(signals, df, BacktestConfig(), symbol)

        if len(bt_result.trades) < 5:
            return f"Too few trades ({len(bt_result.trades)}) for Monte Carlo"

        # Run Monte Carlo on trade P&Ls
        trade_pnls = [t.pnl for t in bt_result.trades]
        mc_result = run_monte_carlo(trade_pnls, n_simulations=simulations)

        return format_monte_carlo_report(mc_result)

    except Exception as exc:
        logger.exception("Monte Carlo failed")
        return f"Monte Carlo error: {exc}"


@mcp.tool()
async def get_learning_insights() -> str:
    """Get AI learning insights from past trading performance.

    Analyzes all recorded trades to find:
    - Which stocks the AI is best/worst at trading
    - Confidence calibration (is the AI overconfident or underconfident?)
    - Sector performance (which sectors have edge?)
    - Stop-loss effectiveness (too tight or too loose?)
    - Timing patterns (when do entries work best?)

    These insights should inform future trading decisions.
    """
    try:
        from skopaq.learning.tracker import (
            generate_learning_insights,
            get_confidence_calibration,
            get_sector_performance,
            get_timing_patterns,
            get_stop_loss_analysis,
        )

        insights = generate_learning_insights()

        # Add detailed breakdowns
        sections = [insights, ""]

        calibration = get_confidence_calibration()
        if calibration:
            sections.append("Confidence Calibration:")
            for c in calibration:
                sections.append(
                    f"  {c['confidence_range']}%: stated={c['stated_confidence']:.0f}% "
                    f"actual={c['actual_win_rate']:.0f}% gap={c['calibration_gap']:+.0f}%"
                )

        sectors = get_sector_performance()
        if sectors:
            sections.append("\nSector Performance:")
            for s in sectors:
                sections.append(
                    f"  {s['sector']}: {s['win_rate']:.0f}% win ({s['trades']} trades) "
                    f"P&L={s['total_pnl']:+,.0f}"
                )

        timing = get_timing_patterns()
        if timing:
            sections.append("\nTiming Patterns:")
            for t in timing:
                sections.append(
                    f"  {t['hour']}: {t['win_rate']:.0f}% win ({t['trades']} trades)"
                )

        sl = get_stop_loss_analysis()
        if sl:
            sections.append(f"\nStop-Loss: {sl.get('stop_hit_rate', 0):.0f}% hit rate")

        return "\n".join(sections)

    except Exception as exc:
        return f"Learning insights error: {exc}"


@mcp.tool()
async def get_symbol_stats(symbol: str) -> str:
    """Get AI trading performance stats for a specific symbol.

    Shows win rate, average P&L, confidence accuracy, and holding period
    based on all past trades for this symbol.

    Args:
        symbol: Stock symbol to check performance for.
    """
    try:
        from skopaq.learning.tracker import get_symbol_accuracy

        stats = get_symbol_accuracy(symbol)
        if not stats or stats.get("total_trades", 0) == 0:
            return f"No trading history for {symbol}"

        return (
            f"AI Performance: {symbol}\n\n"
            f"Total Trades: {stats['total_trades']}\n"
            f"Win Rate: {stats['win_rate']:.1f}%\n"
            f"Avg P&L: Rs {stats['avg_pnl']:+,.2f} ({stats['avg_pnl_pct']:+.2f}%)\n"
            f"Avg Confidence: {stats['avg_confidence']:.0f}%\n"
            f"Avg Holding: {stats['avg_holding_days']:.1f} days"
        )

    except Exception as exc:
        return f"Stats error: {exc}"


@mcp.tool()
async def evolve_strategy(symbol: str, days: int = 180) -> str:
    """Run a complete self-evolving strategy cycle.

    Backtests → validates (WFO + Monte Carlo) → adapts parameters →
    deploys if improved → persists results to Postgres → sends Telegram alert.

    This is the AI learning loop — every run makes the strategy smarter.

    Args:
        symbol: Stock symbol to evolve strategy for.
        days: Days of history for backtesting.
    """
    try:
        from skopaq.backtest.evolve import run_evolution_cycle, format_evolution_report

        report = await run_evolution_cycle(symbol, days)
        return format_evolution_report(report)

    except Exception as exc:
        logger.exception("Strategy evolution failed")
        return f"Evolution error: {exc}"


def _compute_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Compute RSI indicator."""
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="stdio")
