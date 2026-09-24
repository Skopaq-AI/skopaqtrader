"""LangChain tool definitions wrapping SkopaqTrader subsystem APIs.

Each ``@tool`` function accesses shared infrastructure via the module-level
``_infra`` object, which is set once per session by ``init_tools()``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from langchain_core.tools import tool

from skopaq.chat.session import Infrastructure

logger = logging.getLogger(__name__)

# Module-level infrastructure — set by init_tools() before first tool call.
_infra: Optional[Infrastructure] = None


def init_tools(infra: Infrastructure) -> None:
    """Bind shared infrastructure for all tool functions."""
    global _infra
    _infra = infra


def _get_infra() -> Infrastructure:
    if _infra is None:
        raise RuntimeError("Chat tools not initialised — call init_tools() first")
    return _infra


def _get_kite_client():
    """Return a connected KiteClient if available, else None.

    Only returns a client if both API key AND access token are available.
    The access token is checked from memory/file/env only — no remote
    fetch during this call (to avoid hanging in tests or offline use).
    """
    try:
        from skopaq.broker.kite_client import KiteClient

        # Check memory-level token only (don't trigger remote fetch)
        import skopaq.broker.kite_client as _kmod

        token = _kmod._access_token
        if not token:
            # Check file
            try:
                import json

                with open(_kmod._TOKEN_FILE) as f:
                    token = json.load(f).get("access_token", "")
            except Exception:
                pass
        if not token:
            return None

        config = _get_infra().config
        api_key = getattr(config, "kite_api_key", "")
        if not api_key:
            return None
        return KiteClient(api_key=api_key, access_token=token)
    except Exception:
        return None


# ── Tool Definitions ─────────────────────────────────────────────────────────


@tool
async def analyze_stock(symbol: str, date: str = "") -> str:
    """Run full multi-agent AI analysis on a stock symbol.

    Returns the agent recommendation (BUY/SELL/HOLD), confidence score,
    entry price, stop-loss, target, and detailed reasoning.
    This operation takes 2-5 minutes — tell the user to wait.

    Args:
        symbol: Stock symbol to analyze (e.g. RELIANCE, TCS, INFY).
        date: Trade date YYYY-MM-DD. Defaults to today.
    """
    infra = _get_infra()
    config = infra.config

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        from skopaq.graph.skopaq_graph import SkopaqTradingGraph

        # Handle crypto symbol translation
        analysis_symbol = symbol
        if config.asset_class == "crypto":
            from skopaq.broker.crypto_symbols import to_yfinance_ticker

            analysis_symbol = to_yfinance_ticker(symbol)

        analysts = [
            a.strip() for a in config.selected_analysts.split(",") if a.strip()
        ]
        graph = SkopaqTradingGraph(
            infra.upstream_config,
            infra.executor,
            selected_analysts=analysts,
        )
        result = await graph.analyze(analysis_symbol, date)

        if result.error:
            return f"Analysis failed: {result.error}"

        signal = result.signal
        if signal is None:
            return (
                f"Analysis complete for {symbol} ({date}).\n"
                f"Raw decision: {result.raw_decision[:500]}\n"
                f"Duration: {result.duration_seconds:.1f}s"
            )

        lines = [
            f"**{symbol}** Analysis ({date})",
            f"- Action: **{signal.action}**",
            f"- Confidence: {signal.confidence}%",
        ]
        if signal.entry_price:
            lines.append(f"- Entry Price: ₹{signal.entry_price:,.2f}")
        if signal.stop_loss:
            lines.append(f"- Stop Loss: ₹{signal.stop_loss:,.2f}")
        if signal.target:
            lines.append(f"- Target: ₹{signal.target:,.2f}")
        if signal.reasoning:
            lines.append(f"- Reasoning: {signal.reasoning[:600]}")
        lines.append(
            f"- Duration: {result.duration_seconds:.1f}s "
            f"(cache hits: {result.cache_hits})"
        )
        return "\n".join(lines)

    except Exception as exc:
        logger.exception("analyze_stock failed")
        return f"Analysis error: {exc}"


@tool
async def trade_stock(symbol: str, date: str = "") -> str:
    """Analyze a stock and execute the trade (paper mode by default).

    Runs the full multi-agent analysis pipeline, then executes BUY/SELL
    through the safety checker and order router. This takes 2-5 minutes.
    Paper mode is the default — live trades require explicit mode switch.

    Args:
        symbol: Stock symbol to trade (e.g. RELIANCE, TCS).
        date: Trade date YYYY-MM-DD. Defaults to today.
    """
    infra = _get_infra()
    config = infra.config

    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        from skopaq.graph.skopaq_graph import SkopaqTradingGraph

        # Inject paper quote so fill simulation works
        if config.trading_mode == "paper":
            if config.asset_class == "crypto":
                from skopaq.broker.binance_client import BinanceClient
                from skopaq.broker.crypto_symbols import to_binance_pair

                pair = to_binance_pair(symbol)
                client = BinanceClient(base_url=config.binance_base_url)
                try:
                    async with client:
                        quote = await client.get_quote(pair)
                        infra.paper_engine.update_quote(quote)
                except Exception as exc:
                    logger.warning("Crypto quote injection failed: %s", exc)
            else:
                await _inject_paper_quote(infra, symbol)

        # Handle crypto symbol translation
        analysis_symbol = symbol
        upstream_config = dict(infra.upstream_config)
        if config.asset_class == "crypto":
            from skopaq.broker.crypto_symbols import to_yfinance_ticker

            analysis_symbol = to_yfinance_ticker(symbol)
            upstream_config["_trade_symbol"] = symbol

        # Compute risk scales (equity only)
        regime_scale, calendar_scale = 1.0, 1.0
        if config.asset_class != "crypto":
            regime_scale, calendar_scale = _compute_risk_scales(config, date)

        analysts = [
            a.strip() for a in config.selected_analysts.split(",") if a.strip()
        ]
        graph = SkopaqTradingGraph(
            upstream_config,
            infra.executor,
            selected_analysts=analysts,
        )

        result = await graph.analyze_and_execute(
            analysis_symbol,
            date,
            regime_scale=regime_scale,
            calendar_scale=calendar_scale,
        )

        if result.error:
            return f"Trade failed: {result.error}"

        signal = result.signal
        execution = result.execution

        lines = [f"**{symbol}** Trade Result ({date})"]

        if signal:
            lines.append(f"- Signal: **{signal.action}** ({signal.confidence}%)")

        if execution:
            status = "Filled" if execution.success else "Rejected"
            lines.append(f"- Execution: {status} ({execution.mode} mode)")
            if execution.fill_price:
                lines.append(f"- Fill Price: ₹{execution.fill_price:,.2f}")
            if execution.slippage:
                lines.append(f"- Slippage: {execution.slippage:.4f}")
            if not execution.success and execution.rejection_reason:
                lines.append(f"- Reason: {execution.rejection_reason}")
        else:
            lines.append("- No execution (HOLD signal or analysis-only)")

        lines.append(f"- Duration: {result.duration_seconds:.1f}s")
        return "\n".join(lines)

    except Exception as exc:
        logger.exception("trade_stock failed")
        return f"Trade error: {exc}"


@tool
async def scan_market(max_candidates: int = 5) -> str:
    """Scan the market for top trading candidates using multi-model AI screening.

    Fetches real-time quotes, runs technical + news + social screens
    concurrently, and returns the top-ranked symbols with reasons.

    Args:
        max_candidates: Maximum candidates to return (default 5).
    """
    infra = _get_infra()
    config = infra.config

    try:
        from langchain_core.messages import HumanMessage

        from skopaq.llm import extract_text
        from skopaq.scanner import ScannerEngine, Watchlist

        llm_map = infra.llm_map
        is_crypto = config.asset_class == "crypto"

        if is_crypto:
            from skopaq.broker.crypto_symbols import CRYPTO_TOP_20

            watchlist = Watchlist(symbols=CRYPTO_TOP_20)

            async def quote_fetcher(symbols: list[str]) -> list[dict]:
                return []
        else:
            watchlist = Watchlist()

            async def quote_fetcher(symbols: list[str]) -> list[dict]:
                from skopaq.broker.client import INDstocksClient
                from skopaq.broker.scrip_resolver import resolve_scrip_code
                from skopaq.broker.token_manager import TokenManager

                token_mgr = TokenManager()
                async with INDstocksClient(config, token_mgr) as client:
                    resolved: list[tuple[str, str]] = []
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
                        {
                            "symbol": q.symbol,
                            "ltp": q.ltp,
                            "open": q.open,
                            "high": q.high,
                            "low": q.low,
                            "close": q.close,
                            "volume": q.volume,
                        }
                        for q in raw
                    ]

        async def _invoke_llm(role: str, prompt: str) -> str:
            llm = llm_map.get(role, llm_map.get("_default"))
            if llm is None:
                return "[]"
            msg = HumanMessage(content=prompt)
            if hasattr(llm, "ainvoke"):
                response = await llm.ainvoke([msg])
            else:
                import asyncio

                response = await asyncio.to_thread(lambda: llm.invoke([msg]))
            return extract_text(response.content)

        async def llm_screener(prompt: str) -> str:
            return await _invoke_llm("market_analyst", prompt)

        async def news_screener(prompt: str) -> str:
            return await _invoke_llm("news_analyst", prompt)

        async def social_screener(prompt: str) -> str:
            return await _invoke_llm("social_analyst", prompt)

        scanner = ScannerEngine(
            watchlist=watchlist,
            max_candidates=max_candidates,
            quote_fetcher=quote_fetcher,
            llm_screener=llm_screener,
            news_screener=news_screener,
            social_screener=social_screener,
        )
        candidates = await scanner.scan_once()

        if not candidates:
            return "No candidates found in the current scan cycle."

        lines = [f"**Scanner Results** ({len(candidates)} candidates)"]
        for i, c in enumerate(candidates, 1):
            lines.append(
                f"{i}. **{c.symbol}** — Score: {c.score:.0f}/100 "
                f"({c.screener})\n   {c.reason}"
            )
        return "\n".join(lines)

    except Exception as exc:
        logger.exception("scan_market failed")
        return f"Scan error: {exc}"


@tool
async def get_portfolio() -> str:
    """Get current portfolio positions, holdings, and available funds.

    Returns all open positions with P&L, delivery holdings, and cash balance.
    """
    infra = _get_infra()
    config = infra.config

    try:
        # Try Kite Connect first
        kite = _get_kite_client()
        if kite:
            positions = await kite.get_positions()
            holdings = await kite.get_holdings()
            funds = await kite.get_funds()
        elif config.trading_mode == "live":
            from skopaq.broker.client import INDstocksClient
            from skopaq.broker.token_manager import TokenManager

            token_mgr = TokenManager()
            async with INDstocksClient(config, token_mgr) as client:
                positions = await client.get_positions()
                holdings = await client.get_holdings()
                funds = await client.get_funds()
        else:
            positions = await infra.order_router.get_positions()
            holdings = await infra.order_router.get_holdings()
            funds = await infra.order_router.get_funds()

        lines = [f"**Portfolio** ({infra.order_router.mode} mode)"]

        # Funds
        lines.append(
            f"\nCash: ₹{funds.available_cash:,.2f} | "
            f"Margin Used: ₹{funds.used_margin:,.2f} | "
            f"Collateral: ₹{funds.total_collateral:,.2f}"
        )

        # Positions
        if positions:
            lines.append(f"\n**Open Positions** ({len(positions)})")
            for p in positions:
                pnl_sign = "+" if p.pnl >= 0 else ""
                lines.append(
                    f"- {p.symbol} ({p.exchange}): "
                    f"Qty={p.quantity} Avg=₹{p.average_price:,.2f} "
                    f"LTP=₹{p.last_price:,.2f} P&L={pnl_sign}₹{p.pnl:,.2f}"
                )
        else:
            lines.append("\nNo open positions.")

        # Holdings
        if holdings:
            lines.append(f"\n**Holdings** ({len(holdings)})")
            for h in holdings:
                pnl_sign = "+" if h.pnl >= 0 else ""
                lines.append(
                    f"- {h.symbol}: Qty={h.quantity} "
                    f"Avg=₹{h.average_price:,.2f} LTP=₹{h.last_price:,.2f} "
                    f"P&L={pnl_sign}₹{h.pnl:,.2f}"
                )
        else:
            lines.append("\nNo delivery holdings.")

        return "\n".join(lines)

    except Exception as exc:
        logger.exception("get_portfolio failed")
        return f"Portfolio error: {exc}"


@tool
async def get_quote(symbol: str) -> str:
    """Get a real-time market quote for a stock symbol.

    Returns last traded price, OHLC, bid/ask, volume, and change %.

    Args:
        symbol: Stock symbol (e.g. RELIANCE, TCS, INFY).
    """
    infra = _get_infra()
    config = infra.config

    try:
        # Try Kite Connect first (no IP whitelist issues)
        kite = _get_kite_client()
        if kite:
            q = await kite.get_quote(f"NSE:{symbol}", symbol=symbol)
        elif config.asset_class == "crypto":
            from skopaq.broker.binance_client import BinanceClient
            from skopaq.broker.crypto_symbols import to_binance_pair

            pair = to_binance_pair(symbol)
            client = BinanceClient(base_url=config.binance_base_url)
            async with client:
                q = await client.get_quote(pair)
        else:
            from skopaq.broker.client import INDstocksClient
            from skopaq.broker.scrip_resolver import resolve_scrip_code
            from skopaq.broker.token_manager import TokenManager

            token_mgr = TokenManager()
            async with INDstocksClient(config, token_mgr) as client:
                scrip_code = await resolve_scrip_code(client, symbol)
                q = await client.get_quote(scrip_code, symbol=symbol)

        return (
            f"**{q.symbol}** ({q.exchange})\n"
            f"- LTP: ₹{q.ltp:,.2f}\n"
            f"- Open: ₹{q.open:,.2f} | High: ₹{q.high:,.2f} | "
            f"Low: ₹{q.low:,.2f} | Close: ₹{q.close:,.2f}\n"
            f"- Bid: ₹{q.bid:,.2f} | Ask: ₹{q.ask:,.2f}\n"
            f"- Volume: {q.volume:,}\n"
            f"- Change: {q.change_pct:+.2f}%"
        )
    except Exception as exc:
        logger.exception("get_quote failed")
        return f"Quote error for {symbol}: {exc}"


@tool
async def get_orders() -> str:
    """Get today's orders with status, symbol, quantity, and price."""
    infra = _get_infra()
    config = infra.config

    try:
        if config.trading_mode == "live":
            from skopaq.broker.client import INDstocksClient
            from skopaq.broker.token_manager import TokenManager

            token_mgr = TokenManager()
            async with INDstocksClient(config, token_mgr) as client:
                order_book = await client.get_order_book()
                if not order_book:
                    return "No orders today."
                lines = [f"**Today's Orders** ({len(order_book)})"]
                for o in order_book:
                    if isinstance(o, dict):
                        lines.append(
                            f"- {o.get('id','?')}: {o.get('status','?')} "
                            f"{o.get('txn_type','')} {o.get('name','')} "
                            f"qty={o.get('traded_qty',0)}/{o.get('requested_qty',0)} "
                            f"@ {o.get('traded_price','0')}"
                        )
                    else:
                        lines.append(f"- {o.order_id}: {o.status} — {o.message}")
                return "\n".join(lines)

        orders = await infra.order_router.get_orders()
        if not orders:
            return "No orders today."

        lines = [f"**Today's Orders** ({len(orders)})"]
        for o in orders:
            lines.append(
                f"- {o.order_id}: {o.status} — {o.message}"
            )
        return "\n".join(lines)

    except Exception as exc:
        logger.exception("get_orders failed")
        return f"Orders error: {exc}"


