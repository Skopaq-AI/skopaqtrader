"""Scheduler (skopaq/execution/scheduler.py): settings, due jobs, at-most-once markers,
the main loop with an injected clock, and signal/deadline handling of a real child."""

from __future__ import annotations

import dataclasses
import signal
import sys
import threading
import time as _time
from datetime import date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from skopaq.execution import scheduler
from skopaq.execution.daemon import PRE_OPEN_FAILED_EXIT_CODE
from skopaq.execution.scheduler import (
    INTERRUPTED_RC,
    JobResult,
    SchedulerState,
    ScheduleSettings,
    _tick,
    check_ok,
    daemon_argv,
    describe,
    due_job,
    parse_hhmm,
    run_forever,
    run_job,
)
from skopaq.risk.calendar import IST

MONDAY = (2026, 9, 28)  # an NSE trading day
MONDAY_DATE = date(*MONDAY)
HOLIDAY = (2026, 10, 2)  # Mahatma Gandhi Jayanti
SATURDAY = (2026, 9, 26)


def _config(tmp_path: Path, **overrides) -> SimpleNamespace:
    values = dict(
        scheduler_enabled=True,
        scheduler_mode="paper",
        scheduler_confirm_live=False,
        scheduler_start="09:15",
        scheduler_last_start="11:30",
        scheduler_deadline="15:45",
        scheduler_settle_at="18:30",
        scheduler_preflight="08:45",
        scheduler_poll_seconds=1,
        scheduler_kill_after_seconds=300,
        scheduler_state_dir=str(tmp_path / "state"),
        daemon_session_log_dir=str(tmp_path / "logs"),
        scheduler_ping_url="",
        heartbeat_file="",
        nse_holidays="",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _settings(tmp_path: Path, **overrides) -> ScheduleSettings:
    return ScheduleSettings.from_config(_config(tmp_path, **overrides))


def _at(day: tuple[int, int, int], hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(*day, h, m, tzinfo=IST)


# ── Settings ─────────────────────────────────────────────────────────────────


def test_parse_hhmm():
    assert parse_hhmm("09:15") == time(9, 15)
    for bad in ("9:15", "24:00", "09:60", "", "09:15:00"):
        with pytest.raises(ValueError, match="HH:MM"):
            parse_hhmm(bad)


def test_from_config_rejects_bad_order_and_mode(tmp_path):
    with pytest.raises(ValueError) as exc:
        _settings(tmp_path, scheduler_start="11:30", scheduler_mode="yolo")
    message = str(exc.value)
    assert "SKOPAQ_SCHEDULER_START < SKOPAQ_SCHEDULER_LAST_START" in message
    assert "SKOPAQ_SCHEDULER_MODE" in message  # every problem in one error


def test_from_config_rejects_settle_before_deadline_and_bad_holidays(tmp_path):
    with pytest.raises(ValueError, match="SETTLE_AT"):
        _settings(tmp_path, scheduler_settle_at="15:00")
    with pytest.raises(ValueError, match="not-a-date"):
        _settings(tmp_path, nse_holidays="not-a-date")


def test_from_config_preflight(tmp_path):
    assert _settings(tmp_path).preflight == time(8, 45)
    assert _settings(tmp_path, scheduler_preflight="").preflight is None
    with pytest.raises(ValueError, match="PREFLIGHT"):
        _settings(tmp_path, scheduler_preflight="09:15")


def test_from_config_defaults(tmp_path):
    settings = _settings(tmp_path, scheduler_settle_at="", scheduler_state_dir="~/sched")
    assert settings.settle_at is None
    assert settings.state_dir == Path.home() / "sched"
    assert settings.log_dir == tmp_path / "logs"
    assert settings.heartbeat_file is None


def test_daemon_argv(tmp_path):
    assert daemon_argv(_settings(tmp_path)) == ["daemon", "--once", "--paper"]
    live = _settings(tmp_path, scheduler_mode="live", scheduler_confirm_live=True)
    assert daemon_argv(live) == ["daemon", "--once", "--live", "--confirm-live"]
    assert daemon_argv(_settings(tmp_path, scheduler_mode="live")) is None


# ── Due jobs and markers ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("day", "hhmm", "expected"),
    [
        (HOLIDAY, "09:15", None),
        (SATURDAY, "09:15", None),
        (MONDAY, "09:14", None),
        (MONDAY, "09:15", "daemon"),
        (MONDAY, "11:29", "daemon"),
        (MONDAY, "11:30", None),
        (MONDAY, "18:30", "settle"),
        (HOLIDAY, "18:30", None),
    ],
)
def test_due_job(tmp_path, day, hhmm, expected):
    settings = _settings(tmp_path)
    assert due_job(_at(day, hhmm), settings, SchedulerState(settings.state_dir)) == expected


def test_due_job_runs_each_job_once_a_day(tmp_path):
    settings = _settings(tmp_path)
    state = SchedulerState(settings.state_dir)
    day = _at(MONDAY, "09:15").date()
    state.mark_started("daemon", day)
    assert due_job(_at(MONDAY, "09:20"), settings, state) is None
    state.mark_started("settle", day)
    assert due_job(_at(MONDAY, "19:00"), settings, state) is None


def test_disabled_scheduler_has_no_jobs(tmp_path):
    settings = _settings(tmp_path, scheduler_enabled=False)
    assert due_job(_at(MONDAY, "09:15"), settings, SchedulerState(settings.state_dir)) is None


def test_markers_survive_a_restart(tmp_path):
    day = _at(MONDAY, "09:15").date()
    first = SchedulerState(tmp_path)
    first.mark_started("daemon", day)
    first.record_exit("daemon", day, 3)
    again = SchedulerState(tmp_path)  # a new process
    assert again.started("daemon", day)
    assert again.last_exit("daemon", day) == 3
    assert again.flag_once("missed", day)
    assert not SchedulerState(tmp_path).flag_once("missed", day)


def test_prune_removes_old_markers_and_logs(tmp_path):
    state = SchedulerState(tmp_path / "state", tmp_path / "logs")
    today = _at(MONDAY, "09:15").date()
    old, recent = today - timedelta(days=40), today - timedelta(days=5)
    state.mark_started("daemon", old)
    state.mark_started("daemon", recent)
    (tmp_path / "logs").mkdir()
    old_log = tmp_path / "logs" / f"daemon-{(today - timedelta(days=61)).isoformat()}.log"
    new_log = tmp_path / "logs" / f"daemon-{old.isoformat()}.log"
    old_log.write_text("x")
    new_log.write_text("x")
    state.prune(today)
    assert not state.started("daemon", old)
    assert state.started("daemon", recent)
    assert not old_log.exists()
    assert new_log.exists()  # logs are kept 60 days


def test_describe_and_check(tmp_path):
    settings = _settings(tmp_path)
    state = SchedulerState(settings.state_dir)
    lines = "\n".join(describe(settings, _at(SATURDAY, "10:00"), state))
    assert "not a trading day: Saturday" in lines
    assert "[2026]" in lines
    assert "Mon 2026-09-28 at 09:15 IST" in lines
    assert "Mode:           paper" in lines
    assert check_ok(settings, _at(SATURDAY, "10:00"))
    assert not check_ok(settings, datetime(2027, 1, 4, 10, 0, tzinfo=IST))

    catch_up = "\n".join(describe(settings, _at(MONDAY, "10:00"), state))
    assert "now (catch-up window open until 11:30 IST)" in catch_up

    live = _settings(tmp_path, scheduler_mode="live")
    assert "live NOT confirmed" in "\n".join(describe(live, _at(MONDAY, "08:00"), state))


# ── Main loop ────────────────────────────────────────────────────────────────


class Recorder:
    def __init__(self):
        self.alerts, self.pings = [], []

    def alert(self, msg):
        self.alerts.append(msg)

    def ping(self, url, ok):
        self.pings.append(ok)


def _stop_after(stop: threading.Event, sleeps: int):
    calls = []

    def sleep(_seconds):
        calls.append(1)
        if len(calls) >= sleeps:
            stop.set()

    return sleep


def test_run_forever_marks_before_launch_and_reports_failure(tmp_path):
    settings = _settings(tmp_path)
    rec, stop, launched = Recorder(), threading.Event(), []
    marker = settings.state_dir / "daemon-2026-09-28.started"

    def runner(cmd, **kwargs):
        assert marker.exists(), "the marker must be written before launch"
        launched.append((cmd, kwargs))
        return JobResult(1, False, False)

    rc = run_forever(
        settings, clock=lambda: _at(MONDAY, "09:16"), runner=runner,
        alert=rec.alert, ping=rec.ping, sleep=_stop_after(stop, 1), stop=stop,
    )

    assert rc == 0
    assert len(launched) == 1
    cmd, kwargs = launched[0]
    assert cmd[0] == sys.executable
    assert cmd[1:] == ["-m", "skopaq.cli.main", "daemon", "--once", "--paper"]
    assert kwargs["deadline"] == _at(MONDAY, "15:45")
    assert kwargs["log_path"] == settings.log_dir / "daemon-2026-09-28.log"
    assert SchedulerState(settings.state_dir).last_exit("daemon", MONDAY_DATE) == 1
    assert any("rc=1" in a for a in rec.alerts)
    assert rec.pings == [False]


def test_run_forever_deadline_hit_is_reported(tmp_path):
    settings = _settings(tmp_path)
    rec, stop = Recorder(), threading.Event()
    run_forever(
        settings, clock=lambda: _at(MONDAY, "09:16"),
        runner=lambda cmd, **kw: JobResult(-15, True, False),
        alert=rec.alert, ping=rec.ping, sleep=_stop_after(stop, 1), stop=stop,
    )
    assert any("stopped at the 15:45 deadline" in a for a in rec.alerts)


def test_run_forever_alerts_a_clean_exit_at_the_deadline(tmp_path):
    settings = _settings(tmp_path)
    rec, stop = Recorder(), threading.Event()
    run_forever(
        settings, clock=lambda: _at(MONDAY, "09:16"),
        runner=lambda cmd, **kw: JobResult(0, True, False),
        alert=rec.alert, ping=rec.ping, sleep=_stop_after(stop, 1), stop=stop,
    )
    assert len(rec.alerts) == 1
    assert "still running at the 15:45 deadline" in rec.alerts[0]
    assert rec.pings == [True]


def test_run_forever_alerts_a_missed_day_once(tmp_path):
    settings = _settings(tmp_path)
    rec, stop, launched = Recorder(), threading.Event(), []
    run_forever(
        settings, clock=lambda: _at(MONDAY, "12:00"),
        runner=lambda cmd, **kw: launched.append(cmd) or JobResult(0),
        alert=rec.alert, ping=rec.ping, sleep=_stop_after(stop, 2), stop=stop,
    )
    missed = [a for a in rec.alerts if "no daemon session today" in a]
    assert len(missed) == 1
    assert launched == []  # past the catch-up window: no late session


def test_run_forever_skips_unconfirmed_live(tmp_path):
    settings = _settings(tmp_path, scheduler_mode="live")
    rec, stop, launched = Recorder(), threading.Event(), []
    run_forever(
        settings, clock=lambda: _at(MONDAY, "09:16"),
        runner=lambda cmd, **kw: launched.append(cmd) or JobResult(0),
        alert=rec.alert, ping=rec.ping, sleep=_stop_after(stop, 2), stop=stop,
    )
    state = SchedulerState(settings.state_dir)
    assert state.started_note("daemon", MONDAY_DATE) == "skipped: live not confirmed"
    assert len(rec.alerts) == 1
    assert "CONFIRM_LIVE" in rec.alerts[0]
    assert launched == []


def test_run_forever_alerts_a_missing_holiday_list(tmp_path):
    settings = _settings(tmp_path)
    rec, stop, launched = Recorder(), threading.Event(), []
    run_forever(
        settings, clock=lambda: datetime(2027, 1, 4, 9, 20, tzinfo=IST),
        runner=lambda cmd, **kw: launched.append(cmd) or JobResult(0),
        alert=rec.alert, ping=rec.ping, sleep=_stop_after(stop, 2), stop=stop,
    )
    assert len(rec.alerts) == 1
    assert "no NSE holiday list for 2027" in rec.alerts[0]
    assert launched == []


def test_run_forever_runs_settle(tmp_path):
    settings = _settings(tmp_path)
    rec, stop, launched = Recorder(), threading.Event(), []
    SchedulerState(settings.state_dir).mark_started("daemon", MONDAY_DATE)
    SchedulerState(settings.state_dir).record_exit("daemon", MONDAY_DATE, 0)
    run_forever(
        settings, clock=lambda: _at(MONDAY, "18:31"),
        runner=lambda cmd, **kw: launched.append(cmd) or JobResult(0),
        alert=rec.alert, ping=rec.ping, sleep=_stop_after(stop, 2), stop=stop,
    )
    assert [cmd[3:] for cmd in launched] == [["settle"]]
    assert rec.alerts == []


def test_disabled_scheduler_keeps_the_heartbeat(tmp_path):
    heartbeat = tmp_path / "hb"
    settings = _settings(tmp_path, scheduler_enabled=False, heartbeat_file=str(heartbeat))
    stop, launched = threading.Event(), []
    run_forever(
        settings, clock=lambda: _at(MONDAY, "09:16"),
        runner=lambda cmd, **kw: launched.append(cmd) or JobResult(0),
        alert=lambda msg: None, ping=lambda url, ok: None, sleep=_stop_after(stop, 1), stop=stop,
    )
    assert heartbeat.exists()
    assert launched == []


# ── Interrupted sessions, PRE_OPEN retries, pre-flight ──────────────────────


class Runs:
    """A runner that records launches and returns the queued exit codes (default 0)."""

    def __init__(self, *rcs: int):
        self.rcs, self.calls = list(rcs), []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd[3:], kwargs))
        return JobResult(self.rcs.pop(0) if self.rcs else 0)


