"""Multi-model tiering — assigns different LLMs to different agent roles.

The upstream TradingAgentsGraph uses a single provider with two LLMs
(quick + deep).  Skopaq wants per-role assignment across providers:

    market_analyst      → Gemini 3 Flash   (cheap, fast)
    social_analyst      → Grok             (Twitter/X integration)
    news_analyst        → Gemini 3 Flash   (tool-calling required; Perplexity
                                            Sonar doesn't support tool use)
    fundamentals_analyst→ Gemini 3 Flash   (cheap, fast)
    bull_researcher     → Gemini 3 Flash
    bear_researcher     → Gemini 3 Flash
    research_manager    → Claude Opus 4.6  (strongest reasoning — judge role)
    portfolio_manager   → Claude Opus 4.6  (strongest reasoning — judge role;
                                            upstream renamed risk_manager in v0.2.2)
    trader              → Gemini 3 Flash
    aggressive_debator  → Gemini 3 Flash
    neutral_debator     → Gemini 3 Flash
    conservative_debator→ Gemini 3 Flash
    _default            → Gemini 3 Flash   (fallback for any unlisted role)

Note: Perplexity Sonar is used for the scanner's news screener (plain
prompts), NOT for the news_analyst agent (which needs tool calling).

Each role gracefully falls back to Gemini Flash if its preferred
provider key is missing.  When Ollama is enabled, local models serve
as the **last** fallback for non-judge roles — zero cost, works offline.
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.language_models import BaseChatModel

logger = logging.getLogger(__name__)

# Cached Ollama availability check (set on first call to _has_key)
_ollama_available: bool | None = None

# Role → (provider, model) tuples.  First match with an available key wins.
# OpenRouter is used as gateway for Grok (xAI) and Perplexity Sonar models.
_ROLE_PREFERENCES: dict[str, list[tuple[str, str]]] = {
    "market_analyst":       [("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    "social_analyst":       [("openrouter", "x-ai/grok-3-mini"), ("xai", "grok-3-mini"), ("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    # Perplexity Sonar doesn't support tool calling via OpenRouter (404).
    # Agent analysts need bind_tools() → must use a tool-capable model.
    "news_analyst":         [("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    "fundamentals_analyst": [("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    "bull_researcher":      [("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    "bear_researcher":      [("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    # Judge roles: NO local fallback — reasoning quality is critical
    "research_manager":     [("anthropic", "claude-opus-4-6"), ("google", "gemini-3-flash-preview")],
    "portfolio_manager":    [("anthropic", "claude-opus-4-6"), ("google", "gemini-3-flash-preview")],
    "trader":               [("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    "aggressive_debator":   [("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    "neutral_debator":      [("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    "conservative_debator": [("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    "sell_analyst":         [("google", "gemini-3-flash-preview"), ("ollama", "auto")],
    "chat_brain":           [("anthropic", "claude-opus-4-6"), ("google", "gemini-3-flash-preview"), ("ollama", "auto")],
}

# Env var names per provider (checked to see if key is available)
_PROVIDER_ENV_KEYS: dict[str, str] = {
    "google": "GOOGLE_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "xai": "XAI_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}

# Default Ollama settings (overridable via SkopaqConfig)
_OLLAMA_BASE_URL = "http://localhost:11434"


def _is_ollama_available() -> bool:
    """Check if Ollama is running locally by pinging the API."""
    global _ollama_available
    if _ollama_available is not None:
        return _ollama_available

    import os

    # Opt-in: SKOPAQ_OLLAMA_ENABLED must be set
    if not os.environ.get("SKOPAQ_OLLAMA_ENABLED", "").lower() in ("1", "true", "yes"):
        _ollama_available = False
        return False

    try:
        import urllib.request

        base_url = os.environ.get("SKOPAQ_OLLAMA_BASE_URL", _OLLAMA_BASE_URL)
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            _ollama_available = resp.status == 200
    except Exception:
        _ollama_available = False

    if _ollama_available:
        logger.info("Ollama detected at %s — local fallback enabled", _OLLAMA_BASE_URL)
    return _ollama_available


def _get_ollama_model() -> str:
    """Get the best available Ollama model."""
    import os

    configured = os.environ.get("SKOPAQ_OLLAMA_MODEL", "")
    if configured:
        return configured

    # Auto-detect: pick the first available model
    try:
        import json
        import urllib.request

        base_url = os.environ.get("SKOPAQ_OLLAMA_BASE_URL", _OLLAMA_BASE_URL)
        req = urllib.request.Request(f"{base_url}/api/tags", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            data = json.loads(resp.read())
            models = data.get("models", [])
            if models:
                name = models[0].get("name", "")
                logger.info("Ollama auto-detected model: %s", name)
                return name
    except Exception:
        pass

    return "mistral"  # Safe default


def _has_key(provider: str) -> bool:
    """Check if the env var for *provider* is set and non-empty."""
    import os

    if provider == "ollama":
        return _is_ollama_available()

    env_var = _PROVIDER_ENV_KEYS.get(provider, "")
    return bool(os.environ.get(env_var))


def _create_llm(provider: str, model: str, **kwargs) -> BaseChatModel:
    """Create a LangChain chat model for the given provider."""
    if provider == "ollama":
        return _create_ollama_llm(model)

    from tradingagents.llm_clients import create_llm_client
    client = create_llm_client(provider=provider, model=model, **kwargs)
    return client.get_llm()


def _create_ollama_llm(model: str) -> BaseChatModel:
    """Create a ChatOllama instance for local inference."""
    import os

    from langchain_ollama import ChatOllama

    base_url = os.environ.get("SKOPAQ_OLLAMA_BASE_URL", _OLLAMA_BASE_URL)

    # "auto" means auto-detect the best available model
    if model == "auto":
        model = _get_ollama_model()

    llm = ChatOllama(
        model=model,
        base_url=base_url,
        temperature=0.1,  # Low temperature for trading analysis
    )
    logger.info("Created Ollama LLM: %s at %s", model, base_url)
    return llm


def build_llm_map(config: dict[str, Any] | None = None) -> dict[str, BaseChatModel]:
    """Build a role → LLM mapping from available API keys.

    Args:
        config: Optional upstream config dict.  Currently unused but
            reserved for future per-role overrides from SkopaqConfig.

    Returns:
        Dict mapping role keys to LangChain ``BaseChatModel`` instances.
        Always includes a ``_default`` key.
    """
    llm_cache: dict[tuple[str, str], BaseChatModel] = {}
    llm_map: dict[str, BaseChatModel] = {}

    for role, preferences in _ROLE_PREFERENCES.items():
        assigned = False
        for provider, model in preferences:
            if not _has_key(provider):
                continue

            cache_key = (provider, model)
            if cache_key not in llm_cache:
                try:
                    llm_cache[cache_key] = _create_llm(provider, model)
                    logger.debug("Created LLM: %s/%s", provider, model)
                except Exception:
                    logger.warning("Failed to create %s/%s, trying next", provider, model, exc_info=True)
                    continue

            llm_map[role] = llm_cache[cache_key]
            if provider != preferences[0][0]:
                logger.info("Role '%s' fell back to %s/%s", role, provider, model)
            assigned = True
            break

        if not assigned:
            logger.warning("Role '%s' has no available LLM — will use _default", role)

    # Ensure _default always exists (Gemini 3 Flash or first available)
    if ("google", "gemini-3-flash-preview") in llm_cache:
        llm_map["_default"] = llm_cache[("google", "gemini-3-flash-preview")]
    elif llm_cache:
        llm_map["_default"] = next(iter(llm_cache.values()))
    else:
        # No keys at all — create will fail at call time, but we need _something_
        logger.error("No LLM API keys available — build_llm_map returning empty _default")
        llm_map["_default"] = _create_llm("google", "gemini-3-flash-preview")

    logger.info(
        "LLM map built: %d roles assigned, %d unique models",
        len([r for r in llm_map if r != "_default"]),
        len(llm_cache),
    )
    return llm_map
