"""Tests for the autonomous trading daemon.

Tests cover:
- PRE_OPEN: token validation, infra build
- SCANNING: candidate discovery, delay wait
- ANALYZING: max trades enforcement, stop event, error handling
- MONITORING: position handoff
- CLOSING: safety net sell-all
- Min profit gate integration
- Session report compilation
- Configuration defaults
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skopaq.execution.daemon import (
    DaemonPhase,
    DaemonSessionReport,
    TradingDaemon,
    _IST,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def config():
    """Minimal config mock for daemon tests."""
    cfg = MagicMock()
    cfg.trading_mode = "paper"
    cfg.daemon_max_trades_per_session = 3
    cfg.daemon_max_candidates_to_analyze = 5
    cfg.daemon_pre_open_minutes = 5
    cfg.daemon_scan_delay_after_open_seconds = 0  # No delay in tests
    cfg.daemon_min_profit_threshold_pct = 0.5
    cfg.daemon_min_profit_threshold_inr = 150.0
    cfg.daemon_session_log_dir = "logs/daemon"
    cfg.daemon_heartbeat_interval_seconds = 300
    cfg.monitor_poll_interval_seconds = 1
    cfg.monitor_hard_stop_pct = 0.04
    cfg.monitor_eod_exit_minutes_before_close = 10
    cfg.monitor_ai_interval_cycles = 6
    cfg.monitor_trailing_stop_enabled = False
    cfg.monitor_trailing_stop_pct = 0.02
    cfg.initial_paper_capital = 100_000
    cfg.max_sector_concentration_pct = 0.40
    cfg.position_sizing_enabled = False
    cfg.risk_per_trade_pct = 0.01
    cfg.atr_multiplier = 2.0
    cfg.atr_period = 14
    cfg.selected_analysts = "market,social"
    cfg.reflection_enabled = False
    cfg.supabase_url = ""
    cfg.supabase_service_key = MagicMock()
    cfg.supabase_service_key.get_secret_value.return_value = ""
    cfg.google_api_key = MagicMock()
    cfg.google_api_key.get_secret_value.return_value = "test"
    cfg.anthropic_api_key = MagicMock()
    cfg.anthropic_api_key.get_secret_value.return_value = ""
    cfg.openrouter_api_key = MagicMock()
    cfg.openrouter_api_key.get_secret_value.return_value = ""
    cfg.regime_detection_enabled = False
    cfg.asset_class = "equity"
    cfg.langcache_enabled = False
    return cfg


@pytest.fixture
def daemon(config):
    """Create a TradingDaemon instance with mock config."""
    return TradingDaemon(config)


# ── Config Defaults ──────────────────────────────────────────────────────────


def test_daemon_config_defaults():
    """Daemon config fields exist with correct defaults."""
    from skopaq.config import SkopaqConfig

    cfg = SkopaqConfig()
    assert cfg.daemon_max_trades_per_session == 3
    assert cfg.daemon_max_candidates_to_analyze == 5
    assert cfg.daemon_pre_open_minutes == 5
    assert cfg.daemon_scan_delay_after_open_seconds == 60
    assert cfg.daemon_min_profit_threshold_pct == 0.5
    assert cfg.daemon_min_profit_threshold_inr == 150.0
    assert cfg.daemon_session_log_dir == "logs/daemon"
    assert cfg.daemon_heartbeat_interval_seconds == 300


# ── Safety Rules ─────────────────────────────────────────────────────────────


def test_daemon_safety_rules_tighter():
    """Daemon safety rules are tighter than defaults."""
    from skopaq.constants import DAEMON_SAFETY_RULES, SAFETY_RULES

    assert DAEMON_SAFETY_RULES.max_open_positions < SAFETY_RULES.max_open_positions
    assert DAEMON_SAFETY_RULES.max_order_value_inr < SAFETY_RULES.max_order_value_inr
    assert DAEMON_SAFETY_RULES.max_orders_per_minute < SAFETY_RULES.max_orders_per_minute


def test_daemon_paper_safety_rules_relaxed():
    """Daemon paper rules relax timing for testing."""
    from skopaq.constants import DAEMON_PAPER_SAFETY_RULES

    assert DAEMON_PAPER_SAFETY_RULES.market_hours_only is False
    assert DAEMON_PAPER_SAFETY_RULES.require_stop_loss is False
    assert DAEMON_PAPER_SAFETY_RULES.max_open_positions == 3


# ── Phase Sequencing ─────────────────────────────────────────────────────────


def test_daemon_starts_idle(daemon):
    """Daemon starts in IDLE phase."""
    assert daemon.phase == DaemonPhase.IDLE


# ── Session Report ───────────────────────────────────────────────────────────


def test_session_report_defaults():
    """DaemonSessionReport fields initialise correctly."""
    report = DaemonSessionReport()
    assert report.candidates_scanned == 0
    assert report.candidates_analyzed == 0
    assert report.trades_opened == 0
    assert report.trades_rejected == 0
    assert report.holds == 0
    assert report.sells_executed == 0
    assert report.sells_failed == 0
    assert report.gross_pnl == 0.0
    assert report.errors == []
    assert report.monitor_result is None
    assert report.phase_times == {}


def test_session_report_accumulates():
    """Report fields accumulate correctly during a session."""
    report = DaemonSessionReport(session_date="2026-03-04")
    report.candidates_scanned = 10
    report.candidates_analyzed = 5
    report.trades_opened = 2
    report.holds = 3
    report.gross_pnl = 450.0
    report.phase_times = {"pre_open": 2.0, "scanning": 30.0}

    assert report.session_date == "2026-03-04"
    assert report.trades_opened == 2
    assert report.holds == 3
    assert sum(report.phase_times.values()) == 32.0


# ── PRE_OPEN Phase ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pre_open_validates_token(daemon):
    """PRE_OPEN fails if token is invalid."""
    with patch("skopaq.broker.token_manager.TokenManager") as MockTM:
        mock_health = MagicMock()
        mock_health.valid = False
        mock_health.warning = "Token expired"
        MockTM.return_value.get_health.return_value = mock_health

        with pytest.raises(RuntimeError, match="INDstocks token invalid"):
            await daemon._phase_pre_open()


# ── SCANNING Phase ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scan_returns_candidates(daemon):
    """Scanner phase returns candidate list."""
    mock_candidate = MagicMock()
    mock_candidate.symbol = "RELIANCE"
    mock_candidate.urgency = "high"

    with patch("skopaq.cli.main._run_scan", new_callable=AsyncMock) as mock_scan:
        mock_scan.return_value = [mock_candidate]
        result = await daemon._phase_scan()

    assert len(result) == 1
    assert result[0].symbol == "RELIANCE"


@pytest.mark.asyncio
async def test_scan_returns_empty_on_failure(daemon):
    """Scanner phase returns empty list if scanner raises."""
    with patch("skopaq.cli.main._run_scan", new_callable=AsyncMock) as mock_scan:
        mock_scan.side_effect = RuntimeError("Scanner crash")
        result = await daemon._phase_scan()

    assert result == []


@pytest.mark.asyncio
async def test_scan_respects_stop_event(config):
    """Scanner returns empty if stop event fires during delay."""
    config.daemon_scan_delay_after_open_seconds = 30  # Long delay
    stop = asyncio.Event()
    daemon = TradingDaemon(config, stop_event=stop)

    # Fire stop immediately
    stop.set()
    result = await daemon._phase_scan()
    assert result == []


# ── ANALYZING Phase ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_analyze_respects_max_trades(daemon):
    """Analysis stops after max_trades BUYs."""
    daemon._config.daemon_max_trades_per_session = 1
    daemon._max_trades = 1
    daemon._graph = AsyncMock()
    daemon._router = MagicMock()
    daemon._router._paper = MagicMock()
    daemon._config.reflection_enabled = False

    # Two candidates, but max_trades = 1
    candidates = [
        MagicMock(symbol="AAA", urgency="high"),
        MagicMock(symbol="BBB", urgency="normal"),
    ]

    # Both return BUY with success
    mock_result = MagicMock()
    mock_result.error = None
    mock_result.signal = MagicMock(action="BUY", confidence=80, quantity=1)
    mock_result.execution = MagicMock(success=True, fill_price=100.0)
    daemon._graph.analyze_and_execute = AsyncMock(return_value=mock_result)

    report = DaemonSessionReport()

    with patch("skopaq.cli.main._compute_risk_scales", return_value=(1.0, 1.0)):
        with patch("skopaq.cli.main._inject_paper_quote", new_callable=AsyncMock):
            buys = await daemon._phase_analyze_and_trade(candidates, report)

    assert report.trades_opened == 1  # Only 1 trade, despite 2 candidates
    assert len(buys) == 1


@pytest.mark.asyncio
async def test_analyze_stops_on_stop_event(config):
    """Analysis halts when stop event is set."""
    stop = asyncio.Event()
    d = TradingDaemon(config, stop_event=stop)
    d._graph = AsyncMock()
    d._router = MagicMock()

    stop.set()  # Signal shutdown

    candidates = [MagicMock(symbol="AAA", urgency="high")]
    report = DaemonSessionReport()

    with patch("skopaq.cli.main._compute_risk_scales", return_value=(1.0, 1.0)):
        buys = await d._phase_analyze_and_trade(candidates, report)

    assert buys == []
    assert report.candidates_analyzed == 0


@pytest.mark.asyncio
async def test_analyze_skips_hold_signals(daemon):
    """HOLD signals don't count toward max trades."""
    daemon._graph = AsyncMock()
    daemon._router = MagicMock()
    daemon._router._paper = MagicMock()

    # Returns HOLD
    mock_result = MagicMock()
    mock_result.error = None
    mock_result.signal = MagicMock(action="HOLD", confidence=30)
    mock_result.execution = None
    daemon._graph.analyze_and_execute = AsyncMock(return_value=mock_result)

    candidates = [MagicMock(symbol="AAA", urgency="high")]
    report = DaemonSessionReport()

    with patch("skopaq.cli.main._compute_risk_scales", return_value=(1.0, 1.0)):
        with patch("skopaq.cli.main._inject_paper_quote", new_callable=AsyncMock):
            buys = await daemon._phase_analyze_and_trade(candidates, report)

    assert report.holds == 1
    assert report.trades_opened == 0
    assert len(buys) == 0