def _tick_at(settings, state, day, hhmm, rec, runner):
    now = _at(day, hhmm)
    _tick(now, settings, state, runner=runner, alert=rec.alert, ping=rec.ping,
          stop=threading.Event(), clock=lambda: now)


def _interrupted(settings) -> SchedulerState:
    """Today's session was launched at 09:15 by a process that then died (no exit code)."""
    state = SchedulerState(settings.state_dir, settings.log_dir)
    state.mark_started("daemon", MONDAY_DATE, note="2026-09-28T09:15:02+05:30")
    return state


def test_interrupted_paper_session_is_alerted_once_and_not_restarted(tmp_path):
    settings = _settings(tmp_path)
    state, rec, runs = _interrupted(settings), Recorder(), Runs()
    for hhmm in ("09:40", "10:30", "12:00", "15:00"):
        _tick_at(settings, state, MONDAY, hhmm, rec, runs)
    assert len(rec.alerts) == 1
    assert "interrupted" in rec.alerts[0] and "paper positions" in rec.alerts[0]
    assert runs.calls == []  # no second session that day
    assert state.last_exit("daemon", MONDAY_DATE) == INTERRUPTED_RC
    assert "rc=-1 (interrupted)" in "\n".join(describe(settings, _at(MONDAY, "15:00"), state))


