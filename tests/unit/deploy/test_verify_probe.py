"""scripts/macmini/verify.sh --probe-kill-switch: a halt already in place is never lifted.

The script runs against a fake ``docker``: ``docker compose exec -T <svc> <cmd>`` runs
``<cmd>`` here (with this checkout on PYTHONPATH, a temporary halt file and a fake Supabase
``system_flags`` row in a JSON file); everything else answers as a stack that is not
running, so only the host checks and the probe run."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[3]
VERIFY = ROOT / "scripts" / "macmini" / "verify.sh"

FAKE_DOCKER = """#!/bin/sh
[ "$1" = compose ] || exit 0
shift
case "$1" in
    exec)
        shift
        [ "$1" = -T ] && shift
        svc="$1"
        shift
        echo "$svc $*" >> "$FAKE_DOCKER_LOG"
        if [ -n "${FAKE_DOCKER_HOOK:-}" ]; then "$FAKE_DOCKER_HOOK" "$svc" "$@"; fi
        exec "$@" ;;
    version) echo 2.0.0 ;;
esac
exit 0
"""

# Run by the fake docker before each exec: an operator halts (Telegram /halt, another
# terminal) just as the probe asks the scheduler whether it sees the probe's halt.
OPERATOR_HALTS_MID_PROBE = """#!/bin/sh
svc="$1"
shift
case "$svc $*" in
    "scheduler "*kill_switch*)
        mkdir -p "$(dirname "$SKOPAQ_HALT_FILE")"
        cat "$OPERATOR_HALT" > "$SKOPAQ_HALT_FILE" ;;
esac
exit 0
"""

# An operator halts (Telegram /halt) just as the probe is about to halt: before, a separate
# status read had already said "active", and the probe's halt overwrote the operator's.
OPERATOR_HALTS_BEFORE_THE_PROBE = """#!/bin/sh
svc="$1"
shift
case "$svc $*" in
    "api "*"k.halt(sys.argv"*|"api "*"skopaq.cli.main halt"*)
        mkdir -p "$(dirname "$SKOPAQ_HALT_FILE")"
        cat "$OPERATOR_HALT" > "$SKOPAQ_HALT_FILE" ;;
esac
exit 0
"""

# An operator halts from another machine (the host's MCP server, Railway): only the Supabase
# row changes, not this stack's halt file.
REMOTE_HALTS_MID_PROBE = """#!/bin/sh
svc="$1"
shift
case "$svc $*" in
    "scheduler "*kill_switch*)
        printf '{"trading_halt": %s}' "$(cat "$OPERATOR_HALT")" > "$FAKE_SUPABASE" ;;
esac
exit 0
"""

# sitecustomize: kill_switch._flags returns a fake system_flags repository backed by
# $FAKE_SUPABASE; every read fails while $FAKE_SUPABASE.down exists.
FAKE_SUPABASE = """
import importlib.abc, importlib.machinery, json, os, sys

class Flags:
    path = os.environ["FAKE_SUPABASE"]

    def get(self, key):
        if os.path.exists(self.path + ".down"):
            raise ConnectionError("fake Supabase: read timed out")
        if not os.path.exists(self.path):
            return None
        with open(self.path) as f:
            return json.load(f).get(key)

    def set(self, key, value):
        data = {}
        if os.path.exists(self.path):
            with open(self.path) as f:
                data = json.load(f)
        data[key] = value
        with open(self.path, "w") as f:
            json.dump(data, f)

class Loader(importlib.abc.Loader):
    def __init__(self, inner):
        self.inner = inner

    def create_module(self, spec):
        return self.inner.create_module(spec)

    def exec_module(self, module):
        self.inner.exec_module(module)
        module._flags = lambda config: Flags()

class Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if name != "skopaq.execution.kill_switch":
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec is not None:
            spec.loader = Loader(spec.loader)
        return spec

if os.environ.get("FAKE_SUPABASE"):
    sys.meta_path.insert(0, Finder())
