"""Optional API access control: a bearer token and the CORS origin list.

``SKOPAQ_API_TOKEN`` unset keeps every endpoint open (the previous behaviour).
Set, it guards the endpoints that execute tools or hand out credentials
(``/api/chat/*``, ``/api/kite/token``), which then need
``Authorization: Bearer <token>``.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from skopaq.config import SkopaqConfig


def require_api_token(authorization: str = Header(default="")) -> None:
    """FastAPI dependency: 401 unless the request carries the configured bearer token."""
    expected = SkopaqConfig().api_token.get_secret_value()
    if not expected:
        return
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(
        value.strip().encode(), expected.encode()
    ):
        raise HTTPException(
            401, "Missing or invalid API token", headers={"WWW-Authenticate": "Bearer"}
        )


def cors_origins(config) -> list[str]:
    """``SKOPAQ_CORS_ORIGINS`` as a list; ``""`` allows no browser origin."""
    return [o.strip() for o in config.cors_origins.split(",") if o.strip()]
