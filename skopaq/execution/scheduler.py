"""Always-on scheduler: one autonomous daemon session per NSE trading day.

Replaces Railway's cron on a single always-on host (the docker compose
``scheduler`` service runs ``skopaq schedule``). Rules:

- A session starts at ``SKOPAQ_SCHEDULER_START`` (09:15 IST) on NSE trading
  days only (``skopaq/risk/calendar.py``; a year with no holiday list is not
  traded, and is alerted once a day).
- Catch-up: if the host was down at START, a session still starts until
  ``SKOPAQ_SCHEDULER_LAST_START`` (11:30). After that the day is skipped and
  alerted once ("missed").
- At most once per day: a marker file is written *before* launch, so a restart
  (container recreate, host reboot) never starts a second session that day.
  The one exception: a session whose PRE_OPEN failed (exit code 3: token, broker
  session or LLM setup; nothing was traded) is started again every 5 minutes
  until LAST_START, so setting a missing token at 09:30 still gives a session.
- Pre-flight: at ``SKOPAQ_SCHEDULER_PREFLIGHT`` (08:45) an alert is sent if the
  INDstocks token is missing or expires before the session would end.
- Interrupted sessions: a session that was started but has no exit code (the
  host or container died mid-session) is alerted once. In live mode, before the
  deadline, ``skopaq monitor`` then runs until the deadline, so the open
  positions still get their stop-loss and the 15:20 EOD exit. The same happens
  when the session exits non-zero on its own (an exception, or OOM/SIGKILL)
  while the scheduler keeps running, and after a restart when a scheduler stop
  had to SIGKILL the session (CLOSING may not have finished).
- The recovery monitor is tracked the same way: one cut off by a dead scheduler,
  or stopped with it before the 15:20 EOD exit (or killed), has no exit code, and
  the next scheduler process runs it again (live, before the deadline) or alerts
  to check the broker. One whose launch fails is retried every poll, alerted once.
- Deadline: a session still running at ``SKOPAQ_SCHEDULER_DEADLINE`` (15:45)
  gets SIGTERM (the daemon closes its positions), then SIGKILL
  ``kill_after_seconds`` later. SIGTERM/SIGINT to the scheduler is forwarded to
  a running session the same way.
- One scheduler per state dir: ``<state_dir>/scheduler.lock`` is flocked for the
  scheduler's lifetime; a second one exits 1, alerting once a day (``--check``
  takes no lock).
- Settle backstop: ``skopaq settle`` once per trading day at
  ``SKOPAQ_SCHEDULER_SETTLE_AT`` (18:30).
- Alerts go to Telegram (``skopaq.notifications``); an optional dead-man's
  switch URL is pinged after each session. A heartbeat file is touched on
  every loop for the container health check.

The loop polls the IST wall clock every ``poll_seconds`` instead of sleeping
until a computed time, so it stays correct across host sleep, VM pauses and
clock jumps.
"""

from __future__ import annotations

import asyncio
import fcntl
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Optional

from pydantic import TypeAdapter, ValidationError

from skopaq.constants import NSE_MARKET_CLOSE
from skopaq.execution.daemon import PRE_OPEN_FAILED_EXIT_CODE
from skopaq.risk.calendar import (
    IST,
    NSE_TRADING_HOLIDAYS,
    now_ist,
    parse_extra_holidays,
    trading_day_status,
)

logger = logging.getLogger(__name__)

_HHMM = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")
_MARKER = re.compile(r"-(\d{4}-\d{2}-\d{2})\.(started|rc|flag|log)$")
_LOG_KEEP_DAYS = 60
# Recorded as the exit code of a session found started but never finished (host/container died).
INTERRUPTED_RC = -1
_INTERRUPTED_LOOKBACK_DAYS = 7
# A session whose PRE_OPEN failed (nothing traded) is started again this often until LAST_START.
PRE_OPEN_RETRY = timedelta(minutes=5)
# Live session that ended without managing its positions, too late for the recovery monitor.
_CHECK_THE_BROKER = ("LIVE: check open positions at the broker now; delivery (CNC) positions "
                     "are carried overnight and nothing is managing them.")
_LOCK_FILE = "scheduler.lock"
# What pydantic accepts for a bool; the scheduler settings are plain str (see config.py).
_TRUE = frozenset({"1", "on", "t", "true", "y", "yes"})
_FALSE = frozenset({"0", "off", "f", "false", "n", "no"})


def parse_hhmm(value: str) -> time:
    """Parse a zero-padded 24-hour ``HH:MM`` (IST)."""
    match = _HHMM.fullmatch((value or "").strip())
    if not match:
        raise ValueError(f"expected HH:MM (IST), got {value!r}")
    return time(int(match.group(1)), int(match.group(2)))


