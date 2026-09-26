"""Alerts about live orders: logged at once, sent in the background, deduplicated.

Placing, confirming and cancelling an order must never wait for Telegram, so
``OrderAlerter.alert()`` logs straight away and sends the notification as a background
task. Alerts are deduplicated by key within this process: a blocked exit retried every
10 seconds sends one message, not one per retry. ``drain()`` gives pending sends a
bounded time to finish before the process exits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

NotifyFn = Callable[..., Awaitable[None]]


class OrderAlerter:
    """Sends CRITICAL/WARNING order alerts without blocking the caller.

    Args:
        notify: ``async (severity, title, text, *, order_ids)``; defaults to
            ``skopaq.notifications.notify_order_alert``.
        clock: Monotonic clock for the dedup windows.
    """

    def __init__(self, notify: Optional[NotifyFn] = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._notify = notify
        self._clock = clock
        self._last: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    def alert(
        self,
        severity: str,
        key: str,
        text: str,
        *,
        order_ids: Iterable[str] = (),
        dedup_s: float = 0.0,
    ) -> None:
        """Log the alert now and send it in the background; never raises.

        The same ``key`` is sent again only after ``dedup_s`` seconds, and never again
        in this process when ``dedup_s`` is 0.
        """
        try:
            now = self._clock()
            last = self._last.get(key)
            if last is not None and (dedup_s <= 0 or now - last < dedup_s):
                logger.debug("Order alert %s suppressed (sent %.0fs ago)", key, now - last)
                return
            self._last[key] = now
            ids = tuple(i for i in order_ids if isinstance(i, str) and i)
            level = logging.ERROR if severity.upper() == "CRITICAL" else logging.WARNING
            logger.log(level, "ORDER ALERT %s %s: %s%s", severity, key, text,
                       f" (orders: {', '.join(ids)})" if ids else "")
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return                  # no event loop (a synchronous caller): the log is the alert
            task = loop.create_task(self._send(severity, key, text, ids))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception:
            logger.exception("Order alert %s could not be raised", key)

    def background(self, coro: Awaitable[None], what: str = "notification") -> None:
        """Run ``coro`` (another notification, say a live trade's) as a background task
        that ``drain()`` waits for too; its errors are logged. Without an event loop the
        coroutine is closed unsent."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            close = getattr(coro, "close", None)
            if close is not None:
                close()
            return

        async def run() -> None:
            try:
                await coro
            except Exception as exc:
                logger.warning("Background %s failed: %s", what, exc)

        task = loop.create_task(run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _send(self, severity: str, key: str, text: str, order_ids: tuple[str, ...]) -> None:
        try:
            notify = self._notify
            if notify is None:
                from skopaq.notifications import notify_order_alert as notify
            await notify(severity, key, text, order_ids=order_ids)
        except Exception as exc:
            logger.warning("Order alert %s: notification failed: %s", key, exc)

    async def drain(self, timeout: float = 10.0) -> None:
        """Wait up to ``timeout`` seconds for alerts still being sent (at process end)."""
        loop = asyncio.get_running_loop()
        pending = [t for t in self._tasks if not t.done() and t.get_loop() is loop]
        if not pending:
            return
        _, still = await asyncio.wait(pending, timeout=timeout)
        if still:
            logger.warning("%d order alert(s) not delivered within %.0fs", len(still), timeout)


_alerter: Optional[OrderAlerter] = None


def get_alerter() -> OrderAlerter:
    """The process-wide alerter (one dedup memory for the daemon, monitor and chat)."""
    global _alerter
    if _alerter is None:
        _alerter = OrderAlerter()
    return _alerter


def reset_alerter() -> None:
    """Forget the process-wide alerter and its dedup memory (tests)."""
    global _alerter
    _alerter = None
