"""`skopaq schedule`: a bad setting stops only the scheduler (with an alert), a second
scheduler exits non-zero, and --check does not take the single-instance lock."""

from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from skopaq.cli.main import app
from skopaq.risk.calendar import IST

runner = CliRunner()

MONDAY_10AM = datetime(2026, 9, 28, 10, 0, tzinfo=IST)


@pytest.fixture
def state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("SKOPAQ_SCHEDULER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("SKOPAQ_DAEMON_SESSION_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SKOPAQ_NSE_HOLIDAYS", "")
    monkeypatch.setenv("SKOPAQ_SCHEDULER_MODE", "paper")
    monkeypatch.setenv("SKOPAQ_SCHEDULER_ENABLED", "true")
    return tmp_path / "state"


@contextmanager
def _another_scheduler(state_dir):
    """Hold the lock as a running scheduler would."""
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "scheduler.lock", "a+") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _invoke(*args: str):
    notify = AsyncMock()
    with patch("skopaq.risk.calendar.now_ist", return_value=MONDAY_10AM), \
         patch("skopaq.notifications.notify", notify):
        result = runner.invoke(app, ["schedule", *args])
    return result, [call.args[0] for call in notify.await_args_list]


def test_check_works_while_a_scheduler_is_running(state_dir):
    with _another_scheduler(state_dir):
        result, alerts = _invoke("--check")
    assert result.exit_code == 0, result.output
    assert "Next session" in result.output
    assert alerts == []


def test_a_second_scheduler_exits_1_and_alerts(state_dir):
    done = {}

    def second():  # in a thread: a scheduler that does start would otherwise run forever
        done["result"], done["alerts"] = _invoke()

    with _another_scheduler(state_dir):
        thread = threading.Thread(target=second, daemon=True)
        thread.start()
        thread.join(30)
    assert not thread.is_alive(), "the second scheduler kept running"
    result, alerts = done["result"], done["alerts"]
    assert result.exit_code == 1
    assert len(alerts) == 1 and "another scheduler is already running" in alerts[0]


def test_a_mistyped_confirm_live_leaves_check_usable(state_dir, monkeypatch):
    monkeypatch.setenv("SKOPAQ_SCHEDULER_MODE", "live")
    monkeypatch.setenv("SKOPAQ_SCHEDULER_CONFIRM_LIVE", "ture")
    result, _ = _invoke("--check")
    assert result.exit_code == 0, result.output
    assert "live NOT confirmed" in result.output and "'ture'" in result.output


def test_a_bad_scheduler_setting_is_alerted_once_a_day(state_dir, monkeypatch):
    monkeypatch.setenv("SKOPAQ_SCHEDULER_POLL_SECONDS", "30s")
    first, alerts = _invoke()
    again, alerts_again = _invoke()  # restart: unless-stopped runs it again
    check, alerts_check = _invoke("--check")

    assert first.exit_code == again.exit_code == check.exit_code == 1
    assert "SKOPAQ_SCHEDULER_POLL_SECONDS" in first.output
    assert len(alerts) == 1 and "SKOPAQ_SCHEDULER_POLL_SECONDS" in alerts[0]
    assert alerts_again == [] and alerts_check == []