def test_interrupted_live_session_runs_the_monitor_until_the_deadline(tmp_path):
    settings = _settings(tmp_path, scheduler_mode="live", scheduler_confirm_live=True)
    state, rec, runs = _interrupted(settings), Recorder(), Runs(0)
    _tick_at(settings, state, MONDAY, "10:30", rec, runs)
    _tick_at(settings, state, MONDAY, "10:31", rec, runs)  # handled once
    assert len(runs.calls) == 1
    args, kwargs = runs.calls[0]
    assert args == ["monitor"]
    assert kwargs["env"] == {"SKOPAQ_TRADING_MODE": "live"}
    assert kwargs["deadline"] == _at(MONDAY, "15:45")
    assert kwargs["log_path"] == settings.log_dir / "daemon-2026-09-28.log"
    assert len(rec.alerts) == 1 and "running `skopaq monitor`" in rec.alerts[0]
    assert state.last_exit("daemon", MONDAY_DATE) == INTERRUPTED_RC
    assert state.last_exit("monitor", MONDAY_DATE) == 0


def test_interrupted_live_session_after_the_deadline_is_only_alerted(tmp_path):
    settings = _settings(tmp_path, scheduler_mode="live", scheduler_confirm_live=True)
    state, rec, runs = _interrupted(settings), Recorder(), Runs()
    _tick_at(settings, state, MONDAY, "16:00", rec, runs)
    assert runs.calls == []
    assert len(rec.alerts) == 1 and "carried overnight" in rec.alerts[0]


