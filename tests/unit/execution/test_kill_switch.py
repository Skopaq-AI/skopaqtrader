"""Kill switch: one halt obeyed by every order path (skopaq/execution/kill_switch.py)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from skopaq.broker.models import Funds
from skopaq.execution import kill_switch


@pytest.fixture
def halt_file(tmp_path, monkeypatch):
    path = tmp_path / "HALT"
    monkeypatch.setenv("SKOPAQ_HALT_FILE", str(path))
    return path


class FakeFlags:
    def __init__(self, value=None, fail=False):
        self.value, self.fail, self.writes = value, fail, []

    def get(self, key):
        if self.fail:
            raise RuntimeError("supabase down")
        return self.value

    def set(self, key, value):
        self.writes.append((key, value))
        self.value = value


# ── Sources ──────────────────────────────────────────────────────────────────


def test_active_by_default(halt_file):
    assert kill_switch.status() == kill_switch.HaltStatus(False)


def test_halt_and_resume_with_the_local_file(halt_file):
    where = kill_switch.halt("broker outage", by="test")

    assert where == [str(halt_file)]
    status = kill_switch.status()
    assert (status.halted, status.reason, status.source) == (True, "broker outage", "file")
    assert "broker outage" in status.describe()

    assert kill_switch.resume(by="test") == [str(halt_file)]
    assert not kill_switch.status().halted


def test_unreadable_halt_file_still_halts(halt_file):
    halt_file.write_text("{not json")
    assert kill_switch.status().halted


def test_env_var_halts_and_cannot_be_resumed_from_code(halt_file, monkeypatch):
    monkeypatch.setenv("SKOPAQ_TRADING_HALTED", "true")
    assert kill_switch.status().source == "env"

    kill_switch.resume()
    assert kill_switch.status(use_cache=False).halted


def test_supabase_halt_reaches_every_process(halt_file, monkeypatch):
    flags = FakeFlags()
    monkeypatch.setattr(kill_switch, "_flags", lambda config: flags)

    assert kill_switch.halt("drawdown review") == [str(halt_file), "supabase:system_flags"]
    key, value = flags.writes[0]
    assert (key, value["halted"], value["reason"]) == ("trading_halt", True, "drawdown review")

    halt_file.unlink()  # another machine: no local file, only the shared row
    assert kill_switch.status(use_cache=False).source == "supabase"

    kill_switch.resume()
    assert flags.value["halted"] is False
    assert not kill_switch.status(use_cache=False).halted


def test_supabase_outage_does_not_halt_on_its_own(halt_file, monkeypatch):
    monkeypatch.setattr(kill_switch, "_flags", lambda config: FakeFlags(fail=True))
    assert not kill_switch.status().halted


def test_status_is_cached_briefly_and_halt_refreshes_it(halt_file):
    assert not kill_switch.status().halted
    halt_file.write_text(json.dumps({"reason": "written by another process"}))
    assert not kill_switch.status().halted  # still cached
    assert kill_switch.status(use_cache=False).halted


# ── Where it is enforced ─────────────────────────────────────────────────────


def _checker():
    from skopaq.constants import SafetyRules
    from skopaq.execution.safety_checker import SafetyChecker

    return SafetyChecker(rules=SafetyRules(market_hours_only=False, require_stop_loss=False))


def _validate(order, positions=()):
    return _checker().validate(order, None, list(positions),
                               Funds(available_margin=1_000_000), 1_000_000)


def test_safety_checker_rejects_buys_while_halted(halt_file):
    from tests.unit.execution.test_safety_checker import _buy_order

    kill_switch.halt("stop")
    result = _validate(_buy_order(qty=1, price=100))

    assert not result.passed
    assert any("HALTED" in r and "skopaq resume" in r for r in result.rejections)


def test_sells_of_held_shares_stay_allowed_while_halted(halt_file):
    from tests.unit.execution.test_safety_checker import _held, _sell_order

    kill_switch.halt("stop")
    assert _validate(_sell_order(qty=5), positions=_held(qty=5)).passed


@pytest.mark.asyncio
async def test_halted_daemon_skips_scan_and_trades(halt_file):
    from skopaq.execution.daemon import TradingDaemon

    daemon = TradingDaemon(MagicMock(trading_mode="paper"))
    kill_switch.halt("stop")
    with patch.object(daemon, "_phase_pre_open", new_callable=AsyncMock), \
         patch.object(daemon, "_phase_scan", new_callable=AsyncMock) as scan, \
         patch.object(daemon, "_settle_due_decisions", new_callable=AsyncMock, return_value=0):
        report = await daemon.run_session()

    scan.assert_not_awaited()
    assert "HALTED" in report.halted


@pytest.mark.asyncio
async def test_halt_during_a_session_stops_further_analysis(halt_file):
    from skopaq.execution.daemon import DaemonSessionReport, TradingDaemon

    daemon = TradingDaemon(MagicMock(trading_mode="paper"))
    daemon._graph = MagicMock()
    kill_switch.halt("stop")
    report = DaemonSessionReport()
    with patch("skopaq.cli.main._compute_risk_scales", return_value=(1.0, 1.0)):
        await daemon._phase_analyze_and_trade([MagicMock(symbol="TCS")], report)

    assert report.candidates_analyzed == 0
    daemon._graph.analyze_and_execute.assert_not_called()


# ── Controls ─────────────────────────────────────────────────────────────────


def test_cli_halt_status_and_resume(halt_file):
    from skopaq.cli.main import app

    runner = CliRunner()
    halted = runner.invoke(app, ["halt", "earnings week"])
    assert halted.exit_code == 0, halted.output
    assert "earnings week" in json.loads(halt_file.read_text())["reason"]
    assert "only this machine" in halted.output  # no Supabase in tests

    declined = runner.invoke(app, ["resume"], input="n\n")
    assert declined.exit_code == 0 and halt_file.exists()

    resumed = runner.invoke(app, ["resume", "--yes"])
    assert resumed.exit_code == 0, resumed.output
    assert not halt_file.exists()


def test_cli_resume_reports_an_env_halt(halt_file, monkeypatch):
    from skopaq.cli.main import app

    monkeypatch.setenv("SKOPAQ_TRADING_HALTED", "true")
    result = CliRunner().invoke(app, ["resume", "--yes"])
    assert result.exit_code == 1
    assert "Unset SKOPAQ_TRADING_HALTED" in result.output


def test_mcp_halt_and_resume(halt_file):
    import asyncio

    from skopaq import mcp_server

    halted = json.loads(asyncio.run(mcp_server.halt_trading("from claude")))
    assert halted["halted"] is True and halted["every_process"] is False
    assert kill_switch.status(use_cache=False).halted

    resumed = json.loads(asyncio.run(mcp_server.resume_trading()))
    assert resumed["halted"] is False


# ── Halt when the local file cannot be written ───────────────────────────────


def _unwritable(monkeypatch):
    """Path.write_text raises, as on a read-only volume (works as root too)."""
    def refuse(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied", str(self))

    monkeypatch.setattr(kill_switch.Path, "write_text", refuse)


def test_halt_still_reaches_supabase_when_the_file_is_unwritable(halt_file, monkeypatch):
    flags = FakeFlags()
    monkeypatch.setattr(kill_switch, "_flags", lambda config: flags)
    _unwritable(monkeypatch)

    assert kill_switch.halt("volume read-only", by="test") == ["supabase:system_flags"]
    key, value = flags.writes[0]
    assert (key, value["halted"], value["reason"]) == ("trading_halt", True, "volume read-only")


def test_halt_recorded_nowhere_raises(halt_file, monkeypatch):
    monkeypatch.setattr(kill_switch, "_flags", lambda config: None)
    _unwritable(monkeypatch)

    with pytest.raises(RuntimeError, match="Halt not recorded"):
        kill_switch.halt("nowhere", by="test")


def test_cli_halt_recorded_nowhere_exits_1(halt_file, monkeypatch):
    from skopaq.cli.main import app

    monkeypatch.setattr(kill_switch, "_flags", lambda config: None)
    _unwritable(monkeypatch)

    result = CliRunner().invoke(app, ["halt", "nowhere"])
    assert result.exit_code == 1
    assert "Halt not recorded" in result.output