@pytest.mark.asyncio
async def test_analyze_handles_errors_gracefully(daemon):
    """Individual analysis failures don't crash the session."""
    daemon._graph = AsyncMock()
    daemon._router = MagicMock()
    daemon._router._paper = MagicMock()

    # First candidate raises, second succeeds
    daemon._graph.analyze_and_execute = AsyncMock(
        side_effect=[
            RuntimeError("LLM timeout"),
            MagicMock(
                error=None,
                signal=MagicMock(action="BUY", confidence=80, quantity=1),
                execution=MagicMock(success=True, fill_price=200.0),
            ),
        ],
    )

    candidates = [
        MagicMock(symbol="FAIL", urgency="high"),
        MagicMock(symbol="OK", urgency="normal"),
    ]
    report = DaemonSessionReport()

    with patch("skopaq.cli.main._compute_risk_scales", return_value=(1.0, 1.0)):
        with patch("skopaq.cli.main._inject_paper_quote", new_callable=AsyncMock):
            buys = await daemon._phase_analyze_and_trade(candidates, report)

    assert report.candidates_analyzed == 2
    assert report.trades_opened == 1  # Second candidate succeeded
    assert len(report.errors) == 1   # First candidate's error captured


# ── MONITORING Phase ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_monitor_receives_positions(daemon):
    """Monitor phase creates a PositionMonitor and runs it."""
    daemon._executor = MagicMock()
    daemon._client = MagicMock()
    daemon._router = MagicMock()
    daemon._llm_map = {"sell_analyst": MagicMock()}

    from skopaq.execution.position_monitor import MonitorResult

    mock_result = MonitorResult(
        positions_monitored=1,
        sells_executed=1,
        total_pnl=450.0,
        exit_reasons=["AI SELL"],
    )

    with patch(
        "skopaq.execution.position_monitor.PositionMonitor",
    ) as MockMonitor:
        mock_instance = AsyncMock()
        mock_instance.run = AsyncMock(return_value=mock_result)
        MockMonitor.return_value = mock_instance

        result = await daemon._phase_monitor()

    assert result.positions_monitored == 1
    assert result.sells_executed == 1
    assert result.total_pnl == 450.0


