"""Tests for Ollama local model fallback in model_tier.py."""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest


class TestOllamaDetection:
    """Tests for Ollama availability detection."""

    def setup_method(self):
        """Reset the cached availability before each test."""
        import skopaq.llm.model_tier as mt

        mt._ollama_available = None

    def test_ollama_disabled_by_default(self):
        from skopaq.llm.model_tier import _is_ollama_available

        # Ensure the env var is NOT set
        os.environ.pop("SKOPAQ_OLLAMA_ENABLED", None)
        assert _is_ollama_available() is False

    def test_ollama_enabled_but_not_running(self):
        from skopaq.llm.model_tier import _is_ollama_available

        os.environ["SKOPAQ_OLLAMA_ENABLED"] = "true"
        # Patch urllib to simulate Ollama not running
        with patch("urllib.request.urlopen", side_effect=ConnectionError):
            import skopaq.llm.model_tier as mt

            mt._ollama_available = None
            assert _is_ollama_available() is False
        os.environ.pop("SKOPAQ_OLLAMA_ENABLED", None)

    def test_ollama_enabled_and_running(self):
        from skopaq.llm.model_tier import _is_ollama_available

        os.environ["SKOPAQ_OLLAMA_ENABLED"] = "true"

        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            import skopaq.llm.model_tier as mt

            mt._ollama_available = None
            assert _is_ollama_available() is True
        os.environ.pop("SKOPAQ_OLLAMA_ENABLED", None)


class TestOllamaModelDetection:
    """Tests for auto-detecting the best Ollama model."""

    def test_configured_model_takes_priority(self):
        from skopaq.llm.model_tier import _get_ollama_model

        os.environ["SKOPAQ_OLLAMA_MODEL"] = "llama3.2"
        assert _get_ollama_model() == "llama3.2"
        os.environ.pop("SKOPAQ_OLLAMA_MODEL", None)

    def test_auto_detect_returns_first_model(self):
        import json

        from skopaq.llm.model_tier import _get_ollama_model

        os.environ.pop("SKOPAQ_OLLAMA_MODEL", None)

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "models": [{"name": "mistral:latest"}, {"name": "llama2:7b"}]
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch("urllib.request.urlopen", return_value=mock_response):
            assert _get_ollama_model() == "mistral:latest"

    def test_fallback_to_mistral(self):
        from skopaq.llm.model_tier import _get_ollama_model

        os.environ.pop("SKOPAQ_OLLAMA_MODEL", None)

        with patch("urllib.request.urlopen", side_effect=ConnectionError):
            assert _get_ollama_model() == "mistral"


class TestOllamaLLMCreation:
    """Tests for creating ChatOllama instances."""

    def test_create_ollama_llm(self):
        from skopaq.llm.model_tier import _create_ollama_llm

        with patch("langchain_ollama.ChatOllama") as MockChatOllama:
            MockChatOllama.return_value = MagicMock()
            result = _create_ollama_llm("mistral")
            MockChatOllama.assert_called_once_with(
                model="mistral",
                base_url="http://localhost:11434",
                temperature=0.1,
            )

    def test_create_ollama_llm_auto_model(self):
        from skopaq.llm.model_tier import _create_ollama_llm

        with patch("langchain_ollama.ChatOllama") as MockChatOllama, \
             patch("skopaq.llm.model_tier._get_ollama_model", return_value="qwen2.5-coder:7b"):
            MockChatOllama.return_value = MagicMock()
            _create_ollama_llm("auto")
            MockChatOllama.assert_called_once_with(
                model="qwen2.5-coder:7b",
                base_url="http://localhost:11434",
                temperature=0.1,
            )


class TestOllamaInRolePreferences:
    """Tests that Ollama appears as fallback in role preferences."""

    def test_analyst_roles_have_ollama_fallback(self):
        from skopaq.llm.model_tier import _ROLE_PREFERENCES

        roles_with_ollama = [
            role for role, prefs in _ROLE_PREFERENCES.items()
            if any(p == "ollama" for p, _ in prefs)
        ]
        # Analyst, researcher, debater roles should have Ollama
        assert "market_analyst" in roles_with_ollama
        assert "chat_brain" in roles_with_ollama
        assert "trader" in roles_with_ollama

    def test_judge_roles_skip_ollama(self):
        from skopaq.llm.model_tier import _ROLE_PREFERENCES

        # Judge roles (research_manager, portfolio_manager) need cloud quality
        for role in ("research_manager", "portfolio_manager"):
            providers = [p for p, _ in _ROLE_PREFERENCES[role]]
            assert "ollama" not in providers, f"{role} should not have Ollama fallback"