@tool
async def check_status() -> str:
    """Check system health: version, trading mode, token status, configured LLMs."""
    infra = _get_infra()
    config = infra.config

    try:
        from skopaq import __version__
        from skopaq.broker.token_manager import TokenManager

        mgr = TokenManager()
        health = mgr.get_health()

        llms = []
        if config.google_api_key.get_secret_value():
            llms.append("Gemini")
        if config.anthropic_api_key.get_secret_value():
            llms.append("Claude")
        if config.perplexity_api_key.get_secret_value():
            llms.append("Perplexity")
        if config.xai_api_key.get_secret_value():
            llms.append("Grok")
        if config.openrouter_api_key.get_secret_value():
            llms.append("OpenRouter")

        token_status = "Valid" if health.valid else "Invalid/Missing"
        return (
            f"**System Status**\n"
            f"- Version: {__version__}\n"
            f"- Mode: {config.trading_mode}\n"
            f"- Asset Class: {config.asset_class}\n"
            f"- Token: {token_status}\n"
            f"- LLMs: {', '.join(llms) if llms else 'None configured'}\n"
            f"- Paper Capital: ₹{config.initial_paper_capital:,.0f}"
        )
    except Exception as exc:
        logger.exception("check_status failed")
        return f"Status error: {exc}"


