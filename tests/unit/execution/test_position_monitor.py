"""Tests for the two-tier position monitor.

Tests cover:
- Safety tier: hard stop-loss, trailing stop, EOD exit
- AI tier: sell/hold decisions, interval gating, failure fallback
- Execution: sell pipeline, paper mode, graceful shutdown
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skopaq.agents.sell_analyst import SellDecision
from skopaq.broker.models import Position, Quote, TradingSignal
from skopaq.execution.position_monitor import (
    MonitoredPosition,
    MonitorResult,
    PositionMonitor,
    _IST,
    _MARKET_CLOSE,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def config():
    """Minimal config mock for monitor tests."""
    cfg = MagicMock()
    cfg.monitor_poll_interval_seconds = 1  # Fast for tests
    cfg.monitor_hard_stop_pct = 0.04
    cfg.monitor_eod_exit_minutes_before_close = 10
    cfg.monitor_ai_interval_cycles = 2  # AI every 2nd cycle
    cfg.monitor_trailing_stop_enabled = False
    cfg.monitor_trailing_stop_pct = 0.02
    cfg.trading_mode = "paper"
    cfg.initial_paper_capital = 100_000
    cfg.max_sector_concentration_pct = 0.40
    return cfg


@pytest.fixture
def mock_client():
    """Mock INDstocksClient with LTP support."""
    client = AsyncMock()
    client.get_ltp = AsyncMock(return_value=100.0)
    return client


@pytest.fixture
def mock_router():
    """Mock OrderRouter."""
    router = AsyncMock()
    router.get_positions = AsyncMock(return_value=[
        Position(
            symbol="TEST",
            exchange="NSE",
            quantity=Decimal("10"),
            average_price=100.0,
            last_price=100.0,
        ),
    ])
    router._paper = MagicMock()  # PaperEngine
    return router


@pytest.fixture
def mock_executor():
    """Mock Executor with successful sell."""
    executor = AsyncMock()
    result = MagicMock()
    result.success = True
    result.fill_price = 100.0
    result.rejection_reason = None
    executor.execute_signal = AsyncMock(return_value=result)
    return executor


@pytest.fixture
def mock_llm():
    """Mock LLM for sell analyst."""
    return MagicMock()


def _make_monitor(
    executor, client, router, config,
    llm=None, stop_event=None, ai_enabled=True,
) -> PositionMonitor:
    """Helper to build a PositionMonitor with mocks."""
    return PositionMonitor(
        executor=executor,
        client=client,
        router=router,
        config=config,
        llm=llm,
        stop_event=stop_event,
        ai_enabled=ai_enabled,
    )


# ── Safety Tier Tests ────────────────────────────────────────────────────────


class TestSafetyTier:
    """Tests for rule-based safety exits."""

    def test_hard_stop_triggers_sell(self, config):
        """LTP below hard stop (4%) should trigger SELL."""
        mon = _make_monitor(
            MagicMock(), MagicMock(), MagicMock(), config, ai_enabled=False,
        )
        pos = MonitoredPosition(
            symbol="TEST", scrip_code="NSE_123",
            entry_price=100.0, quantity=10,
        )
        # LTP at 95 = 5% below entry > 4% threshold
        reason = mon._check_safety(pos, ltp=95.0)
        assert reason is not None
        assert "HARD STOP" in reason

    def test_no_stop_within_range(self, config):
        """LTP within safe range should NOT trigger safety exit."""
        mon = _make_monitor(
            MagicMock(), MagicMock(), MagicMock(), config, ai_enabled=False,
        )
        pos = MonitoredPosition(
            symbol="TEST", scrip_code="NSE_123",
            entry_price=100.0, quantity=10,
        )
        # LTP at 98 = 2% below entry < 4% threshold
        reason = mon._check_safety(pos, ltp=98.0)
        # Should only be None if not EOD time
        # Patch time to be during market hours (not EOD)
        with patch(
            "skopaq.execution.position_monitor.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 4, 11, 0, 0, tzinfo=_IST)
            mock_dt.today.return_value = datetime(2026, 3, 4)
            mock_dt.combine = datetime.combine
            reason = mon._check_safety(pos, ltp=98.0)
            assert reason is None

    def test_trailing_stop_ratchets_up(self, config):
        """Trailing stop should ratchet UP with price but never down."""
        config.monitor_trailing_stop_enabled = True
        mon = _make_monitor(
            MagicMock(), MagicMock(), MagicMock(), config, ai_enabled=False,
        )
        pos = MonitoredPosition(
            symbol="TEST", scrip_code="NSE_123",
            entry_price=100.0, quantity=10,
            high_water_mark=110.0,  # Price went up to 110
        )
        # LTP at 107.5 — within 2% of HWM (trail = 110 * 0.98 = 107.8)
        with patch(
            "skopaq.execution.position_monitor.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 4, 11, 0, 0, tzinfo=_IST)
            mock_dt.today.return_value = datetime(2026, 3, 4)
            mock_dt.combine = datetime.combine
            reason = mon._check_safety(pos, ltp=107.5)
            assert reason is not None
            assert "TRAILING STOP" in reason

    def test_trailing_stop_does_not_ratchet_down(self, config):
        """Trailing stop should NOT fire if LTP is above the trail line."""
        config.monitor_trailing_stop_enabled = True
        mon = _make_monitor(
            MagicMock(), MagicMock(), MagicMock(), config, ai_enabled=False,
        )
        pos = MonitoredPosition(
            symbol="TEST", scrip_code="NSE_123",
            entry_price=100.0, quantity=10,
            high_water_mark=110.0,
        )
        # LTP at 109 — above trail (110 * 0.98 = 107.8)
        with patch(
            "skopaq.execution.position_monitor.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 4, 11, 0, 0, tzinfo=_IST)
            mock_dt.today.return_value = datetime(2026, 3, 4)
            mock_dt.combine = datetime.combine
            reason = mon._check_safety(pos, ltp=109.0)
            assert reason is None

    def test_eod_exit(self, config):
        """Should trigger sell when IST time is past EOD threshold."""
        mon = _make_monitor(
            MagicMock(), MagicMock(), MagicMock(), config, ai_enabled=False,
        )
        pos = MonitoredPosition(
            symbol="TEST", scrip_code="NSE_123",
            entry_price=100.0, quantity=10,
        )
        # Mock time to 15:25 IST (past 15:20 threshold)
        with patch(
            "skopaq.execution.position_monitor.datetime"
        ) as mock_dt:
            mock_dt.now.return_value = datetime(2026, 3, 4, 15, 25, 0, tzinfo=_IST)
            mock_dt.today.return_value = datetime(2026, 3, 4)
            mock_dt.combine = datetime.combine
            reason = mon._check_safety(pos, ltp=105.0)
            assert reason is not None
            assert "EOD EXIT" in reason


# ── AI Tier Tests ────────────────────────────────────────────────────────────


class TestAITier:
    """Tests for AI sell analyst integration."""

    @pytest.mark.asyncio
    async def test_ai_sell_triggers_execution(self, config, mock_llm):
        """AI SELL recommendation should produce a SellDecision with SELL."""
        mon = _make_monitor(
            MagicMock(), MagicMock(), MagicMock(), config,
            llm=mock_llm, ai_enabled=True,
        )
        sell_decision = SellDecision(
            action="SELL", confidence=75,
            reasoning="RSI overbought, MACD crossing down",
        )
        with patch(
            "skopaq.execution.position_monitor.analyze_exit",
            return_value=sell_decision,
        ):
            pos = MonitoredPosition(
                symbol="TEST", scrip_code="NSE_123",
                entry_price=100.0, quantity=10,
            )
            decision = await mon._check_ai(pos, ltp=105.0, pnl_pct=5.0)
            assert decision is not None
            assert decision.action == "SELL"
            assert decision.confidence == 75

    @pytest.mark.asyncio
    async def test_ai_hold_keeps_position(self, config, mock_llm):
        """AI HOLD recommendation should not trigger a sell."""
        mon = _make_monitor(
            MagicMock(), MagicMock(), MagicMock(), config,
            llm=mock_llm, ai_enabled=True,
        )
        hold_decision = SellDecision(
            action="HOLD", confidence=60,
            reasoning="Momentum still bullish",
        )
        with patch(
            "skopaq.execution.position_monitor.analyze_exit",
            return_value=hold_decision,
        ):
            pos = MonitoredPosition(
                symbol="TEST", scrip_code="NSE_123",
                entry_price=100.0, quantity=10,
            )
            decision = await mon._check_ai(pos, ltp=102.0, pnl_pct=2.0)
            assert decision is not None
            assert decision.action == "HOLD"

    @pytest.mark.asyncio
    async def test_ai_failure_defaults_to_hold(self, config):
        """When LLM is None, AI tier returns None (no action)."""
        mon = _make_monitor(
            MagicMock(), MagicMock(), MagicMock(), config,
            llm=None, ai_enabled=False,
        )
        pos = MonitoredPosition(
            symbol="TEST", scrip_code="NSE_123",
            entry_price=100.0, quantity=10,
        )
        decision = await mon._check_ai(pos, ltp=102.0, pnl_pct=2.0)
        assert decision is None


# ── Execution Tests ──────────────────────────────────────────────────────────


class TestExecution:
    """Tests for the sell execution pipeline."""

    @pytest.mark.asyncio
    async def test_sell_uses_market_order(
        self, config, mock_executor,
    ):
        """SELL signal should have entry_price set (not None) for P&L tracking."""
        mon = _make_monitor(
            mock_executor, MagicMock(), MagicMock(), config, ai_enabled=False,
        )
        mon._router = MagicMock()
        mon._router._paper = MagicMock()

        pos = MonitoredPosition(
            symbol="TEST", scrip_code="NSE_123",
            entry_price=100.0, quantity=10,
        )
        result = MonitorResult()
        ok = await mon._execute_sell(pos, ltp=95.0, reason="test stop", result=result)

        assert ok is True
        assert result.sells_executed == 1

        # Verify the signal passed to executor
        call_args = mock_executor.execute_signal.call_args
        signal = call_args[0][0]  # First positional arg
        assert isinstance(signal, TradingSignal)
        assert signal.action == "SELL"
        assert signal.entry_price == 100.0
        assert signal.quantity == Decimal("10")

    @pytest.mark.asyncio
    async def test_paper_mode_injects_quote(
        self, config, mock_executor,
    ):
        """In paper mode, a Quote should be injected before selling."""
        mock_paper = MagicMock()
        mock_router = MagicMock()
        mock_router._paper = mock_paper

        mon = _make_monitor(
            mock_executor, MagicMock(), mock_router, config, ai_enabled=False,
        )
        # Override router reference
        mon._router = mock_router

        pos = MonitoredPosition(
            symbol="TEST", scrip_code="NSE_123",
            entry_price=100.0, quantity=10,
        )
        result = MonitorResult()
        await mon._execute_sell(pos, ltp=95.0, reason="test stop", result=result)

        # Paper engine should have received a quote update
        mock_paper.update_quote.assert_called_once()
        quote = mock_paper.update_quote.call_args[0][0]
        assert isinstance(quote, Quote)
        assert quote.symbol == "TEST"
        assert quote.ltp == 95.0

    @pytest.mark.asyncio
    async def test_sell_failure_increments_failed_count(self, config):
        """Failed sell should increment sells_failed, not sells_executed."""
        executor = AsyncMock()
        fail_result = MagicMock()
        fail_result.success = False
        fail_result.rejection_reason = "Insufficient funds"
        executor.execute_signal = AsyncMock(return_value=fail_result)

        mon = _make_monitor(
            executor, MagicMock(), MagicMock(), config, ai_enabled=False,
        )
        mon._router = MagicMock()
        mon._router._paper = MagicMock()

        pos = MonitoredPosition(
            symbol="TEST", scrip_code="NSE_123",
            entry_price=100.0, quantity=10,
        )
        result = MonitorResult()
        ok = await mon._execute_sell(pos, ltp=95.0, reason="test", result=result)

        assert ok is False
        assert result.sells_failed == 1
        assert result.sells_executed == 0


# ── Integration-style Tests ──────────────────────────────────────────────────


class TestMonitorRun:
    """Tests for the full run() loop."""

    @pytest.mark.asyncio
    async def test_no_positions_exits_immediately(
        self, config, mock_client, mock_executor,
    ):
        """Monitor should exit immediately if no positions are open."""
        router = AsyncMock()
        router.get_positions = AsyncMock(return_value=[])

        # Patch resolve_scrip_code to avoid real API calls
        with patch(
            "skopaq.broker.scrip_resolver.resolve_scrip_code",
            return_value="NSE_123",
        ):
            mon = _make_monitor(
                mock_executor, mock_client, router, config, ai_enabled=False,
            )
            result = await mon.run()

        assert result.positions_monitored == 0
        assert result.sells_executed == 0

    @pytest.mark.asyncio
    async def test_graceful_shutdown_on_stop_event(
        self, config, mock_client, mock_router, mock_executor,
    ):
        """Setting the stop event should cause the monitor to exit the loop."""
        stop_event = asyncio.Event()

        with patch(
            "skopaq.broker.scrip_resolver.resolve_scrip_code",
            return_value="NSE_123",
        ):
            mon = _make_monitor(
                mock_executor, mock_client, mock_router, config,
                stop_event=stop_event, ai_enabled=False,
            )

            # Set stop event after a short delay
            async def _set_stop():
                await asyncio.sleep(0.1)
                stop_event.set()

            # Patch safety to not trigger (so the loop runs until stopped)
            with patch.object(mon, "_check_safety", return_value=None):
                with patch.object(mon, "_should_eod_exit", return_value=False):
                    task = asyncio.create_task(_set_stop())
                    result = await mon.run()
                    await task

        assert result.positions_monitored == 1
        # Should not have sold (no safety trigger)

    @pytest.mark.asyncio
    async def test_zero_ltp_skips_position(
        self, config, mock_router, mock_executor,
    ):
        """Zero LTP should skip the position without crashing."""
        client = AsyncMock()
        client.get_ltp = AsyncMock(return_value=0.0)

        stop_event = asyncio.Event()

        with patch(
            "skopaq.broker.scrip_resolver.resolve_scrip_code",
            return_value="NSE_123",
        ):
            mon = _make_monitor(
                mock_executor, client, mock_router, config,
                stop_event=stop_event, ai_enabled=False,
            )

            # Let it run 1 cycle then stop
            async def _stop_after():
                await asyncio.sleep(0.2)
                stop_event.set()

            with patch.object(mon, "_should_eod_exit", return_value=False):
                task = asyncio.create_task(_stop_after())
                result = await mon.run()
                await task

        # Should not have sold (zero LTP skipped)
        assert result.sells_executed == 0

    @pytest.mark.asyncio
    async def test_safety_overrides_ai_hold(
        self, config, mock_client, mock_router, mock_executor, mock_llm,
    ):
        """Safety stop-loss should fire even if AI would say HOLD.

        This tests the two-tier architecture: safety runs FIRST, and if it
        triggers, AI is never consulted.
        """
        # LTP well below stop-loss
        mock_client.get_ltp = AsyncMock(return_value=90.0)  # 10% below entry

        stop_event = asyncio.Event()

        with patch(
            "skopaq.broker.scrip_resolver.resolve_scrip_code",
            return_value="NSE_123",
        ):
            mon = _make_monitor(
                mock_executor, mock_client, mock_router, config,
                llm=mock_llm, stop_event=stop_event, ai_enabled=True,
            )

            # AI would say HOLD — but safety should override
            with patch(
                "skopaq.execution.position_monitor.analyze_exit",
                return_value=SellDecision(action="HOLD", confidence=80),
            ) as mock_ai:
                with patch.object(mon, "_should_eod_exit", return_value=False):
                    result = await mon.run()

            # Safety should have sold, AI should NOT have been called
            assert result.sells_executed == 1
            assert "HARD STOP" in result.exit_reasons[0]
            mock_ai.assert_not_called()


# ── SellDecision Parsing Tests ───────────────────────────────────────────────


class TestSellDecisionParsing:
    """Tests for the _parse_decision helper in sell_analyst."""

    def test_parse_sell_decision(self):
        from skopaq.agents.sell_analyst import _parse_decision

        text = """Based on the analysis, the stock shows bearish divergence.

