"""Shared test fixtures."""

import os

import pytest

# Tests never reach production state or people, even when run with a real .env loaded
# (docker compose env_file, --env-file, or tradingagents' load_dotenv of ./.env): a halt
# written to Supabase system_flags stops every BUY everywhere, and notify() sends Telegram
# messages. Overwrite, never setdefault; set to "" rather than pop, because load_dotenv
# (override=False) and pydantic's env_file only fill variables that are unset.
# Tests that need a value set it with monkeypatch.
os.environ["SKOPAQ_TRADING_MODE"] = "paper"
for _var in (
    "SKOPAQ_SUPABASE_URL",
    "SKOPAQ_SUPABASE_ANON_KEY",
    "SKOPAQ_SUPABASE_SERVICE_KEY",
    "SKOPAQ_TELEGRAM_BOT_TOKEN",
    "SKOPAQ_TELEGRAM_CHAT_ID",
    "SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS",
    "SKOPAQ_UPSTASH_REDIS_URL",
    "SKOPAQ_UPSTASH_REDIS_TOKEN",
    "SKOPAQ_REDIS_URL",
    "REDIS_URL",
    "SKOPAQ_DATABASE_URL",
    "DATABASE_URL",
    "SKOPAQ_API_BASE_URL",
    "SKOPAQ_API_TOKEN",
    "SKOPAQ_INDSTOCKS_TOKEN",
    "SKOPAQ_KITE_ACCESS_TOKEN",
    "SKOPAQ_KITE_API_KEY",
    "SKOPAQ_KITE_API_SECRET",
):
    os.environ[_var] = ""


# The kill switch reads a halt file from the home directory; point it at a
# path that does not exist so a real `skopaq halt` never leaks into tests.
os.environ["SKOPAQ_HALT_FILE"] = os.path.join(
    os.path.dirname(__file__), ".no-such-dir", "HALT-for-tests"
)
os.environ.pop("SKOPAQ_TRADING_HALTED", None)
# Likewise the Kite session file (/data or /tmp/skopaq_kite_token.json, which the native MCP
# server writes on a Mac): a real token there would make tests call api.kite.trade. A test
# that needs a token file sets kite_client._TOKEN_FILE to its own tmp path.
os.environ["SKOPAQ_KITE_TOKEN_FILE"] = os.path.join(
    os.path.dirname(__file__), ".no-such-dir", "kite-token-for-tests.json"
)


@pytest.fixture(autouse=True)
def _fresh_kill_switch():
    """Each test sees the kill switch uncached."""
    from skopaq.execution import kill_switch

    kill_switch._cache = None
    yield
    kill_switch._cache = None
