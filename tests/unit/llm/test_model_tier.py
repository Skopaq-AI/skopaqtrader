"""Tests for the multi-model tier builder."""

import os
import pytest
from unittest.mock import MagicMock, patch


class TestBuildLlmMap:
    """Tests for build_llm_map()."""

    def _mock_create_llm(self, provider, model, **kwargs):
        """Return a mock LLM client with a mock get_llm()."""
        client = MagicMock()
        llm = MagicMock()
        llm._provider = provider
        llm._model = model
        client.get_llm.return_value = llm
        return client

    @patch("skopaq.llm.model_tier._create_llm")
    def test_all_keys_available(self, mock_create):
        """When all API keys are set, each role gets its preferred model."""
        from skopaq.llm.model_tier import build_llm_map

        # Track which (provider, model) pairs were requested
        created = {}
        def side_effect(provider, model, **kw):
            llm = MagicMock()
            llm._provider = provider
            llm._model = model
            created[(provider, model)] = llm
            return llm

        mock_create.side_effect = side_effect

        env = {
            "GOOGLE_API_KEY": "g-key",
            "ANTHROPIC_API_KEY": "a-key",
            "XAI_API_KEY": "x-key",
            "PERPLEXITY_API_KEY": "p-key",
            "OPENROUTER_API_KEY": "or-key",
        }

        with patch.dict(os.environ, env, clear=False):
            llm_map = build_llm_map()

        # With all keys available, roles get their FIRST preference:
        # social_analyst → openrouter (Grok), news_analyst → google (Gemini;
        # Perplexity Sonar doesn't support tool calling via OpenRouter)
        assert llm_map["social_analyst"]._provider == "openrouter"
        assert llm_map["news_analyst"]._provider == "google"
        assert llm_map["research_manager"]._provider == "anthropic"
        assert llm_map["portfolio_manager"]._provider == "anthropic"
        assert llm_map["market_analyst"]._provider == "google"
        assert "_default" in llm_map

        assert llm_map["portfolio_manager"]._model == "claude-opus-5"
        assert llm_map["research_manager"]._model == "claude-opus-5"
        assert llm_map["social_analyst"]._model == "x-ai/grok-4.6"
        assert llm_map["market_analyst"]._model == "gemini-3.8-flash"
        assert llm_map["_default"]._model == "gemini-3.8-flash"

    def test_judges_avoid_models_that_reject_forced_tool_calls(self):
        """LangChain's structured output forces a tool call on Anthropic models;
        Opus 5.5 and Fable 5.1 answer that with a 400."""
        from skopaq.llm.model_tier import _ROLE_PREFERENCES

        for role in ("research_manager", "portfolio_manager"):
            for provider, model in _ROLE_PREFERENCES[role]:
                assert not model.startswith(("claude-opus-5-5", "claude-fable-5-1"))

    @patch("skopaq.llm.model_tier._create_llm")
    def test_only_google_key(self, mock_create):
        """When only Google key is set, all roles fall back to Gemini Flash."""
        from skopaq.llm.model_tier import build_llm_map

        gemini_llm = MagicMock()
        gemini_llm._provider = "google"
        mock_create.return_value = gemini_llm

        env = {"GOOGLE_API_KEY": "g-key"}

        with patch.dict(os.environ, env, clear=False):
            # Clear other keys so only Google is available
            for key in ["ANTHROPIC_API_KEY", "XAI_API_KEY",
                        "PERPLEXITY_API_KEY", "OPENROUTER_API_KEY"]:
                os.environ.pop(key, None)
            llm_map = build_llm_map()

        # Everything should be Gemini
        for role in ["market_analyst", "social_analyst", "news_analyst",
                      "research_manager", "portfolio_manager"]:
            assert llm_map[role]._provider == "google"

    @patch("skopaq.llm.model_tier._create_llm")
    def test_fallback_on_missing_key(self, mock_create):
        """social_analyst falls back to Gemini when OpenRouter/XAI keys are missing."""
        from skopaq.llm.model_tier import build_llm_map

        def side_effect(provider, model, **kw):
            llm = MagicMock()
            llm._provider = provider
            return llm

        mock_create.side_effect = side_effect

        env = {"GOOGLE_API_KEY": "g-key"}

        with patch.dict(os.environ, env, clear=False):
            for key in ["XAI_API_KEY", "OPENROUTER_API_KEY",
                        "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY"]:
                os.environ.pop(key, None)
            llm_map = build_llm_map()

        # social_analyst should have fallen back to google
        assert llm_map["social_analyst"]._provider == "google"

    @patch("skopaq.llm.model_tier._create_llm")
    def test_default_key_always_present(self, mock_create):
        """_default key is always in the map."""
        from skopaq.llm.model_tier import build_llm_map

        mock_create.return_value = MagicMock()

        env = {"GOOGLE_API_KEY": "g-key"}

        with patch.dict(os.environ, env, clear=False):
            llm_map = build_llm_map()

        assert "_default" in llm_map

    @patch("skopaq.llm.model_tier._create_llm")
    def test_caches_same_model(self, mock_create):
        """Same (provider, model) pair should be created only once."""
        from skopaq.llm.model_tier import build_llm_map

        call_count = 0
        def side_effect(provider, model, **kw):
            nonlocal call_count
            call_count += 1
            llm = MagicMock()
            llm._provider = provider
            return llm

        mock_create.side_effect = side_effect

        env = {"GOOGLE_API_KEY": "g-key"}

        with patch.dict(os.environ, env, clear=False):
            for key in ["ANTHROPIC_API_KEY", "XAI_API_KEY",
                        "PERPLEXITY_API_KEY", "OPENROUTER_API_KEY"]:
                os.environ.pop(key, None)
            llm_map = build_llm_map()

        # Only one Gemini Flash instance should be created (all roles share it)
        assert call_count == 1
