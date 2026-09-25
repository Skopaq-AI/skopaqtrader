"""Telegram bot: only allow-listed chats, and trades wait for /confirm."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("telegram")

from skopaq import telegram_bot  # noqa: E402


def _update(chat_id: int):
    message = MagicMock()
    message.chat.id = chat_id
    message.reply_text = AsyncMock()
    message.chat.send_action = AsyncMock()
    return SimpleNamespace(effective_chat=SimpleNamespace(id=chat_id), message=message)


@pytest.fixture
def allow(monkeypatch):
    monkeypatch.setenv("SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS", "111, -222")


@pytest.mark.asyncio
async def test_strangers_get_only_their_chat_id(allow):
    handler = AsyncMock()
    update = _update(999)

    await telegram_bot.authorized(handler)(update, MagicMock())

    handler.assert_not_awaited()
    assert "999" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_allow_listed_chats_reach_the_handler(allow):
    handler = AsyncMock()
    for chat_id in (111, -222):  # group chats have negative IDs
        await telegram_bot.authorized(handler)(_update(chat_id), MagicMock())
    assert handler.await_count == 2


@pytest.mark.asyncio
async def test_empty_allow_list_answers_nobody(monkeypatch):
    monkeypatch.delenv("SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("SKOPAQ_TELEGRAM_CHAT_ID", raising=False)
    handler = AsyncMock()
    await telegram_bot.authorized(handler)(_update(111), MagicMock())
    handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_owner_notification_chat_is_allowed(monkeypatch):
    monkeypatch.delenv("SKOPAQ_TELEGRAM_ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.setenv("SKOPAQ_TELEGRAM_CHAT_ID", "555")
    handler = AsyncMock()
    await telegram_bot.authorized(handler)(_update(555), MagicMock())
    handler.assert_awaited_once()


class FakeAgent:
    """Paused on a sequence of tool calls; each ainvoke(None) runs one step."""

    def __init__(self, steps):
        self.steps = list(steps)  # tool-call lists the agent pauses on, in order
        self.resumed = 0
        self.updates = []

    def get_state(self, config):
        if not self.steps:
            return SimpleNamespace(next=(), values={"messages": []})
        message = SimpleNamespace(tool_calls=self.steps[0])
        return SimpleNamespace(next=("tools",), values={"messages": [message]})

    async def ainvoke(self, _input, config=None):
        self.resumed += 1
        self.steps.pop(0)
        return {"messages": [SimpleNamespace(type="ai", content="done")]}

    def update_state(self, config, values):
        self.updates.append(values)


QUOTE = [{"name": "get_quote", "args": {"symbol": "TCS"}, "id": "q1"}]
TRADE = [{"name": "trade_stock", "args": {"symbol": "TCS"}, "id": "t1"}]


@pytest.mark.asyncio
async def test_read_only_tools_resume_but_a_trade_waits_for_confirmation():
    agent = FakeAgent([QUOTE, TRADE])
    update = _update(111)

    result = await telegram_bot._resume_until_gated(update, agent, {}, {"messages": []})

    assert result is None  # stopped before the trade
    assert agent.resumed == 1  # only the quote ran
    reply = update.message.reply_text.await_args.args[0]
    assert "trade_stock" in reply and "/confirm" in reply


@pytest.mark.asyncio
async def test_confirm_executes_the_pending_trade(allow, monkeypatch):
    agent = FakeAgent([TRADE])
    session = MagicMock(ensure_agent=MagicMock(return_value=agent), thread_config={})
    monkeypatch.setitem(telegram_bot._telegram_sessions, 111, session)

    await telegram_bot.cmd_confirm(_update(111), MagicMock())

    assert agent.resumed == 1
    assert agent.updates == []


@pytest.mark.asyncio
async def test_cancel_answers_the_trade_call_without_running_it(allow, monkeypatch):
    agent = FakeAgent([TRADE])
    session = MagicMock(ensure_agent=MagicMock(return_value=agent), thread_config={})
    monkeypatch.setitem(telegram_bot._telegram_sessions, 111, session)

    await telegram_bot.cmd_cancel(_update(111), MagicMock())

    cancelled = agent.updates[0]["messages"][0]
    assert (cancelled.tool_call_id, cancelled.content) == ("t1", "Trade cancelled by user.")


@pytest.mark.asyncio
async def test_confirm_without_a_pending_trade(allow):
    update = _update(111)
    await telegram_bot.cmd_confirm(update, MagicMock())
    assert "No trade" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_halt_and_resume_commands(allow, tmp_path, monkeypatch):
    from skopaq.execution import kill_switch

    monkeypatch.setenv("SKOPAQ_HALT_FILE", str(tmp_path / "HALT"))
    update = _update(111)

    await telegram_bot.cmd_halt(update, SimpleNamespace(args=["results", "day"]))
    assert kill_switch.status(use_cache=False).reason == "results day"
    assert "HALTED" in update.message.reply_text.await_args.args[0]

    await telegram_bot.cmd_resume(update, SimpleNamespace(args=[]))
    assert not kill_switch.status(use_cache=False).halted



def test_kite_login_url_comes_from_config(monkeypatch):
    monkeypatch.setenv("SKOPAQ_PUBLIC_BASE_URL", "")
    assert telegram_bot._kite_login_url() == ""

    monkeypatch.setenv("SKOPAQ_PUBLIC_BASE_URL", "https://x.example/")
    assert telegram_bot._kite_login_url() == "https://x.example/api/kite/login"


@pytest.mark.asyncio
async def test_login_without_a_public_url_says_so(allow, monkeypatch):
    monkeypatch.setenv("SKOPAQ_PUBLIC_BASE_URL", "")
    update = _update(111)
    # Not a sys.modules stub: `import skopaq.broker.kite_client as kite` finds an already
    # imported real module through the package attribute and would read its token file.
    monkeypatch.setattr(telegram_bot, "_kite_token", lambda: "")

    await telegram_bot.cmd_login(update, MagicMock())
    assert "SKOPAQ_PUBLIC_BASE_URL" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_market_jobs_skip_non_trading_days(monkeypatch):
    from datetime import datetime

    from skopaq.risk.calendar import IST

    monkeypatch.setenv("SKOPAQ_NSE_HOLIDAYS", "")
    monkeypatch.setattr("skopaq.risk.calendar.now_ist",
                        lambda: datetime(2026, 10, 2, 9, 0, tzinfo=IST))  # a holiday
    monkeypatch.setattr(telegram_bot, "alert_chat_ids", {111})
    context = MagicMock()
    context.bot.send_message = AsyncMock()

    for job in (telegram_bot.job_pre_market_login, telegram_bot.job_market_scan,
                telegram_bot.job_eod_summary):
        await job(context)

    context.bot.send_message.assert_not_awaited()