def test_a_session_interrupted_yesterday_is_alerted_after_a_restart(tmp_path):
    settings = _settings(tmp_path, scheduler_mode="live", scheduler_confirm_live=True)
    state, rec, runs = _interrupted(settings), Recorder(), Runs()
    state.flag_once("preflight", MONDAY_DATE + timedelta(days=1))  # no token check here
    _tick_at(settings, state, (2026, 9, 29), "08:50", rec, runs)  # Tuesday, before 09:15
    assert runs.calls == []
    assert len(rec.alerts) == 1 and "2026-09-28" in rec.alerts[0]
    assert state.last_exit("daemon", MONDAY_DATE) == INTERRUPTED_RC


def test_skipped_days_and_finished_sessions_are_not_interrupted(tmp_path):
    settings = _settings(tmp_path)
    state, rec = SchedulerState(settings.state_dir), Recorder()
    state.mark_started("daemon", MONDAY_DATE, note="skipped: live not confirmed")
    state.mark_started("daemon", MONDAY_DATE - timedelta(days=3))
    state.record_exit("daemon", MONDAY_DATE - timedelta(days=3), 0)
    _tick_at(settings, state, MONDAY, "10:00", rec, Runs())
    assert rec.alerts == []


def test_pre_open_failure_is_retried_until_the_session_starts(tmp_path):
    settings = _settings(tmp_path)
    state, rec = SchedulerState(settings.state_dir), Recorder()
    rc_at_launch, rcs = [], [PRE_OPEN_FAILED_EXIT_CODE, PRE_OPEN_FAILED_EXIT_CODE, 0]

    def runner(cmd, **kwargs):  # PRE_OPEN fails twice, then the session runs
        rc_at_launch.append(state.last_exit("daemon", MONDAY_DATE))
        return JobResult(rcs.pop(0))

    for hhmm in ("09:15", "09:17", "09:20", "09:24", "09:25", "09:40"):
        _tick_at(settings, state, MONDAY, hhmm, rec, runner)
    assert len(rc_at_launch) == 3  # 09:15, 09:20 and 09:25 (5 min apart); none at 09:17/09:24
    assert rc_at_launch == [None, None, None]  # a failed attempt's exit code is cleared first
    assert state.last_exit("daemon", MONDAY_DATE) == 0
    assert len(rec.alerts) == 1 and "Retrying every 5 min until 11:30" in rec.alerts[0]
    assert rec.pings == [False, False, True]