def _parse_bool(value) -> Optional[bool]:
    """true/false, yes/no, on/off, 1/0 (any case) as pydantic reads them; None otherwise."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return True if text in _TRUE else False if text in _FALSE else None


def _known_years(extra: str) -> list[int]:
    """Years with an NSE holiday list: built-in plus any year in SKOPAQ_NSE_HOLIDAYS."""
    return sorted(set(NSE_TRADING_HOLIDAYS) | {d.year for d in parse_extra_holidays(extra)})


@dataclass(frozen=True)
class ScheduleSettings:
    enabled: bool
    mode: str  # paper | live
    confirm_live: bool
    start: time
    last_start: time
    deadline: time
    eod_exit: time  # when `skopaq monitor` sells everything (and sells on a stop from then on)
    settle_at: Optional[time]
    preflight: Optional[time]
    poll_seconds: int
    kill_after_seconds: int
    state_dir: Path
    log_dir: Path
    ping_url: str
    heartbeat_file: Optional[Path]
    extra_holidays: str
    confirm_live_invalid: str = ""  # an unparseable SKOPAQ_SCHEDULER_CONFIRM_LIVE: not confirmed

    @classmethod
    def from_config(cls, config) -> "ScheduleSettings":
        """Validate the ``SKOPAQ_SCHEDULER_*`` settings; one ValueError lists every problem.

        They are plain str in SkopaqConfig and parsed here, so a typo stops only the
        scheduler. An unparseable CONFIRM_LIVE is no problem: live stays unconfirmed (the
        day is skipped and alerted, naming the value).
        """
        problems: list[str] = []

        def hhmm(env: str, value: str) -> Optional[time]:
            try:
                return parse_hhmm(value)
            except ValueError as exc:
                problems.append(f"{env}: {exc}")
                return None

        def seconds(env: str, value) -> int:
            try:  # as the int field it was: "30", " 30 ", "30.0" (not "30.5", "30s")
                number = TypeAdapter(int).validate_python(value)
            except ValidationError:
                problems.append(f"{env}: expected a whole number of seconds, got {value!r}")
                return 0
            if number < 1:
                problems.append(f"{env} must be at least 1")
            return number

        enabled = _parse_bool(config.scheduler_enabled)
        if enabled is None:
            problems.append(f"SKOPAQ_SCHEDULER_ENABLED: expected true or false, got "
                            f"{config.scheduler_enabled!r}")
        confirm_live = _parse_bool(config.scheduler_confirm_live)
        mode = (config.scheduler_mode or "").strip().lower()
        if mode not in ("paper", "live"):
            problems.append(
                f"SKOPAQ_SCHEDULER_MODE: expected paper or live, got {config.scheduler_mode!r}"
            )
        start = hhmm("SKOPAQ_SCHEDULER_START", config.scheduler_start)
        last_start = hhmm("SKOPAQ_SCHEDULER_LAST_START", config.scheduler_last_start)
        deadline = hhmm("SKOPAQ_SCHEDULER_DEADLINE", config.scheduler_deadline)
        settle_at = None
        if (config.scheduler_settle_at or "").strip():
            settle_at = hhmm("SKOPAQ_SCHEDULER_SETTLE_AT", config.scheduler_settle_at)
        preflight = None
        if (config.scheduler_preflight or "").strip():
            preflight = hhmm("SKOPAQ_SCHEDULER_PREFLIGHT", config.scheduler_preflight)

        if start and last_start and deadline and not start < last_start < deadline:
            problems.append(
                "expected SKOPAQ_SCHEDULER_START < SKOPAQ_SCHEDULER_LAST_START < "
                f"SKOPAQ_SCHEDULER_DEADLINE, got {start:%H:%M} / {last_start:%H:%M} / "
                f"{deadline:%H:%M}"
            )
        if preflight and start and preflight >= start:
            problems.append(
                f"SKOPAQ_SCHEDULER_PREFLIGHT ({preflight:%H:%M}) must be earlier than "
                f"SKOPAQ_SCHEDULER_START ({start:%H:%M}), or empty"
            )
        if settle_at and deadline and settle_at <= deadline:
            problems.append(
                f"SKOPAQ_SCHEDULER_SETTLE_AT ({settle_at:%H:%M}) must be later than "
                f"SKOPAQ_SCHEDULER_DEADLINE ({deadline:%H:%M}), or empty"
            )
        poll_seconds = seconds("SKOPAQ_SCHEDULER_POLL_SECONDS", config.scheduler_poll_seconds)
        kill_after_seconds = seconds("SKOPAQ_SCHEDULER_KILL_AFTER_SECONDS",
                                     config.scheduler_kill_after_seconds)
        try:
            parse_extra_holidays(config.nse_holidays)
        except ValueError as exc:
            problems.append(str(exc))

        if problems:
            raise ValueError("Invalid scheduler configuration:\n- " + "\n- ".join(problems))

        heartbeat = (config.heartbeat_file or "").strip()
        eod_exit = datetime.combine(date(2000, 1, 1), NSE_MARKET_CLOSE) - timedelta(
            minutes=config.monitor_eod_exit_minutes_before_close)
        return cls(
            enabled=enabled,
            mode=mode,
            confirm_live=bool(confirm_live),
            start=start,
            last_start=last_start,
            deadline=deadline,
            eod_exit=eod_exit.time(),
            settle_at=settle_at,
            preflight=preflight,
            poll_seconds=poll_seconds,
            kill_after_seconds=kill_after_seconds,
            # Relative paths resolve against the cwd: /home/skopaq (the home volume) in the image.
            state_dir=Path(config.scheduler_state_dir).expanduser().absolute(),
            log_dir=Path(config.daemon_session_log_dir).expanduser().absolute(),
            ping_url=(config.scheduler_ping_url or "").strip(),
            heartbeat_file=Path(heartbeat).expanduser() if heartbeat else None,
            extra_holidays=config.nse_holidays or "",
            confirm_live_invalid="" if confirm_live is not None else str(
                config.scheduler_confirm_live),
        )


def daemon_argv(settings: ScheduleSettings) -> Optional[list[str]]:
    """The ``skopaq`` arguments for today's session; ``None`` for unconfirmed live mode.

    ``--once`` starts PRE_OPEN immediately (the scheduler launches at 09:15), and
    the scan follows the daemon's scan delay after that.
    """
    if settings.mode == "live":
        if not settings.confirm_live:
            return None
        return ["daemon", "--once", "--live", "--confirm-live"]
    return ["daemon", "--once", "--paper"]


class SchedulerState:
    """Per-day marker files in ``state_dir`` (on the home volume, so they survive restarts).

    ``<job>-<YYYY-MM-DD>.started`` holds the launch time (or ``skipped: <why>``),
    ``<job>-<day>.rc`` the exit code, and ``<key>-<day>.flag`` one-shot alert flags.
    One instance per scheduler process: ``once_this_process`` notes live in memory only.
    """

    def __init__(self, state_dir: Path, log_dir: Optional[Path] = None) -> None:
        self.state_dir = Path(state_dir)
        self.log_dir = Path(log_dir) if log_dir is not None else None
        self._noted: set[tuple[str, date]] = set()

    def _path(self, name: str, day: date, suffix: str) -> Path:
        return self.state_dir / f"{name}-{day.isoformat()}.{suffix}"

    def started(self, job: str, day: date) -> bool:
        return self._path(job, day, "started").exists()

    def started_note(self, job: str, day: date) -> str:
        try:
            return self._path(job, day, "started").read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def mark_started(self, job: str, day: date, note: str = "") -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        text = note or datetime.now(IST).isoformat(timespec="seconds")
        self._path(job, day, "started").write_text(text, encoding="utf-8")

    def record_exit(self, job: str, day: date, rc: int) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._path(job, day, "rc").write_text(str(rc), encoding="utf-8")

    def last_exit(self, job: str, day: date) -> Optional[int]:
        try:
            return int(self._path(job, day, "rc").read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def clear_exit(self, job: str, day: date) -> None:
        self._path(job, day, "rc").unlink(missing_ok=True)

    def unfinished(self, job: str, today: date, days: int) -> list[date]:
        """Days (today and the *days* before) when *job* was launched but never recorded
        an exit code: the process running it died. Skipped days are not launches."""
        found = []
        for back in range(days, -1, -1):
            day = today - timedelta(days=back)
            if (
                self.started(job, day)
                and self.last_exit(job, day) is None
                and not self.started_note(job, day).startswith("skipped")
            ):
                found.append(day)
        return found

    def flag_once(self, key: str, day: date) -> bool:
        """True only the first time this is called for *key* on *day* (across restarts)."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._path(key, day, "flag").touch(exist_ok=False)
        except FileExistsError:
            return False
        return True

    def once_this_process(self, key: str, day: date) -> bool:
        """True only the first time this is called for *key* on *day* in this process
        (a restarted scheduler starts afresh)."""
        if (key, day) in self._noted:
            return False
        self._noted.add((key, day))
        return True

    def prune(self, today: date, keep_days: int = 30) -> None:
        """Delete markers older than *keep_days*, and session logs older than 60 days."""
        targets = [(self.state_dir, keep_days, ("started", "rc", "flag"))]
        if self.log_dir is not None:
            targets.append((self.log_dir, _LOG_KEEP_DAYS, ("log",)))
        for directory, days, suffixes in targets:
            if not directory.is_dir():
                continue
            cutoff = today - timedelta(days=days)
            for path in directory.iterdir():
                match = _MARKER.search(path.name)
                if not match or match.group(2) not in suffixes:
                    continue
                try:
                    if date.fromisoformat(match.group(1)) < cutoff:
                        path.unlink()
                except (ValueError, OSError):
                    logger.debug("Could not prune %s", path, exc_info=True)


def due_job(now: datetime, settings: ScheduleSettings, state: SchedulerState) -> Optional[str]:
    """The job due at *now*: ``"daemon"``, ``"settle"`` or ``None`` (reads markers only)."""
    now = now.astimezone(IST)
    if not settings.enabled:
        return None
    day, t = now.date(), now.time()
    if not trading_day_status(day, settings.extra_holidays)[0]:
        return None
    if settings.start <= t < settings.last_start and _daemon_may_start(now, state):
        return "daemon"
    settle_at = settings.settle_at
    if settle_at is not None and t >= settle_at and not state.started("settle", day):
        return "settle"
    return None


def _daemon_may_start(now: datetime, state: SchedulerState) -> bool:
    """Not started today, or only a PRE_OPEN failure (nothing traded) at least
    PRE_OPEN_RETRY ago."""
    day = now.date()
    if not state.started("daemon", day):
        return True
    if state.last_exit("daemon", day) != PRE_OPEN_FAILED_EXIT_CODE:
        return False
    try:
        launched = datetime.fromisoformat(state.started_note("daemon", day))
    except ValueError:
        return False
    return launched.tzinfo is not None and now - launched >= PRE_OPEN_RETRY


def _session_end(day: date, settings: ScheduleSettings) -> datetime:
    """Until when a session needs the broker (as the daemon's PRE_OPEN computes it)."""
    return datetime.combine(day, max(NSE_MARKET_CLOSE, settings.deadline), tzinfo=IST)


def _token_problem(day: date, settings: ScheduleSettings) -> str:
    """Why the stored INDstocks token cannot carry today's session ("" if it can)."""
    try:
        from skopaq.broker.token_manager import TokenManager, session_token_problem

        health = TokenManager().get_health(notify=False)
        return session_token_problem(health, _session_end(day, settings))
    except Exception as exc:
        return f"could not check the INDstocks token: {exc}"


@dataclass(frozen=True)
class JobResult:
    rc: int
    deadline_hit: bool = False
    stopped: bool = False
    killed: bool = False  # still running kill_after_seconds after SIGTERM: SIGKILLed
    stopped_at: Optional[datetime] = None  # when run_job saw the scheduler's stop (IST)


def _touch(path: Optional[Path]) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    except OSError:
        logger.debug("Could not touch heartbeat %s", path, exc_info=True)


def _close_quietly(handle) -> None:
    try:
        handle.close()
    except (OSError, ValueError):
        pass