@tool
async def compute_position_size(
    symbol: str,
    equity: float = 0,
    price: float = 0,
    date: str = "",
) -> str:
    """Compute ATR-based position size for a stock.

    Uses volatility-adjusted sizing: high-ATR stocks get smaller positions
    so every trade risks the same amount of capital.

    Args:
        symbol: Stock symbol (e.g. RELIANCE).
        equity: Portfolio equity in INR (0 = use paper capital).
        price: Entry price in INR (0 = fetch current LTP).
        date: Trade date YYYY-MM-DD (default today).
    """
    infra = _get_infra()
    config = infra.config

    if not infra.position_sizer:
        return "Position sizing is disabled in config."

    try:
        if not equity:
            funds = await infra.order_router.get_funds()
            equity = funds.available_cash

        if not price:
            # Try to get current price via quote
            try:
                from skopaq.broker.client import INDstocksClient
                from skopaq.broker.scrip_resolver import resolve_scrip_code
                from skopaq.broker.token_manager import TokenManager

                token_mgr = TokenManager()
                async with INDstocksClient(config, token_mgr) as client:
                    scrip_code = await resolve_scrip_code(client, symbol)
                    q = await client.get_quote(scrip_code, symbol=symbol)
                    price = q.ltp
            except Exception:
                return (
                    f"Could not fetch price for {symbol}. "
                    "Please provide the price parameter."
                )

        if not date:
            date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        ps = infra.position_sizer.compute_size(
            equity=equity,
            price=price,
            symbol=symbol,
            trade_date=date,
        )

        return (
            f"**Position Size for {symbol}**\n"
            f"- Quantity: {ps.quantity} shares\n"
            f"- Stop Loss: ₹{ps.stop_loss:,.2f}\n"
            f"- Risk Amount: ₹{ps.risk_amount:,.2f}\n"
            f"- ATR: ₹{ps.atr:,.2f} (source: {ps.atr_source})\n"
            f"- Entry Price: ₹{price:,.2f}\n"
            f"- Equity: ₹{equity:,.0f}"
        )
    except Exception as exc:
        logger.exception("compute_position_size failed")
        return f"Position sizing error: {exc}"