def test_pre_open_failing_all_morning_is_alerted_once(tmp_path):
    settings = _settings(tmp_path)
    state, rec = SchedulerState(settings.state_dir), Recorder()
    runs = Runs(*[PRE_OPEN_FAILED_EXIT_CODE] * 100)
    hhmm = datetime(*MONDAY, 9, 15)
    while hhmm.time() < time(12, 0):
        _tick_at(settings, state, MONDAY, f"{hhmm:%H:%M}", rec, runs)
        hhmm += timedelta(minutes=1)
    assert len(runs.calls) == 27  # every 5 min from 09:15 through 11:25
    assert len(rec.alerts) == 2
    assert "PRE_OPEN kept failing until 11:30" in rec.alerts[1]
    assert not any("no daemon session today: the scheduler was not running" in a
                   for a in rec.alerts)


def test_other_failures_are_not_retried(tmp_path):
    settings = _settings(tmp_path)
    state, rec, runs = SchedulerState(settings.state_dir), Recorder(), Runs(1)
    for hhmm in ("09:15", "09:30", "10:00"):
        _tick_at(settings, state, MONDAY, hhmm, rec, runs)
    assert len(runs.calls) == 1


# ── A session that exits non-zero on its own (scheduler still running) ──────


def _live(tmp_path):
    return _settings(tmp_path, scheduler_mode="live", scheduler_confirm_live=True)


def _session_ending_at(hhmm: str, *results: JobResult):
    """A runner whose daemon returns *results* in turn; the wall clock is then *hhmm*
    (the session ran until then). Later launches (the monitor) return rc 0."""
    clock = {"now": None}
    calls = []
    queue = list(results)

    def runner(cmd, **kwargs):
        calls.append((cmd[3:], kwargs))
        if cmd[3] == "daemon":
            clock["now"] = _at(MONDAY, hhmm)
            return queue.pop(0)
        return JobResult(0)

    return runner, calls, clock


def _tick_session(settings, state, rec, runner, clock, hhmm):
    start = _at(MONDAY, hhmm)
    clock["now"] = start
    _tick(start, settings, state, runner=runner, alert=rec.alert, ping=rec.ping,
          stop=threading.Event(), clock=lambda: clock["now"])