# ── CLOSING Phase ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_closing_no_positions(daemon):
    """Closing phase does nothing when no positions remain."""
    daemon._router = AsyncMock()
    daemon._router.get_positions = AsyncMock(return_value=[])

    await daemon._phase_close()  # Should not raise


@pytest.mark.asyncio
async def test_closing_force_sells(daemon):
    """Closing phase force-sells remaining positions."""
    mock_pos = MagicMock()
    mock_pos.symbol = "RELIANCE"
    mock_pos.quantity = 5
    mock_pos.average_price = 2500.0

    daemon._router = AsyncMock()
    daemon._router.get_positions = AsyncMock(return_value=[mock_pos])

    daemon._executor = AsyncMock()
    mock_exec_result = MagicMock(success=True)
    daemon._executor.execute_signal = AsyncMock(return_value=mock_exec_result)

    await daemon._phase_close()

    daemon._executor.execute_signal.assert_called_once()
    call_signal = daemon._executor.execute_signal.call_args[0][0]
    assert call_signal.action == "SELL"
    assert call_signal.symbol == "RELIANCE"


# ── Wait for Market Open ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_market_open_already_past(daemon):
    """Returns immediately if already past pre-open time."""
    with patch("skopaq.execution.daemon.datetime") as mock_dt:
        # Simulate 10:00 IST — well past 09:10
        mock_now = datetime(2026, 3, 4, 10, 0, tzinfo=_IST)
        mock_dt.now.return_value = mock_now
        mock_dt.combine = datetime.combine
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        await daemon.wait_for_market_open()  # Should return immediately


