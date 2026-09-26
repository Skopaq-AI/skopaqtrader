"""Typer CLI for SkopaqTrader.

Usage examples::

    skopaq token set <token>          # Store INDstocks API token
    skopaq token status               # Check token health
    skopaq status                     # System health overview
    skopaq analyze RELIANCE           # Run agent analysis (no execution)
    skopaq trade RELIANCE             # Analyze + execute
    skopaq serve                      # Start FastAPI backend
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Optional

import typer

from skopaq import __version__
from skopaq.cli.display import (
    display_analyze_result,
    display_analyze_start,
    display_daemon_report,
    display_daemon_start,
    display_error,
    display_info,
    display_legacy_memories,
    display_monitor_ai_decision,
    display_report,
    display_monitor_result,
    display_monitor_start,
    display_monitor_tick,
    display_scan_results,
    display_scan_start,
    display_serve_banner,
    display_status,
    display_success,
    display_token_health,
    display_token_set,
    display_trade_result,
    display_trade_start,
    display_version,
    display_welcome,
)

logger = logging.getLogger(__name__)

# `skopaq trade` (live), stopped while a confirmed fill's trade row is being written: wait
# this long for the write before the process exits
_PERSIST_WAIT_S = 30.0

app = typer.Typer(
    name="skopaq",
    help="SkopaqTrader — AI algorithmic trading platform for Indian equities.",
    no_args_is_help=True,
)

# ── Token management ─────────────────────────────────────────────────────────

token_app = typer.Typer(help="INDstocks API token management.")
app.add_typer(token_app, name="token")

memory_app = typer.Typer(help="Agent memory stored in Supabase.")
app.add_typer(memory_app, name="memory")


@token_app.command("set")
def token_set(
    token: str = typer.Argument(..., help="Bearer token from INDstocks dashboard."),
    ttl: float = typer.Option(24.0, help="Token TTL in hours."),
) -> None:
    """Encrypt and store an INDstocks API token."""
    from skopaq.broker.token_manager import TokenManager

    mgr = TokenManager()
    mgr.set_token(token, ttl_hours=ttl)
    health = mgr.get_health()
    display_token_set(health)


@token_app.command("status")
def token_status() -> None:
    """Check current token health."""
    from skopaq.broker.token_manager import TokenManager

    mgr = TokenManager()
    health = mgr.get_health()
    display_token_health(health)

    if not health.valid:
        raise typer.Exit(code=1)


@token_app.command("clear")
def token_clear() -> None:
    """Delete stored token."""
    from skopaq.broker.token_manager import TokenManager

    mgr = TokenManager()
    mgr.clear()
    display_success("Token cleared")


# ── Status ───────────────────────────────────────────────────────────────────


@app.command("status")
def status() -> None:
    """Show system health overview."""
    from skopaq.broker.token_manager import TokenManager
    from skopaq.config import SkopaqConfig

    config = SkopaqConfig()
    mgr = TokenManager()
    health = mgr.get_health()

    # Detect configured LLMs
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

    from skopaq.execution import kill_switch

    display_welcome()
    display_status(__version__, config, health, llms, halt=kill_switch.status())


@app.command("report")
def report(
    days: int = typer.Option(90, help="How many days back to include."),
) -> None:
    """Track record: AI calls vs NIFTY, closed trades, confidence calibration.

    Read from the decision log and the trades table (paper or live, per
    SKOPAQ_TRADING_MODE). Forward results only: backtests of an LLM on past
    dates are contaminated by what the model already knows.
    """
    from skopaq.config import SkopaqConfig
    from skopaq.learning.report import build_report

    display_report(build_report(SkopaqConfig(), days=days))


@app.command("halt")
def halt(
    reason: str = typer.Argument("manual halt", help="Why trading is being halted."),
) -> None:
    """Kill switch: reject every BUY everywhere until `skopaq resume`.

    Applies to the daemon, `skopaq trade`, MCP and chat. SELLs stay allowed
    so open positions can still be protected.
    """
    from skopaq.execution import kill_switch

    try:
        where = kill_switch.halt(reason, by="cli")
    except RuntimeError as exc:  # neither the halt file nor Supabase could record it
        display_error(str(exc))
        raise typer.Exit(1)
    display_success(f"Trading HALTED: {reason}\nRecorded in: {', '.join(where)}")
    if "supabase:system_flags" not in where:
        display_error(
            "Not recorded in Supabase, so only this machine is halted. On other "
            "machines (e.g. the Railway daemon) set SKOPAQ_TRADING_HALTED=true."
        )


@app.command("resume")
def resume(
    yes: bool = typer.Option(False, "--yes", help="Resume without asking for confirmation."),
) -> None:
    """Lift the kill switch set by `skopaq halt`."""
    from skopaq.execution import kill_switch

    current = kill_switch.status(use_cache=False)
    if not current.halted:
        display_info("Trading is not halted.")
        return
    if not yes and not typer.confirm(f"{current.describe()}\nResume trading?"):
        display_info("Still halted.")
        return
    cleared = kill_switch.resume(by="cli")
    after = kill_switch.status(use_cache=False)
    if after.halted:
        display_error(f"Unset SKOPAQ_TRADING_HALTED to resume — {after.describe()}")
        raise typer.Exit(1)
    display_success(f"Trading resumed (cleared: {', '.join(cleared) or 'nothing'}).")


# ── Analyze ──────────────────────────────────────────────────────────────────


@app.command("analyze")
def analyze(
    symbol: str = typer.Argument(..., help="Stock symbol to analyze (e.g., RELIANCE)."),
    date: str = typer.Option("", help="Trade date (YYYY-MM-DD). Defaults to today."),
) -> None:
    """Run agent analysis for a symbol (no execution)."""
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    display_analyze_start(symbol, date)
    result = asyncio.run(_run_analyze(symbol, date))

    if result.error:
        display_error(result.error)
        raise typer.Exit(code=1)

    display_analyze_result(result)


async def _run_analyze(symbol: str, trade_date: str):
    """Helper to run async analysis."""
    from skopaq.broker.paper_engine import PaperEngine
    from skopaq.config import SkopaqConfig
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter
    from skopaq.execution.safety_checker import SafetyChecker
    from skopaq.graph.skopaq_graph import SkopaqTradingGraph

    config = SkopaqConfig()
    paper = PaperEngine(initial_capital=config.initial_paper_capital)
    router = OrderRouter(config, paper)
    safety = SafetyChecker(
        max_sector_concentration_pct=config.max_sector_concentration_pct,
    )
    from skopaq.execution.pnl_history import seed_safety_checker
    seed_safety_checker(safety, config)
    executor = Executor(router, safety)

    # Build upstream config (uses upstream defaults + our keys)
    upstream_config = _build_upstream_config(config)

    # For crypto, translate BTCUSDT → BTC-USD for yfinance-based analysis
    if config.asset_class == "crypto":
        from skopaq.broker.crypto_symbols import to_yfinance_ticker
        analysis_symbol = to_yfinance_ticker(symbol)
        logger.info("Crypto: %s → analysis as %s", symbol, analysis_symbol)
    else:
        analysis_symbol = symbol

    # Load persisted memories so agents have context from past trades
    memory_store = _create_memory_store(config)

    analysts = [a.strip() for a in config.selected_analysts.split(",") if a.strip()]
    graph = SkopaqTradingGraph(
        upstream_config, executor,
        selected_analysts=analysts,
        memory_store=memory_store,
    )
    return await graph.analyze(analysis_symbol, trade_date)


# ── Trade ────────────────────────────────────────────────────────────────────


@app.command("trade")
def trade(
    symbol: str = typer.Argument(..., help="Stock symbol to trade (e.g., RELIANCE)."),
    date: str = typer.Option("", help="Trade date (YYYY-MM-DD). Defaults to today."),
) -> None:
    """Analyze and execute a trade for a symbol."""
    if not date:
        date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    from skopaq.config import SkopaqConfig
    config = SkopaqConfig()

    # Double-confirmation gate for LIVE mode — real money at stake
    if config.trading_mode == "live":
        typer.echo(
            "\n  ⚠  LIVE TRADING MODE — real orders will be placed on INDstocks.\n"
            f"     Symbol: {symbol}    Date: {date}\n"
        )
        if not typer.confirm("  Proceed with LIVE execution?", default=False):
            typer.echo("  Aborted.")
            raise typer.Exit()

    display_trade_start(symbol, date, config.trading_mode)
    result = asyncio.run(_run_trade(symbol, date))

    if result.error:
        display_error(result.error)
        raise typer.Exit(code=1)

    display_trade_result(result)


async def _run_trade(symbol: str, trade_date: str):
    """Helper to run async trade.

    For paper mode, this function:
        1. Uses relaxed safety rules (no market-hours or stop-loss gate).
        2. Fetches a real-time quote from INDstocks and injects it into the
           paper engine so ``execute_order()`` can simulate a fill.
        3. Wires the trade lifecycle manager for auto-reflection on SELL.
    """
    from skopaq.broker.paper_engine import PaperEngine
    from skopaq.config import SkopaqConfig
    from skopaq.constants import (
        CRYPTO_PAPER_SAFETY_RULES, CRYPTO_SAFETY_RULES,
        PAPER_SAFETY_RULES, SAFETY_RULES,
    )
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter
    from skopaq.execution.safety_checker import SafetyChecker
    from skopaq.graph.skopaq_graph import SkopaqTradingGraph

    from skopaq.risk.position_sizer import PositionSizer

    config = SkopaqConfig()
    is_crypto = config.asset_class == "crypto"

    # Paper engine — crypto uses % brokerage + USDT capital
    if is_crypto:
        paper = PaperEngine(
            initial_capital=config.initial_paper_capital,
            brokerage_pct=0.001,         # 0.1% Binance spot fee
            currency_label="USDT",
        )
    else:
        paper = PaperEngine(initial_capital=config.initial_paper_capital)

    # Choose safety rules based on trading mode + asset class
    if is_crypto:
        rules = CRYPTO_PAPER_SAFETY_RULES if config.trading_mode == "paper" else CRYPTO_SAFETY_RULES
    else:
        rules = PAPER_SAFETY_RULES if config.trading_mode == "paper" else SAFETY_RULES

    # Wire live broker client when in live mode
    live_client = None
    if config.trading_mode == "live":
        from skopaq.broker.client import INDstocksClient
        from skopaq.broker.token_manager import TokenManager

        token_mgr = TokenManager()
        live_client = INDstocksClient(config, token_mgr)

    router = OrderRouter(config, paper, live_client=live_client)
    safety = SafetyChecker(
        rules=rules,
        max_sector_concentration_pct=config.max_sector_concentration_pct,
    )
    from skopaq.execution.pnl_history import seed_safety_checker
    seed_safety_checker(safety, config)

    # ATR-based position sizer (optional — also works for crypto via yfinance)
    sizer = None
    if config.position_sizing_enabled:
        sizer = PositionSizer(
            risk_per_trade_pct=config.risk_per_trade_pct,
            atr_multiplier=config.atr_multiplier,
            atr_period=config.atr_period,
        )

    executor = Executor(router, safety, position_sizer=sizer)

    # For paper mode, inject a real-time quote so the fill simulation has a price.
    if config.trading_mode == "paper":
        if is_crypto:
            await _inject_crypto_quote(config, paper, symbol)
        else:
            await _inject_paper_quote(config, paper, symbol)

    # Compute regime and calendar scales for position sizing
    # (India VIX + NSE calendar are irrelevant for crypto — skip)
    if is_crypto:
        regime_scale, calendar_scale = 1.0, 1.0
    else:
        regime_scale, calendar_scale = _compute_risk_scales(config, trade_date)

    upstream_config = _build_upstream_config(config)

    # For crypto, translate BTCUSDT → BTC-USD for yfinance-based analysis
    if is_crypto:
        from skopaq.broker.crypto_symbols import to_yfinance_ticker
        analysis_symbol = to_yfinance_ticker(symbol)
        # Store the original Binance symbol for trade record building
        upstream_config["_trade_symbol"] = symbol
        logger.info("Crypto: %s → analysis as %s", symbol, analysis_symbol)
    else:
        analysis_symbol = symbol

    # Load persisted memories + wire lifecycle manager
    memory_store = _create_memory_store(config)
    analysts = [a.strip() for a in config.selected_analysts.split(",") if a.strip()]
    graph = SkopaqTradingGraph(
        upstream_config, executor,
        selected_analysts=analysts,
        memory_store=memory_store,
    )

    # Open live client context if wired (INDstocksClient requires async with)
    if live_client is not None:
        await live_client.__aenter__()

    persist: Optional[asyncio.Future] = None
    try:
        result = await graph.analyze_and_execute(
            analysis_symbol, trade_date,
            regime_scale=regime_scale,
            calendar_scale=calendar_scale,
        )
        # Post-execution: persist the trade (its P&L feeds the loss limits) and, when
        # reflection is on, link BUY/SELL and reflect — first, before the client is closed
        # and the alerts drained. Live, the fill is final at the broker: a Ctrl+C meanwhile
        # must not lose its row, so the write runs to the end (shielded, awaited below)
        persist = asyncio.ensure_future(_run_lifecycle(
            config, _reflection_graph(config, graph, memory_store), memory_store, result))
        await (asyncio.shield(persist) if live_client is not None else persist)
    finally:
        if live_client is not None:
            if persist is not None and not persist.done():
                await asyncio.wait({persist}, timeout=_PERSIST_WAIT_S)   # cancelled meanwhile
            await live_client.__aexit__(None, None, None)
            # Order alerts are sent in the background: let them go out before exiting
            from skopaq.execution.order_alerts import get_alerter

            drain = getattr(get_alerter(), "drain", None)
            if drain is not None:
                await drain()

    return result


async def _inject_paper_quote(config, paper, symbol: str) -> None:
    """Fetch a real quote from INDstocks and inject it into the paper engine.

    The paper engine requires a Quote in its ``_quotes`` cache before
    ``execute_order()`` can simulate a fill.  Without this, it returns
    "No quote available for {symbol}".
    """
    from skopaq.broker.client import INDstocksClient
    from skopaq.broker.scrip_resolver import resolve_scrip_code
    from skopaq.broker.token_manager import TokenManager

    token_mgr = TokenManager()
    client = INDstocksClient(config, token_mgr)

    try:
        async with client:
            scrip_code = await resolve_scrip_code(client, symbol)
            logger.info("Resolved %s → %s", symbol, scrip_code)

            quote = await client.get_quote(scrip_code, symbol=symbol)
            paper.update_quote(quote)
            logger.info(
                "Injected quote: %s LTP=%.2f bid=%.2f ask=%.2f",
                symbol, quote.ltp, quote.bid, quote.ask,
            )
    except Exception as exc:
        logger.warning(
            "Could not fetch quote for %s — paper fill may fail: %s",
            symbol, exc,
        )


async def _inject_crypto_quote(config, paper, symbol: str) -> None:
    """Fetch a real quote from Binance and inject it into the paper engine.

    The paper engine requires a Quote in its ``_quotes`` cache before
    ``execute_order()`` can simulate a fill.  Uses the Binance public
    24hr ticker endpoint (no authentication needed).
    """
    from skopaq.broker.binance_client import BinanceClient
    from skopaq.broker.crypto_symbols import to_binance_pair

    pair = to_binance_pair(symbol)
    client = BinanceClient(base_url=config.binance_base_url)

    try:
        async with client:
            quote = await client.get_quote(pair)
            paper.update_quote(quote)
            logger.info(
                "Injected crypto quote: %s LTP=%.2f bid=%.2f ask=%.2f",
                pair, quote.ltp, quote.bid, quote.ask,
            )
    except Exception as exc:
        logger.warning(
            "Could not fetch crypto quote for %s — paper fill may fail: %s",
            pair, exc,
        )


# ── Scan ──────────────────────────────────────────────────────────────────────


@app.command("scan")
def scan(
    max_candidates: int = typer.Option(5, help="Max candidates to return."),
) -> None:
    """Run a single scanner cycle on the NIFTY 50 watchlist."""
    display_scan_start()
    candidates = asyncio.run(_run_scan(max_candidates))
    display_scan_results(candidates)


async def _run_scan(max_candidates: int):
    """Helper to run async scanner with real providers.

    Wires up:
    - quote_fetcher:  INDstocks batch quotes (equity) or stub (crypto)
    - llm_screener:   Gemini 3 Flash  (technical screening)
    - news_screener:  Perplexity Sonar (news-aware screening)
    - social_screener: Grok            (social sentiment screening)
    - jev:            catalyst scoring of the candidates (when enabled)
    """
    from langchain_core.messages import HumanMessage

    from skopaq.config import SkopaqConfig
    from skopaq.llm import bridge_env_vars, build_llm_map, extract_text
    from skopaq.llm.jev import get_jev
    from skopaq.scanner import ScannerEngine, Watchlist

    config = SkopaqConfig()

    # Bridge SKOPAQ_ env vars → standard env vars
    bridge_env_vars(config)

    # Build per-role LLM map
    llm_map = build_llm_map()

    # Activate semantic cache (saves $$)
    from skopaq.llm.cache import init_langcache
    cache = init_langcache(config)
    if cache:
        from langchain_core.globals import set_llm_cache
        set_llm_cache(cache)
        logger.info("Scanner: semantic cache enabled")

    # ── Quote fetcher ────────────────────────────────────────────────
    is_crypto = config.asset_class == "crypto"

    if is_crypto:
        from skopaq.broker.crypto_symbols import CRYPTO_TOP_20
        watchlist = Watchlist(symbols=CRYPTO_TOP_20)

        async def quote_fetcher(symbols: list[str]) -> list[dict]:
            """Crypto stub — no real-time quotes wired yet."""
            return []
    else:
        watchlist = Watchlist()

        async def quote_fetcher(symbols: list[str]) -> list[dict]:
            """Batch-fetch INDstocks quotes for equity symbols."""
            from skopaq.broker.client import INDstocksClient
            from skopaq.broker.scrip_resolver import resolve_scrip_code
            from skopaq.broker.token_manager import TokenManager

            token_mgr = TokenManager()
            async with INDstocksClient(config, token_mgr) as client:
                # Resolve all symbols → scrip codes (instruments CSV cached 1h)
                resolved: list[tuple[str, str]] = []
                for sym in symbols:
                    try:
                        code = await resolve_scrip_code(client, sym)
                        resolved.append((sym, code))
                    except ValueError:
                        logger.debug("Scrip resolve failed: %s", sym)

                if not resolved:
                    return []

                syms, codes = zip(*resolved)
                raw_quotes = await client.get_quotes(list(codes), symbols=list(syms))

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
                    for q in raw_quotes
                ]

    # ── LLM screener factories ───────────────────────────────────────

    async def _invoke_llm(role: str, prompt: str) -> str:
        """Invoke a LangChain LLM by role and return normalised text."""
        llm = llm_map.get(role, llm_map.get("_default"))
        if llm is None:
            return "[]"
        msg = HumanMessage(content=prompt)
        if hasattr(llm, "ainvoke"):
            response = await llm.ainvoke([msg])
        else:
            import asyncio as _aio
            response = await _aio.to_thread(lambda: llm.invoke([msg]))
        return extract_text(response.content)

    async def llm_screener(prompt: str) -> str:
        return await _invoke_llm("market_analyst", prompt)

    async def news_screener(prompt: str) -> str:
        return await _invoke_llm("news_analyst", prompt)

    async def social_screener(prompt: str) -> str:
        return await _invoke_llm("social_analyst", prompt)

    # ── Run scanner ──────────────────────────────────────────────────
    scanner = ScannerEngine(
        watchlist=watchlist,
        max_candidates=max_candidates,
        quote_fetcher=quote_fetcher,
        llm_screener=llm_screener,
        news_screener=news_screener,
        social_screener=social_screener,
        jev=get_jev(),
    )
    return await scanner.scan_once()


# ── Monitor ─────────────────────────────────────────────────────────────────


@app.command("settle")
def settle() -> None:
    """Settle past decisions whose holding window has traded, for every ticker."""
    from skopaq.config import SkopaqConfig
    from skopaq.graph.skopaq_graph import SkopaqTradingGraph

    config = SkopaqConfig()
    try:
        graph = SkopaqTradingGraph(
            _build_upstream_config(config),
            executor=None,
            memory_store=_create_memory_store(config),
        )
        settled = graph.settle_due()
    except Exception as exc:
        # Settling writes an LLM reflection per decision, so it needs the LLM keys.
        display_error(f"Settling failed: {exc}")
        raise typer.Exit(1)
    display_success(f"Settled {settled} past decision(s).")


@memory_app.command("legacy")
def memory_legacy(
    export: str = typer.Option("", "--export", help="Write the rows to this JSON file."),
    delete: bool = typer.Option(
        False, "--delete", help="Delete the rows from Supabase (exports them first)."
    ),
    yes: bool = typer.Option(False, "--yes", help="Delete without asking for confirmation."),
) -> None:
    """Show, export or delete the per-agent memories from before the v0.5.1 sync.

    Upstream replaced them with the decision log. Until deleted they are
    still searched by the recall_agent_memories MCP tool.
    """
    import json
    from pathlib import Path

    from skopaq.config import SkopaqConfig

    config = SkopaqConfig()
    store = _create_memory_store(config, require_reflection=False)
    if store is None:
        display_error("Supabase is not configured (SKOPAQ_SUPABASE_URL / SERVICE_KEY).")
        raise typer.Exit(1)

    records = store.legacy_records()
    if not records:
        display_info("No legacy memory rows in Supabase.")
        return
    display_legacy_memories(records)

    if delete and not export:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        export = f"legacy-memories-{stamp}.json"
    if export:
        payload = {
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "rows": [r.model_dump(mode="json") for r in records],
        }
        Path(export).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        display_success(f"Exported {len(records)} row(s) to {export}")

    if not delete:
        return
    if not yes and not typer.confirm(
        f"Delete {len(records)} legacy row(s) from Supabase? This cannot be undone."
    ):
        display_info("Nothing deleted.")
        return
    deleted = store.delete_legacy(records)
    display_success(f"Deleted {deleted} legacy row(s). Backup: {export}")


@app.command("monitor")
def monitor(
    poll_interval: int = typer.Option(0, help="Poll interval in seconds (0 = use config)."),
    stop_loss: float = typer.Option(0.0, help="Hard stop-loss % override (0 = use config)."),
    eod_exit_minutes: int = typer.Option(0, help="EOD exit minutes override (0 = use config)."),
    no_ai: bool = typer.Option(False, "--no-ai", help="Disable AI sell analyst (rule-based only)."),
) -> None:
    """Monitor open positions and auto-sell using AI + safety rules."""
    from skopaq.config import SkopaqConfig

    config = SkopaqConfig()

    # Apply CLI overrides
    if poll_interval > 0:
        config.monitor_poll_interval_seconds = poll_interval
    if stop_loss > 0:
        config.monitor_hard_stop_pct = stop_loss
    if eod_exit_minutes > 0:
        config.monitor_eod_exit_minutes_before_close = eod_exit_minutes

    ai_enabled = not no_ai

    display_monitor_start(
        mode=config.trading_mode,
        poll_interval=config.monitor_poll_interval_seconds,
        stop_pct=config.monitor_hard_stop_pct,
        eod_minutes=config.monitor_eod_exit_minutes_before_close,
        ai_enabled=ai_enabled,
    )

    result = asyncio.run(_run_monitor(config, ai_enabled))
    display_monitor_result(result)
    # Live: positions still open, a failed exit or an unconfirmed order at the end is
    # rc 4, which the scheduler turns into a "check the broker" alert
    if config.trading_mode == "live" and (result.positions_left or result.orders_unconfirmed):
        from skopaq.execution.daemon import POSITIONS_LEFT_EXIT_CODE

        raise typer.Exit(POSITIONS_LEFT_EXIT_CODE)


async def _run_monitor(config, ai_enabled: bool):
    """Async helper to run the position monitor."""
    import signal as sig

    from skopaq.broker.client import INDstocksClient
    from skopaq.broker.token_manager import TokenManager
    from skopaq.constants import PAPER_SAFETY_RULES, SAFETY_RULES
    from skopaq.execution.executor import Executor
    from skopaq.execution.order_router import OrderRouter
    from skopaq.execution.position_monitor import PositionMonitor
    from skopaq.execution.safety_checker import SafetyChecker

    # Always need an INDstocksClient for LTP polling
    token_mgr = TokenManager()
    client = INDstocksClient(config, token_mgr)

    # Wire live or paper backend
    from skopaq.broker.paper_engine import PaperEngine

    paper = PaperEngine(initial_capital=config.initial_paper_capital)
    live_client = None
    if config.trading_mode == "live":
        live_client = client  # reuse same client

    router = OrderRouter(config, paper, live_client=live_client)
    rules = PAPER_SAFETY_RULES if config.trading_mode == "paper" else SAFETY_RULES
    safety = SafetyChecker(
        rules=rules,
        max_sector_concentration_pct=config.max_sector_concentration_pct,
    )
    from skopaq.execution.pnl_history import seed_safety_checker
    seed_safety_checker(safety, config)
    executor = Executor(router, safety)

    # Build LLM for sell analyst (if AI enabled)
    llm = None
    if ai_enabled:
        try:
            from skopaq.llm import bridge_env_vars, build_llm_map

            bridge_env_vars(config)
            llm_map = build_llm_map()
            llm = llm_map.get("sell_analyst", llm_map.get("_default"))
            if llm:
                logger.info("Sell analyst LLM ready")
            else:
                logger.warning("No LLM available for sell_analyst — AI tier disabled")
                ai_enabled = False
        except Exception:
            logger.warning("Failed to build LLM map — AI tier disabled", exc_info=True)
            ai_enabled = False

    # Graceful shutdown via Ctrl+C or SIGTERM (docker stop)
    stop_event = asyncio.Event()

    def _handle_sigint(*_):
        logger.info("Stop signal received — shutting down monitor gracefully...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for s in (sig.SIGINT, sig.SIGTERM):
        loop.add_signal_handler(s, _handle_sigint)

    # Run monitor within client context
    async with client:
        monitor_instance = PositionMonitor(
            executor=executor,
            client=client,
            router=router,
            config=config,
            llm=llm,
            stop_event=stop_event,
            ai_enabled=ai_enabled,
            # No analysis graph here: record the exit and its P&L, no reflection.
            on_exit=lambda signal, execution: _record_exit(config, None, None, signal, execution),
            on_late_fill=lambda tracked, conf: _record_late_fill(config, None, None, tracked,
                                                                 conf),
        )
        return await monitor_instance.run()


# ── Daemon ───────────────────────────────────────────────────────────────────


@app.command("daemon")
def daemon(
    max_trades: int = typer.Option(0, help="Override max trades per session (0 = use config)."),
    paper: bool = typer.Option(False, "--paper", help="Force paper mode."),
    live: bool = typer.Option(False, "--live", help="Force live mode (real orders on INDstocks)."),
    once: bool = typer.Option(False, "--once", help="Run immediately without waiting for market open."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Scanner only — print candidates, don't trade."),
    confirm_live: bool = typer.Option(False, "--confirm-live", help="Skip interactive confirmation for live mode (for cron/CI)."),
    ignore_calendar: bool = typer.Option(
        False, "--ignore-calendar",
        help="Run even on a weekend, an NSE holiday or after 15:30 IST (testing only).",
    ),
) -> None:
    """Run the autonomous trading daemon (scan -> trade -> monitor -> close)."""
    from skopaq.config import SkopaqConfig

    config = SkopaqConfig()

    # CLI overrides
    if paper:
        config.trading_mode = "paper"
    if live:
        config.trading_mode = "live"
    if max_trades > 0:
        config.daemon_max_trades_per_session = max_trades

    # NSE calendar gate: no session on weekends, NSE holidays or after the close.
    # Exits 0 so cron/scheduler runs on those days are not failures.
    if not dry_run and not ignore_calendar:
        from skopaq.risk import calendar as nse_calendar

        try:
            blocked = nse_calendar.daemon_block_reason(
                nse_calendar.now_ist(), config.nse_holidays,
            )
        except ValueError as exc:  # a malformed SKOPAQ_NSE_HOLIDAYS
            display_error(str(exc))
            raise typer.Exit(1)
        if blocked:
            display_info(f"No daemon session: {blocked}")
            raise typer.Exit(0)

    # Double-confirmation gate for LIVE mode — real money at stake
    if config.trading_mode == "live" and not dry_run:
        typer.echo(
            "\n  !!  LIVE DAEMON MODE — autonomous trading with real money.\n"
            f"     Max trades: {config.daemon_max_trades_per_session}\n"
            f"     Min profit: {config.daemon_min_profit_threshold_pct}% / "
            f"{config.daemon_min_profit_threshold_inr:.0f}\n"
        )
        if not confirm_live:
            if not typer.confirm("  Proceed with LIVE daemon?", default=False):
                typer.echo("  Aborted.")
                raise typer.Exit()

    display_daemon_start(config)
    report = asyncio.run(_run_daemon(config, once=once, dry_run=dry_run))
    display_daemon_report(report)
    if report.pre_open_failed:  # nothing traded: `skopaq schedule` may retry this morning
        from skopaq.execution.daemon import PRE_OPEN_FAILED_EXIT_CODE

        raise typer.Exit(PRE_OPEN_FAILED_EXIT_CODE)
    if report.failed:  # not for a candidate's failed analysis: the session itself ran
        raise typer.Exit(1)  # lets schedulers (skopaq schedule, Railway cron) see failed sessions


async def _run_daemon(config, *, once: bool = False, dry_run: bool = False):
    """Async helper to run the daemon session."""
    import signal as sig

    from skopaq.execution.daemon import TradingDaemon

    stop_event = asyncio.Event()

    # Graceful shutdown on SIGINT/SIGTERM
    def _handle_signal(signum, *_):
        sig_name = sig.Signals(signum).name
        logger.info("%s received — initiating graceful shutdown...", sig_name)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for s in (sig.SIGINT, sig.SIGTERM):
        loop.add_signal_handler(s, _handle_signal, s)

    daemon_instance = TradingDaemon(config, stop_event=stop_event)

    # Wait for market open (unless --once or --dry-run)
    if not once and not dry_run:
        await daemon_instance.wait_for_market_open()

    if stop_event.is_set():
        from skopaq.execution.daemon import DaemonSessionReport
        return DaemonSessionReport(session_date="cancelled")

    return await daemon_instance.run_session(dry_run=dry_run)


# ── Schedule ─────────────────────────────────────────────────────────────────


@app.command("schedule")
def schedule(
    check: bool = typer.Option(
        False, "--check",
        help="Print today's plan and the next session, then exit (1 if the configuration "
        "or this year's NSE holiday list is not usable).",
    ),
) -> None:
    """Run one daemon session per NSE trading day (always-on host; docs/deployment/mac-mini.md)."""
    from skopaq.config import SkopaqConfig
    from skopaq.execution.scheduler import (
        SchedulerState,
        ScheduleSettings,
        alert_invalid_config,
        check_ok,
        describe,
        run_forever,
    )
    from skopaq.risk import calendar as nse_calendar

    config = None
    try:
        config = SkopaqConfig()
        settings = ScheduleSettings.from_config(config)
    except ValueError as exc:
        display_error(str(exc))
        if not check and config is not None:  # a SkopaqConfig error stops every service anyway
            alert_invalid_config(config, str(exc))
        raise typer.Exit(1)

    if check:
        now = nse_calendar.now_ist()
        for line in describe(settings, now, SchedulerState(settings.state_dir, settings.log_dir)):
            typer.echo(line)
        raise typer.Exit(0 if check_ok(settings, now) else 1)

    raise typer.Exit(run_forever(settings))


# ── Chat ─────────────────────────────────────────────────────────────────────


@app.command("chat")
def chat(
    live: bool = typer.Option(False, "--live", help="Start in live trading mode."),
    confirm_live: bool = typer.Option(
        False, "--confirm-live", help="Skip live-mode confirmation prompt.",
    ),
) -> None:
    """Interactive AI trading assistant (Claude Code-style REPL)."""
    from skopaq.config import SkopaqConfig

    config = SkopaqConfig()

    if live:
        if not confirm_live:
            typer.echo(
                "\n  WARNING: Live mode uses REAL money on INDstocks/Binance.\n"
            )
            if not typer.confirm("  Start in LIVE mode?", default=False):
                typer.echo("  Starting in paper mode instead.")
                live = False

        if live:
            config.trading_mode = "live"

    from skopaq.chat.repl import run_repl
    from skopaq.chat.session import ChatSession

    session = ChatSession(config)
    asyncio.run(run_repl(session))


# ── Serve ────────────────────────────────────────────────────────────────────


@app.command("serve")
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind address."),
    port: int = typer.Option(8000, help="Port."),
    reload: bool = typer.Option(False, help="Auto-reload on code changes."),
) -> None:
    """Start the FastAPI backend server."""
    import uvicorn

    display_serve_banner(host, port)
    uvicorn.run(
        "skopaq.api.server:app",
        host=host,
        port=port,
        reload=reload,
        log_level="info",
    )


# ── Version ──────────────────────────────────────────────────────────────────


@app.command("version")
def version() -> None:
    """Print version."""
    display_version(__version__)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _compute_risk_scales(config, trade_date: str) -> tuple[float, float]:
    """Compute regime and calendar position-sizing multipliers.

    Returns:
        (regime_scale, calendar_scale) — both default to 1.0 when disabled.
    """
    from datetime import date as date_cls

    regime_scale = 1.0
    calendar_scale = 1.0

    # Regime detection (India VIX + NIFTY trend)
    if config.regime_detection_enabled:
        try:
            from skopaq.risk.regime import RegimeDetector, fetch_regime_data

            india_vix, nifty_price, nifty_sma200 = fetch_regime_data()
            detector = RegimeDetector()
            regime = detector.detect(india_vix, nifty_price, nifty_sma200)
            regime_scale = regime.position_scale

            if not regime.should_trade:
                logger.warning(
                    "Regime detector says NO TRADE: %s VIX=%.1f",
                    regime.label, regime.vix or 0,
                )
        except Exception:
            logger.warning("Regime detection failed — using default scale", exc_info=True)

    # NSE Event Calendar
    try:
        from skopaq.risk.calendar import NSEEventCalendar

        cal = NSEEventCalendar()
        try:
            d = date_cls.fromisoformat(trade_date)
        except (ValueError, TypeError):
            d = date_cls.today()

        calendar_scale = cal.get_position_scale(d)
        events = cal.get_events(d)
        if events:
            logger.info("Calendar events for %s: %s (scale=%.1f)", d, events, calendar_scale)
    except Exception:
        logger.warning("Event calendar check failed — using default scale", exc_info=True)

    return regime_scale, calendar_scale


def _build_upstream_config(config) -> dict:
    """Build config dict for upstream TradingAgentsGraph from SkopaqConfig."""
    from pathlib import Path
    from skopaq.llm import bridge_env_vars, build_llm_map
    from skopaq.llm.model_tier import GEMINI_FLASH

    # Bridge SKOPAQ_ env vars → standard env vars (GOOGLE_API_KEY, etc.)
    bridge_env_vars(config)

    project_dir = str(Path.cwd())
    is_crypto = config.asset_class == "crypto"

    upstream = {
        "results_dir": str(Path(project_dir) / "results"),
        "data_cache_dir": str(Path(project_dir) / ".cache" / "data"),
        "llm_provider": "google",  # Default to Gemini (cheapest)
        "deep_think_llm": GEMINI_FLASH,
        "quick_think_llm": GEMINI_FLASH,
        "backend_url": None,
        "max_debate_rounds": config.max_debate_rounds,
        "max_risk_discuss_rounds": config.max_risk_discuss_rounds,
        "google_thinking_level": config.google_thinking_level or None,
        "max_recur_limit": 100,
        "asset_class": config.asset_class,
        # Data vendor routing — crypto uses yfinance everywhere (no INDstocks)
        "data_vendors": {
            "core_stock_apis": "yfinance" if is_crypto else "indstocks,yfinance",
            "technical_indicators": "yfinance",
            "fundamental_data": "yfinance",
            "news_data": "yfinance",
        },
        # Crypto: no suffix (BTC-USD works as-is); Equity: .NS for NSE
        "yfinance_symbol_suffix": "" if is_crypto else ".NS",
    }

    # Build per-role LLM map (multi-model tiering)
    try:
        upstream["llm_map"] = build_llm_map(upstream)
    except Exception:
        logger.warning("Failed to build LLM map — falling back to single-model", exc_info=True)

    # Activate semantic LLM cache (Redis LangCache)
    from skopaq.llm.cache import init_langcache
    cache = init_langcache(config)
    if cache:
        from langchain_core.globals import set_llm_cache
        set_llm_cache(cache)
        logger.info(
            "Semantic cache enabled (threshold=%.2f)",
            config.langcache_threshold,
        )

    return upstream


def _create_memory_store(config, require_reflection: bool = True):
    """Create a MemoryStore if reflection is enabled and Supabase is configured.

    Returns None if either condition is not met (graceful degradation).
    ``require_reflection=False`` skips the reflection check (memory admin).
    """
    if require_reflection and not config.reflection_enabled:
        return None

    if not config.supabase_url or not config.supabase_service_key.get_secret_value():
        logger.debug("Supabase not configured — agent memory disabled")
        return None

    try:
        from supabase import create_client
        from skopaq.memory.store import MemoryStore

        client = create_client(
            config.supabase_url,
            config.supabase_service_key.get_secret_value(),
        )
        return MemoryStore(client, max_entries=config.reflection_max_memory_entries)
    except Exception:
        logger.warning("Failed to initialise MemoryStore — continuing without memory", exc_info=True)
        return None


def _reflection_graph(config, graph, memory_store):
    """The graph to reflect with, or ``None`` when reflection is off."""
    if config.reflection_enabled and memory_store is not None:
        return graph
    return None


async def _record_exit(config, graph, memory_store, signal, execution, *,
                       rollback_unbooked: bool = False) -> bool:
    """Persist an exit that did not come from an analysis (monitor, EOD close).

    It goes through the same lifecycle as an analysed trade, so the opening
    BUY row is closed with its realized P&L, which the loss limits read back
    (``skopaq.execution.pnl_history``). False when it was not booked
    (``_run_lifecycle``, which ``rollback_unbooked`` is passed to).
    """
    from datetime import timedelta

    from skopaq.graph.skopaq_graph import AnalysisResult

    ist_today = datetime.now(timezone(timedelta(hours=5, minutes=30))).date().isoformat()
    result = AnalysisResult(
        symbol=signal.symbol, trade_date=ist_today, signal=signal, execution=execution,
    )
    return await _run_lifecycle(config, _reflection_graph(config, graph, memory_store),
                                memory_store, result, rollback_unbooked=rollback_unbooked)


def _trade_repository(config):
    """A TradeRepository over Supabase, or None when it is not configured or unreachable."""
    if not config.supabase_url or not config.supabase_service_key.get_secret_value():
        return None
    try:
        from supabase import create_client
        from skopaq.db.repositories import TradeRepository

        return TradeRepository(create_client(
            config.supabase_url, config.supabase_service_key.get_secret_value()))
    except Exception:
        logger.warning("Supabase unavailable — trade rows not written", exc_info=True)
        return None


def _delta_price(avg_now, filled_now, avg_before, filled_before):
    """Average price of the shares filled since the last report: (P·F − p·f) / (F − f)
    when both averages are known, else the current average (None if unknown)."""
    from decimal import Decimal

    if avg_now is None:
        return None
    if avg_before is None or not filled_before:
        return avg_now
    price = (avg_now * filled_now - avg_before * filled_before) / (filled_now - filled_before)
    return price.quantize(Decimal("0.0001")) if price > 0 else avg_now


async def _record_late_fill(config, graph, memory_store, tracked, conf) -> bool:
    """Persist a live fill the broker confirmed after its order had been reported.

    The monitor or CLOSING resumed the order (``tracked``) and read its final state
    (``conf``); only the shares beyond ``tracked.filled_reported`` are new.

    - BUY: the order's open row gets the new quantity and average price; a row already
      closed gets a separate row for the extra shares (``order_id`` None: it is UNIQUE);
      a BUY that was reported failed or unconfirmed gets its row now.
    - SELL (a stuck exit that filled later): the extra shares close BUY rows like any
      other exit, at their own average price.

    Returns False when the trade rows could not be written, so the caller does not count
    the shares as booked; True otherwise — also when Supabase is not configured (there
    are no rows to write) and when an exit with no price at all is left to the user
    (CRITICAL ``exit-late-unpriced``), so that no process books it again.
    """
    from decimal import Decimal

    from skopaq.broker.models import ExecutionResult, OrderType, TradingSignal
    from skopaq.db.models import TradeRecord

    filled = conf.filled_qty
    if filled is None or filled <= tracked.filled_reported:
        return True
    delta = filled - tracked.filled_reported
    price = _delta_price(conf.avg_price, filled, tracked.avg_price_reported,
                         tracked.filled_reported)
    # INDstocks' flat fee is per order that fills: charged now unless already reported
    fee = Decimal("0") if tracked.filled_reported else Decimal("20")

    if tracked.side == "SELL":
        from skopaq.execution.order_alerts import get_alerter

        source = conf.price_source
        if not price:
            # No broker price: the exit's reference price (the LTP when it was sent) or
            # its limit price, as a normal exit would use — never a P&L of 0
            estimate = getattr(tracked.signal, "entry_price", None) or tracked.price
            if not estimate:
                get_alerter().alert(
                    "CRITICAL", f"exit-late-unpriced:{tracked.order_id}",
                    f"SELL {tracked.symbol}: order {tracked.order_id} filled {delta} more but "
                    "the broker gave no price and there is no estimate; the trade rows are "
                    "left open — close them by hand at the broker's fill price",
                    order_ids=[tracked.order_id])
                # Left to the user: counted as handled, so no process books them after
                # the user has
                return True
            price, source = Decimal(str(estimate)), "estimate"
            get_alerter().alert(
                "WARNING", f"fill-price-unknown:{tracked.order_id}",
                f"SELL {tracked.symbol}: order {tracked.order_id} filled {delta} more but the "
                f"broker gave no price; recorded at the estimate {price}",
                order_ids=[tracked.order_id])
        signal = TradingSignal(
            symbol=tracked.symbol, action="SELL", confidence=100,
            entry_price=float(price), order_type=OrderType.MARKET,
            quantity=delta, reasoning=f"Late fill of exit order {tracked.order_id}",
        )
        execution = ExecutionResult(
            success=True, signal=signal, mode="live",
            fill_price=float(price), brokerage=float(fee),
            filled_quantity=delta, requested_quantity=delta, outcome="late_fill",
            order_ids=[tracked.order_id], fill_price_source=source,
            broker_message=f"late fill of {tracked.order_id}",
        )
        # Not booked: its SELL row is removed again, as the next booking writes its own
        booked = await _record_exit(config, graph, memory_store, signal, execution,
                                    rollback_unbooked=True)
        return booked is not False

    repo = _trade_repository(config)
    if repo is None:
        # Not configured: nothing to book; configured but unreachable: not booked
        return not (config.supabase_url and config.supabase_service_key.get_secret_value())
    from skopaq.execution.order_alerts import get_alerter

    avg, source = conf.avg_price, conf.price_source
    if not avg:
        # No broker price: the order's limit price (or its signal's reference price) as the
        # cost basis, as a normal entry would use — a row without one books its exit at 0
        estimate = tracked.price or getattr(tracked.signal, "entry_price", None)
        if estimate:
            avg, source = Decimal(str(estimate)), "estimate"
            price = price or avg
            get_alerter().alert(
                "WARNING", f"fill-price-unknown:{tracked.order_id}",
                f"BUY {tracked.symbol}: order {tracked.order_id} filled {delta} more but the "
                f"broker gave no price; recorded at the estimate {avg}",
                order_ids=[tracked.order_id])
        else:
            get_alerter().alert(
                "CRITICAL", f"late-fill-unpriced:{tracked.order_id}",
                f"BUY {tracked.symbol}: order {tracked.order_id} filled {delta} more but the "
                "broker gave no price and there is no estimate; its trade row has no cost "
                "basis — set it by hand from the broker's fill price, or its exit books a "
                "P&L of 0", order_ids=[tracked.order_id])
    broker = {"outcome": "late_fill", "order_ids": [tracked.order_id],
              "late_fill_of": tracked.order_id, "fill_price_source": source}
    try:
        row = repo.find_by_order_id(tracked.order_id)
        if row is not None and row.closed_at is None:
            updates = {"quantity": str(filled)}
            if conf.avg_price or (avg and not (row.fill_price or row.price)):
                updates["fill_price"] = str(avg)
            repo.update(row.id, updates)
        elif row is not None:
            repo.insert(TradeRecord(
                symbol=tracked.symbol, side="BUY", quantity=delta, order_id=None,
                fill_price=price, price=price, status="COMPLETE", is_paper=False,
                brokerage=Decimal("0"), signal_source="skopaq-ai",
                entry_reason=f"Late fill of {tracked.order_id} after its row was closed",
                model_signals={"broker": broker},
            ))
        else:
            repo.insert(TradeRecord(
                symbol=tracked.symbol, side="BUY", quantity=filled,
                order_id=tracked.order_id, fill_price=avg, price=avg,
                status="COMPLETE", is_paper=False, brokerage=fee, signal_source="skopaq-ai",
                entry_reason=f"Late fill of {tracked.order_id} adopted by the monitor",
                model_signals={"broker": broker},
            ))
        logger.warning("Late fill of BUY %s (%s %s) recorded", tracked.order_id, delta,
                       tracked.symbol)
        return True
    except Exception:
        logger.warning("Recording the late fill of %s failed", tracked.order_id,
                       exc_info=True)
        return False


async def _run_lifecycle(config, graph, memory_store, result, *,
                         rollback_unbooked: bool = False) -> bool:
    """Run trade lifecycle tracking (BUY/SELL linkage + auto-reflection).

    Flow:
        1. Persist the trade to Supabase (so find_open_buy() works for future SELLs)
        2. Run lifecycle manager (BUY/SELL linkage + reflection; ``graph=None``
           links and records P&L without reflecting)

    Silently does nothing if Supabase is not configured (True: nothing to write).
    Returns False when the trade was not booked: a BUY's row could not be inserted, the
    lifecycle failed, or a live SELL closed no BUY row because the rows could not be read
    (a caller that books it again — a late fill — relies on it; the others log it). A
    SELL whose row could not be inserted but whose BUY rows were closed is booked: its
    P&L is. ``rollback_unbooked``: a SELL that closed no row has its row removed again,
    so booking it again does not leave a second one.
    """
    if not config.supabase_url or not config.supabase_service_key.get_secret_value():
        return True

    booked = True
    try:
        from supabase import create_client
        from skopaq.db.repositories import TradeRepository
        from skopaq.memory.lifecycle import TradeLifecycleManager

        client = create_client(
            config.supabase_url,
            config.supabase_service_key.get_secret_value(),
        )
        trade_repo = TradeRepository(client)

        # Step 1: Persist the trade BEFORE lifecycle processing.
        # This is the critical link — without it, find_open_buy() never finds
        # BUYs and the entire self-evolution loop is broken.
        trade_record = _build_trade_record(result, config)
        saved = None
        if trade_record is not None:
            try:
                saved = trade_repo.insert(trade_record)
                result.trade_id = saved.id
                logger.info(
                    "Trade persisted: %s %s id=%s (paper=%s)",
                    trade_record.side, trade_record.symbol,
                    saved.id, trade_record.is_paper,
                )
            except Exception:
                if trade_record.side != "SELL":
                    booked = False            # a BUY is booked by its row alone
                logger.warning(
                    "Failed to persist trade to Supabase — lifecycle will "
                    "continue but BUY/SELL linkage may not work",
                    exc_info=True,
                )

        # Step 2: Run lifecycle (BUY/SELL linkage + reflection)
        lifecycle = TradeLifecycleManager(trade_repo, graph, memory_store)
        if await lifecycle.on_trade(result) is False:
            booked = False                    # a SELL that closed no BUY row
            if rollback_unbooked and saved is not None:
                try:
                    trade_repo.delete(saved.id)
                    result.trade_id = None
                except Exception:
                    logger.warning("Removing the unbooked SELL row %s failed", saved.id,
                                   exc_info=True)
    except Exception:
        logger.warning("Trade lifecycle processing failed", exc_info=True)
        return False
    return booked


def _build_trade_record(result, config):
    """Convert an AnalysisResult into a TradeRecord for Supabase persistence.

    Returns None if the result doesn't represent a successful execution
    (e.g., HOLD signals, failed orders, or analysis-only runs).
    """
    if result.signal is None:
        return None
    if result.execution is None or not result.execution.success:
        return None
    if result.signal.action == "HOLD":
        return None

    from decimal import Decimal
    from skopaq.broker.models import (
        filled_quantity_of,
        is_remaining_open,
        order_ids_of,
        outcome_of,
    )
    from skopaq.db.models import TradeRecord

    execution = result.execution

    # Build model_signals dict from cache/timing metadata
    model_signals = {}
    if result.cache_hits or result.cache_misses:
        model_signals["cache_hits"] = result.cache_hits
        model_signals["cache_misses"] = result.cache_misses
    if result.duration_seconds:
        model_signals["duration_seconds"] = result.duration_seconds

    # Live: the row is what the broker confirmed — the filled quantity, the order ids
    # (none on a late-fill row: trades.order_id is UNIQUE and the order may be on a row)
    order_id = None
    if execution.mode == "live":
        ids = order_ids_of(execution)
        late = outcome_of(execution) == "late_fill"
        if not late:
            order_id = ",".join(ids)[:255] or None
        requested = execution.requested_quantity
        broker = {
            "outcome": outcome_of(execution),
            "requested_qty": _json_number(requested) if requested is not None else None,
            "fill_price_source": execution.fill_price_source,
            "remaining_open": is_remaining_open(execution),
            "fill_unconfirmed": execution.fill_unconfirmed is True,
            "order_ids": ids,
        }
        if late and ids:
            broker["late_fill_of"] = ids[0]
        model_signals["broker"] = broker

    # Determine exchange and product based on asset class
    is_crypto = config.asset_class == "crypto"
    trade_symbol = result.symbol
    if is_crypto:
        # result.symbol may be the yfinance format (BTC-USD); restore Binance pair
        from skopaq.broker.crypto_symbols import to_binance_pair
        trade_symbol = to_binance_pair(result.symbol, config.crypto_quote_currency)

    return TradeRecord(
        symbol=trade_symbol,
        exchange="BINANCE" if is_crypto else "NSE",
        product="SPOT" if is_crypto else "CNC",
        side=result.signal.action,
        quantity=filled_quantity_of(execution, result.signal.quantity or Decimal("1")),
        order_id=order_id,
        price=(
            Decimal(str(result.signal.entry_price))
            if result.signal.entry_price else None
        ),
        fill_price=(
            Decimal(str(result.execution.fill_price))
            if result.execution.fill_price else None
        ),
        slippage=(
            Decimal(str(result.execution.slippage))
            if result.execution.slippage else Decimal("0")
        ),
        brokerage=Decimal(str(result.execution.brokerage)),
        is_paper=(result.execution.mode == "paper"),
        status="COMPLETE",
        signal_source="skopaq-ai",
        consensus_score=result.signal.confidence,
        entry_reason=(
            result.signal.reasoning[:2000]
            if result.signal.reasoning else None
        ),
        agent_decision={
            "action": result.signal.action,
            "confidence": result.signal.confidence,
        },
        model_signals=model_signals,
    )


def _json_number(value):
    """A Decimal as a JSON number (int when whole) for model_signals."""
    number = float(value)
    return int(number) if number == int(number) else number


def _setup_logging(level: str = "INFO") -> None:
    """Configure root logger."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
    )


if __name__ == "__main__":
    _setup_logging()
    app()
