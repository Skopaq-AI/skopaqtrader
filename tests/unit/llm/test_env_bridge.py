"""Tests for the environment variable bridge."""

import os
import pytest
from unittest.mock import MagicMock, patch

from pydantic import SecretStr


class TestBridgeEnvVars:
    """Tests for bridge_env_vars()."""

    def _make_config(self, **kwargs):
        """Create a mock SkopaqConfig with SecretStr fields.

        All bridged keys default to empty SecretStr so MagicMock's
        auto-attribute creation doesn't leak non-string values into
        os.environ.
        """
        from skopaq.llm.env_bridge import _BRIDGE_MAP

        config = MagicMock()
        # Set all bridge-mapped keys to empty by default
        for key in _BRIDGE_MAP:
            setattr(config, key, SecretStr(""))
        # Override with caller-supplied values
        for key, value in kwargs.items():
            setattr(config, key, SecretStr(value) if value else SecretStr(""))
        return config

    def test_bridges_google_key(self):
        """SKOPAQ_GOOGLE_API_KEY → GOOGLE_API_KEY."""
        from skopaq.llm.env_bridge import bridge_env_vars

        config = self._make_config(google_api_key="test-google-key-123")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_API_KEY", None)
            bridged = bridge_env_vars(config)
            # Assert inside the patch.dict block (it restores env on exit)
            assert "GOOGLE_API_KEY" in bridged
            assert os.environ.get("GOOGLE_API_KEY") == "test-google-key-123"

    def test_does_not_overwrite_existing(self):
        """If GOOGLE_API_KEY is already set, bridge should not touch it."""
        from skopaq.llm.env_bridge import bridge_env_vars

        config = self._make_config(google_api_key="new-key")

        with patch.dict(os.environ, {"GOOGLE_API_KEY": "existing-key"}, clear=False):
            bridged = bridge_env_vars(config)
            assert "GOOGLE_API_KEY" not in bridged
            assert os.environ.get("GOOGLE_API_KEY") == "existing-key"

    def test_skips_empty_values(self):
        """Empty secrets should not be bridged."""
        from skopaq.llm.env_bridge import bridge_env_vars

        config = self._make_config(google_api_key="", anthropic_api_key="")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOOGLE_API_KEY", None)
            os.environ.pop("ANTHROPIC_API_KEY", None)
            bridged = bridge_env_vars(config)

        assert bridged == []

    def test_bridges_multiple_keys(self):
        """Multiple keys get bridged in one call."""
        from skopaq.llm.env_bridge import bridge_env_vars

        config = self._make_config(
            google_api_key="g-key",
            anthropic_api_key="a-key",
            xai_api_key="x-key",
        )

        with patch.dict(os.environ, {}, clear=False):
            for var in ["GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY"]:
                os.environ.pop(var, None)
            bridged = bridge_env_vars(config)
            assert set(bridged) == {"GOOGLE_API_KEY", "ANTHROPIC_API_KEY", "XAI_API_KEY"}


def test_bridges_typesafe_key():
    """SKOPAQ_TYPESAFE_API_KEY → TYPESAFE_API_KEY (upstream's Jev post screening)."""
    from skopaq.llm.env_bridge import bridge_env_vars

    config = TestBridgeEnvVars()._make_config(typesafe_api_key="ts-key")
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TYPESAFE_API_KEY", None)
        assert "TYPESAFE_API_KEY" in bridge_env_vars(config)
        assert os.environ["TYPESAFE_API_KEY"] == "ts-key"


def test_bridges_jev_base_url():
    """SKOPAQ_JEV_BASE_URL → TYPESAFE_BASE_URL, so post screening uses the same gateway."""
    from skopaq.llm.env_bridge import bridge_env_vars

    config = TestBridgeEnvVars()._make_config()
    config.jev_base_url = "https://openrouter.ai/api"  # a plain str field, not a secret
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TYPESAFE_BASE_URL", None)
        assert "TYPESAFE_BASE_URL" in bridge_env_vars(config)
        assert os.environ["TYPESAFE_BASE_URL"] == "https://openrouter.ai/api"


def test_empty_jev_base_url_is_not_bridged():
    from skopaq.llm.env_bridge import bridge_env_vars

    config = TestBridgeEnvVars()._make_config()
    config.jev_base_url = ""
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TYPESAFE_BASE_URL", None)
        assert "TYPESAFE_BASE_URL" not in bridge_env_vars(config)
        assert "TYPESAFE_BASE_URL" not in os.environ


def test_jev_base_url_is_not_paired_with_a_different_key():
    """An existing TYPESAFE_API_KEY (say, a TypeSafe key) must not be sent to the gateway."""
    from skopaq.llm.env_bridge import bridge_env_vars

    config = TestBridgeEnvVars()._make_config(typesafe_api_key="openrouter-key")
    config.jev_base_url = "https://openrouter.ai/api"
    with patch.dict(os.environ, {"TYPESAFE_API_KEY": "typesafe-key"}, clear=False):
        os.environ.pop("TYPESAFE_BASE_URL", None)
        assert bridge_env_vars(config) == []
        assert "TYPESAFE_BASE_URL" not in os.environ
        assert os.environ["TYPESAFE_API_KEY"] == "typesafe-key"


def test_jev_base_url_follows_a_matching_or_directly_set_key():
    from skopaq.llm.env_bridge import bridge_env_vars

    for configured, existing in (("or-key", "or-key"), ("", "or-key")):
        config = TestBridgeEnvVars()._make_config(typesafe_api_key=configured)
        config.jev_base_url = "https://openrouter.ai/api"
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": existing}, clear=False):
            os.environ.pop("TYPESAFE_BASE_URL", None)
            assert bridge_env_vars(config) == ["TYPESAFE_BASE_URL"]
            assert os.environ["TYPESAFE_BASE_URL"] == "https://openrouter.ai/api"