def _tee(stream, log_path: Path, log_errors: list[str]) -> None:
    """Copy each line of the child's output to our stdout and the session log.

    Reads the pipe to the end whatever happens to either copy: a child blocked on a full
    pipe cannot run its monitor or handle SIGTERM. A copy that fails (disk full, EIO, a
    closed stdout) is dropped; why the session log failed goes to *log_errors*.
    """
    try:
        log = open(log_path, "a", encoding="utf-8")
    except OSError as exc:
        log = None
        log_errors.append(f"cannot open {log_path}: {exc}")
    to_stdout = True
    for line in stream:
        if to_stdout:
            try:
                sys.stdout.write(line)
                sys.stdout.flush()
            except (OSError, ValueError):
                to_stdout = False
                logger.warning("Cannot write the child's output to stdout; the session log "
                               "%s still gets it", log_path, exc_info=True)
        if log is not None:
            try:
                log.write(line)
                log.flush()
            except (OSError, ValueError) as exc:
                log_errors.append(f"writing {log_path} failed: {exc}")
                _close_quietly(log)
                log = None
    if log is not None:
        _close_quietly(log)


def run_job(
    cmd: list[str],
    *,
    deadline: datetime,
    settings: ScheduleSettings,
    log_path: Path,
    stop: threading.Event,
    clock: Callable[[], datetime] = now_ist,
    env: Optional[dict[str, str]] = None,
    alert: Optional[Callable[[str], None]] = None,
) -> JobResult:
    """Run *cmd* to completion, stopping it at *deadline* or when *stop* is set.

    *env* adds to (or overrides) the scheduler's environment for the child.

    Stopping means SIGTERM (once), then SIGKILL ``kill_after_seconds`` later if the
    child is still alive. The heartbeat is touched every second meanwhile. If the
    session log cannot be written, *alert* (default: Telegram) is called once. If the
    child cannot be watched (the tee thread does not start), it is killed and the error
    raised: it never outlives this call.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Starting %s (log: %s)", " ".join(cmd), log_path)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1"},
    )
    log_errors: list[str] = []
    tee = threading.Thread(target=_tee, args=(proc.stdout, log_path, log_errors), daemon=True)
    deadline_hit = stopped = killed = log_alerted = False
    stopped_at: Optional[datetime] = None
    term_sent_at: Optional[float] = None

    def alert_log_failure() -> None:
        nonlocal log_alerted
        if log_errors and not log_alerted:
            log_alerted = True
            job = " ".join(cmd[3:]) or "a job"  # _cli(...): python -m skopaq.cli.main <job>
            (alert or _alert)(
                f"the session log failed ({log_errors[0]}) while `{job}` runs. It keeps "
                "running; its output now goes only to `docker compose logs scheduler`. Free "
                "disk space on the host."
            )

    def terminate(why: str) -> None:
        nonlocal term_sent_at
        if term_sent_at is not None:
            return
        logger.warning("%s: sending SIGTERM to pid %d", why, proc.pid)
        term_sent_at = _time.monotonic()
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass

    try:
        tee.start()
        while proc.poll() is None:
            _touch(settings.heartbeat_file)
            alert_log_failure()
            if stop.is_set() and not stopped:
                stopped, stopped_at = True, clock()
                terminate("Scheduler stopping")
            if not deadline_hit and clock() >= deadline:
                deadline_hit = True
                terminate(f"Deadline {deadline:%H:%M} IST reached")
            if (
                term_sent_at is not None
                and not killed
                and _time.monotonic() - term_sent_at >= settings.kill_after_seconds
            ):
                killed = True
                logger.error(
                    "Still running %ds after SIGTERM: sending SIGKILL to pid %d",
                    settings.kill_after_seconds, proc.pid,
                )
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
    except BaseException:
        # Never leave the child running unwatched (e.g. the tee thread cannot start): the
        # next tick would take its job for an interrupted one and start another beside it.
        logger.error("Could not watch pid %d: killing it", proc.pid)
        proc.kill()
        proc.wait()
        raise

    rc = proc.wait()
    tee.join(timeout=10)
    alert_log_failure()
    logger.info("Finished with rc=%d%s", rc, " (deadline)" if deadline_hit else "")
    return JobResult(rc=rc, deadline_hit=deadline_hit, stopped=stopped,
                     killed=killed and rc == -signal.SIGKILL, stopped_at=stopped_at)


def _alert(msg: str) -> None:
    """Log *msg* and send it to Telegram (best effort, bounded)."""
    logger.warning("ALERT: %s", msg)
    try:
        from skopaq.notifications import notify

        asyncio.run(asyncio.wait_for(notify("SkopaqTrader scheduler: " + msg), 30))
    except Exception:
        logger.warning("Could not send the scheduler alert", exc_info=True)


def _ping(url: str, ok: bool) -> None:
    """Dead-man's switch (healthchecks.io style): GET URL on success, URL/fail on failure."""
    if not url:
        return
    try:
        import httpx

        httpx.get(url if ok else url.rstrip("/") + "/fail", timeout=10)
    except Exception:
        logger.warning("Scheduler ping failed", exc_info=True)


def _cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "skopaq.cli.main", *args]