@pytest.mark.parametrize("rc", [1, -9])  # an exception that skipped CLOSING; OOM/SIGKILL
def test_live_session_exiting_mid_session_runs_the_monitor(tmp_path, rc):
    settings = _live(tmp_path)
    state, rec = SchedulerState(settings.state_dir, settings.log_dir), Recorder()
    runner, calls, clock = _session_ending_at("10:00", JobResult(rc))

    _tick_session(settings, state, rec, runner, clock, "09:15")
    _tick_session(settings, state, rec, runner, clock, "10:30")  # handled once

    assert [args[0] for args, _ in calls] == ["daemon", "monitor"]
    args, kwargs = calls[1]
    assert args == ["monitor"]
    assert kwargs["env"] == {"SKOPAQ_TRADING_MODE": "live"}
    assert kwargs["deadline"] == _at(MONDAY, "15:45")
    assert kwargs["log_path"] == settings.log_dir / "daemon-2026-09-28.log"
    assert len(rec.alerts) == 1
    assert f"rc={rc}" in rec.alerts[0] and "running `skopaq monitor`" in rec.alerts[0]
    assert rec.pings == [False]
    assert state.last_exit("daemon", MONDAY_DATE) == rc
    assert state.last_exit("monitor", MONDAY_DATE) == 0


def test_paper_session_exiting_mid_session_is_only_alerted(tmp_path):
    settings = _settings(tmp_path)
    state, rec = SchedulerState(settings.state_dir, settings.log_dir), Recorder()
    runner, calls, clock = _session_ending_at("10:00", JobResult(1))

    _tick_session(settings, state, rec, runner, clock, "09:15")

    assert [args[0] for args, _ in calls] == ["daemon"]
    assert rec.alerts == ["daemon exited rc=1 on 2026-09-28"]
    assert rec.pings == [False]


def test_live_session_exiting_after_the_deadline_is_only_alerted(tmp_path):
    settings = _live(tmp_path)
    state, rec = SchedulerState(settings.state_dir, settings.log_dir), Recorder()
    runner, calls, clock = _session_ending_at("15:46", JobResult(-9))

    _tick_session(settings, state, rec, runner, clock, "09:15")

    assert [args[0] for args, _ in calls] == ["daemon"]
    assert len(rec.alerts) == 1
    assert "rc=-9" in rec.alerts[0] and "check open positions at the broker" in rec.alerts[0]


@pytest.mark.parametrize("result", [JobResult(-15, deadline_hit=True),
                                    JobResult(-15, stopped=True)])
def test_live_session_stopped_by_the_scheduler_gets_no_monitor(tmp_path, result):
    settings = _live(tmp_path)
    state, rec = SchedulerState(settings.state_dir, settings.log_dir), Recorder()
    runner, calls, clock = _session_ending_at("10:00", result)

    _tick_session(settings, state, rec, runner, clock, "09:15")

    assert [args[0] for args, _ in calls] == ["daemon"]  # SIGTERM: the daemon ran CLOSING
    assert len(rec.alerts) == 1 and "rc=-15" in rec.alerts[0]


@pytest.mark.parametrize(
    ("day", "hhmm", "checked"),
    [(MONDAY, "08:44", False), (MONDAY, "08:45", True), (MONDAY, "09:16", False),
     (SATURDAY, "08:45", False), (HOLIDAY, "08:45", False)],
)
def test_preflight_runs_once_before_the_session(tmp_path, monkeypatch, day, hhmm, checked):
    settings = _settings(tmp_path)
    state, rec, calls = SchedulerState(settings.state_dir), Recorder(), []
    monkeypatch.setattr(scheduler, "_token_problem",
                        lambda d, s: calls.append(d) or "INDstocks token expires at 09:10")
    _tick_at(settings, state, day, hhmm, rec, Runs())
    _tick_at(settings, state, day, hhmm, rec, Runs())
    assert len(calls) == (1 if checked else 0)
    preflight = [a for a in rec.alerts if a.startswith("pre-flight")]
    assert len(preflight) == (1 if checked else 0)
    if checked:
        assert "expires at 09:10" in preflight[0]