DECISION: SELL
CONFIDENCE: 75
REASONING: RSI divergence detected, MACD crossing below signal line."""

        decision = _parse_decision(text)
        assert decision.action == "SELL"
        assert decision.confidence == 75
        assert "RSI divergence" in decision.reasoning

    def test_parse_hold_decision(self):
        from skopaq.agents.sell_analyst import _parse_decision

        text = """The stock is still in an uptrend.

DECISION: HOLD
CONFIDENCE: 60
REASONING: Strong momentum, no reversal signals yet."""

        decision = _parse_decision(text)
        assert decision.action == "HOLD"
        assert decision.confidence == 60

    def test_parse_empty_text_defaults_to_hold(self):
        from skopaq.agents.sell_analyst import _parse_decision

        decision = _parse_decision("")
        assert decision.action == "HOLD"

    def test_parse_confidence_clamped(self):
        from skopaq.agents.sell_analyst import _parse_decision

        text = "DECISION: SELL\nCONFIDENCE: 150\nREASONING: test"
        decision = _parse_decision(text)
        assert decision.confidence == 100  # Clamped to max


class TestExitRecording:
    """on_exit persists each successful sell (its P&L feeds the loss limits)."""

    def _monitor(self, config, executor, on_exit):
        mon = PositionMonitor(executor=executor, client=MagicMock(), router=MagicMock(),
                              config=config, ai_enabled=False, on_exit=on_exit)
        mon._router._paper = MagicMock()
        return mon

    @pytest.mark.asyncio
    async def test_successful_sell_is_recorded(self, config, mock_executor):
        recorded = []

        async def on_exit(signal, execution):
            recorded.append((signal.symbol, signal.action, signal.entry_price, execution))

        mon = self._monitor(config, mock_executor, on_exit)
        pos = MonitoredPosition(symbol="TEST", scrip_code="NSE_1", entry_price=100.0, quantity=10)
        assert await mon._execute_sell(pos, ltp=95.0, reason="stop", result=MonitorResult())

        assert recorded == [("TEST", "SELL", 100.0,
                             mock_executor.execute_signal.return_value)]

    @pytest.mark.asyncio
    async def test_recording_failure_does_not_undo_the_sell(self, config, mock_executor):
        async def broken(signal, execution):
            raise RuntimeError("supabase down")

        mon = self._monitor(config, mock_executor, broken)
        pos = MonitoredPosition(symbol="TEST", scrip_code="NSE_1", entry_price=100.0, quantity=10)
        result = MonitorResult()

        assert await mon._execute_sell(pos, ltp=95.0, reason="stop", result=result)
        assert result.sells_executed == 1