def _lock(state_dir: Path) -> Optional[int]:
    """Take ``<state_dir>/scheduler.lock`` (flock) for this process's lifetime.

    Returns the lock's file descriptor (keep it open), or ``None`` when another scheduler
    holds it. The kernel drops the lock when the process dies, so it never goes stale.
    Opened read-only: flock needs no write access, so a lock file left by another user
    (e.g. a scheduler once run as root) still works. Raises OSError when the file cannot
    be locked at all.
    """
    fd = os.open(state_dir / _LOCK_FILE, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except OSError:
        os.close(fd)
        raise
    return fd


def _alert_once_a_day(state_dir: Path, key: str, msg: str, *, alert, clock) -> None:
    """*alert* once a day (a flag in *state_dir*), for a scheduler that exits at start and
    that a restart policy runs again and again; every time if the flag cannot be written."""
    try:
        first = SchedulerState(state_dir).flag_once(key, clock().astimezone(IST).date())
    except Exception:
        first = True
    if first:
        alert(msg)


def alert_invalid_config(
    config,
    error: str,
    *,
    alert: Callable[[str], None] = _alert,
    clock: Callable[[], datetime] = now_ist,
) -> None:
    """Alert that the scheduler does not start because of its settings (once a day)."""
    _alert_once_a_day(
        Path(config.scheduler_state_dir).expanduser().absolute(), "config-invalid",
        f"not running: {error}\nNo sessions start until it is fixed (see "
        "`docker compose logs scheduler`).",
        alert=alert, clock=clock,
    )


def run_forever(
    settings: ScheduleSettings,
    *,
    clock: Callable[[], datetime] = now_ist,
    runner: Callable[..., JobResult] = run_job,
    alert: Callable[[str], None] = _alert,
    ping: Callable[[str, bool], None] = _ping,
    sleep: Callable[[float], None] = _time.sleep,
    stop: Optional[threading.Event] = None,
) -> int:
    """Run the scheduler loop until SIGTERM/SIGINT (or *stop*); returns the exit code
    (1 when another scheduler already runs on the same state dir)."""
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    # One scheduler per state dir: a second one would take the running session for an
    # interrupted one and start `skopaq monitor` beside it. Its alerts go out once a day, as
    # a restart policy runs a scheduler that exits again and again.
    lock_path = settings.state_dir / _LOCK_FILE
    try:
        lock = _lock(settings.state_dir)
    except OSError as exc:
        logger.error("Cannot lock %s", lock_path, exc_info=True)
        _alert_once_a_day(settings.state_dir, "lock-failed",
                          f"cannot lock {lock_path} ({exc}): not starting, so no sessions run",
                          alert=alert, clock=clock)
        return 1
    if lock is None:
        logger.error("Another scheduler holds %s: exiting", lock_path)
        _alert_once_a_day(settings.state_dir, "lock-held",
                          f"another scheduler is already running on {settings.state_dir} (it "
                          f"holds {lock_path}): this one exits without starting anything",
                          alert=alert, clock=clock)
        return 1
    try:
        return _loop(settings, clock=clock, runner=runner, alert=alert, ping=ping,
                     sleep=sleep, stop=stop)
    finally:
        os.close(lock)


def _loop(settings, *, clock, runner, alert, ping, sleep, stop) -> int:
    """``run_forever`` once it holds the lock."""
    if stop is None:
        stop = threading.Event()

        def _on_signal(signum, _frame) -> None:
            logger.warning("%s received: stopping the scheduler", signal.Signals(signum).name)
            stop.set()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, _on_signal)

    state = SchedulerState(settings.state_dir, settings.log_dir)
    logger.info(
        "Scheduler started: mode=%s%s start=%s last_start=%s deadline=%s settle=%s "
        "(IST), NSE holiday years known: %s",
        settings.mode,
        "" if settings.mode == "paper" or settings.confirm_live else " (NOT confirmed)",
        f"{settings.start:%H:%M}", f"{settings.last_start:%H:%M}",
        f"{settings.deadline:%H:%M}",
        f"{settings.settle_at:%H:%M}" if settings.settle_at else "off",
        _known_years(settings.extra_holidays),
    )

    disabled_logged = False
    pruned_on: Optional[date] = None
    error_alerted_on: Optional[date] = None
    while not stop.is_set():
        _touch(settings.heartbeat_file)
        now = clock().astimezone(IST)
        day = now.date()
        try:
            if pruned_on != day:
                state.prune(day)
                pruned_on = day
            if not settings.enabled:
                if not disabled_logged:
                    logger.info("scheduler disabled (SKOPAQ_SCHEDULER_ENABLED=false): "
                                "no sessions will start")
                    disabled_logged = True
            else:
                _tick(now, settings, state, runner=runner, alert=alert, ping=ping,
                      stop=stop, clock=clock)
        except Exception as exc:
            logger.exception("Scheduler iteration failed")
            if error_alerted_on != day:
                error_alerted_on = day
                alert(f"scheduler error on {day}: {exc}")

        for _ in range(settings.poll_seconds):
            if stop.is_set():
                break
            sleep(1)
    logger.info("Scheduler stopped")
    return 0


