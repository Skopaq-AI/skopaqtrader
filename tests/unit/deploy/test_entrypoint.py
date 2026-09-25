"""docker/entrypoint.sh: service routing, argument passthrough and a clean MCP stdout."""

from __future__ import annotations

CLI = ["-m", "skopaq.cli.main"]


def test_api_serves_on_port_8000_by_default(run_entrypoint):
    argv, proc = run_entrypoint("api")
    assert proc.returncode == 0
    assert argv == CLI + ["serve", "--host", "0.0.0.0", "--port", "8000"]


def test_api_honours_port(run_entrypoint):
    argv, _ = run_entrypoint("api", env={"PORT": "9999"})
    assert argv == CLI + ["serve", "--host", "0.0.0.0", "--port", "9999"]


def test_no_arguments_starts_the_api(run_entrypoint):
    argv, _ = run_entrypoint()
    assert argv == CLI + ["serve", "--host", "0.0.0.0", "--port", "8000"]


def test_mcp_stdout_carries_only_the_server(run_entrypoint):
    argv, proc = run_entrypoint("mcp")
    assert argv == ["-m", "skopaq.mcp_server"]
    # Nothing but the (fake) server's own output: a banner would corrupt JSON-RPC.
    assert proc.stdout == "ARG:-m\nARG:skopaq.mcp_server\n"
    assert proc.stderr == ""


def test_daemon_passes_extra_arguments(run_entrypoint):
    argv, _ = run_entrypoint("daemon", "--dry-run")
    assert argv == CLI + ["daemon", "--once", "--paper", "--dry-run"]


def test_daemon_live(run_entrypoint):
    argv, _ = run_entrypoint("daemon-live")
    assert argv == CLI + ["daemon", "--once", "--live", "--confirm-live"]


def test_scheduler(run_entrypoint):
    argv, proc = run_entrypoint("scheduler")
    assert argv == CLI + ["schedule"]
    assert "SkopaqTrader:" in proc.stderr  # log lines go to stderr, never stdout
    assert "SkopaqTrader" not in proc.stdout


def test_unknown_words_go_to_the_cli_with_arguments_intact(run_entrypoint):
    argv, _ = run_entrypoint("halt", "two words")
    assert argv == CLI + ["halt", "two words"]


def test_programs_are_executed_directly(run_entrypoint):
    argv, _ = run_entrypoint("python", "-c", "pass")
    assert argv == ["-c", "pass"]


def test_railway_start_command_passes_through(run_entrypoint):
    argv, _ = run_entrypoint("python", "-m", "skopaq.cli.main", "daemon", "--once")
    assert argv == CLI + ["daemon", "--once"]
