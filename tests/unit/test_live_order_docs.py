"""Live order confirmation: .env.example, the docs and CLAUDE.md say what the code does.

- .env.example and docs/trading/live-trading.md list every live order setting
  (skopaq/config.py, "Live order confirmation", plus ``monitor_resync_cycles``) with its
  real default, and the example values load without being clamped.
- docs/indstocks_api.md (CLAUDE.md sends agents there) covers every endpoint the INDstocks
  client calls, and its status table agrees with ``skopaq.broker.order_status``.
- Every repo file CLAUDE.md names exists.

Files the image leaves out (docs/) are skipped when absent, so this also runs inside the
image (scripts/macmini/verify.sh --unit-tests).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from skopaq.broker.order_status import classify, normalise_status
from skopaq.config import SkopaqConfig
from skopaq.execution.live_orders import FillSettings

ROOT = Path(__file__).resolve().parents[2]

# The "Live order confirmation" settings: every order_* field, the two flags and the
# monitor's resync interval
LIVE_ORDER_FIELDS = sorted(
    [name for name in SkopaqConfig.model_fields if name.startswith("order_")]
    + ["allow_sell_without_order_book", "indstocks_order_remarks_enabled",
       "monitor_resync_cycles"]
)
# Commented out in .env.example: the defaults are right on the compose home volume
COMMENTED_OUT = {"order_lock_dir", "order_journal_dir"}

# INDstocks' 15 documented REST order statuses
DOCUMENTED_STATUSES = (
    "QUEUED", "O-PENDING", "SL-PENDING", "PROCESSING", "ABORTED", "INITIATED", "SUCCESS",
    "CANCELLED", "MODIFIED", "PENDING", "EXPIRED", "FAILED", "PARTIALLY FILLED",
    "PARTIALLY FILLED - CANCELLED", "PARTIALLY FILLED - EXPIRED",
)

_SECTION = "# ===== Live order confirmation (INDstocks) ====="


def _require(path: Path) -> Path:
    if not path.exists():
        pytest.skip(f"{path.relative_to(ROOT)} is not available here")
    return path


def _env(name: str) -> str:
    return f"SKOPAQ_{name.upper()}"


def _default(name: str):
    return SkopaqConfig.model_fields[name].default


def _env_example_section() -> list[str]:
    """The lines of .env.example's live order section (header excluded)."""
    lines = _require(ROOT / ".env.example").read_text().splitlines()
    assert _SECTION in lines, f"{_SECTION!r} missing from .env.example"
    start = lines.index(_SECTION) + 1
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("# =====")),
               len(lines))
    return lines[start:end]


def _assignments(lines: list[str]) -> dict[str, tuple[str, bool, int]]:
    """``KEY -> (value, commented out, line index)`` for ``KEY=value`` and ``# KEY=value``."""
    found = {}
    for i, line in enumerate(lines):
        match = re.fullmatch(r"(#\s*)?(SKOPAQ_[A-Z0-9_]+)=(.*)", line.strip())
        if match:
            found[match.group(2)] = (match.group(3).strip(), bool(match.group(1)), i)
    return found


def _same(text: str, default) -> bool:
    """Does a documented value (``30``, ``false``, ``""``, ``~/.skopaq/locks``) equal it?"""
    text = text.strip().strip("`")
    if isinstance(default, bool):
        return text.lower() == str(default).lower()
    if isinstance(default, (int, float)):
        try:
            return float(text) == float(default)
        except ValueError:
            return False
    if default == "":
        return text in ("", '""', "(empty)")
    return text == default


# ── .env.example ──────────────────────────────────────────────────────────────


def test_env_example_section_follows_trading_mode():
    headers = [line for line in _require(ROOT / ".env.example").read_text().splitlines()
               if line.startswith("# =====")]
    trading_mode = next(i for i, h in enumerate(headers) if "Trading Mode" in h)
    assert headers[trading_mode + 1] == _SECTION


def test_env_example_lists_every_live_order_setting_with_its_default():
    found = _assignments(_env_example_section())
    for name in LIVE_ORDER_FIELDS:
        key = _env(name)
        assert key in found, f"{key} missing from the live order section of .env.example"
        value, commented, _ = found[key]
        assert _same(value, _default(name)), f"{key}={value} but the default is {_default(name)!r}"
        assert commented is (name in COMMENTED_OUT), key
    assert set(found) == {_env(name) for name in LIVE_ORDER_FIELDS}


def test_env_example_flags_the_order_book_override_as_danger():
    lines = _env_example_section()
    value, commented, index = _assignments(lines)["SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK"]
    assert value == "false" and not commented
    comment = []
    for line in reversed(lines[:index]):  # the comment block just above it
        if not line.startswith("#") or "=" in line:
            break
        comment.append(line)
    assert "DANGER" in " ".join(comment)


def test_env_example_values_load_as_the_defaults_without_clamping(monkeypatch, caplog):
    for key, (value, commented, _) in _assignments(_env_example_section()).items():
        if not commented:
            monkeypatch.setenv(key, value)
    config = SkopaqConfig(_env_file=None)
    for name in LIVE_ORDER_FIELDS:
        if name not in COMMENTED_OUT:  # tests point those at tmp_path (tests/conftest.py)
            assert getattr(config, name) == _default(name), name

    with caplog.at_level(logging.WARNING, logger="skopaq.execution.live_orders"):
        settings = FillSettings.from_config(config)
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []  # nothing clamped, nothing shortened to fit
    assert settings == FillSettings()  # the built-in defaults


# ── docs/trading/live-trading.md ─────────────────────────────────────────────


def test_live_trading_doc_tables_every_live_order_setting_with_its_default():
    text = _require(ROOT / "docs" / "trading" / "live-trading.md").read_text()
    for name in LIVE_ORDER_FIELDS:
        row = re.search(rf"^\|\s*`{_env(name)}`\s*\|([^|]*)\|", text, re.M)
        assert row, f"{_env(name)} missing from the configuration table in live-trading.md"
        assert _same(row.group(1), _default(name)), (
            f"live-trading.md gives {_env(name)} = {row.group(1).strip()}, "
            f"the default is {_default(name)!r}")


def test_live_trading_doc_no_longer_says_accepted_orders_count_as_filled():
    text = _require(ROOT / "docs" / "trading" / "live-trading.md").read_text()
    assert "treats an order the broker accepted as filled" not in text
    assert "route to KiteClient" not in text  # the live broker is INDstocks


# ── docs/indstocks_api.md ────────────────────────────────────────────────────


def _client_paths() -> set[str]:
    """Every path INDstocksClient requests, as written (``/trades/{order_id}``)."""
    source = (ROOT / "skopaq" / "broker" / "client.py").read_text()
    paths = set(re.findall(r'"(?:GET|POST)",\s*f?"(/[^"]*)"', source))
    assert {"/order", "/order-book", "/portfolio/positions"} <= paths  # the regex still works
    return paths


def test_indstocks_reference_covers_every_endpoint_the_client_calls():
    text = _require(ROOT / "docs" / "indstocks_api.md").read_text()
    missing = [
        path for path in sorted(_client_paths())
        if not re.search(rf"(?<![\w/-]){re.escape(path)}(?![\w/-])", text)
    ]
    assert missing == [], f"docs/indstocks_api.md does not document {missing}"


def test_indstocks_status_table_matches_the_parser():
    text = _require(ROOT / "docs" / "indstocks_api.md").read_text()
    rows = dict(re.findall(r"^\|\s*`([A-Z][A-Z -]*[A-Z])`\s*\|\s*`?(\w+)`?\s*\|", text, re.M))
    for status in DOCUMENTED_STATUSES:
        assert status in rows, f"{status} missing from the status table in docs/indstocks_api.md"
        assert rows[status] == classify(normalise_status(status), None, None).value, status


# ── CLAUDE.md ────────────────────────────────────────────────────────────────


def test_every_file_claude_md_names_exists():
    text = _require(ROOT / "CLAUDE.md").read_text()
    named = set(re.findall(r"\b((?:skopaq|docs|scripts|tests)/[\w./-]*\w\.(?:py|md|sh))", text))
    assert "docs/indstocks_api.md" in named
    if not (ROOT / "docs").exists():
        named = {path for path in named if not path.startswith("docs/")}
    missing = sorted(path for path in named if not (ROOT / path).is_file())
    assert missing == [], f"CLAUDE.md names files that do not exist: {missing}"


def test_claude_md_describes_live_fills_and_the_monitor_exit_code():
    text = _require(ROOT / "CLAUDE.md").read_text()
    assert "live fills are not yet confirmed" not in text
    monitor = next(line for line in text.splitlines() if line.startswith("skopaq monitor"))
    assert "exits 4" in monitor
    # rc 4 whenever it ends with positions open or orders unconfirmed, not only after
    # the close (a stop ends it earlier)
    assert "orders unconfirmed" in monitor
    assert "positions are still open after the close" not in text
    for name in ("order_status", "_request_envelope", "read_broker_snapshot",
                 "resolve_tick_size", "SKOPAQ_ALLOW_SELL_WITHOUT_ORDER_BOOK"):
        assert name in text, name


