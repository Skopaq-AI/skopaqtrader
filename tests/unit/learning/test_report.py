"""Track record (skopaq/learning/report.py) from stored calls and trades."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from skopaq.db.models import TradeRecord
from skopaq.learning import report as report_mod
from skopaq.learning.report import calibration, parse_tag, summarize_calls, summarize_trades


def _settled(date, rating, raw, alpha, ticker="TCS.NS"):
    tag = f"[{date} | {ticker} | {rating} | {raw} | {alpha} | 5d | resolved:{date}]"
    return tag + "\n\nDECISION:\nx"


def _pending(date, rating, ticker="TCS.NS"):
    return f"[{date} | {ticker} | {rating} | pending]\n\nDECISION:\nx"


def _trade(pnl, closed, confidence=None, price="100", qty="10"):
    return TradeRecord(
        symbol="TCS", side="BUY", quantity=Decimal(qty), fill_price=Decimal(price),
        pnl=None if pnl is None else Decimal(str(pnl)),
        closed_at=datetime.fromisoformat(closed),
        agent_decision={} if confidence is None else {"confidence": confidence},
    )


def test_parse_tag_reads_upstream_entries():
    assert parse_tag(_settled("2026-09-01", "Buy", "+2.0%", "-0.5%")) == {
        "date": "2026-09-01", "ticker": "TCS.NS", "rating": "Buy", "pending": False,
        "raw": pytest.approx(0.02), "alpha": pytest.approx(-0.005)}
    assert parse_tag(_pending("2026-09-02", "Hold"))["pending"] is True
    assert parse_tag("no tag here") is None


def test_summarize_calls_scores_direction_and_alpha():
    stats = summarize_calls([
        _settled("2026-09-01", "Buy", "+3.0%", "+1.0%"),        # hit
        _settled("2026-09-02", "Overweight", "-1.0%", "-2.0%"),  # miss
        _settled("2026-09-03", "Sell", "-4.0%", "-3.0%"),        # hit (fell)
        _settled("2026-09-04", "Hold", "+1.0%", "+0.5%"),        # not directional
        _pending("2026-09-05", "Buy"),
        "garbage",
    ])

    assert (stats.total, stats.settled, stats.pending, stats.directional) == (5, 4, 1, 3)
    assert stats.hit_rate == pytest.approx(2 / 3)
    assert stats.long_alpha == pytest.approx(-0.005)  # mean of +1% and -2%
    assert stats.by_rating["Buy"].calls == 2 and stats.by_rating["Buy"].settled == 1
    assert stats.by_rating["Sell"].avg_return == pytest.approx(-0.04)
    assert not stats.enough_data  # 3 < 30


def test_summarize_trades():
    trades = [
        _trade(500, "2026-09-01T10:00:00+00:00"),
        _trade(-300, "2026-09-02T10:00:00+00:00"),
        _trade(-400, "2026-09-03T10:00:00+00:00"),
        _trade(900, "2026-09-04T10:00:00+00:00"),
        _trade(None, "2026-09-05T10:00:00+00:00"),  # no P&L recorded: ignored
    ]
    stats = summarize_trades(trades)

    assert (stats.closed, stats.wins, stats.total_pnl) == (4, 2, 700.0)
    assert stats.win_rate == 0.5
    assert (stats.avg_win, stats.avg_loss) == (700.0, -350.0)
    assert stats.profit_factor == pytest.approx(1400 / 700)
    assert stats.max_drawdown == 700.0  # +500 peak, then -300 and -400
    assert stats.avg_return == pytest.approx(700 / 4 / 1000)  # on ₹1,000 per trade


def test_calibration_buckets_by_entry_confidence():
    buckets = calibration([
        _trade(100, "2026-09-01T10:00:00+00:00", confidence=85),
        _trade(-50, "2026-09-01T11:00:00+00:00", confidence=82),
        _trade(20, "2026-09-01T12:00:00+00:00", confidence=55),
        _trade(20, "2026-09-01T13:00:00+00:00"),  # no confidence stored
    ])
    by_label = {b.label: b for b in buckets}
    assert (by_label["80–100"].trades, by_label["80–100"].win_rate) == (2, 0.5)
    assert by_label["50–60"].win_rate == 1.0
    assert by_label["0–50"].win_rate is None


def test_build_report_reads_supabase_and_the_local_log(tmp_path):
    log = tmp_path / "trading_memory.md"
    log.write_text(_settled("2026-09-10", "Buy", "+1.0%", "+0.2%") + "\n\n<!-- ENTRY_END -->\n\n")
    config = MagicMock(supabase_url="https://x", trading_mode="paper")
    config.supabase_service_key.get_secret_value.return_value = "key"
    memory = MagicMock()
    memory.get_by_role.return_value = MagicMock(documents=[
        _settled("2026-09-11", "Sell", "-2.0%", "-1.0%"),
        _settled("2025-01-01", "Buy", "+9.0%", "+9.0%"),  # outside the window
    ])
    trades = MagicMock()
    trades.get_closed_since.return_value = [_trade(250, "2026-09-12T10:00:00+00:00", 70)]

    with patch("supabase.create_client"), \
         patch("skopaq.db.repositories.MemoryRepository", return_value=memory), \
         patch("skopaq.db.repositories.TradeRepository", return_value=trades), \
         patch.dict("tradingagents.default_config.DEFAULT_CONFIG", {"memory_log_path": str(log)}):
        result = report_mod.build_report(
            config, days=30, now=datetime(2026, 9, 24, tzinfo=timezone.utc))

    assert result.calls.total == 2 and result.calls.hit_rate == 1.0
    assert result.trades.closed == 1 and result.trades.total_pnl == 250.0
    assert trades.get_closed_since.call_args.kwargs == {"is_paper": True}
    assert result.sources == ["supabase:decision_log", str(log), "supabase:trades"]


def test_cli_and_mcp_render_the_report(monkeypatch):
    from typer.testing import CliRunner

    from skopaq import mcp_server
    from skopaq.cli.main import app
    from skopaq.learning.report import CallStats, Report, TradeStats

    fake = Report(days=90, mode="paper", calls=CallStats(total=3, settled=2, directional=2,
                                                         hit_rate=0.5),
                  trades=TradeStats(closed=1, wins=1, total_pnl=1200.0),
                  calibration=calibration([]), sources=["supabase:trades"])
    monkeypatch.setattr(report_mod, "build_report", lambda config, days=90: fake)

    result = CliRunner().invoke(app, ["report"])
    assert result.exit_code == 0, result.output
    assert "Track record" in result.output and "₹1,200" in result.output
    assert "mostly noise" in result.output  # 2 settled calls is not a sample

    import asyncio

    monkeypatch.setattr(mcp_server, "_get_config", lambda: MagicMock())
    data = json.loads(asyncio.run(mcp_server.performance_report()))
    assert data["calls"]["hit_rate"] == 0.5 and data["calls"]["enough_data"] is False
    assert data["trades"]["win_rate"] == 1.0
