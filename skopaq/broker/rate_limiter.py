"""Token bucket rate limiter for INDstocks API calls."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable


class RateLimiter:
    """Token bucket rate limiter.

    Enforces a maximum number of calls per second.  Callers ``await acquire()``
    before each API call — it will sleep if the bucket is empty.
    """

    def __init__(self, max_calls: float, period: float = 1.0) -> None:
        self.max_calls = max_calls
        self.period = period
        self._tokens = max_calls
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume one."""
        async with self._lock:
            self._refill()
            if self._tokens < 1:
                wait_time = (1 - self._tokens) * (self.period / self.max_calls)
                await asyncio.sleep(wait_time)
                self._refill()
            self._tokens -= 1

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.max_calls, self._tokens + elapsed * (self.max_calls / self.period))
        self._last_refill = now


class SlidingWindowLimiter:
    """At most ``max_calls`` calls in any ``period`` seconds.

    A rolling window, unlike the token bucket above: a full bucket plus a second of refill
    can let nearly twice ``max_calls`` through within one second, which a broker counting
    requests per second answers with 429. No lock is needed: the check and the record have
    no ``await`` between them.
    """

    def __init__(
        self,
        max_calls: int,
        period: float = 1.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.max_calls = max_calls
        self.period = period
        self._clock = clock
        self._sleep = sleep
        self._calls: deque[float] = deque()

    async def acquire(self) -> None:
        """Wait until a call fits in the window, then record it."""
        while True:
            now = self._clock()
            while self._calls and now - self._calls[0] >= self.period:
                self._calls.popleft()
            if len(self._calls) < self.max_calls:
                self._calls.append(now)
                return
            await self._sleep(max(self.period - (now - self._calls[0]), 0.001))