def _tick(now, settings, state, *, runner, alert, ping, stop, clock) -> None:
    """One scheduler iteration at *now* (IST): alerts, then the due job, if any."""
    day, t = now.date(), now.time()
    trading, _ = trading_day_status(day, settings.extra_holidays)

    # Jobs launched by an earlier scheduler process that has gone (or a recovery monitor this
    # one could not launch). They cannot be running: within one process the runner blocks
    # until the child exits (and kills it if it cannot watch it), and the state dir's lock
    # keeps a second scheduler out. The monitor comes first, so a recovery monitor started
    # just below is never picked up twice in one tick.
    for past in state.unfinished("monitor", day, _INTERRUPTED_LOOKBACK_DAYS):
        _resume_recovery_monitor(past, now, settings, state, runner=runner, alert=alert,
                                 stop=stop, clock=clock)
    for past in state.unfinished("daemon", day, _INTERRUPTED_LOOKBACK_DAYS):
        _recover_interrupted(past, now, settings, state, runner=runner, alert=alert,
                             stop=stop, clock=clock)

    if (
        trading
        and settings.preflight is not None
        and settings.preflight <= t < settings.start
        and state.flag_once("preflight", day)
    ):
        problem = _token_problem(day, settings)
        if problem:
            alert(f"pre-flight for today's {settings.start:%H:%M} IST session: {problem}")

    if (
        trading
        and settings.last_start <= t < settings.deadline
        and state.last_exit("daemon", day) == PRE_OPEN_FAILED_EXIT_CODE
        and state.flag_once("preopen-gave-up", day)
    ):
        alert(
            f"no daemon session today: PRE_OPEN kept failing until {settings.last_start:%H:%M} "
            f"IST (nothing was traded). See logs/daemon/daemon-{day.isoformat()}.log"
        )

    if day.weekday() < 5 and t >= settings.start and day.year not in _known_years(
        settings.extra_holidays
    ) and state.flag_once("holidays-missing", day):
        alert(trading_day_status(day, settings.extra_holidays)[1] + " (no sessions until then)")

    if (
        trading
        and settings.last_start <= t < settings.deadline
        and not state.started("daemon", day)
        and state.flag_once("missed", day)
    ):
        alert(
            "no daemon session today: the scheduler was not running between "
            f"{settings.start:%H:%M} and {settings.last_start:%H:%M} IST"
        )

    job = due_job(now, settings, state)
    if job == "daemon":
        argv = daemon_argv(settings)
        if argv is None:
            state.mark_started("daemon", day, note="skipped: live not confirmed")
            got = (f" (got {settings.confirm_live_invalid!r}, which is not true or false)"
                   if settings.confirm_live_invalid else "")
            alert(
                f"SKOPAQ_SCHEDULER_MODE=live but SKOPAQ_SCHEDULER_CONFIRM_LIVE is not true{got}: "
                f"no session on {day}"
            )
            return
        # Before launch: at most one session per day. The launch time is the note (a retry
        # after a PRE_OPEN failure waits PRE_OPEN_RETRY from it); clearing that failure's
        # exit code makes this attempt count as interrupted if the host dies during it.
        state.mark_started("daemon", day, note=now.isoformat(timespec="seconds"))
        state.clear_exit("daemon", day)
        result = runner(
            _cli(*argv),
            deadline=datetime.combine(day, settings.deadline, tzinfo=IST),
            settings=settings,
            log_path=settings.log_dir / f"daemon-{day.isoformat()}.log",
            stop=stop,
            clock=clock,
            alert=alert,
        )
        live = settings.mode == "live" and settings.confirm_live
        if result.killed and result.stopped and not result.deadline_hit and live:
            # A scheduler stop had to SIGKILL the session: CLOSING may not have finished (a
            # SIGTERM during a synchronous analysis is only seen when it returns). No exit
            # code, as if this process had died with it: the next scheduler process runs
            # the recovery monitor before the deadline, or says to check the broker.
            ping(settings.ping_url, ok=False)
            alert(
                f"daemon session of {day} did not stop within {settings.kill_after_seconds}s "
                f"of the scheduler's SIGTERM and was killed (rc={result.rc}): CLOSING may not "
                f"have finished. {_CHECK_THE_BROKER} A scheduler restarted before "
                f"{settings.deadline:%H:%M} IST runs `skopaq monitor` until then."
            )
            return
        state.record_exit("daemon", day, result.rc)
        if result.rc not in (0, PRE_OPEN_FAILED_EXIT_CODE) and not (
            result.deadline_hit or result.stopped
        ):
            # Ended on its own before the deadline: positions it opened may still be open.
            ping(settings.ping_url, ok=False)
            _recover_failed(day, result.rc, settings, state, runner=runner, alert=alert,
                            stop=stop, clock=clock)
            return
        if result.rc == PRE_OPEN_FAILED_EXIT_CODE and not (result.deadline_hit or result.stopped):
            if state.flag_once("preopen-failed", day):
                alert(
                    f"daemon PRE_OPEN failed on {day} (rc={result.rc}: nothing was traded; "
                    f"see logs/daemon/daemon-{day.isoformat()}.log). Retrying every "
                    f"{PRE_OPEN_RETRY.seconds // 60} min until {settings.last_start:%H:%M} IST: "
                    "fix the cause (e.g. skopaq token set <TOKEN>)"
                )
            ping(settings.ping_url, ok=False)
            return
        if result.rc != 0:
            msg = f"daemon exited rc={result.rc} on {day}"
            if result.deadline_hit:
                msg += f" (stopped at the {settings.deadline:%H:%M} deadline)"
            if result.killed:
                msg += (f": killed {settings.kill_after_seconds}s after SIGTERM, so CLOSING may "
                        "not have finished")
                if live:
                    msg += f". {_CHECK_THE_BROKER}"
            alert(msg)
        elif result.deadline_hit:  # a clean exit, but the session overran
            alert(f"daemon was still running at the {settings.deadline:%H:%M} deadline on {day} "
                  "and was stopped (rc=0)")
        ping(settings.ping_url, ok=result.rc == 0)
    elif job == "settle":
        state.mark_started("settle", day)
        result = runner(
            _cli("settle"),
            deadline=now + timedelta(hours=1),
            settings=settings,
            log_path=settings.log_dir / f"settle-{day.isoformat()}.log",
            stop=stop,
            clock=clock,
            alert=alert,
        )
        state.record_exit("settle", day, result.rc)
        if result.rc != 0:
            alert(f"settle exited rc={result.rc} on {day}")


