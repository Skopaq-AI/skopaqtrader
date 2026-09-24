"""`skopaq memory legacy` — show, export and delete pre-v0.5.1 memory rows."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from skopaq.cli.main import app
from skopaq.db.models import AgentMemoryRecord

runner = CliRunner()


@pytest.fixture
def store():
    store = MagicMock()
    store.legacy_records.return_value = [
        AgentMemoryRecord(role="bull_memory", documents=["d1", "d2"], recommendations=["r1", "r2"]),
        AgentMemoryRecord(role="trader_memory", documents=["d3"], recommendations=["r3"]),
    ]
    store.delete_legacy.return_value = 2
    with patch("skopaq.cli.main._create_memory_store", return_value=store), \
         patch("skopaq.config.SkopaqConfig"):
        yield store


def test_shows_rows_without_changing_anything(store):
    result = runner.invoke(app, ["memory", "legacy"])

    assert result.exit_code == 0, result.output
    assert "bull_memory" in result.output and "trader_memory" in result.output
    store.delete_legacy.assert_not_called()


def test_export_writes_every_row(store, tmp_path):
    out = tmp_path / "legacy.json"
    result = runner.invoke(app, ["memory", "legacy", "--export", str(out)])

    assert result.exit_code == 0, result.output
    rows = json.loads(out.read_text())["rows"]
    assert [(r["role"], r["recommendations"]) for r in rows] == [
        ("bull_memory", ["r1", "r2"]), ("trader_memory", ["r3"]),
    ]
    store.delete_legacy.assert_not_called()


def test_delete_exports_first_then_asks(store, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["memory", "legacy", "--delete"], input="y\n")

    assert result.exit_code == 0, result.output
    backups = list(tmp_path.glob("legacy-memories-*.json"))
    assert len(backups) == 1
    assert len(json.loads(backups[0].read_text())["rows"]) == 2
    store.delete_legacy.assert_called_once_with(["bull_memory", "trader_memory"])


def test_declining_the_prompt_deletes_nothing(store, tmp_path):
    out = tmp_path / "backup.json"
    result = runner.invoke(app, ["memory", "legacy", "--delete", "--export", str(out)], input="n\n")

    assert result.exit_code == 0, result.output
    assert out.exists()
    store.delete_legacy.assert_not_called()


def test_yes_skips_the_prompt(store, tmp_path):
    out = tmp_path / "backup.json"
    result = runner.invoke(app, ["memory", "legacy", "--delete", "--yes", "--export", str(out)])

    assert result.exit_code == 0, result.output
    store.delete_legacy.assert_called_once()


def test_without_supabase_exits_with_an_error():
    with patch("skopaq.cli.main._create_memory_store", return_value=None), \
         patch("skopaq.config.SkopaqConfig"):
        result = runner.invoke(app, ["memory", "legacy"])
    assert result.exit_code == 1
