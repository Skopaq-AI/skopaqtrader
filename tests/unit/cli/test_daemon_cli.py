"""`skopaq daemon`: the NSE calendar gate and the exit code of a failed session."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from skopaq.cli.main import app
from skopaq.execution.daemon import PRE_OPEN_FAILED_EXIT_CODE, DaemonSessionReport
from skopaq.risk.calendar import IST

runner = CliRunner()

HOLIDAY_10AM = datetime(2026, 10, 2, 10, 0, tzinfo=IST)
MONDAY_10AM = datetime(2026, 9, 28, 10, 0, tzinfo=IST)
MONDAY_1540 = datetime(2026, 9, 28, 15, 40, tzinfo=IST)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.setenv("SKOPAQ_NSE_HOLIDAYS", "")
    monkeypatch.setenv("SKOPAQ_TRADING_MODE", "paper")


def _invoke(now: datetime, *args: str, report: DaemonSessionReport | None = None):
    run = AsyncMock(return_value=report or DaemonSessionReport(session_date="2026-09-28"))
    with patch("skopaq.risk.calendar.now_ist", return_value=now), \
         patch("skopaq.cli.main._run_daemon", run), \
         patch("skopaq.cli.main.display_daemon_start"), \
         patch("skopaq.cli.main.display_daemon_report"):
        result = runner.invoke(app, ["daemon", *args])
    return result, run


def test_no_session_on_a_holiday():
    result, run = _invoke(HOLIDAY_10AM, "--paper")
    assert result.exit_code == 0
    assert "No daemon session" in result.output
    run.assert_not_awaited()


def test_no_session_after_the_close():
    result, run = _invoke(MONDAY_1540, "--paper")
    assert result.exit_code == 0
    assert "NSE closed" in result.output
    run.assert_not_awaited()


def test_session_runs_on_a_trading_day():
    result, run = _invoke(MONDAY_10AM, "--paper", "--once")
    assert result.exit_code == 0
    run.assert_awaited_once()


def test_failed_session_exits_1():
    report = DaemonSessionReport(session_date="2026-09-28", errors=["broker down"])
    result, _ = _invoke(MONDAY_10AM, "--paper", report=report)
    assert result.exit_code == 1


def test_pre_open_failure_exits_3():
    report = DaemonSessionReport(
        session_date="2026-09-28", errors=["INDstocks token invalid"], pre_open_failed=True,
    )
    result, _ = _invoke(MONDAY_10AM, "--paper", report=report)
    assert result.exit_code == PRE_OPEN_FAILED_EXIT_CODE == 3


@pytest.mark.parametrize("flag", ["--dry-run", "--ignore-calendar"])
def test_dry_run_and_ignore_calendar_bypass_the_gate(flag):
    result, run = _invoke(HOLIDAY_10AM, "--paper", flag)
    assert result.exit_code == 0
    run.assert_awaited_once()


def test_live_prompt_is_not_reached_on_a_holiday():
    result, run = _invoke(HOLIDAY_10AM, "--live")
    assert result.exit_code == 0
    assert "Proceed with LIVE daemon" not in result.output
    run.assert_not_awaited()


def test_malformed_extra_holidays_fail_closed(monkeypatch):
    monkeypatch.setenv("SKOPAQ_NSE_HOLIDAYS", "tomorrow")
    result, run = _invoke(MONDAY_10AM, "--paper")
    assert result.exit_code == 1
    run.assert_not_awaited()
