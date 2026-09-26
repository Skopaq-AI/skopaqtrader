"""SkopaqTrader configuration — loaded from environment variables and .env file."""

from __future__ import annotations

import logging
import math
from typing import Any, Literal

from pydantic import SecretStr, ValidationError, ValidatorFunctionWrapHandler, field_validator
from pydantic_core.core_schema import ValidationInfo
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# Live order settings typed as numbers/bools. Every service builds a SkopaqConfig, so a
# typo in one of them must not stop api, telegram and the scheduler: an unparseable (or
# non-finite) value is logged and replaced by the field's default (off, for the bools).
_LENIENT_FIELDS = (
    "order_fill_timeout_seconds",
    "order_fill_poll_interval_seconds",
    "order_cancel_confirm_timeout_seconds",
    "order_exit_attempt_timeout_seconds",
    "order_exit_max_attempts",
    "order_exit_reprice_buffer_pct",
    "order_reconcile_timeout_seconds",
    "order_shutdown_margin_seconds",
    "order_sell_fill_lag_window_seconds",
    "allow_sell_without_order_book",
    "indstocks_order_remarks_enabled",
    "monitor_resync_cycles",
)


class SkopaqConfig(BaseSettings):
    """Central configuration for SkopaqTrader.

    All values are read from environment variables prefixed with ``SKOPAQ_``.
    A ``.env`` file in the project root is loaded automatically.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SKOPAQ_",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Supabase ────────────────────────────────────────────────────────
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_key: SecretStr = SecretStr("")

    # ── Upstash Redis ───────────────────────────────────────────────────
    upstash_redis_url: str = ""
    upstash_redis_token: SecretStr = SecretStr("")

    # ── INDstocks Broker ────────────────────────────────────────────────
    indstocks_token: SecretStr = SecretStr("")
    indstocks_base_url: str = "https://api.indstocks.com"
    indstocks_ws_price_url: str = "wss://ws-prices.indstocks.com/api/v1/ws/prices"
    indstocks_ws_order_url: str = "wss://ws-order-updates.indstocks.com"

    # ── Trading Mode ────────────────────────────────────────────────────
    trading_mode: Literal["paper", "live"] = "paper"
    initial_paper_capital: float = 1_000_000.0  # INR
    # Kill switch set at deploy level; also `skopaq halt` (skopaq/execution/kill_switch.py)
    trading_halted: bool = False

    # ── Live order confirmation (skopaq/execution/live_orders.py) ───────
    # Clamped to the [ranges] where they are used, with a WARNING naming the var; an
    # unparseable value is logged and replaced by the default (_LENIENT_FIELDS)
    order_fill_timeout_seconds: float = 30.0  # entries: wait, then cancel the rest [5, 120]
    order_fill_poll_interval_seconds: float = 1.0  # status polls (order history: 15 req/s) [0.5, 5]
    order_cancel_confirm_timeout_seconds: float = 10.0  # retry the cancel, re-read [3, 30]
    order_exit_attempt_timeout_seconds: float = 10.0  # protective exits, per attempt [3, 30]
    order_exit_max_attempts: int = 3  # cancel + re-place a resting exit at most N times [1, 5]
    # re-placed LIMIT = LTP × (1 − buf% × (attempt − 1)), at most 5% [0.1, 2]
    order_exit_reprice_buffer_pct: float = 0.5
    order_reconcile_timeout_seconds: float = 15.0  # find an uncertain placement in the book [5, 30]
    order_shutdown_margin_seconds: float = 60.0  # stop order work before kill-after [20, 120]
    # count filled SELLs that positions don't show yet for this long [60, 1800]
    order_sell_fill_lag_window_seconds: float = 600.0
    order_extra_terminal_statuses: str = ""  # comma-separated broker statuses to treat as final
    allow_sell_without_order_book: bool = False  # DANGER: SELL even if the order book can't be read
    indstocks_order_remarks_enabled: bool = False  # tag orders with `remarks` (docs: unreleased)
    order_lock_dir: str = "~/.skopaq/locks"  # per-symbol SELL locks (shared home volume)
    order_journal_dir: str = "~/.skopaq/orders"  # per-day order journal (shared home volume)

    # ── LLM API Keys ───────────────────────────────────────────────────
    google_api_key: SecretStr = SecretStr("")  # Gemini Flash (scanner)
    anthropic_api_key: SecretStr = SecretStr("")  # Claude Sonnet (analysis)
    perplexity_api_key: SecretStr = SecretStr("")  # Sonar (news)
    xai_api_key: SecretStr = SecretStr("")  # Grok (sentiment)
    openrouter_api_key: SecretStr = SecretStr("")  # OpenRouter (Grok + Perplexity)
    typesafe_api_key: SecretStr = SecretStr("")  # TypeSafe Jev (post screening, decisions)

    # ── TypeSafe Jev (calibrated decisions; skopaq/llm/jev.py) ─────────
    jev_enabled: bool = False  # Jev confidence on entries + exit gate
    jev_model: str = "jev-1.13.0"  # pinned: thresholds are tuned per version
    jev_min_confidence: float = 0.6  # act on a Jev answer at or above this
    jev_min_catalyst_score: float = 0.0  # scanner: drop below this (0-3); 0 ranks only
    jev_timeout_seconds: float = 5.0
    # TypeSafe-compatible API root; "" = api.typesafe.ai. OpenRouter serves Jev
    # at https://openrouter.ai/api (OpenRouter key, SKOPAQ_JEV_MODEL=jev-1.13;
    # not confirmed to be the same build as jev-1.13.0).
    jev_base_url: str = ""

    # ── Cloudflare Tunnel ───────────────────────────────────────────────
    cf_tunnel_id: str = ""

    # ── Scanner ────────────────────────────────────────────────────────
    scanner_enabled: bool = False
    scanner_cycle_seconds: int = 30
    scanner_max_candidates: int = 5

    # ── API Server ──────────────────────────────────────────────────────
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # ── Public URLs / API access ────────────────────────────────────────
    # Public HTTPS URL of the API (Kite login links), e.g. the tunnel host
    public_base_url: str = ""
    # HTTP fallback for other processes to fetch the Kite token; never set on the api itself
    api_base_url: str = ""
    # When set, /api/chat/* and /api/kite/token require Bearer auth
    api_token: SecretStr = SecretStr("")
    cors_origins: str = "*"  # comma-separated browser origins; "" = none

    # ── Reflection / Memory ─────────────────────────────────────────────
    reflection_enabled: bool = True
    reflection_max_memory_entries: int = 50

    # ── Upstream Agent Tuning ─────────────────────────────────────────
    max_debate_rounds: int = 1
    max_risk_discuss_rounds: int = 1
    selected_analysts: str = "market,social,news,fundamentals"
    google_thinking_level: str = ""

    # ── Risk-Adjusted Position Sizing ─────────────────────────────────
    position_sizing_enabled: bool = True
    risk_per_trade_pct: float = 0.01  # 1% of equity per trade
    atr_multiplier: float = 2.0  # Stop distance in ATR units
    atr_period: int = 14  # ATR lookback period

    # ── Confidence Gating ─────────────────────────────────────────────
    min_confidence_pct: int = 0  # 0 = disabled; e.g. 40 to reject <40%
    confidence_sizing_enabled: bool = True  # Scale position size by confidence

    # ── Sector Concentration ──────────────────────────────────────────
    max_sector_concentration_pct: float = 0.40  # Max 40% in any one sector

    # ── Position Monitor ─────────────────────────────────────────────
    monitor_poll_interval_seconds: int = 10
    monitor_hard_stop_pct: float = 0.04  # 4% hard stop (safety tier)
    monitor_eod_exit_minutes_before_close: int = 10  # sell at 15:20 IST
    monitor_ai_interval_cycles: int = 6  # AI every 6 polls (~60s)
    monitor_trailing_stop_enabled: bool = False
    monitor_trailing_stop_pct: float = 0.02  # 2% trail from high-water
    monitor_resync_cycles: int = 3  # live: re-read broker book/positions every N polls [1, 60]

    # ── Daemon (autonomous session) ──────────────────────────────────
    daemon_max_trades_per_session: int = 3  # Max BUY orders per day
    daemon_max_candidates_to_analyze: int = 5  # Top N scanner picks to analyze
    daemon_pre_open_minutes: int = 5  # Start N minutes before 9:15
    daemon_scan_delay_after_open_seconds: int = 60  # Wait for prices to settle
    daemon_min_profit_threshold_pct: float = 0.5  # Min P&L% for AI sell
    daemon_min_profit_threshold_inr: float = (
        150.0  # Min absolute profit (INR, covers ~₹120 brokerage)
    )
    daemon_session_log_dir: str = "logs/daemon"  # Session log directory
    daemon_heartbeat_interval_seconds: int = 300  # Heartbeat log interval (5 min)

    # ── Scheduler (always-on host: docker compose `scheduler`) ─────────
    # Plain str, not bool/int/Literal: every service builds a SkopaqConfig, so a typo here
    # would stop api and telegram too. ScheduleSettings.from_config parses them, and a bad
    # value stops only the scheduler (a bad CONFIRM_LIVE counts as not confirmed).
    scheduler_enabled: str = "true"  # false: stay up and healthy, start no sessions (cutover)
    scheduler_mode: str = "paper"  # paper | live (live also needs scheduler_confirm_live)
    scheduler_confirm_live: str = "false"  # true: like --confirm-live
    scheduler_start: str = "09:15"  # IST, first daemon launch (scan follows the scan delay)
    scheduler_last_start: str = "11:30"  # IST, end of the catch-up window if the host was down
    scheduler_deadline: str = "15:45"  # IST, SIGTERM a session still running (EOD exit is 15:20)
    scheduler_settle_at: str = "18:30"  # IST, `skopaq settle` backstop; "" = off
    # IST, alert if the INDstocks token is missing or expires before the session ends; "" = off
    scheduler_preflight: str = "08:45"
    scheduler_poll_seconds: str = "30"
    scheduler_kill_after_seconds: str = "300"  # SIGKILL this long after the deadline/stop SIGTERM
    scheduler_state_dir: str = "~/scheduler"  # at-most-once markers (on the home volume)
    # Optional dead-man's switch: GET URL on success, URL/fail on failure
    scheduler_ping_url: str = ""
    heartbeat_file: str = ""  # touched by long-running services for container health checks
    nse_holidays: str = ""  # extra NSE closures, comma-separated YYYY-MM-DD

    # ── Regime Detection ──────────────────────────────────────────────
    regime_detection_enabled: bool = False  # Off until tested with live data

    # ── Semantic Cache (Redis LangCache) ────────────────────────────────
    langcache_enabled: bool = False
    langcache_api_key: SecretStr = SecretStr("")
    langcache_server_url: str = ""
    langcache_cache_id: str = ""
    langcache_threshold: float = 0.90  # Cosine similarity threshold (0–1)

    # ── Asset Class ──────────────────────────────────────────────────────
    asset_class: Literal["equity", "crypto"] = "equity"
    crypto_quote_currency: str = "USDT"
    binance_base_url: str = "https://api.binance.com"

    # ── Crypto Exchange (Live Trading) ──────────────────────────────────────
    binance_api_key: SecretStr = SecretStr("")
    binance_api_secret: SecretStr = SecretStr("")
    binance_testnet: bool = True  # Default to testnet for safety

    # ── Multi-Exchange Support ──────────────────────────────────────────────
    preferred_exchange: str = "binance"  # binance, coinbase, kraken (future)

    # ── Blockchain / On-Chain ────────────────────────────────────────────────
    whale_alert_threshold_usd: int = 100000  # $100K min for whale alerts
    gas_alert_enabled: bool = False
    gas_alert_threshold_gwei: float = 100.0  # Alert when gas exceeds this

    # ── WebSocket ───────────────────────────────────────────────────────────
    ws_reconnect_enabled: bool = True
    ws_reconnect_delay_seconds: float = 5.0

    # ── Ollama (Local LLM Fallback) ──────────────────────────────────────────
    ollama_enabled: bool = False        # Opt-in: set True to use local models
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = ""              # Auto-detect if empty

    # ── Kite Connect (Zerodha) ───────────────────────────────────────────────
    kite_api_key: str = ""
    kite_api_secret: SecretStr = SecretStr("")
    kite_client_id: str = ""
    kite_access_token: SecretStr = SecretStr("")  # Set after OAuth login

    # ── Telegram Bot ────────────────────────────────────────────────────────
    telegram_bot_token: SecretStr = SecretStr("")
    # Comma-separated chat IDs allowed to use the bot, besides
    # SKOPAQ_TELEGRAM_CHAT_ID; none = nobody (/start replies with the chat ID).
    telegram_allowed_chat_ids: str = ""

    # ── Database (Fly.io Postgres) ──────────────────────────────────────────
    database_url: str = ""  # Set by Fly.io attachment or manually

    # ── Logging ─────────────────────────────────────────────────────────────
    log_level: str = "INFO"

    @field_validator(*_LENIENT_FIELDS, mode="wrap")
    @classmethod
    def _default_if_malformed(cls, value: Any, handler: ValidatorFunctionWrapHandler,
                              info: ValidationInfo) -> Any:
        """A malformed live order setting becomes its default, with a WARNING."""
        default = cls.model_fields[info.field_name].default
        try:
            parsed = handler(value)
        except ValidationError:
            parsed = None
        else:
            if not (isinstance(parsed, float) and not math.isfinite(parsed)):
                return parsed
        logger.warning("SKOPAQ_%s=%r is not a valid value; using the default %r",
                       info.field_name.upper(), value, default)
        return default
