"""Kill switch — one halt that every order path obeys.

While trading is halted, ``SafetyChecker`` rejects every BUY, whether it
comes from the daemon, ``skopaq trade``, MCP ``place_order`` or chat, and
the daemon skips scanning and analysis. SELLs stay allowed so open positions
can still be protected (the no-short-sale check means a SELL can only
reduce what is held).

Trading is halted if any of these says so:

- ``SKOPAQ_TRADING_HALTED=true`` — a deploy-level halt (env or ``.env``);
- the local halt file (``~/.skopaq/HALT``, or ``SKOPAQ_HALT_FILE``);
- the ``trading_halt`` row of Supabase's ``system_flags`` table, shared by
  every process and machine (migration ``003_system_flags.sql``).

``skopaq halt`` writes the file and the Supabase row; ``skopaq resume``
clears both. A failed Supabase read is logged and does not halt trading on
its own; the file and env var still work without it.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

HALT_FLAG_KEY = "trading_halt"
_CACHE_SECONDS = 5.0
_cache: Optional[tuple[float, "HaltStatus"]] = None


@dataclass(frozen=True)
class HaltStatus:
    halted: bool
    reason: str = ""
    since: str = ""  # ISO timestamp, when known
    source: str = ""  # "env", "file" or "supabase"

    def describe(self) -> str:
        if not self.halted:
            return "Trading is active"
        since = f" since {self.since}" if self.since else ""
        return f"Trading HALTED ({self.source}){since}: {self.reason or 'no reason given'}"


def halt_file() -> Path:
    override = os.environ.get("SKOPAQ_HALT_FILE")
    return Path(override) if override else Path.home() / ".skopaq" / "HALT"


def _config():
    from skopaq.config import SkopaqConfig

    return SkopaqConfig()


def _flags(config):
    """The ``system_flags`` repository, or ``None`` without Supabase."""
    if not config.supabase_url or not config.supabase_service_key.get_secret_value():
        return None
    from skopaq.db.repositories import SystemFlagRepository
    from supabase import create_client

    client = create_client(config.supabase_url, config.supabase_service_key.get_secret_value())
    return SystemFlagRepository(client)


def _file_status() -> Optional[HaltStatus]:
    path = halt_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        data = {}  # an unreadable halt file still halts
    return HaltStatus(True, data.get("reason", ""), data.get("since", ""), "file")


def _supabase_status(config) -> Optional[HaltStatus]:
    try:
        flags = _flags(config)
        value = flags.get(HALT_FLAG_KEY) if flags is not None else None
    except Exception:
        logger.warning("Could not read the kill switch from Supabase", exc_info=True)
        return None
    if not value or not value.get("halted"):
        return None
    return HaltStatus(True, value.get("reason", ""), value.get("since", ""), "supabase")


def status(use_cache: bool = True) -> HaltStatus:
    """Whether trading is halted, from the env var, halt file or Supabase."""
    global _cache
    if use_cache and _cache is not None and time.monotonic() - _cache[0] < _CACHE_SECONDS:
        return _cache[1]

    try:
        config = _config()
    except Exception:
        logger.warning("Could not load config for the kill switch", exc_info=True)
        config = None

    result = HaltStatus(False)
    if config is not None and config.trading_halted:
        result = HaltStatus(True, "SKOPAQ_TRADING_HALTED is set", "", "env")
    else:
        result = _file_status() or (config and _supabase_status(config)) or result

    _cache = (time.monotonic(), result)
    return result


def halt(reason: str, by: str = "") -> list[str]:
    """Halt trading everywhere; returns where the halt was recorded."""
    global _cache
    record = {
        "halted": True,
        "reason": reason,
        "since": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "by": by,
    }
    written = []
    path = halt_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record), encoding="utf-8")
    written.append(str(path))
    try:
        flags = _flags(_config())
        if flags is not None:
            flags.set(HALT_FLAG_KEY, record)
            written.append("supabase:system_flags")
    except Exception:
        logger.warning("Halt not recorded in Supabase — only this machine is halted",
                       exc_info=True)
    _cache = None
    logger.warning("TRADING HALTED by %s: %s", by or "unknown", reason)
    return written


def resume(by: str = "") -> list[str]:
    """Lift the halt from the file and Supabase; returns what was cleared.

    ``SKOPAQ_TRADING_HALTED`` cannot be cleared from here; ``status()``
    keeps reporting that halt until the variable is unset.
    """
    global _cache
    cleared = []
    path = halt_file()
    if path.exists():
        path.unlink()
        cleared.append(str(path))
    try:
        flags = _flags(_config())
        if flags is not None:
            flags.set(HALT_FLAG_KEY, {
                "halted": False,
                "resumed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "by": by,
            })
            cleared.append("supabase:system_flags")
    except Exception:
        logger.warning("Could not clear the halt in Supabase", exc_info=True)
    _cache = None
    logger.warning("Trading resumed by %s", by or "unknown")
    return cleared
