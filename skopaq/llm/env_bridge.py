"""Bridge Skopaq-prefixed environment variables to standard LLM env vars.

Upstream TradingAgents (and LangChain providers) expect keys like
``GOOGLE_API_KEY``, ``ANTHROPIC_API_KEY``, etc.  SkopaqConfig stores
them under the ``SKOPAQ_`` prefix.  This module copies the Skopaq
values into the standard vars so upstream code works transparently.

**Rule:** never overwrite an existing env var — explicit standard vars
take precedence over bridged ones.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

# Maps SkopaqConfig field → standard env var expected by upstream / LangChain
_BRIDGE_MAP: dict[str, str] = {
    "google_api_key": "GOOGLE_API_KEY",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "xai_api_key": "XAI_API_KEY",
    "perplexity_api_key": "PERPLEXITY_API_KEY",
    "openrouter_api_key": "OPENROUTER_API_KEY",
    "typesafe_api_key": "TYPESAFE_API_KEY",
}


def bridge_env_vars(config=None) -> list[str]:
    """Copy Skopaq secret values into standard env vars.

    Args:
        config: A ``SkopaqConfig`` instance.  If *None*, one is created
            from the current environment.

    Returns:
        List of env var names that were actually set (useful for tests).
    """
    if config is None:
        from skopaq.config import SkopaqConfig
        config = SkopaqConfig()

    bridged: list[str] = []

    for attr, env_var in _BRIDGE_MAP.items():
        # Only bridge if the standard var is not already set
        if os.environ.get(env_var):
            continue

        secret = getattr(config, attr, None)
        if secret is None:
            continue

        value = secret.get_secret_value() if hasattr(secret, "get_secret_value") else str(secret)
        if not value:
            continue

        os.environ[env_var] = value
        bridged.append(env_var)
        logger.debug("Bridged SKOPAQ_%s → %s", attr.upper(), env_var)

    if bridged:
        logger.info("Environment bridge set %d vars: %s", len(bridged), ", ".join(bridged))

    return bridged
