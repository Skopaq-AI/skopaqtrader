"""_record_late_fill: a live fill the broker confirmed after its order was reported reaches the
trade rows — a BUY's row gets the new quantity, a stuck exit's extra shares close BUY rows."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from skopaq.db.models import TradeRecord
from skopaq.execution.live_orders import Confirmation, OrderOutcome, TrackedOrder


def _tracked(side="BUY", reported=0, price=None, requested=10) -> TrackedOrder:
    return TrackedOrder(order_id="EQ-1", side=side, symbol="TCS", security_id="11536",
                        segment="EQUITY", requested=Decimal(requested),
                        filled_reported=Decimal(reported),
                        avg_price_reported=None if price is None else Decimal(str(price)),
                        purpose="entry" if side == "BUY" else "exit")


def _conf(filled, price) -> Confirmation:
    return Confirmation(OrderOutcome.FILLED, "EQ-1", Decimal(10), Decimal(filled),
                        None if price is None else Decimal(str(price)), "trades",
                        "SUCCESS", "SUCCESS", "", may_be_open=False)


def _buy_row(**kw) -> TradeRecord:
    return TradeRecord(id=uuid4(), symbol="TCS", side="BUY", quantity=Decimal(4),
                       fill_price=Decimal("100"), order_id="EQ-1", is_paper=False, **kw)


async def _record(tracked, conf, repo):
    from skopaq.cli.main import _record_late_fill

    with patch("skopaq.cli.main._trade_repository", return_value=repo):
        return await _record_late_fill(MagicMock(), None, None, tracked, conf)


async def test_a_late_buy_fill_updates_its_open_row():   # T11
    repo = MagicMock()
    row = _buy_row()
    repo.find_by_order_id.return_value = row

    await _record(_tracked(reported=4, price=100), _conf(10, "100.6"), repo)

    repo.find_by_order_id.assert_called_once_with("EQ-1")
    repo.update.assert_called_once_with(row.id, {"quantity": "10", "fill_price": "100.6"})
    repo.insert.assert_not_called()


async def test_a_buy_reported_unconfirmed_gets_its_row_when_it_fills():   # T12
    repo = MagicMock()
    repo.find_by_order_id.return_value = None

    await _record(_tracked(), _conf(10, "101"), repo)

    [inserted] = [c.args[0] for c in repo.insert.call_args_list]
    assert (inserted.side, inserted.quantity, inserted.fill_price) == ("BUY", 10, Decimal("101"))
    assert inserted.order_id == "EQ-1" and inserted.is_paper is False
    assert inserted.status == "COMPLETE"
    assert inserted.entry_reason == "Late fill of EQ-1 adopted by the monitor"


async def test_a_late_fill_of_a_closed_buy_row_gets_its_own_row():   # T12
    repo = MagicMock()
    repo.find_by_order_id.return_value = _buy_row(closed_at="2026-09-25T10:00:00+00:00")

    await _record(_tracked(reported=4, price=100), _conf(10, 101), repo)

    [inserted] = [c.args[0] for c in repo.insert.call_args_list]
    assert inserted.quantity == 6 and inserted.order_id is None   # trades.order_id is UNIQUE
    assert inserted.fill_price == Decimal("101.6667")               # (101×10 − 100×4) / 6
    assert inserted.model_signals["broker"]["late_fill_of"] == "EQ-1"
    repo.update.assert_not_called()


async def test_a_stuck_exit_filling_later_is_recorded_as_an_exit_of_the_extra_shares():  # T13
    from skopaq.cli.main import _record_late_fill

    with patch("skopaq.cli.main._record_exit", new_callable=AsyncMock) as record:
        await _record_late_fill(MagicMock(), None, None,
                                _tracked("SELL", reported=4, price=95), _conf(10, "96.2"))

    _config, _graph, _store, signal, execution = record.await_args.args
    assert (signal.action, signal.quantity, signal.order_type.value) == ("SELL", 6, "MARKET")
    assert signal.entry_price == pytest.approx(97.0)          # (96.2×10 − 95×4) / 6
    assert (execution.success, execution.mode, execution.outcome) == (True, "live", "late_fill")
    assert execution.filled_quantity == 6 and execution.fill_price == pytest.approx(97.0)
    assert execution.order_ids == ["EQ-1"]


async def test_nothing_new_is_nothing_recorded():
    repo = MagicMock()
    await _record(_tracked(reported=10, price=100), _conf(10, 100), repo)
    repo.find_by_order_id.assert_not_called()


async def test_a_late_exit_fill_without_a_broker_price_uses_an_estimate(monkeypatch):
    from skopaq.broker.models import TradingSignal
    from skopaq.cli.main import _record_late_fill
    from skopaq.execution import order_alerts
    from tests.unit.execution._fakes import AlertSpy

    spy = AlertSpy()
    monkeypatch.setattr(order_alerts, "_alerter", spy)
    tracked = _tracked("SELL")
    tracked.signal = TradingSignal(symbol="TCS", action="SELL", entry_price=94.0)
    with patch("skopaq.cli.main._record_exit", new_callable=AsyncMock) as record:
        await _record_late_fill(MagicMock(), None, None, tracked, _conf(10, None))

    _config, _graph, _store, signal, execution = record.await_args.args
    assert signal.entry_price == 94.0 and execution.fill_price == 94.0   # never a P&L of 0
    assert execution.fill_price_source == "estimate"
    assert "fill-price-unknown:EQ-1" in spy.keys("WARNING")


async def test_a_late_exit_fill_with_no_price_at_all_leaves_the_rows_open(monkeypatch):
    from skopaq.cli.main import _record_late_fill
    from skopaq.execution import order_alerts
    from tests.unit.execution._fakes import AlertSpy

    spy = AlertSpy()
    monkeypatch.setattr(order_alerts, "_alerter", spy)
    with patch("skopaq.cli.main._record_exit", new_callable=AsyncMock) as record:
        await _record_late_fill(MagicMock(), None, None, _tracked("SELL"), _conf(10, None))

    record.assert_not_awaited()                                # not booked at break-even
    assert "exit-late-unpriced:EQ-1" in spy.keys("CRITICAL")


async def test_an_unpriced_late_buy_fill_is_recorded_at_its_limit_price(monkeypatch):
    from skopaq.execution import order_alerts
    from tests.unit.execution._fakes import AlertSpy

    alerts = AlertSpy()
    monkeypatch.setattr(order_alerts, "_alerter", alerts)
    repo = MagicMock()
    repo.find_by_order_id.return_value = None
    tracked = _tracked()
    tracked.price = 101.0                             # the LIMIT BUY's price

    await _record(tracked, _conf(10, None), repo)

    [inserted] = [c.args[0] for c in repo.insert.call_args_list]
    assert (inserted.fill_price, inserted.price) == (Decimal("101.0"), Decimal("101.0"))
    assert inserted.model_signals["broker"]["fill_price_source"] == "estimate"
    assert "fill-price-unknown:EQ-1" in alerts.keys("WARNING")


async def test_a_late_buy_fill_with_no_price_at_all_is_recorded_and_alerted(monkeypatch):
    from skopaq.execution import order_alerts
    from tests.unit.execution._fakes import AlertSpy

    alerts = AlertSpy()
    monkeypatch.setattr(order_alerts, "_alerter", alerts)
    repo = MagicMock()
    repo.find_by_order_id.return_value = None

    await _record(_tracked(), _conf(10, None), repo)   # a MARKET BUY: no limit price either

    [inserted] = [c.args[0] for c in repo.insert.call_args_list]
    assert inserted.quantity == 10 and inserted.fill_price is None
    assert "late-fill-unpriced:EQ-1" in alerts.keys("CRITICAL")


# ── Whether it was booked (the caller counts nothing it could not book) ─────


async def test_it_says_whether_a_late_buy_fill_was_booked():
    repo = MagicMock()
    repo.find_by_order_id.return_value = _buy_row()
    assert await _record(_tracked(reported=4, price=100), _conf(10, 100), repo) is True

    repo.update.side_effect = RuntimeError("503 Service Unavailable")
    assert await _record(_tracked(reported=4, price=100), _conf(10, 100), repo) is False


async def test_a_late_buy_fill_with_supabase_unreachable_is_not_booked():
    assert await _record(_tracked(reported=4, price=100), _conf(10, 100), None) is False


async def test_a_late_exit_fill_is_booked_only_if_the_exit_was(monkeypatch):
    from skopaq.cli.main import _record_late_fill

    for booked in (True, False):
        with patch("skopaq.cli.main._record_exit", new_callable=AsyncMock,
                   return_value=booked) as record:
            assert await _record_late_fill(MagicMock(), None, None,
                                           _tracked("SELL", reported=4, price=95),
                                           _conf(10, "96.2")) is booked
        assert record.await_args.kwargs == {"rollback_unbooked": True}


async def test_a_late_exit_fill_with_no_price_at_all_is_left_to_the_user(monkeypatch):
    """Handled (the CRITICAL says to close the rows by hand): no process books it later,
    after the user has."""
    from skopaq.cli.main import _record_late_fill
    from skopaq.execution import order_alerts
    from tests.unit.execution._fakes import AlertSpy

    monkeypatch.setattr(order_alerts, "_alerter", AlertSpy())
    with patch("skopaq.cli.main._record_exit", new_callable=AsyncMock) as record:
        assert await _record_late_fill(MagicMock(), None, None, _tracked("SELL"),
                                       _conf(10, None)) is True
    record.assert_not_awaited()