@tool
async def check_safety(
    symbol: str,
    quantity: int = 1,
    price: float = 0,
    side: str = "BUY",
) -> str:
    """Validate a hypothetical order against safety rules without executing.

    Checks market hours, position limits, stop-loss, concentration, and
    loss limits. Returns pass/fail with rejection reasons.

    Args:
        symbol: Stock symbol.
        quantity: Number of shares.
        price: Order price in INR (0 = skip price-dependent checks).
        side: BUY or SELL.
    """
    infra = _get_infra()

    try:
        from decimal import Decimal

        from skopaq.broker.models import OrderRequest, Side, TradingSignal

        order = OrderRequest(
            symbol=symbol,
            side=Side(side.upper()),
            quantity=Decimal(str(quantity)),
            price=price if price else None,
        )
        signal = TradingSignal(
            symbol=symbol,
            action=side.upper(),
            confidence=50,
            entry_price=price if price else None,
            reasoning="Safety check query",
        )

        positions = await infra.order_router.get_positions()
        holdings = await infra.order_router.get_holdings()
        funds = await infra.order_router.get_funds()
        portfolio_value = funds.available_cash + funds.used_margin

        result = infra.safety_checker.validate(
            order, signal, positions, funds, portfolio_value, holdings=holdings
        )

        if result.passed:
            return f"Safety check **PASSED** for {side} {quantity}x {symbol}."
        else:
            lines = [f"Safety check **FAILED** for {side} {quantity}x {symbol}:"]
            for reason in result.rejections:
                lines.append(f"- {reason}")
            return "\n".join(lines)

    except Exception as exc:
        logger.exception("check_safety failed")
        return f"Safety check error: {exc}"


