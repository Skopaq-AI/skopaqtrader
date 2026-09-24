"""Chat session wiring."""

from __future__ import annotations

from unittest.mock import MagicMock


def test_chat_upstream_config_uses_the_current_gemini_model(monkeypatch):
    """_build_upstream_config referenced GEMINI_FLASH without importing it."""
    from skopaq.chat.session import _build_upstream_config
    from skopaq.llm.model_tier import GEMINI_FLASH

    # No semantic cache: it would be installed globally for every later test.
    monkeypatch.setattr("skopaq.llm.cache.init_langcache", lambda config: None)
    config = MagicMock(asset_class="equity", google_thinking_level="")

    upstream = _build_upstream_config(config, llm_map={})

    assert upstream["deep_think_llm"] == upstream["quick_think_llm"] == GEMINI_FLASH
