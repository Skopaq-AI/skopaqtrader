"""Deployment contract: compose services, entrypoint targets, Dockerfile, .dockerignore,
CI Python coverage, the Railway cron in IST and the runbook links.

Files the image leaves out (docs/, .github/) are skipped when absent, so this also runs
inside the image (scripts/macmini/verify.sh --unit-tests).
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import tomllib
from pathlib import Path

import pytest
import typer.main
import yaml

from skopaq import healthcheck
from skopaq.config import SkopaqConfig

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SERVICES = {"api", "telegram", "scheduler"}


def _require(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"{path.relative_to(ROOT)} is not available here")
    return path


@pytest.fixture(scope="module")
def services() -> dict:
    return yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]


def _default(services: dict) -> dict:
    return {name: svc for name, svc in services.items() if not svc.get("profiles")}


def _seconds(duration: str) -> float:
    units = {"h": 3600, "m": 60, "s": 1, "ms": 0.001}
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|h|m|s)", str(duration))
    assert parts, f"unparseable duration {duration!r}"
    return sum(float(n) * units[u] for n, u in parts)


def _dockerfile() -> list[tuple[str, str]]:
    """(INSTRUCTION, arguments) pairs, with line continuations joined."""
    instructions, current = [], ""
    for raw in (ROOT / "Dockerfile").read_text().splitlines():
        line = raw.strip()
        if not current and (not line or line.startswith("#")):
            continue
        if line.endswith("\\"):
            current += line[:-1] + " "
            continue
        current += line
        keyword, _, args = current.partition(" ")
        instructions.append((keyword.upper(), args.strip()))
        current = ""
    return instructions


def _ci() -> dict:
    return yaml.safe_load(_require(ROOT / ".github" / "workflows" / "ci.yml").read_text())


# ── docker-compose.yml ───────────────────────────────────────────────────────


def test_default_services_are_the_always_on_stack(services):
    default = _default(services)
    assert set(default) == DEFAULT_SERVICES
    for name, svc in default.items():
        assert svc.get("restart") == "unless-stopped", name
        assert svc.get("init") is True, name
        health = svc.get("healthcheck") or {}
        assert health.get("test") and health["test"][0] != "NONE", name
        assert health.get("disable") is not True, name
        assert svc["environment"]["TZ"] == "Asia/Kolkata", name
        assert {"skopaq-home:/home/skopaq", "skopaq-data:/data"} <= set(svc["volumes"]), name
        assert svc["logging"]["options"]["max-size"], name
        assert svc["image"] == "skopaqtrader:local", name


def test_api_is_published_on_loopback_only(services):
    assert services["api"]["ports"] == ["127.0.0.1:8000:8000"]


def test_scheduler_grace_period_outlasts_the_kill_after(services):
    kill_after = int(SkopaqConfig.model_fields["scheduler_kill_after_seconds"].default)
    assert _seconds(services["scheduler"]["stop_grace_period"]) > kill_after


def test_profile_services_do_not_restart(services):
    for name, svc in services.items():
        if svc.get("profiles"):
            assert "restart" not in svc, name


def test_healthchecks_use_real_checks(services):
    for name, svc in services.items():
        test = (svc.get("healthcheck") or {}).get("test") or []
        if "skopaq.healthcheck" not in test:
            continue
        args = test[test.index("skopaq.healthcheck") + 1:]
        assert args[0] in ("api", "heartbeat"), name
        if args[0] == "heartbeat":
            assert args[1] == svc["environment"]["SKOPAQ_HEARTBEAT_FILE"], name
            # a missing file is "unhealthy" (1), never a usage error (2)
            assert healthcheck.main(args) == 1, name


def _cli_commands() -> set[str]:
    from skopaq.cli.main import app

    return set(typer.main.get_command(app).commands)


def _assert_target_exists(argv: list[str], what: str) -> None:
    assert argv[:1] == ["-m"], f"{what}: python was not run as a module: {argv}"
    if argv[1] == "skopaq.cli.main":
        assert argv[2] in _cli_commands(), f"{what}: no `skopaq {argv[2]}` command"
    else:
        assert importlib.util.find_spec(argv[1]) is not None, f"{what}: no module {argv[1]}"


def test_every_service_command_resolves(services, run_entrypoint):
    for name, svc in services.items():
        entrypoint = svc.get("entrypoint") or []
        command = svc.get("command") or []
        if isinstance(command, str):
            command = shlex.split(command)
        args = list(entrypoint[1:]) + list(command)
        argv, proc = run_entrypoint(*args)
        assert proc.returncode == 0, name
        _assert_target_exists(argv, f"compose service {name}")


def test_fly_and_railway_commands_resolve(run_entrypoint):
    fly = tomllib.loads((ROOT / "fly-telegram.toml").read_text())
    argv, _ = run_entrypoint(*shlex.split(fly["processes"]["app"]))
    _assert_target_exists(argv, "fly-telegram.toml")
    for name in ("railway.toml", "railway-daemon.toml"):
        start = tomllib.loads((ROOT / name).read_text())["deploy"]["startCommand"]
        argv, _ = run_entrypoint(*shlex.split(start))
        _assert_target_exists(argv, name)


# ── Dockerfile / .dockerignore ───────────────────────────────────────────────


def test_dockerfile_contract():
    instructions = _dockerfile()
    runs = [args for keyword, args in instructions if keyword == "RUN"]
    for run in runs:
        unquoted = re.sub(r"\"[^\"]*\"|'[^']*'", "", run)
        assert not re.search(r"\S>=", unquoted), f"unquoted >= (a redirection): {run}"
    users = [args for keyword, args in instructions if keyword == "USER"]
    assert users and users[-1] not in ("root", "0")
    workdirs = [args for keyword, args in instructions if keyword == "WORKDIR"]
    assert workdirs[-1] == "/home/skopaq"
    assert any("chown" in r and "/data" in r and "/home/skopaq" in r for r in runs)
    assert all(keyword != "HEALTHCHECK" for keyword, _ in instructions)
    assert any(".[deploy]" in r for r in runs)


def test_image_python_is_tested_in_ci():
    text = (ROOT / "Dockerfile").read_text()
    minor = re.search(r"python:(\d+\.\d+)", text).group(1)
    assert minor in _ci()["jobs"]["unit"]["strategy"]["matrix"]["python"]


def test_dockerignore_keeps_secrets_out_and_code_in():
    lines = [
        line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.startswith("#")
    ]
    # **/ so the pattern also matches in subdirectories (without it, only at the root)
    for pattern in (
        "**/.env*", "!.env.example", "**/.envrc", "**/.skopaq/", "**/.cloudflared/",
        "**/.pypirc", "**/*.pem", "**/*.key", "**/__pycache__", "**/*.py[co]", "**/*.log",
        "**/*.db", "**/*.sqlite*", "**/.DS_Store", "**/data_cache/",
    ):
        assert pattern in lines, pattern
    needed = {"skopaq", "tradingagents", "cli", "docker", "pyproject.toml", "README.md"}
    for line in lines:
        if not line.startswith("!"):
            assert line.strip("/") not in needed, f".dockerignore excludes {line}"


# ── Railway / pyproject / docs ───────────────────────────────────────────────


def test_railway_builds_the_root_image_and_cron_matches_the_scheduler():
    for name in ("railway.toml", "railway-daemon.toml"):
        build = tomllib.loads((ROOT / name).read_text())["build"]
        assert (ROOT / build["dockerfilePath"]).is_file(), name
    cron = tomllib.loads((ROOT / "railway-daemon.toml").read_text())["deploy"]["cronSchedule"]
    minute, hour, _, _, dow = cron.split()
    assert dow == "1-5"
    ist = (int(hour) * 60 + int(minute) + 330) % (24 * 60)  # Railway cron is UTC
    assert f"{ist // 60:02d}:{ist % 60:02d}" == SkopaqConfig.model_fields["scheduler_start"].default


def test_deploy_extra_has_a_python_314_safe_telegram_bot():
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"][
        "optional-dependencies"
    ]["deploy"]
    ptb = [d for d in extras if d.startswith("python-telegram-bot[job-queue]")]
    assert ptb, extras
    major, minor = map(int, re.search(r">=(\d+)\.(\d+)", ptb[0]).groups())
    assert (major, minor) >= (22, 4)


def test_runbook_is_linked():
    # mkdocs.yml has !!python tags that yaml.safe_load refuses: check the text.
    assert "deployment/mac-mini.md" in (ROOT / "mkdocs.yml").read_text()
    assert "docs/deployment/mac-mini.md" in (ROOT / "README.md").read_text()
    assert _require(ROOT / "docs" / "deployment" / "mac-mini.md").is_file()


def test_no_hard_coded_fly_urls_in_code():
    offenders = [
        str(p.relative_to(ROOT)) for p in (ROOT / "skopaq").rglob("*.py")
        if "fly.dev" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []
