"""SellLock: one SELL per symbol at a time, across processes and coroutines (finding 13).

The lock is an flock on ``<lock dir>/sell-<SYMBOL>.lock``, held from before the order-book
read until the SELL is final, so two Skopaq processes (or two coroutines of one) cannot
both pass the no-short-sale check for the same shares.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
import textwrap
from unittest.mock import MagicMock

import pytest

from skopaq.execution import order_alerts
from skopaq.execution.sell_lock import SellLock, SellLockBusy, lock_dir_from_config
from tests.unit.execution._fakes import AlertSpy, FakeClock


def _lock(symbol, tmp_path, clock, wait_s=30.0):
    return SellLock(symbol, wait_s=wait_s, lock_dir=tmp_path, sleep=clock.sleep,
                    clock=clock.clock)


async def test_a_second_seller_of_the_same_symbol_waits_for_the_first(tmp_path):   # T12
    clock = FakeClock()
    events = []

    async def first():
        async with _lock("RELIANCE", tmp_path, clock):
            events.append(("first in", clock.t))
            await clock.sleep(5)
            events.append(("first out", clock.t))

    async def second():
        await clock.sleep(1)
        async with _lock("NSE:RELIANCE-EQ", tmp_path, clock):   # the same base symbol
            events.append(("second in", clock.t))

    await asyncio.gather(first(), second())

    assert [e[0] for e in events] == ["first in", "first out", "second in"]
    assert 5.0 <= events[2][1] <= 5.25          # polled every 0.2 s, got it on release


async def test_other_symbols_do_not_wait(tmp_path):
    clock = FakeClock()
    async with _lock("TCS", tmp_path, clock):
        async with _lock("INFY", tmp_path, clock, wait_s=0):
            pass
    assert clock.t == 0


async def test_busy_past_the_wait_raises(tmp_path):   # T13
    clock = FakeClock()
    async with _lock("TCS", tmp_path, clock):
        with pytest.raises(SellLockBusy, match="TCS"):
            async with _lock("TCS", tmp_path, clock, wait_s=2):
                pytest.fail("the lock is held")
    assert clock.t == pytest.approx(2.0, abs=0.01)

    # Released: free again at once
    async with _lock("TCS", tmp_path, clock, wait_s=0):
        pass


async def test_a_cancelled_waiter_leaves_the_lock_usable(tmp_path):
    clock = FakeClock()
    holder = _lock("TCS", tmp_path, clock)
    await holder.__aenter__()
    waiter = asyncio.ensure_future(_lock("TCS", tmp_path, clock).__aenter__())
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    await holder.__aexit__(None, None, None)

    async with _lock("TCS", tmp_path, clock, wait_s=0):
        pass


def test_another_process_holding_the_lock_blocks_us(tmp_path):   # T14
    path = tmp_path / "sell-RELIANCE.lock"
    holder = textwrap.dedent(f"""
        import fcntl, os, sys
        fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
        print("locked", flush=True)
        sys.stdin.read()
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)

    async def enter(wait_s):
        async with SellLock("RELIANCE", wait_s=wait_s, lock_dir=tmp_path):
            pass

    try:
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(SellLockBusy, match="RELIANCE"):
            asyncio.run(enter(0.3))
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)

    asyncio.run(enter(0))                        # the other process is gone: free


async def test_an_unusable_lock_dir_proceeds_with_one_warning(tmp_path, caplog):   # T15
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("a file where the lock dir should be")
    clock = FakeClock()
    entered = 0

    with caplog.at_level(logging.WARNING, logger="skopaq.execution.order_alerts"):
        for _ in range(2):
            async with _lock("TCS", blocker / "locks", clock, wait_s=0):
                entered += 1

    assert entered == 2                          # the SELL goes ahead (the book still guards)
    warnings = [r for r in caplog.records if "sell-lock-unavailable" in r.getMessage()]
    assert len(warnings) == 1                    # once per process
    assert warnings[0].levelno == logging.WARNING


async def test_the_unavailable_warning_goes_through_the_alerter(tmp_path, monkeypatch):
    spy = AlertSpy()
    monkeypatch.setattr(order_alerts, "_alerter", spy)
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    async with _lock("TCS", blocker / "locks", FakeClock(), wait_s=0):
        pass
    [(severity, key, text, _)] = spy.alerts
    assert (severity, key) == ("WARNING", "sell-lock-unavailable")
    assert "not-a-dir" in text


def test_the_lock_file_name_is_the_base_symbol_restricted_to_safe_characters(tmp_path):
    assert SellLock("reliance.ns", wait_s=0, lock_dir=tmp_path).path == \
        tmp_path / "sell-RELIANCE.lock"
    assert SellLock("M&M", wait_s=0, lock_dir=tmp_path).path == tmp_path / "sell-M_M.lock"
    assert SellLock("../../etc/x", wait_s=0, lock_dir=tmp_path).path.parent == tmp_path


def test_the_lock_dir_comes_from_the_env_then_the_config(tmp_path, monkeypatch):
    config = MagicMock()
    config.order_lock_dir = str(tmp_path / "from-config")
    monkeypatch.setenv("SKOPAQ_ORDER_LOCK_DIR", str(tmp_path / "from-env"))
    assert lock_dir_from_config(config) == tmp_path / "from-env"

    monkeypatch.delenv("SKOPAQ_ORDER_LOCK_DIR")
    assert lock_dir_from_config(config) == tmp_path / "from-config"
    # A mock config value (not a str) falls back to the default
    assert str(lock_dir_from_config(MagicMock())).endswith(".skopaq/locks")


def test_paper_mode_imports_without_fcntl():
    # fcntl is POSIX-only: the executor, router and MCP server (paper mode on any
    # platform) must import without it; only a live SELL's lock and the journal use it
    code = ("import sys; sys.modules['fcntl'] = None; "
            "import skopaq.execution.executor, skopaq.execution.order_router, "
            "skopaq.mcp_server")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]


async def test_without_fcntl_the_sell_goes_ahead_with_one_warning(tmp_path, monkeypatch):
    spy = AlertSpy()
    monkeypatch.setattr(order_alerts, "_alerter", spy)
    monkeypatch.setitem(sys.modules, "fcntl", None)
    clock = FakeClock()
    async with _lock("RELIANCE", tmp_path, clock):
        pass
    assert spy.keys("WARNING") == ["sell-lock-unavailable"]
