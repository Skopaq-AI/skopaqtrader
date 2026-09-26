"""Order alerts and trade events as they reach Telegram."""

from __future__ import annotations

import pytest

from skopaq import notifications


@pytest.fixture
def sent(monkeypatch):
    messages: list[str] = []

    async def fake_notify(message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(notifications, "notify", fake_notify)
    return messages


async def test_order_alert_format(sent):
    await notifications.notify_order_alert(
        "CRITICAL", "order-stuck:EQ-1", "may still be working — cancel it at the broker",
        order_ids=["EQ-1", "EQ-2"])
    await notifications.notify_order_alert("WARNING", "late-fill:EQ-3", "adopted")

    critical, warning = sent
    assert critical.splitlines() == [
        "🚨 CRITICAL ORDER ALERT", "order-stuck:EQ-1",
        "may still be working — cancel it at the broker", "Orders: EQ-1, EQ-2"]
    assert warning.splitlines()[0] == "⚠️ WARNING ORDER ALERT"
    assert "Orders:" not in warning


async def test_order_alert_never_raises(monkeypatch):
    async def broken(message: str) -> None:
        raise RuntimeError("telegram down")

    monkeypatch.setattr(notifications, "notify", broken)
    await notifications.notify_order_alert("CRITICAL", "k", "t")


async def test_trade_event_reason_and_partial_statuses(sent):
    await notifications.notify_trade_event("SELL", "TCS", 94.0, 3, "PARTIAL",
                                           reason="sold 3 of 5 after 3 attempt(s)")
    await notifications.notify_trade_event("BUY", "INFY", 1500.0, 0, "UNCONFIRMED",
                                           reason="order may still be working")
    assert sent[0].splitlines()[0] == "🔴 SELL TCS — 🟡 PARTIAL"
    assert "sold 3 of 5 after 3 attempt(s)" in sent[0]
    assert sent[1].splitlines()[0] == "🟢 BUY INFY — ❓ UNCONFIRMED"


async def test_trade_event_without_a_reason_is_unchanged(sent):
    await notifications.notify_trade_event("BUY", "TCS", 2503.0, 1, "FILLED", order_id="EQ-1")
    assert sent == ["🟢 BUY TCS — ✅ FILLED\nQty: 1 @ Rs 2,503.00\nOrder: EQ-1"]