def test_preflight_is_quiet_when_the_token_is_fine(tmp_path, monkeypatch):
    settings = _settings(tmp_path)
    state, rec = SchedulerState(settings.state_dir), Recorder()
    monkeypatch.setattr(scheduler, "_token_problem", lambda d, s: "")
    _tick_at(settings, state, MONDAY, "08:50", rec, Runs())
    assert rec.alerts == []


def test_token_problem_uses_the_session_end(tmp_path):
    from skopaq.broker.token_manager import TokenHealth

    settings = _settings(tmp_path)
    end = datetime(*MONDAY, 15, 45, tzinfo=IST)
    with patch("skopaq.broker.token_manager.TokenManager") as tm:
        tm.return_value.get_health.return_value = TokenHealth(
            valid=True, token="t", expires_at=end - timedelta(minutes=1))
        assert "before the session ends (15:45 IST)" in scheduler._token_problem(
            MONDAY_DATE, settings)
        tm.return_value.get_health.assert_called_with(notify=False)
        tm.return_value.get_health.return_value = TokenHealth(
            valid=True, token="t", expires_at=end + timedelta(minutes=1))
        assert scheduler._token_problem(MONDAY_DATE, settings) == ""


# ── run_job with a real child process ────────────────────────────────────────

GRACEFUL = """
import signal, sys, time
def bye(*_):
    print("graceful", flush=True)
    sys.exit(0)
signal.signal(signal.SIGTERM, bye)
print("started", flush=True)
time.sleep(60)
"""

STUBBORN = """
import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("started", flush=True)
time.sleep(60)
"""


def _when_started(log: Path, then) -> threading.Thread:
    """Call *then* once the child has printed "started" (its signal handler is set)."""
    def wait():
        deadline = _time.monotonic() + 20
        while _time.monotonic() < deadline:
            if log.exists() and "started" in log.read_text():
                then()
                return
            _time.sleep(0.05)
    thread = threading.Thread(target=wait, daemon=True)
    thread.start()
    return thread


def _far_future():
    return datetime.now(IST) + timedelta(hours=1)


def test_run_job_forwards_a_stop_as_sigterm(tmp_path):
    settings = _settings(tmp_path)
    log, stop = tmp_path / "logs" / "job.log", threading.Event()
    _when_started(log, stop.set)
    began = _time.monotonic()
    result = run_job([sys.executable, "-c", GRACEFUL], deadline=_far_future(),
                     settings=settings, log_path=log, stop=stop)
    assert result == JobResult(rc=0, deadline_hit=False, stopped=True)
    assert "graceful" in log.read_text()
    assert _time.monotonic() - began < 10


def test_run_job_stops_at_the_deadline(tmp_path):
    settings = _settings(tmp_path)
    log = tmp_path / "logs" / "job.log"
    started = threading.Event()
    _when_started(log, started.set)

    def clock():  # the deadline passes once the child is ready
        return datetime.now(IST) + (timedelta(hours=2) if started.is_set() else timedelta(0))

    result = run_job([sys.executable, "-c", GRACEFUL], deadline=_far_future(),
                     settings=settings, log_path=log, stop=threading.Event(), clock=clock)
    assert result.deadline_hit
    assert result.rc == 0
    assert "graceful" in log.read_text()


def test_run_job_kills_a_child_that_ignores_sigterm(tmp_path):
    settings = dataclasses.replace(_settings(tmp_path), kill_after_seconds=1)
    log, stop = tmp_path / "logs" / "job.log", threading.Event()
    _when_started(log, stop.set)
    began = _time.monotonic()
    result = run_job([sys.executable, "-c", STUBBORN], deadline=_far_future(),
                     settings=settings, log_path=log, stop=stop)
    assert result.rc == -signal.SIGKILL
    assert result.stopped
    assert _time.monotonic() - began < 15


def test_run_job_touches_the_heartbeat(tmp_path):
    heartbeat = tmp_path / "hb" / "scheduler.heartbeat"
    settings = _settings(tmp_path, heartbeat_file=str(heartbeat))
    result = run_job(
        [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(1.5)"],
        deadline=_far_future(), settings=settings, log_path=tmp_path / "logs" / "hb.log",
        stop=threading.Event(),
    )
    assert result.rc == 0
    assert heartbeat.exists()
    assert (tmp_path / "logs" / "hb.log").read_text() == "x\n"