# ── What reaches a broker (Kite) ──────────────────────────────────────────────


def _kite_order_tools() -> list[str]:
    """MCP tools that place orders on the Zerodha account through Kite: each calls
    ``_get_kite()`` and an order-placing helper (``skopaq.trading.advanced_orders``'s
    place_*/buy_option/trade_futures, or ``skopaq.options.gtt``'s place_gtt_*)."""
    import ast

    source = (ROOT / "skopaq" / "mcp_server.py").read_text()
    placing = re.compile(r"^(place_|buy_option$|trade_futures$)")
    tools = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        called = {n.func.id for n in ast.walk(node)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        imported = {alias.name for n in ast.walk(node) if isinstance(n, ast.ImportFrom)
                    and n.module in ("skopaq.trading.advanced_orders", "skopaq.options.gtt")
                    for alias in n.names}
        if "_get_kite" in called and any(placing.match(name) for name in imported):
            tools.append(node.name)
    assert {"place_amo_order", "place_gtt_order", "place_basket"} <= set(tools)  # still found
    return sorted(tools)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


def test_docs_never_say_kite_does_not_place_orders():
    live = _require(ROOT / "docs" / "trading" / "live-trading.md").read_text()
    for claim in ("never orders", "for market data only", "Kite Connect Setup (market data"):
        assert claim not in live, f"live-trading.md still says Kite: {claim!r}"
    mac = _require(ROOT / "docs" / "deployment" / "mac-mini.md").read_text()
    assert "nothing it does reaches the broker's order book" not in mac


def test_the_residual_limits_name_every_mcp_tool_that_places_real_kite_orders():
    live = _require(ROOT / "docs" / "trading" / "live-trading.md").read_text()
    limits = _section(live, "## Residual limits")
    missing = [tool for tool in _kite_order_tools() if f"`{tool}`" not in limits]
    assert missing == [], f"live-trading.md's residual limits do not name {missing}"
    assert "SKOPAQ_TRADING_MODE" in limits and "SafetyChecker" in limits
    mac = _require(ROOT / "docs" / "deployment" / "mac-mini.md").read_text()
    mcp = _section(mac, "## 13.")
    missing = [tool for tool in _kite_order_tools() if f"`{tool}`" not in mcp]
    assert missing == [], f"mac-mini.md section 13 does not name {missing}"


# ── Commands the live docs tell the operator to run ──────────────────────────


def _cli_options() -> dict[str, set[str]]:
    import typer

    from skopaq.cli.main import app

    group = typer.main.get_command(app)
    return {name: {opt for p in cmd.params for opt in (*p.opts, *p.secondary_opts)}
            for name, cmd in group.commands.items()}


def test_live_trading_doc_commands_exist():
    text = _require(ROOT / "docs" / "trading" / "live-trading.md").read_text()
    options = _cli_options()
    for line in re.findall(r"^(?:\S+=\S+\s+)*skopaq \S.*$", text, re.M):
        words = line.split("#")[0].split()
        words = words[words.index("skopaq"):]
        assert words[1] in options, f"live-trading.md runs a missing command: {line}"
        for flag in (w for w in words[2:] if w.startswith("--")):
            assert flag in options[words[1]], f"`skopaq {words[1]}` has no {flag}: {line}"
    # The kill switch is `skopaq halt` / Telegram /halt; there is no /stop or API route
    kill = _section(text, "## Kill Switch")
    assert "skopaq halt" in kill and "/halt" in kill
    assert "/stop" not in kill and "/api/kill-switch" not in kill


def test_the_lag_window_is_not_documented_as_the_unshown_buy_wait():
    stale = "waits for a confirmed BUY to show, this long"
    assert stale not in _require(ROOT / "docs" / "trading" / "live-trading.md").read_text()
    assert stale not in _require(ROOT / ".env.example").read_text().replace("\n# ", " ")


def test_live_trading_doc_says_progress_is_booked_when_seen_and_once():
    text = _require(ROOT / "docs" / "trading" / "live-trading.md").read_text()
    assert "recorded once the order is final" not in text
    assert "booked as soon as a resume reads it" in text
    assert "each share is booked once" in text


def test_live_trading_doc_does_not_promise_an_api_failure_shutdown():
    # SafetyRules.auto_shutdown_on_api_failure_minutes is declared but nothing enforces it
    text = _require(ROOT / "docs" / "trading" / "live-trading.md").read_text()
    assert "automatically shuts down if the API fails" not in text
