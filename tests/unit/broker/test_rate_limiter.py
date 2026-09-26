"""Tests for token bucket rate limiter."""

import asyncio
import time

import pytest

from skopaq.broker.rate_limiter import RateLimiter


@pytest.mark.asyncio
async def test_acquire_within_limit():
    """Acquiring within limit should not block."""
    limiter = RateLimiter(max_calls=10, period=1.0)
    start = time.monotonic()
    for _ in range(10):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    # Should complete almost instantly
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_acquire_exceeding_limit_sleeps():
    """Exceeding limit should cause a brief sleep."""
    limiter = RateLimiter(max_calls=2, period=1.0)
    # Use up all tokens
    await limiter.acquire()
    await limiter.acquire()
    # Third call should sleep
    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed > 0.1  # Should have waited


@pytest.mark.asyncio
async def test_tokens_refill_over_time():
    """Tokens should refill after waiting."""
    limiter = RateLimiter(max_calls=1, period=0.1)
    await limiter.acquire()
    # Wait for refill
    await asyncio.sleep(0.15)
    start = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - start
    # Should not block since token was refilled
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_sliding_window_never_lets_more_than_the_limit_through_in_any_second():
    from skopaq.broker.rate_limiter import SlidingWindowLimiter

    now = {"t": 0.0}

    async def sleep(seconds):
        now["t"] += seconds

    limiter = SlidingWindowLimiter(12, 1.0, clock=lambda: now["t"], sleep=sleep)
    times = []
    for _ in range(40):
        await limiter.acquire()
        times.append(now["t"])
        now["t"] += 0.01                     # the request itself
    for start in times:
        assert sum(1 for t in times if start <= t < start + 1.0) <= 12
    assert times[11] < 0.2 and times[12] >= 1.0