@tool
async def get_market_data(
    symbol: str,
    days: int = 5,
    resolution: int = 1,
) -> str:
    """Fetch historical OHLCV candles for a symbol.

    Args:
        symbol: Stock symbol (e.g. RELIANCE).
        days: Number of days of history (default 5).
        resolution: Candle resolution in minutes (1, 5, 15, 60). Default 1.
    """
    infra = _get_infra()
    config = infra.config

    try:
        from skopaq.broker.client import INDstocksClient
        from skopaq.broker.scrip_resolver import resolve_scrip_code
        from skopaq.broker.token_manager import TokenManager

        now = datetime.now(timezone.utc)
        from_ts = int((now.timestamp() - days * 86400) * 1000)  # milliseconds
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

        if not candles:
            return f"No candle data available for {symbol} (last {days} days)."

        lines = [
            f"**{symbol}** — {len(candles)} candles "
            f"({days}d, {resolution}min resolution)"
        ]
        # Show last 10 candles to avoid flooding
        for c in candles[-10:]:
            lines.append(
                f"  {c.timestamp:%Y-%m-%d %H:%M} | "
                f"O={c.open:.2f} H={c.high:.2f} L={c.low:.2f} C={c.close:.2f} V={c.volume}"
            )
        if len(candles) > 10:
            lines.insert(1, f"  (showing last 10 of {len(candles)})")

        return "\n".join(lines)

    except Exception as exc:
        logger.exception("get_market_data failed")
        return f"Market data error for {symbol}: {exc}"


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _inject_paper_quote(infra: Infrastructure, symbol: str) -> None:
    """Fetch a real quote and inject it into the paper engine for fill simulation."""
    config = infra.config
    try:
        from skopaq.broker.client import INDstocksClient
        from skopaq.broker.scrip_resolver import resolve_scrip_code
        from skopaq.broker.token_manager import TokenManager

        token_mgr = TokenManager()
        async with INDstocksClient(config, token_mgr) as client:
            scrip_code = await resolve_scrip_code(client, symbol)
            quote = await client.get_quote(scrip_code, symbol=symbol)
            infra.paper_engine.update_quote(quote)
    except Exception as exc:
        logger.warning("Paper quote injection failed for %s: %s", symbol, exc)


