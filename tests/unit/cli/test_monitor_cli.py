"""`skopaq monitor`: live, it exits 4 when positions are left open or orders unconfirmed, so
the scheduler alerts "check the broker"; paper always exits 0. The summary says what is left."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from skopaq.cli import display
from skopaq.cli.main import app
from skopaq.cli.theme import console
from skopaq.execution.daemon import POSITIONS_LEFT_EXIT_CODE
from skopaq.execution.position_monitor import MonitorResult

runner = CliRunner()


def _invoke(monkeypatch, mode: str, result: MonitorResult):
    monkeypatch.setenv("SKOPAQ_TRADING_MODE", mode)
    with patch("skopaq.cli.main._run_monitor", new=AsyncMock(return_value=result)), \
         patch("skopaq.cli.main.display_monitor_start"), \
         patch("skopaq.cli.main.display_monitor_result"):
        return runner.invoke(app, ["monitor"])


@pytest.mark.parametrize(("result", "code"), [
    (MonitorResult(positions_left=["TCS"]), POSITIONS_LEFT_EXIT_CODE),
    (MonitorResult(orders_unconfirmed=["EQ-1"]), POSITIONS_LEFT_EXIT_CODE),
    (MonitorResult(sells_executed=1), 0),
])
def test_live_monitor_exit_code(monkeypatch, result, code):
    assert _invoke(monkeypatch, "live", result).exit_code == code
    assert POSITIONS_LEFT_EXIT_CODE == 4


def test_paper_monitor_always_exits_0(monkeypatch):
    left = MonitorResult(positions_left=["TCS"], orders_unconfirmed=["EQ-1"])
    assert _invoke(monkeypatch, "paper", left).exit_code == 0


def test_the_summary_shows_what_is_left():
    with console.capture() as captured:
        display.display_monitor_result(MonitorResult(
            positions_left=["TCS", "INFY"], orders_unconfirmed=["EQ-7"],
            exits_blocked=["TCS"], late_fills=1))
    out = captured.get()
    assert "TCS, INFY" in out and "EQ-7" in out and "Late Fills" in out
    with console.capture() as captured:
        display.display_monitor_result(MonitorResult(sells_executed=1))
    assert "Still Open" not in captured.get()


def test_the_daemon_report_shows_what_is_left():
    from skopaq.execution.daemon import DaemonSessionReport

    with console.capture() as captured:
        display.display_daemon_report(DaemonSessionReport(
            session_date="2026-09-25", orders_unconfirmed=1, positions_left=["TCS"],
            exits_blocked=["INFY"]))
    out = captured.get()
    assert "Unconfirmed Orders" in out and "TCS" in out and "INFY" in out
    with console.capture() as captured:
        display.display_daemon_report(DaemonSessionReport(session_date="2026-09-25"))
    assert "Positions Left" not in captured.get()
