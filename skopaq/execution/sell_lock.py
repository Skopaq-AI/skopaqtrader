"""One live SELL per symbol at a time, across Skopaq processes on this host.

The no-short-sale check reads the order book, positions and holdings, then the order is
placed and worked until final. Two processes (the daemon and a recovery ``skopaq monitor``,
or chat, or ``skopaq trade``) doing that for the same shares at the same time could both
pass the check and both sell. ``SellLock`` is an ``flock`` on
``<SKOPAQ_ORDER_LOCK_DIR>/sell-<SYMBOL>.lock`` (default ``~/.skopaq/locks``, on the home
volume every container shares), held from before the order-book read until the SELL is
final, so the second seller sees the first one's order in the book.

flock locks belong to an open file, so two coroutines of one process exclude each other
too. ``OrderLock`` is the same kind of lock on one order (``order-<ID>.lock``): one
process at a time resumes it and records its late fill. If the lock directory cannot be
used, the SELL goes ahead without the lock (with one WARNING per process): the order-book
check still guards it.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import re
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from skopaq.execution.order_alerts import get_alerter
from skopaq.execution.safety_checker import _base_symbol

logger = logging.getLogger(__name__)

_DEFAULT_DIR = "~/.skopaq/locks"
_POLL_S = 0.2
_BUSY = (errno.EAGAIN, errno.EWOULDBLOCK)   # held by someone else


class SellLockBusy(Exception):
    """Another Skopaq process (or task) kept selling this symbol past the wait."""


def lock_dir_from_config(config: object = None) -> Path:
    """``SKOPAQ_ORDER_LOCK_DIR``, else ``config.order_lock_dir`` (if a string), else
    ``~/.skopaq/locks``."""
    env = os.environ.get("SKOPAQ_ORDER_LOCK_DIR", "").strip()
    value = getattr(config, "order_lock_dir", None) if config is not None else None
    configured = value.strip() if isinstance(value, str) else ""
    return Path(env or configured or _DEFAULT_DIR).expanduser()


def lock_dir_or_none(config: object = None) -> Optional[Path]:
    """``lock_dir_from_config``, or None (with a WARNING naming the setting) when the path
    cannot be resolved, e.g. ``~user`` for a user this host does not have: then SELLs go
    ahead without the cross-process lock, as for any unusable lock directory."""
    try:
        return lock_dir_from_config(config)
    except (RuntimeError, OSError, ValueError) as exc:
        logger.warning("SKOPAQ_ORDER_LOCK_DIR cannot be used (%s); live SELLs run without "
                       "the cross-process lock", exc)
        return None


def _lock_key(symbol: str) -> str:
    """The base symbol (``NSE:RELIANCE-EQ`` / ``reliance.ns`` → ``RELIANCE``), with anything
    but letters, digits, ``_`` and ``-`` replaced, so it is always a plain file name."""
    return re.sub(r"[^A-Z0-9_-]", "_", _base_symbol(symbol)) or "_"


class SellLock:
    """flock on <lock_dir>/sell-<SYMBOL>.lock, held from before the order-book read until
    the SELL is final.

    Across processes that share the lock dir (the daemon, ``skopaq monitor``, chat: all on
    the skopaq-home volume) and across coroutines of one process (flock
    conflicts between separate open file descriptions). Polls LOCK_EX | LOCK_NB every
    0.2 s (never blocks the event loop) for up to ``wait_s``, then raises SellLockBusy.
    """

    def __init__(
        self,
        symbol: str,
        *,
        wait_s: float,
        lock_dir: Optional[Path | str] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.symbol = symbol
        self.wait_s = max(0.0, float(wait_s))
        directory = Path(lock_dir).expanduser() if lock_dir is not None else lock_dir_from_config()
        self.path = directory / f"sell-{_lock_key(symbol)}.lock"
        self._sleep = sleep
        self._clock = clock
        self._fd: Optional[int] = None

    def _busy(self, holder: str) -> SellLockBusy:
        return SellLockBusy(f"Another Skopaq process is already selling {self.symbol}"
                            f"{f' (pid {holder})' if holder else ''}; waited {self.wait_s:.0f}s")

    async def __aenter__(self) -> SellLock:
        try:
            # Imported here: only live SELLs lock, and fcntl is POSIX-only (paper mode
            # imports this module on any platform)
            import fcntl
        except ImportError as exc:
            self._unavailable(exc)
            return self
        try:
            self.path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        except OSError as exc:
            self._unavailable(exc)
            return self
        deadline = self._clock() + self.wait_s
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in _BUSY:
                        os.close(fd)
                        self._unavailable(exc)
                        return self
                remaining = deadline - self._clock()
                if remaining <= 0:
                    holder = self._holder(fd)
                    os.close(fd)
                    raise self._busy(holder)
                await self._sleep(min(_POLL_S, remaining))
        except SellLockBusy:
            raise
        except BaseException:
            os.close(fd)                     # cancelled while waiting: nothing is held
            raise
        self._fd = fd
        self._note_holder(fd)
        return self

    @property
    def held(self) -> bool:
        """The lock is held (False once the lock directory proved unusable: then the caller
        goes ahead without it)."""
        return self._fd is not None

    async def __aexit__(self, *exc: object) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        import fcntl

        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _unavailable(self, exc: Exception) -> None:
        # Deduplicated by the process-wide alerter: one WARNING per process
        get_alerter().alert(
            "WARNING", "sell-lock-unavailable",
            f"SELL locks unavailable ({self.path.parent}: {exc}); selling without the "
            "cross-process lock — the order-book check still applies")

    @staticmethod
    def _note_holder(fd: int) -> None:
        """Write our pid into the lock file (only for the busy message of the next seller)."""
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, f"{os.getpid()}\n".encode(), 0)
        except OSError:
            pass

    @staticmethod
    def _holder(fd: int) -> str:
        try:
            text = os.pread(fd, 32, 0).decode(errors="replace").strip()
        except OSError:
            return ""
        return text if text.isdigit() else ""


class OrderLock(SellLock):
    """flock on <lock_dir>/order-<ORDER ID>.lock: one Skopaq process at a time resumes a
    live order and records its late fill, so a fill is never recorded twice when the
    daemon and a recovery ``skopaq monitor`` both know the order.

    Tried once, never waited for: busy means another process is on it right now, and the
    caller leaves the order for its next resync. Raises ``SellLockBusy`` then.
    """

    def __init__(
        self,
        order_id: str,
        *,
        lock_dir: Optional[Path | str] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(order_id, wait_s=0.0, lock_dir=lock_dir, sleep=sleep, clock=clock)
        key = re.sub(r"[^A-Za-z0-9_-]", "_", order_id) or "_"
        self.path = self.path.parent / f"order-{key}.lock"

    def _busy(self, holder: str) -> SellLockBusy:
        return SellLockBusy(f"Another Skopaq process is resuming order {self.symbol}"
                            f"{f' (pid {holder})' if holder else ''}")