def _compute_risk_scales(config, trade_date: str) -> tuple[float, float]:
    """Compute regime and calendar position-sizing multipliers."""
    from datetime import date as date_cls

    regime_scale = 1.0
    calendar_scale = 1.0

    if config.regime_detection_enabled:
        try:
            from skopaq.risk.regime import RegimeDetector, fetch_regime_data

            india_vix, nifty_price, nifty_sma200 = fetch_regime_data()
            detector = RegimeDetector()
            regime = detector.detect(india_vix, nifty_price, nifty_sma200)
            regime_scale = regime.position_scale
        except Exception:
            logger.warning("Regime detection failed", exc_info=True)

    try:
        from skopaq.risk.calendar import NSEEventCalendar

        cal = NSEEventCalendar()
        try:
            d = date_cls.fromisoformat(trade_date)
        except (ValueError, TypeError):
            d = date_cls.today()
        calendar_scale = cal.get_position_scale(d)
    except Exception:
        logger.warning("Calendar check failed", exc_info=True)

    return regime_scale, calendar_scale


@tool
async def recall_memory(query: str) -> str:
    """Search past trade reflections and lessons by keyword.

    Retrieves lessons learned from previous trades that match the query.
    Useful for questions like "what happened with momentum trades?" or
    "lessons about RELIANCE".

    Args:
        query: Search keyword or phrase (e.g. "momentum", "RELIANCE", "loss").
    """
    infra = _get_infra()
    config = infra.config

    if not config.supabase_url or not config.supabase_service_key.get_secret_value():
        return "Memory system not configured (Supabase credentials missing)."

    try:
        from supabase import create_client

        from skopaq.memory.reflection import recall

        client = create_client(
            config.supabase_url,
            config.supabase_service_key.get_secret_value(),
        )
        lessons = recall(client, query, limit=5)

        if not lessons:
            return f"No past reflections found matching '{query}'."

        lines = [f"**Past Lessons** matching '{query}' ({len(lessons)} results)"]
        for l in lessons:
            symbol = l.get("symbol", "?")
            date = l.get("trade_date", "?")
            pnl = l.get("pnl", 0)
            outcome = "PROFIT" if pnl > 0 else "LOSS" if pnl < 0 else "BREAKEVEN"
            lines.append(f"- **{symbol}** ({date}, {outcome}): {l.get('reflection', '')}")

        return "\n".join(lines)

    except Exception as exc:
        logger.exception("recall_memory failed")
        return f"Memory recall error: {exc}"


