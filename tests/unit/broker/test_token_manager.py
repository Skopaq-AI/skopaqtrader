"""Tests for INDstocks token manager."""

import json
import time
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from skopaq.broker.token_manager import TokenExpiredError, TokenManager


@pytest.fixture
def tmp_token_dir(tmp_path):
    """Redirect token storage to a temp directory."""
    token_dir = tmp_path / ".skopaq"
    with (
        patch("skopaq.broker.token_manager.TOKEN_DIR", token_dir),
        patch("skopaq.broker.token_manager.TOKEN_FILE", token_dir / "token.enc"),
        patch("skopaq.broker.token_manager.KEY_FILE", token_dir / "token.key"),
    ):
        yield token_dir


@pytest.fixture
def mgr(tmp_token_dir):
    return TokenManager()


class TestTokenManager:
    def test_no_token_stored(self, mgr, monkeypatch):
        # Clear env var and mock SkopaqConfig so the fallback returns empty
        monkeypatch.delenv("SKOPAQ_INDSTOCKS_TOKEN", raising=False)
        with patch("skopaq.config.SkopaqConfig") as MockConfig:
            mock_cfg = MockConfig.return_value
            mock_cfg.indstocks_token.get_secret_value.return_value = ""
            health = mgr.get_health()
            assert not health.valid
            assert "No token stored" in health.warning

    def test_set_and_get_token(self, mgr):
        mgr.set_token("my-secret-token", ttl_hours=24)
        health = mgr.get_health()
        assert health.valid
        assert health.token == "my-secret-token"
        assert health.remaining.total_seconds() > 0

    def test_get_token_returns_string(self, mgr):
        mgr.set_token("bearer-xyz")
        assert mgr.get_token() == "bearer-xyz"

    def test_expired_token(self, mgr, monkeypatch):
        """Expired file and no env fallback -> genuinely invalid."""
        monkeypatch.delenv("SKOPAQ_INDSTOCKS_TOKEN", raising=False)
        mgr.set_token("old-token", ttl_hours=0)
        with patch("skopaq.config.SkopaqConfig") as MockConfig:
            MockConfig.return_value.indstocks_token.get_secret_value.return_value = ""
            health = mgr.get_health()
        assert not health.valid
        assert "EXPIRED" in health.warning

    def test_get_token_raises_when_expired(self, mgr, monkeypatch):
        monkeypatch.delenv("SKOPAQ_INDSTOCKS_TOKEN", raising=False)
        mgr.set_token("old-token", ttl_hours=0)
        with patch("skopaq.config.SkopaqConfig") as MockConfig:
            MockConfig.return_value.indstocks_token.get_secret_value.return_value = ""
            with pytest.raises(TokenExpiredError):
                mgr.get_token()

    def test_expired_file_falls_back_to_env(self, mgr, monkeypatch):
        """The bug: a stale file must not shadow a valid env token.

        Containers have no token file, so they always took the env path — which
        is why this only ever failed locally, and why the error text told users
        to regenerate a token they already had.
        """
        monkeypatch.setenv("SKOPAQ_INDSTOCKS_TOKEN", "fresh-env-token")
        mgr.set_token("stale-file-token", ttl_hours=0)
        health = mgr.get_health()
        assert health.valid
        assert health.token == "fresh-env-token"
        assert "expired" in health.warning.lower()
        assert "token clear" in health.warning

    def test_unreadable_file_falls_back_to_env(self, mgr, monkeypatch):
        """Same fallback when the keyfile is lost and decryption fails."""
        monkeypatch.setenv("SKOPAQ_INDSTOCKS_TOKEN", "fresh-env-token")
        mgr.set_token("whatever")
        from skopaq.broker import token_manager as tm
        tm.TOKEN_FILE.write_bytes(b"not-valid-fernet-ciphertext")
        health = mgr.get_health()
        assert health.valid and health.token == "fresh-env-token"
        assert "unreadable" in health.warning.lower()

    def test_valid_file_still_wins_over_env(self, mgr, monkeypatch):
        """Precedence is unchanged for the normal case."""
        monkeypatch.setenv("SKOPAQ_INDSTOCKS_TOKEN", "env-token")
        mgr.set_token("file-token", ttl_hours=24)
        health = mgr.get_health()
        assert health.valid and health.token == "file-token"

    def test_fallback_is_not_silent(self, mgr, monkeypatch):
        """Falling back must always say the file was ignored."""
        monkeypatch.setenv("SKOPAQ_INDSTOCKS_TOKEN", "fresh-env-token")
        mgr.set_token("stale", ttl_hours=0)
        assert mgr.get_health().warning, "fell back with no warning"

    def test_repr_does_not_leak_the_token(self):
        """TokenHealth is reprd into logs and pytest output."""
        from skopaq.broker.token_manager import TokenHealth
        h = TokenHealth(valid=True, token="SECRET-JWT-abc123")
        assert "SECRET-JWT" not in repr(h)
        assert h.token == "SECRET-JWT-abc123"

    def test_clear_token(self, mgr, monkeypatch):
        monkeypatch.delenv("SKOPAQ_INDSTOCKS_TOKEN", raising=False)
        mgr.set_token("to-be-cleared")
        mgr.clear()
        with patch("skopaq.config.SkopaqConfig") as MockConfig:
            MockConfig.return_value.indstocks_token.get_secret_value.return_value = ""
            health = mgr.get_health()
            assert not health.valid

    def test_warning_thresholds(self, mgr):
        # Set token expiring in 25 minutes — should trigger 30min warning
        mgr.set_token("expiring-soon", ttl_hours=25 / 60)
        health = mgr.get_health()
        assert health.valid
        assert health.warning  # Should have a warning

    def test_encryption_persists(self, mgr, tmp_token_dir):
        mgr.set_token("persistent-token")
        # Create a new manager (simulates restart)
        mgr2 = TokenManager()
        assert mgr2.get_token() == "persistent-token"
