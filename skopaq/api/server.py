"""FastAPI backend for SkopaqTrader.

Provides REST endpoints for the frontend dashboard and health checks.
Deployed on Railway via ``skopaq serve``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from skopaq import __version__
from skopaq.api.auth import cors_origins, require_api_token
from skopaq.broker.token_manager import TokenManager
from skopaq.config import SkopaqConfig

logger = logging.getLogger(__name__)

app = FastAPI(
    title="SkopaqTrader API",
    version=__version__,
    docs_url="/docs",
)

# CORS — browser origins allowed to call the API (SKOPAQ_CORS_ORIGINS; default "*"
# keeps the Vercel frontend working). The API uses no cookies, so credentials are
# only allowed with an explicit origin list, never echoed to any site.
_origins = cors_origins(SkopaqConfig())
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Chat bridge (OpenClaw + external channels) ───────────────────────────────
from skopaq.chat.bridge import router as chat_router

app.include_router(chat_router)


# ── Kite Connect OAuth ───────────────────────────────────────────────────────

@app.get("/api/kite/login")
async def kite_login():
    """Redirect to Zerodha Kite login page."""
    from skopaq.broker.kite_client import KiteClient

    config = SkopaqConfig()
    if not config.kite_api_key:
        raise HTTPException(400, "SKOPAQ_KITE_API_KEY not configured")

    client = KiteClient(api_key=config.kite_api_key)
    from fastapi.responses import RedirectResponse

    return RedirectResponse(client.login_url)


@app.get("/api/kite/callback")
async def kite_callback(request_token: str = "", status: str = ""):
    """Handle Kite OAuth callback — exchange request_token for access_token."""
    from skopaq.broker.kite_client import KiteClient, set_access_token

    config = SkopaqConfig()
    if not request_token or status != "success":
        raise HTTPException(400, f"Login failed: status={status}")

    client = KiteClient(
        api_key=config.kite_api_key,
        api_secret=config.kite_api_secret.get_secret_value(),
    )
    try:
        session = client.generate_session(request_token)
        return {
            "status": "success",
            "user_id": session.get("user_id"),
            "access_token_set": True,
            "message": "Kite login successful. Bot is now connected to Zerodha.",
        }
    except Exception as exc:
        raise HTTPException(500, f"Session generation failed: {exc}")


@app.get("/api/kite/status")
async def kite_status():
    """Check if Kite access token is set."""
    from skopaq.broker.kite_client import get_access_token

    token = get_access_token()
    return {
        "connected": bool(token),
        "token_length": len(token) if token else 0,
    }


@app.get("/api/kite/token", dependencies=[Depends(require_api_token)])
async def kite_token():
    """Return the Kite access token (for internal service-to-service use).

    The Telegram bot fetches this to share the Kite session established
    via the API app's OAuth login flow. Needs ``Authorization: Bearer
    <SKOPAQ_API_TOKEN>`` when that is set.
    """
    from skopaq.broker.kite_client import get_access_token

    token = get_access_token()
    if not token:
        raise HTTPException(404, "No Kite token available. Login first.")
    return {"access_token": token}


@app.post("/api/kite/postback")
async def kite_postback(request: Request):
    """Receive real-time order updates from Zerodha.

    Zerodha POSTs order status changes (fill, rejection, cancellation)
    to this endpoint. We log them and can trigger alerts via Telegram.
    """
    import json

    try:
        body = await request.json()
    except Exception:
        body = dict(await request.form())

    order_id = body.get("order_id", "?")
    status = body.get("status", "?")
    symbol = body.get("tradingsymbol", "?")
    txn = body.get("transaction_type", "?")
    qty = body.get("filled_quantity", body.get("quantity", 0))
    price = body.get("average_price", 0)

    logger.info(
        "Kite postback: %s %s %s qty=%s price=%s status=%s",
        txn, symbol, order_id, qty, price, status,
    )

    # Auto-notify via centralized notification system
    try:
        from skopaq.notifications import notify_trade_event

        await notify_trade_event(
            action=txn,
            symbol=symbol,
            price=float(price) if price else 0,
            quantity=int(qty) if qty else 0,
            status=status,
            order_id=order_id,
        )
    except Exception:
        logger.warning("Postback notification failed", exc_info=True)

    return {"status": "ok"}


@app.get("/health")
async def health() -> dict:
    """Health check endpoint (used by Railway)."""
    config = SkopaqConfig()
    # notify=False: the compose health check polls this every 30 s; a probe sends no Telegram.
    health = TokenManager().get_health(notify=False)

    return {
        "status": "ok",
        "version": __version__,
        "mode": config.trading_mode,
        "token_valid": health.valid,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/api/status")
async def system_status() -> dict:
    """Detailed system status for the dashboard."""
    config = SkopaqConfig()
    token_health = TokenManager().get_health(notify=False)  # a status read sends no Telegram

    return {
        "version": __version__,
        "mode": config.trading_mode,
        "broker": {
            "name": "INDstocks",
            "base_url": config.indstocks_base_url,
            "token_valid": token_health.valid,
            "token_expires_at": token_health.expires_at.isoformat() if token_health.expires_at else None,
            "token_remaining": str(token_health.remaining) if token_health.remaining else None,
            "token_warning": token_health.warning or None,
        },
        "services": {
            "supabase": bool(config.supabase_url),
            "redis": bool(config.upstash_redis_url),
            "llms": {
                "gemini": bool(config.google_api_key.get_secret_value()),
                "claude": bool(config.anthropic_api_key.get_secret_value()),
                "perplexity": bool(config.perplexity_api_key.get_secret_value()),
                "grok": bool(config.xai_api_key.get_secret_value()),
            },
        },
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── Scanner ───────────────────────────────────────────────────────────────────

_scanner_engine = None


def _get_scanner():
    """Lazy-init scanner engine singleton."""
    global _scanner_engine
    if _scanner_engine is None:
        from skopaq.scanner import ScannerEngine, Watchlist
        config = SkopaqConfig()
        _scanner_engine = ScannerEngine(
            watchlist=Watchlist(),
            cycle_seconds=config.scanner_cycle_seconds,
            max_candidates=config.scanner_max_candidates,
        )
    return _scanner_engine


@app.on_event("startup")
async def _start_scanner():
    """Start scanner as a background task if enabled in config."""
    config = SkopaqConfig()
    if config.scanner_enabled:
        scanner = _get_scanner()
        await scanner.start()
        logger.info("Scanner background task started")


@app.on_event("shutdown")
async def _stop_scanner():
    """Stop scanner on shutdown."""
    if _scanner_engine and _scanner_engine.running:
        await _scanner_engine.stop()


@app.get("/api/scanner/status")
async def scanner_status() -> dict:
    """Scanner engine status."""
    scanner = _get_scanner()
    return scanner.status


@app.get("/api/scanner/candidates")
async def scanner_candidates() -> dict:
    """Recent scanner candidates."""
    scanner = _get_scanner()
    candidates = []
    # Drain the queue (non-blocking)
    while not scanner.candidate_queue.empty():
        try:
            c = scanner.candidate_queue.get_nowait()
            candidates.append(c.to_dict())
        except Exception:
            break
    return {
        "candidates": candidates,
        "last_candidates": [c.to_dict() for c in scanner._last_candidates],
    }


@app.get("/api/portfolio")
async def portfolio() -> dict:
    """Current portfolio snapshot (paper mode)."""
    from skopaq.broker.paper_engine import PaperEngine

    config = SkopaqConfig()
    paper = PaperEngine(initial_capital=config.initial_paper_capital)
    snapshot = paper.get_snapshot()

    return {
        "total_value": float(snapshot.total_value),
        "cash": float(snapshot.cash),
        "positions_value": float(snapshot.positions_value),
        "day_pnl": float(snapshot.day_pnl),
        "positions": [p.model_dump() for p in snapshot.positions],
        "open_orders": snapshot.open_orders,
        "mode": config.trading_mode,
        "timestamp": snapshot.timestamp.isoformat(),
    }