@tool
async def view_past_trades(symbol: str = "", days: int = 30) -> str:
    """View recent trade history with P&L.

    Args:
        symbol: Filter by symbol (empty = all symbols).
        days: Number of days to look back (default 30).
    """
    infra = _get_infra()
    config = infra.config

    if not config.supabase_url or not config.supabase_service_key.get_secret_value():
        return "Trade history not available (Supabase credentials missing)."

    try:
        from supabase import create_client

        from skopaq.db.repositories import TradeRepository

        client = create_client(
            config.supabase_url,
            config.supabase_service_key.get_secret_value(),
        )
        repo = TradeRepository(client)
        trades = repo.get_recent(limit=20)

        if symbol:
            trades = [t for t in trades if t.symbol.upper() == symbol.upper()]

        if not trades:
            msg = f"No trades found"
            if symbol:
                msg += f" for {symbol}"
            return msg + f" in the last {days} days."

        lines = [f"**Trade History** ({len(trades)} trades)"]
        for t in trades:
            pnl_str = f"₹{float(t.pnl):+,.2f}" if t.pnl else "pending"
            mode = "paper" if t.is_paper else "live"
            lines.append(
                f"- {t.side} **{t.symbol}** @ ₹{float(t.price or 0):,.2f} "
                f"qty={t.quantity} P&L={pnl_str} ({mode})"
            )

        return "\n".join(lines)

    except Exception as exc:
        logger.exception("view_past_trades failed")
        return f"Trade history error: {exc}"


def get_all_tools() -> list:
    """Return all tool functions for agent binding."""
    return [
        analyze_stock,
        trade_stock,
        scan_market,
        get_portfolio,
        get_quote,
        get_orders,
        check_status,
        compute_position_size,
        check_safety,
        get_market_data,
        recall_memory,
        view_past_trades,
    ]
