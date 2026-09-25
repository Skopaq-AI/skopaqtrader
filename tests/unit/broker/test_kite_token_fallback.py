"""Kite token: no hidden calls to a Fly deployment; the API fallback only when configured;
a token past its 06:00 IST expiry is never used."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import types
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pydantic import SecretStr


@pytest.fixture
def kite(monkeypatch, tmp_path):
    if importlib.util.find_spec("kiteconnect") is None:  # a deploy extra, not a dev one
        fake = types.ModuleType("kiteconnect")
        fake.KiteConnect = object
        monkeypatch.setitem(sys.modules, "kiteconnect", fake)
    sys.modules.pop("skopaq.broker.kite_client", None)
    module = importlib.import_module("skopaq.broker.kite_client")
    monkeypatch.setattr(module, "_access_token", "")
    monkeypatch.setattr(module, "_TOKEN_FILE", str(tmp_path / "missing" / "token.json"))
    # "" and restored afterwards: set_access_token() writes this variable.
    monkeypatch.setenv("SKOPAQ_KITE_ACCESS_TOKEN", "")
    yield module
    sys.modules.pop("skopaq.broker.kite_client", None)


@pytest.fixture
def import_kite(monkeypatch):
    """Import kite_client afresh (it reads SKOPAQ_KITE_TOKEN_FILE at import), untouched."""
    if importlib.util.find_spec("kiteconnect") is None:
        fake = types.ModuleType("kiteconnect")
        fake.KiteConnect = object
        monkeypatch.setitem(sys.modules, "kiteconnect", fake)

    def load():
        sys.modules.pop("skopaq.broker.kite_client", None)
        return importlib.import_module("skopaq.broker.kite_client")

    yield load
    sys.modules.pop("skopaq.broker.kite_client", None)


def test_tests_never_see_the_host_kite_session(import_kite):
    """A real Kite session on the host (the native MCP server writes
    /tmp/skopaq_kite_token.json) must never reach a test: tests/conftest.py points the token
    file at a directory that does not exist, so it is never read and never written."""
    path = import_kite()._TOKEN_FILE
    assert path == os.environ["SKOPAQ_KITE_TOKEN_FILE"]
    assert not os.path.exists(os.path.dirname(path))
    assert os.environ["SKOPAQ_KITE_API_KEY"] == ""  # no KiteClient from a real .env either


def test_token_file_location(import_kite, monkeypatch, tmp_path):
    monkeypatch.setenv("SKOPAQ_KITE_TOKEN_FILE", str(tmp_path / "kite.json"))
    assert import_kite()._TOKEN_FILE == str(tmp_path / "kite.json")

    monkeypatch.delenv("SKOPAQ_KITE_TOKEN_FILE")  # production: unchanged
    data_dir = "/data" if os.path.isdir("/data") else "/tmp"
    assert import_kite()._TOKEN_FILE == os.path.join(data_dir, "skopaq_kite_token.json")


def _config(monkeypatch, api_base_url: str, api_token: str = "", public_base_url: str = "") -> None:
    cfg = SimpleNamespace(
        kite_access_token=SecretStr(""),
        api_base_url=api_base_url,
        api_token=SecretStr(api_token),
        public_base_url=public_base_url,
    )
    monkeypatch.setattr("skopaq.config.SkopaqConfig", lambda: cfg)


def test_no_api_base_url_means_no_http_call(kite, monkeypatch):
    _config(monkeypatch, "")
    get = MagicMock()
    monkeypatch.setattr(httpx, "get", get)

    assert kite.get_access_token() == ""
    get.assert_not_called()


def test_configured_api_base_url_is_used_with_the_bearer_token(kite, monkeypatch):
    _config(monkeypatch, "http://api:8000/", api_token="x")
    response = MagicMock(status_code=200)
    response.json.return_value = {"access_token": "kite-tok"}
    get = MagicMock(return_value=response)
    monkeypatch.setattr(httpx, "get", get)

    assert kite.get_access_token() == "kite-tok"
    url = get.call_args.args[0]
    assert url == "http://api:8000/api/kite/token"
    assert get.call_args.kwargs["headers"] == {"Authorization": "Bearer x"}


# ── Expiry at 06:00 IST ──────────────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))


def test_kite_day_starts_at_six_ist(kite):
    before = datetime(2026, 9, 28, 5, 59, tzinfo=IST)
    at = datetime(2026, 9, 28, 6, 0, tzinfo=IST)
    assert kite._kite_day_start(before) == datetime(2026, 9, 27, 6, 0, tzinfo=IST)
    assert kite._kite_day_start(at) == at
    assert kite._kite_day_start(datetime(2026, 9, 28, 0, 45, tzinfo=timezone.utc)) == at


def _persist(kite, tmp_path, monkeypatch, token: str, set_at: datetime) -> str:
    path = tmp_path / "skopaq_kite_token.json"
    path.write_text(json.dumps({"access_token": token, "set_at": set_at.isoformat()}))
    monkeypatch.setattr(kite, "_TOKEN_FILE", str(path))
    return str(path)


def _yesterday(kite) -> datetime:
    return kite._kite_day_start() - timedelta(minutes=1)  # just before the last 06:00 IST


def test_token_persisted_today_is_used(kite, tmp_path, monkeypatch):
    _config(monkeypatch, "")
    _persist(kite, tmp_path, monkeypatch, "fresh", datetime.now(timezone.utc))
    assert kite.get_access_token() == "fresh"


def test_token_persisted_before_six_ist_is_not_used(kite, tmp_path, monkeypatch):
    _config(monkeypatch, "")
    path = _persist(kite, tmp_path, monkeypatch, "stale", _yesterday(kite))
    # set_access_token() also put it in the env var: that copy is expired too
    monkeypatch.setenv("SKOPAQ_KITE_ACCESS_TOKEN", "stale")

    assert kite.get_access_token() == ""
    assert os.path.exists(path)  # left for the api to overwrite at the next login


def test_file_without_set_at_uses_its_mtime(kite, tmp_path, monkeypatch):
    _config(monkeypatch, "")
    path = tmp_path / "skopaq_kite_token.json"
    path.write_text(json.dumps({"access_token": "old"}))
    old = _yesterday(kite).timestamp()
    os.utime(path, (old, old))
    monkeypatch.setattr(kite, "_TOKEN_FILE", str(path))

    assert kite.get_access_token() == ""


def test_cached_token_from_yesterday_is_dropped(kite, monkeypatch):
    _config(monkeypatch, "")
    monkeypatch.setattr(kite, "_access_token", "stale")
    monkeypatch.setattr(kite, "_access_token_set_at", _yesterday(kite))
    monkeypatch.setenv("SKOPAQ_KITE_ACCESS_TOKEN", "stale")

    assert kite.get_access_token() == ""
    assert kite._access_token == ""
    assert os.environ.get("SKOPAQ_KITE_ACCESS_TOKEN") is None


def test_a_token_set_now_is_cached(kite, tmp_path, monkeypatch):
    _config(monkeypatch, "")
    monkeypatch.setattr(kite, "_TOKEN_FILE", str(tmp_path / "skopaq_kite_token.json"))
    kite.set_access_token("new")

    monkeypatch.setattr(kite, "_access_token", "")  # as a restarted process
    assert kite.get_access_token() == "new"


def test_stale_file_falls_through_to_the_api(kite, tmp_path, monkeypatch):
    _config(monkeypatch, "http://api:8000")
    path = _persist(kite, tmp_path, monkeypatch, "stale", _yesterday(kite))
    response = MagicMock(status_code=200)
    response.json.return_value = {"access_token": "today"}
    monkeypatch.setattr(httpx, "get", MagicMock(return_value=response))

    assert kite.get_access_token() == "today"
    assert json.loads(open(path).read())["access_token"] == "today"


def test_local_lookup_never_calls_the_api(kite, monkeypatch):
    _config(monkeypatch, "http://api:8000")
    get = MagicMock()
    monkeypatch.setattr(httpx, "get", get)

    assert kite.get_access_token(remote=False) == ""
    get.assert_not_called()


@pytest.mark.asyncio
async def test_bot_sends_the_login_link_again_the_next_morning(kite, tmp_path, monkeypatch):
    pytest.importorskip("telegram")
    from skopaq import telegram_bot

    _config(monkeypatch, "", public_base_url="https://x.example")
    monkeypatch.setattr(telegram_bot, "_is_trading_day_ist", lambda: True)
    monkeypatch.setattr(telegram_bot, "alert_chat_ids", {111})
    # Yesterday's login, still cached by the long-running bot and persisted on /data
    _persist(kite, tmp_path, monkeypatch, "stale", _yesterday(kite))
    monkeypatch.setattr(kite, "_access_token", "stale")
    monkeypatch.setattr(kite, "_access_token_set_at", _yesterday(kite))
    context = MagicMock()
    context.bot.send_message = AsyncMock()

    await telegram_bot.job_pre_market_login(context)

    text = context.bot.send_message.await_args.kwargs["text"]
    assert "https://x.example/api/kite/login" in text
    assert "already connected" not in text

    update = MagicMock()
    update.message.chat.id = 111
    update.message.reply_text = AsyncMock()
    await telegram_bot.cmd_login(update, MagicMock())
    assert "https://x.example/api/kite/login" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_bot_sees_a_new_login_written_by_the_api(kite, tmp_path, monkeypatch):
    pytest.importorskip("telegram")
    from skopaq import telegram_bot

    _config(monkeypatch, "", public_base_url="https://x.example")
    monkeypatch.setattr(telegram_bot, "_is_trading_day_ist", lambda: True)
    monkeypatch.setattr(telegram_bot, "alert_chat_ids", {111})
    monkeypatch.setattr(kite, "_access_token", "earlier-today")
    monkeypatch.setattr(kite, "_access_token_set_at", datetime.now(timezone.utc))
    _persist(kite, tmp_path, monkeypatch, "relogin", datetime.now(timezone.utc))

    assert telegram_bot._kite_token() == "relogin"