def _unconfirmed_live(settings) -> str:
    """Alert text for SKOPAQ_SCHEDULER_MODE=live without a valid confirmation, else ""."""
    if settings.mode != "live" or settings.confirm_live:
        return ""
    got = (f" ({settings.confirm_live_invalid!r} is not true or false)"
           if settings.confirm_live_invalid else "")
    return (f"SKOPAQ_SCHEDULER_MODE=live but SKOPAQ_SCHEDULER_CONFIRM_LIVE is not true{got}, "
            f"so no recovery `skopaq monitor` runs. {_CHECK_THE_BROKER}")


def _recover_interrupted(day, now, settings, state, *, runner, alert, stop, clock) -> None:
    """Handle a daemon session on *day* that started but never finished (host/container died).

    Alerts once. In live mode, today, before the deadline: runs ``skopaq monitor`` until the
    deadline so the open (CNC, carried overnight otherwise) positions still get their
    stop-loss and EOD exit. Paper positions lived in the dead process's memory.
    """
    state.record_exit("daemon", day, INTERRUPTED_RC)  # handled once, even across restarts
    started = state.started_note("daemon", day) or "?"
    what = f"the daemon session of {day} (started {started}) was interrupted before it finished"
    live = settings.mode == "live" and settings.confirm_live
    if not live:
        alert(f"{what}. " + (_unconfirmed_live(settings)
                             or "Paper mode: its paper positions were in memory and are gone."))
        return
    today = now.date()
    if day != today or now.time() >= settings.deadline:
        alert(f"{what}. {_CHECK_THE_BROKER}")
        return
    alert(f"{what}. LIVE: running `skopaq monitor` until {settings.deadline:%H:%M} IST so "
          "the open positions keep their stop-loss and the EOD exit.")
    _run_recovery_monitor(day, now, settings, state, runner=runner, alert=alert, stop=stop,
                          clock=clock)


def _recover_failed(day, rc, settings, state, *, runner, alert, stop, clock) -> None:
    """Handle today's daemon session exiting non-zero on its own (not the deadline, not a
    scheduler stop): an exception that skipped CLOSING, or OOM/SIGKILL (rc < 0).

    In live mode, before the deadline: runs ``skopaq monitor`` until the deadline, as for
    an interrupted session. Paper positions lived in the dead process's memory.
    """
    what = f"daemon exited rc={rc} on {day}"
    if not (settings.mode == "live" and settings.confirm_live):
        note = _unconfirmed_live(settings)
        alert(f"{what}. {note}" if note else what)
        return
    now = clock().astimezone(IST)  # the session ran for a while: not the tick's time
    if now.date() != day or now.time() >= settings.deadline:
        alert(f"{what}. {_CHECK_THE_BROKER}")
        return
    alert(f"{what} before the {settings.deadline:%H:%M} deadline. LIVE: running `skopaq "
          f"monitor` until {settings.deadline:%H:%M} IST so any open positions keep their "
          "stop-loss and the EOD exit.")
    _run_recovery_monitor(day, now, settings, state, runner=runner, alert=alert, stop=stop,
                          clock=clock)


def _resume_recovery_monitor(day, now, settings, state, *, runner, alert, stop, clock) -> None:
    """Handle a recovery monitor on *day* that an earlier scheduler process started and that
    has no exit code: the process died with it, or a scheduler stop ended it early.

    Live, today, before the deadline: runs it again, alerting once per scheduler process (a
    monitor this process could not start is retried every tick, quietly). Otherwise records
    it as interrupted and alerts to check the broker (it was managing live positions).
    """
    started = state.started_note("monitor", day) or "?"
    what = (f"the recovery `skopaq monitor` of {day} (started {started}) was cut off before "
            "it finished")
    live = settings.mode == "live" and settings.confirm_live
    if not live or day != now.date() or now.time() >= settings.deadline:
        state.record_exit("monitor", day, INTERRUPTED_RC)  # handled once, even across restarts
        alert(f"{what}. {_CHECK_THE_BROKER}")
        return
    if state.once_this_process("monitor", day):  # False: this process already tried it
        alert(f"{what}. LIVE: running it again until {settings.deadline:%H:%M} IST so the open "
              "positions keep their stop-loss and the EOD exit.")
    _run_recovery_monitor(day, now, settings, state, runner=runner, alert=alert, stop=stop,
                          clock=clock)


def _run_recovery_monitor(day, now, settings, state, *, runner, alert, stop, clock) -> None:
    """Live ``skopaq monitor`` until the deadline, logged to the day's session log.

    Its exit code is recorded when it ends on its own, at the deadline, or cleanly (rc 0) on
    a scheduler stop from the EOD exit on (its shutdown then sells what is left). Stopped
    earlier, or not cleanly, it gets none, like a monitor whose scheduler died, so the next
    scheduler process runs it again (``_resume_recovery_monitor``). One that cannot be run
    (the launch fails) gets none either: the next tick retries it.
    """
    state.once_this_process("monitor", day)  # a retry of it in this process is not alerted
    state.mark_started("monitor", day, note=now.isoformat(timespec="seconds"))
    state.clear_exit("monitor", day)
    try:
        result = runner(
            _cli("monitor"),
            deadline=datetime.combine(day, settings.deadline, tzinfo=IST),
            settings=settings,
            log_path=settings.log_dir / f"daemon-{day.isoformat()}.log",
            stop=stop,
            clock=clock,
            env={"SKOPAQ_TRADING_MODE": "live"},
            alert=alert,
        )
    except Exception as exc:  # e.g. fork fails (ENOMEM, EAGAIN); run_job leaves no child
        logger.exception("Could not run the recovery monitor")
        if state.once_this_process("monitor-failed", day):
            alert(
                f"could not run the recovery `skopaq monitor` of {day} ({exc}): open "
                f"positions have no stop-loss or EOD exit. {_CHECK_THE_BROKER} Retrying every "
                f"{settings.poll_seconds}s until {settings.deadline:%H:%M} IST."
            )
        return
    if result.stopped and not result.deadline_hit:
        # Unlike the daemon, the monitor sells on a stop only from the EOD exit on: its own
        # check runs once it gets the SIGTERM, which run_job sent when it saw the stop.
        stopped_at = (result.stopped_at or clock()).astimezone(IST)
        if stopped_at.time() < settings.eod_exit:
            alert(
                f"recovery `skopaq monitor` of {day} was stopped with the scheduler at "
                f"{stopped_at:%H:%M} IST, before the {settings.eod_exit:%H:%M} EOD exit: open "
                f"positions have no stop-loss or EOD exit now. {_CHECK_THE_BROKER} A scheduler "
                f"restarted before {settings.deadline:%H:%M} IST runs the monitor again."
            )
            return
        if result.rc != 0:  # killed, or ended without its shutdown's EOD exit
            killed = f", killed {settings.kill_after_seconds}s later" if result.killed else ""
            alert(
                f"recovery `skopaq monitor` of {day} was stopped with the scheduler at "
                f"{stopped_at:%H:%M} IST and exited rc={result.rc}{killed}: its EOD exit may "
                f"not have finished. {_CHECK_THE_BROKER} A scheduler restarted before "
                f"{settings.deadline:%H:%M} IST runs the monitor again."
            )
            return
    state.record_exit("monitor", day, result.rc)
    if result.rc != 0:
        alert(f"recovery `skopaq monitor` exited rc={result.rc} on {day}: check open positions")


def _next_session(now: datetime, settings: ScheduleSettings, state: SchedulerState) -> str:
    day, t = now.date(), now.time()
    if not settings.enabled:
        return "none (scheduler disabled)"
    if daemon_argv(settings) is None:
        return "none (live NOT confirmed)"
    if trading_day_status(day, settings.extra_holidays)[0] and not state.started("daemon", day):
        if settings.start <= t < settings.last_start:
            return f"now (catch-up window open until {settings.last_start:%H:%M} IST)"
        if t < settings.start:
            return f"today {day:%a %Y-%m-%d} at {settings.start:%H:%M} IST"
    if (
        settings.start <= t < settings.last_start
        and state.last_exit("daemon", day) == PRE_OPEN_FAILED_EXIT_CODE
    ):
        return (f"retrying (PRE_OPEN failed) every {PRE_OPEN_RETRY.seconds // 60} min until "
                f"{settings.last_start:%H:%M} IST")
    d = day
    for _ in range(400):
        d += timedelta(days=1)
        ok, reason = trading_day_status(d, settings.extra_holidays)
        if ok:
            return f"{d:%a %Y-%m-%d} at {settings.start:%H:%M} IST"
        if reason.startswith("no NSE holiday list"):
            return f"unknown: {reason}"
    return "unknown"


def describe(settings: ScheduleSettings, now: datetime, state: SchedulerState) -> list[str]:
    """Human-readable plan for ``skopaq schedule --check``."""
    now = now.astimezone(IST)
    day = now.date()
    trading, reason = trading_day_status(day, settings.extra_holidays)
    if settings.mode == "live" and settings.confirm_live:
        mode = "live"
    elif settings.mode == "live":
        got = (f" (SKOPAQ_SCHEDULER_CONFIRM_LIVE={settings.confirm_live_invalid!r} is not true "
               "or false)" if settings.confirm_live_invalid else "")
        mode = f"live NOT confirmed{got}: no sessions"
    else:
        mode = "paper"

    def marker(job: str) -> str:
        note = state.started_note(job, day)
        if not state.started(job, day):
            return "not started"
        rc = state.last_exit(job, day)
        labels = {INTERRUPTED_RC: " (interrupted)", PRE_OPEN_FAILED_EXIT_CODE: " (PRE_OPEN failed)"}
        return f"started {note}" + ("" if rc is None else f", rc={rc}{labels.get(rc, '')}")

    # The live recovery monitor, only on a day it ran (no rc yet: it runs again after a restart).
    monitor = [f"Today's monitor: {marker('monitor')}"] if state.started("monitor", day) else []
    lines = [
        f"Now:            {now:%Y-%m-%d %H:%M:%S} IST ({now:%A})",
        f"Today:          {'NSE trading day' if trading else 'not a trading day: ' + reason}",
        f"Holiday years:  {_known_years(settings.extra_holidays)}",
        f"Next session:   {_next_session(now, settings, state)}",
        f"Mode:           {mode}",
        f"Window (IST):   start {settings.start:%H:%M}, catch-up until "
        f"{settings.last_start:%H:%M}, deadline {settings.deadline:%H:%M}, settle "
        f"{settings.settle_at.strftime('%H:%M') if settings.settle_at else 'off'}, pre-flight "
        f"{settings.preflight.strftime('%H:%M') if settings.preflight else 'off'}",
        f"Today's daemon: {marker('daemon')}",
        *monitor,
        f"Today's settle: {marker('settle')}",
        f"State dir:      {settings.state_dir}",
        f"Log dir:        {settings.log_dir}",
        f"Ping URL:       {'configured' if settings.ping_url else 'not configured'}",
    ]
    if not settings.enabled:
        lines.insert(0, "Scheduler:      DISABLED (SKOPAQ_SCHEDULER_ENABLED=false): no sessions")
    return lines


def check_ok(settings: ScheduleSettings, now: datetime) -> bool:
    """The scheduler can work this year: the current year's NSE holidays are known."""
    return now.astimezone(IST).year in _known_years(settings.extra_holidays)
