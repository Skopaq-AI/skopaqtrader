"""INDstocks API token lifecycle management.

Tokens expire every 24 hours and must be regenerated manually from the
INDstocks dashboard.  This module encrypts the token at rest, tracks expiry,
sends warnings, and auto-falls-back to paper mode when expired.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet

logger = logging.getLogger(__name__)

TOKEN_DIR = Path.home() / ".skopaq"
TOKEN_FILE = TOKEN_DIR / "token.enc"
KEY_FILE = TOKEN_DIR / "token.key"

# Warn at these intervals before expiry
WARN_THRESHOLDS = [
    timedelta(hours=2),
    timedelta(hours=1),
    timedelta(minutes=30),
    timedelta(minutes=10),
]


@dataclass
class TokenHealth:
    """Current token status."""

    valid: bool
    # repr=False: this dataclass is reprd into log lines, exception messages
    # and pytest assertion output. Without it the raw bearer token is printed
    # verbatim — observed leaking a live JWT into a test failure message.
    token: str = field(default="", repr=False)
    expires_at: Optional[datetime] = None
    remaining: Optional[timedelta] = None
    warning: str = ""


class TokenManager:
    """Manages INDstocks API token encryption, storage, and expiry tracking."""

    def __init__(self) -> None:
        self._fernet: Optional[Fernet] = None
        self._warned_thresholds: set[int] = set()

    def _ensure_key(self) -> Fernet:
        """Load or create encryption key."""
        if self._fernet is not None:
            return self._fernet

        TOKEN_DIR.mkdir(parents=True, exist_ok=True)

        if KEY_FILE.exists():
            key = KEY_FILE.read_bytes()
        else:
            key = Fernet.generate_key()
            KEY_FILE.write_bytes(key)
            KEY_FILE.chmod(0o600)

        self._fernet = Fernet(key)
        return self._fernet

    def set_token(self, token: str, ttl_hours: float = 24.0) -> None:
        """Encrypt and store a new API token.

        Args:
            token: The Bearer token from INDstocks dashboard.
            ttl_hours: Hours until expiry (default 24).
        """
        fernet = self._ensure_key()
        expires_at = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        payload = json.dumps({
            "token": token,
            "expires_at": expires_at.isoformat(),
            "stored_at": datetime.now(timezone.utc).isoformat(),
        })
        encrypted = fernet.encrypt(payload.encode())
        TOKEN_FILE.write_bytes(encrypted)
        TOKEN_FILE.chmod(0o600)
        self._warned_thresholds.clear()
        logger.info("Token stored, expires at %s", expires_at.isoformat())

    @staticmethod
    def _env_token() -> str:
        """Token from ``SKOPAQ_INDSTOCKS_TOKEN`` (env first, then ``.env``)."""
        import os

        env_token = os.environ.get("SKOPAQ_INDSTOCKS_TOKEN", "")
        if env_token:
            return env_token
        try:
            from skopaq.config import SkopaqConfig

            return SkopaqConfig().indstocks_token.get_secret_value()
        except Exception:
            return ""

    @staticmethod
    def _env_health(token: str, warning: str = "") -> TokenHealth:
        """Wrap an env-var token. It carries no expiry, so assume 24h."""
        return TokenHealth(
            valid=True,
            token=token,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
            remaining=timedelta(hours=24),
            warning=warning,
        )

    def get_health(self) -> TokenHealth:
        """Check current token validity and remaining time.

        Priority:
        1. Encrypted token file (``~/.skopaq/token.enc``) — for local use
        2. ``SKOPAQ_INDSTOCKS_TOKEN`` env var — for Docker/cloud deployments

        The env var fallback has no expiry tracking (assumed fresh).

        A file that is present but *unusable* — expired, or undecryptable
        because the keyfile was lost — falls through to the env var rather than
        reporting failure.  Previously it did not, so a months-stale file
        silently shadowed a perfectly good ``SKOPAQ_INDSTOCKS_TOKEN`` and the
        error told the user to regenerate a token they already had.  Containers
        were unaffected (no file), which made it look like a local-only fault.

        The fallback is never silent in the other direction either: the
        returned warning always says the file was ignored and how to clear it.
        """
        env_token = self._env_token()

        if not TOKEN_FILE.exists():
            if env_token:
                return self._env_health(env_token)
            return TokenHealth(valid=False, warning="No token stored. Run: skopaq token set <token>")

        try:
            fernet = self._ensure_key()
            encrypted = TOKEN_FILE.read_bytes()
            payload = json.loads(fernet.decrypt(encrypted).decode())
        except Exception as exc:
            if env_token:
                logger.warning("Token file unreadable (%s) — using env var", exc)
                return self._env_health(
                    env_token,
                    f"Token file unreadable ({exc}); using SKOPAQ_INDSTOCKS_TOKEN. "
                    "Run: skopaq token clear",
                )
            return TokenHealth(valid=False, warning=f"Token decryption failed: {exc}")

        token = payload["token"]
        expires_at = datetime.fromisoformat(payload["expires_at"])
        now = datetime.now(timezone.utc)
        remaining = expires_at - now

        if remaining.total_seconds() <= 0:
            if env_token:
                logger.warning(
                    "Stored token expired %s — using SKOPAQ_INDSTOCKS_TOKEN instead",
                    expires_at.isoformat(),
                )
                return self._env_health(
                    env_token,
                    f"Stored token expired {expires_at.date()}; using "
                    "SKOPAQ_INDSTOCKS_TOKEN. Run: skopaq token clear",
                )
            return TokenHealth(
                valid=False,
                expires_at=expires_at,
                remaining=timedelta(0),
                warning="Token EXPIRED. Regenerate from INDstocks dashboard.",
            )

        warning = ""
        for threshold in WARN_THRESHOLDS:
            mins = int(threshold.total_seconds() / 60)
            if remaining <= threshold and mins not in self._warned_thresholds:
                warning = f"Token expires in {remaining}. Refresh from INDstocks dashboard."
                self._warned_thresholds.add(mins)
                logger.warning(warning)
                # Notify via Telegram
                try:
                    import asyncio
                    from skopaq.notifications import notify

                    msg = f"⚠️ Token Warning\n\n{warning}"
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(notify(msg))
                    except RuntimeError:
                        pass
                except Exception:
                    pass
                break

        return TokenHealth(
            valid=True,
            token=token,
            expires_at=expires_at,
            remaining=remaining,
            warning=warning,
        )

    def get_token(self) -> str:
        """Return the current token or raise if expired/missing."""
        health = self.get_health()
        if not health.valid:
            raise TokenExpiredError(health.warning)
        return health.token

    def clear(self) -> None:
        """Delete stored token."""
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        self._warned_thresholds.clear()
        logger.info("Token cleared")


class TokenExpiredError(Exception):
    """Raised when the INDstocks token is expired or missing."""
