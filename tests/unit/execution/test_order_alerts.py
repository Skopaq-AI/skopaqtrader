"""OrderAlerter: background, deduplicated order alerts (design v2 §6.6, finding 18)."""

from __future__ import annotations

import asyncio
import logging
import time

from skopaq import notifications
from skopaq.execution.order_alerts import OrderAlerter, get_alerter


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def __call__(self, severity, title, text, *, order_ids=()):
        self.sent.append((severity, title, text, tuple(order_ids)))


async def test_the_same_key_within_the_window_is_sent_once():   # T36
    notify, now = _Recorder(), [0.0]
    alerter = OrderAlerter(notify=notify, clock=lambda: now[0])

    alerter.alert("CRITICAL", "exit-not-filled:TCS", "first", dedup_s=600)
    now[0] = 300
    alerter.alert("CRITICAL", "exit-not-filled:TCS", "again", dedup_s=600)
    await alerter.drain(1)
    assert [s[2] for s in notify.sent] == ["first"]

    now[0] = 601
    alerter.alert("CRITICAL", "exit-not-filled:TCS", "later", dedup_s=600)
    await alerter.drain(1)
    assert [s[2] for s in notify.sent] == ["first", "later"]


async def test_zero_dedup_means_once_per_process():   # T36
    notify, now = _Recorder(), [0.0]
    alerter = OrderAlerter(notify=notify, clock=lambda: now[0])
    alerter.alert("CRITICAL", "order-stuck:EQ-1", "stuck", order_ids=["EQ-1"])
    now[0] = 10 ** 6
    alerter.alert("CRITICAL", "order-stuck:EQ-1", "stuck", order_ids=["EQ-1"])
    alerter.alert("CRITICAL", "order-stuck:EQ-2", "stuck", order_ids=["EQ-2"])
    await alerter.drain(1)

    assert [(s[1], s[3]) for s in notify.sent] == [
        ("order-stuck:EQ-1", ("EQ-1",)), ("order-stuck:EQ-2", ("EQ-2",))]


async def test_alert_returns_at_once_and_drain_is_bounded():   # T37
    gate = asyncio.Event()

    async def slow(*args, **kwargs):
        await gate.wait()

    alerter = OrderAlerter(notify=slow)
    started = time.monotonic()
    alerter.alert("CRITICAL", "order-stuck:EQ-1", "stuck")     # synchronous: never waits
    await alerter.drain(timeout=0.05)
    assert time.monotonic() - started < 1.0

    gate.set()
    await alerter.drain(1)


async def test_a_failing_notification_is_swallowed(caplog):   # T37
    async def broken(*args, **kwargs):
        raise RuntimeError("telegram down")

    alerter = OrderAlerter(notify=broken)
    with caplog.at_level(logging.WARNING):
        alerter.alert("WARNING", "late-fill:EQ-1", "late")
        await alerter.drain(1)
    assert "telegram down" in caplog.text


def test_without_an_event_loop_the_alert_is_logged(caplog):
    alerter = OrderAlerter(notify=_Recorder())
    with caplog.at_level(logging.ERROR):
        alerter.alert("CRITICAL", "order-stuck:EQ-1", "cancel it at the broker")
    assert "order-stuck:EQ-1" in caplog.text and "cancel it at the broker" in caplog.text


def test_the_alerter_is_process_wide():
    assert get_alerter() is get_alerter()


async def test_the_default_notifier_sends_an_order_alert(monkeypatch):
    messages: list[str] = []

    async def fake_notify(message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(notifications, "notify", fake_notify)
    alerter = OrderAlerter()
    alerter.alert("CRITICAL", "order-stuck:EQ-1", "cancel it at the broker", order_ids=["EQ-1"])
    await alerter.drain(1)

    [message] = messages
    assert message.startswith("🚨 CRITICAL ORDER ALERT")
    assert "order-stuck:EQ-1" in message and "Orders: EQ-1" in message
