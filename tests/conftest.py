"""Shared test fixtures."""

import os

import pytest

# Ensure tests don't accidentally use real credentials
os.environ.setdefault("SKOPAQ_TRADING_MODE", "paper")
os.environ.setdefault("SKOPAQ_SUPABASE_URL", "")
os.environ.setdefault("SKOPAQ_SUPABASE_SERVICE_KEY", "")


# The kill switch reads a halt file from the home directory; point it at a
# path that does not exist so a real `skopaq halt` never leaks into tests.
os.environ["SKOPAQ_HALT_FILE"] = os.path.join(
    os.path.dirname(__file__), ".no-such-dir", "HALT-for-tests"
)
os.environ.pop("SKOPAQ_TRADING_HALTED", None)


@pytest.fixture(autouse=True)
def _fresh_kill_switch():
    """Each test sees the kill switch uncached."""
    from skopaq.execution import kill_switch

    kill_switch._cache = None
    yield
    kill_switch._cache = None