"""

OPERATOR = {"halted": True, "reason": "operator: broker glitch, do not trade",
            "since": "2026-09-25T09:21:40+00:00", "by": "cli"}


@pytest.fixture
def stack(tmp_path):
    """``stack.run(*args, hook="", supabase=False)`` -> (process, docker calls);
    ``stack.halt_file``; ``stack.supabase`` (the fake row's JSON file)."""
    if shutil.which("bash") is None:
        pytest.skip("bash is not available")
    repo = tmp_path / "repo"
    (repo / "scripts" / "macmini").mkdir(parents=True)
    shutil.copy(VERIFY, repo / "scripts" / "macmini" / "verify.sh")
    (repo / ".env").write_text("SKOPAQ_TRADING_MODE=paper\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER)
    (bin_dir / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (bin_dir / "operator-halts").write_text(OPERATOR_HALTS_MID_PROBE)
    (bin_dir / "remote-halts").write_text(REMOTE_HALTS_MID_PROBE)
    (bin_dir / "halts-first").write_text(OPERATOR_HALTS_BEFORE_THE_PROBE)
    for tool in ("docker", "python", "operator-halts", "remote-halts", "halts-first"):
        (bin_dir / tool).chmod(0o755)
    site = tmp_path / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(FAKE_SUPABASE)
    supabase = tmp_path / "supabase.json"
    operator_halt = tmp_path / "operator-halt.json"
    operator_halt.write_text(json.dumps(OPERATOR))
    halt_file = tmp_path / "home" / ".skopaq" / "HALT"
    calls = tmp_path / "docker-calls.log"

    def run(*args: str, hook: str = "", with_supabase: bool = False):
        env = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": f"{site}{os.pathsep}{ROOT}" if with_supabase else str(ROOT),
            "HOME": str(tmp_path / "home"),
            "SKOPAQ_HALT_FILE": str(halt_file),
            "FAKE_DOCKER_LOG": str(calls),
            "FAKE_DOCKER_HOOK": str(bin_dir / hook) if hook else "",
            "OPERATOR_HALT": str(operator_halt),
            "FAKE_SUPABASE": str(supabase) if with_supabase else "",
        }
        env.pop("SKOPAQ_TRADING_HALTED", None)
        proc = subprocess.run(["bash", str(repo / "scripts" / "macmini" / "verify.sh"), *args],
                              capture_output=True, text=True, env=env, timeout=180)
        return proc, calls.read_text().splitlines() if calls.exists() else []

    return SimpleNamespace(run=run, halt_file=halt_file, supabase=supabase)


def _probe_lines(proc) -> list[str]:
    return [line for line in proc.stdout.splitlines() if "kill-switch probe" in line]


def _api_execs(calls) -> int:
    return sum(1 for c in calls if c.startswith("api "))


def test_probe_skips_when_trading_is_already_halted(stack):
    stack.halt_file.parent.mkdir(parents=True)
    stack.halt_file.write_text(json.dumps(OPERATOR))

    proc, calls = stack.run("--probe-kill-switch", "--force")

    lines = _probe_lines(proc)
    assert len(lines) == 1, proc.stdout
    assert lines[0].startswith("WARN  kill-switch probe: skipped")
    assert "operator: broker glitch" in lines[0]
    assert json.loads(stack.halt_file.read_text()) == OPERATOR  # left exactly as it was
    assert _api_execs(calls) == 1, calls  # the read only: no resume step


def test_probe_halts_and_resumes_its_own_halt(stack):
    proc, calls = stack.run("--probe-kill-switch", "--force")

    lines = _probe_lines(proc)
    assert "PASS  kill-switch probe: the scheduler sees the halt set in api" in lines, proc.stdout
    assert "PASS  kill-switch probe: resumed" in lines, proc.stdout
    assert not stack.halt_file.exists()
    assert any("k.halt(sys.argv[1]" in c for c in calls), calls


def test_probe_with_supabase_halts_and_resumes_its_own_halt(stack):
    proc, _ = stack.run("--probe-kill-switch", "--force", with_supabase=True)

    lines = _probe_lines(proc)
    assert "PASS  kill-switch probe: recorded in Supabase" in lines, proc.stdout
    assert "PASS  kill-switch probe: resumed" in lines, proc.stdout
    assert not stack.halt_file.exists()
    row = json.loads(stack.supabase.read_text())["trading_halt"]
    assert row["halted"] is False and row["by"] == "verify.sh probe"


def test_probe_skips_when_only_supabase_is_halted(stack):
    stack.supabase.write_text(json.dumps({"trading_halt": OPERATOR}))

    proc, _ = stack.run("--probe-kill-switch", "--force", with_supabase=True)

    lines = _probe_lines(proc)
    assert len(lines) == 1 and lines[0].startswith("WARN  kill-switch probe: skipped"), \
        proc.stdout
    assert "(supabase)" in lines[0] and "operator: broker glitch" in lines[0]
    assert json.loads(stack.supabase.read_text()) == {"trading_halt": OPERATOR}
    assert not stack.halt_file.exists()


def test_probe_does_not_run_when_supabase_cannot_be_read(stack):
    """kill_switch.status() takes an unreadable Supabase for "not halted": the probe would
    then overwrite a halt set there (the host's MCP server halts only there) and lift it."""
    stack.supabase.write_text(json.dumps({"trading_halt": OPERATOR}))
    Path(f"{stack.supabase}.down").touch()

    proc, calls = stack.run("--probe-kill-switch", "--force", with_supabase=True)

    lines = _probe_lines(proc)
    assert len(lines) == 1, proc.stdout
    assert lines[0].startswith("FAIL  kill-switch probe: skipped: cannot read the halt state")
    assert "read timed out" in lines[0]
    assert json.loads(stack.supabase.read_text()) == {"trading_halt": OPERATOR}
    assert not stack.halt_file.exists()
    assert _api_execs(calls) == 1, calls
    assert proc.returncode == 1


def test_a_halt_set_just_before_the_probe_halts_is_kept(stack):
    proc, _ = stack.run("--probe-kill-switch", "--force", hook="halts-first")

    lines = _probe_lines(proc)
    assert len(lines) == 1 and lines[0].startswith("WARN  kill-switch probe: skipped"), \
        proc.stdout
    assert json.loads(stack.halt_file.read_text()) == OPERATOR


def test_probe_lifts_only_its_own_halt_when_another_machine_halts_meanwhile(stack):
    proc, _ = stack.run("--probe-kill-switch", "--force", hook="remote-halts",
                        with_supabase=True)

    lines = _probe_lines(proc)
    assert any(line.startswith("WARN  kill-switch probe: not resumed") for line in lines), \
        proc.stdout
    assert "operator: broker glitch" in proc.stdout
    assert json.loads(stack.supabase.read_text()) == {"trading_halt": OPERATOR}
    assert not stack.halt_file.exists()  # the probe's own halt file does not linger


def test_probe_that_cannot_halt_says_so_and_resumes_nothing(stack):
    stack.halt_file.parent.parent.mkdir(parents=True)
    stack.halt_file.parent.write_text("")  # ~/.skopaq is a file: no halt file can be written,
    # and there is no Supabase

    proc, calls = stack.run("--probe-kill-switch", "--force")

    lines = _probe_lines(proc)
    assert len(lines) == 1, proc.stdout
    assert lines[0].startswith("FAIL  kill-switch probe: could not halt in api")
    assert "already gone" not in proc.stdout
    assert _api_execs(calls) == 1, calls


def test_probe_keeps_a_halt_set_while_it_runs(stack):
    proc, _ = stack.run("--probe-kill-switch", "--force", hook="operator-halts")

    lines = _probe_lines(proc)
    assert any(line.startswith("WARN  kill-switch probe: not resumed") for line in lines), \
        proc.stdout
    assert "operator: broker glitch" in proc.stdout
    assert json.loads(stack.halt_file.read_text()) == OPERATOR


def test_verify_sh_parses_and_stays_bash_3_2_compatible():
    """macOS ships bash 3.2: no bash 4+ syntax (associative arrays, mapfile, case
    modification, ;& fall-through, |&, &>>, coproc, -v tests, nameref, EPOCHSECONDS)."""
    if shutil.which("bash") is None:
        pytest.skip("bash is not available")
    parsed = subprocess.run(["bash", "-n", str(VERIFY)], capture_output=True, text=True)
    assert parsed.returncode == 0, parsed.stderr
    bash4 = re.compile(
        r"declare\s+-[a-zA-Z]*[An]|local\s+-n|\bmapfile\b|\breadarray\b|\$\{\w+(,,|\^\^|,|\^)\}"
        r"|;;&|;&|\|&|&>>|\bcoproc\b|\[\[\s+-v\s|\bEPOCH(SECONDS|REALTIME)\b|\bwait\s+-n\b"
    )
    offending = [f"{n}: {line}" for n, line in enumerate(VERIFY.read_text().splitlines(), 1)
                 if not line.lstrip().startswith("#") and bash4.search(line)]
    assert offending == []