# ── Dry Run ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_skips_trading(daemon):
    """Dry run only scans — does not trade or monitor."""
    mock_candidate = MagicMock(symbol="RELIANCE", urgency="high")

    with patch.object(daemon, "_phase_pre_open", new_callable=AsyncMock):
        with patch.object(
            daemon, "_phase_scan",
            new_callable=AsyncMock, return_value=[mock_candidate],
        ):
            report = await daemon.run_session(dry_run=True)

    assert report.candidates_scanned == 1
    assert report.trades_opened == 0
    assert report.sells_executed == 0


@pytest.mark.asyncio
async def test_dry_run_does_not_settle_decisions(daemon):
    """Settling makes LLM reflection calls and Supabase writes: not in a dry run."""
    with patch.object(daemon, "_phase_pre_open", new_callable=AsyncMock), \
         patch.object(daemon, "_phase_scan", new_callable=AsyncMock, return_value=[]), \
         patch.object(daemon, "_settle_due_decisions", new_callable=AsyncMock,
                      return_value=5) as settle:
        dry = await daemon.run_session(dry_run=True)
        settle.assert_not_awaited()
        real = await daemon.run_session()

    assert dry.decisions_settled == 0
    assert real.decisions_settled == 5


# ── Min Profit Gate (integration check) ──────────────────────────────────────


def test_min_profit_gate_config_exists():
    """Config has min profit threshold fields for the daemon."""
    from skopaq.config import SkopaqConfig

    cfg = SkopaqConfig()
    assert hasattr(cfg, "daemon_min_profit_threshold_pct")
    assert hasattr(cfg, "daemon_min_profit_threshold_inr")
    assert cfg.daemon_min_profit_threshold_pct > 0
    assert cfg.daemon_min_profit_threshold_inr > 0


def test_sell_analyst_accepts_min_profit_params():
    """analyze_exit() accepts min_profit_threshold_pct and estimated_round_trip_brokerage."""
    import inspect
    from skopaq.agents.sell_analyst import analyze_exit

    sig = inspect.signature(analyze_exit)
    params = list(sig.parameters.keys())
    assert "min_profit_threshold_pct" in params
    assert "estimated_round_trip_brokerage" in params


@pytest.mark.asyncio
async def test_close_phase_records_force_sells(daemon):
    """EOD force-sells are persisted like any other exit."""
    from skopaq.broker.models import Position

    daemon._router = AsyncMock()
    daemon._router.get_positions = AsyncMock(return_value=[
        Position(symbol="TCS", quantity=Decimal("3"), average_price=4000.0)])
    sold = MagicMock(success=True)
    daemon._executor = AsyncMock()
    daemon._executor.execute_signal = AsyncMock(return_value=sold)

    with patch.object(daemon, "_record_exit", new_callable=AsyncMock) as record:
        await daemon._phase_close()

    signal, execution = record.await_args.args
    assert (signal.symbol, signal.action, signal.entry_price) == ("TCS", "SELL", 4000.0)
    assert execution is sold


@pytest.mark.asyncio
async def test_record_exit_goes_through_the_trade_lifecycle(daemon):
    from skopaq.broker.models import TradingSignal

    signal = TradingSignal(symbol="TCS", action="SELL", confidence=100, entry_price=4000.0)
    daemon._config.reflection_enabled = False
    with patch("skopaq.cli.main._run_lifecycle", new_callable=AsyncMock) as lifecycle:
        await daemon._record_exit(signal, MagicMock(success=True))

    config, graph, _store, result = lifecycle.await_args.args
    assert graph is None  # reflection off: record only
    assert (result.symbol, result.signal) == ("TCS", signal)
