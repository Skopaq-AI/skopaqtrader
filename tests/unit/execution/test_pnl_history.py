"""Loss limits across processes: realized P&L read back from stored trades."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from skopaq.broker.models import Funds
from skopaq.db.models import TradeRecord
from skopaq.execution import pnl_history
from skopaq.execution.pnl_history import RealizedPnl, period_starts, summarize

# Friday 2026-09-25, 01:30 IST
NOW = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)


def _closed(when: str, pnl) -> TradeRecord:
    return TradeRecord(symbol="TCS", side="BUY", quantity=Decimal("1"),
                       closed_at=datetime.fromisoformat(when), pnl=pnl)


def test_periods_follow_the_ist_calendar():
    day, week, month = period_starts(NOW)
    assert day.isoformat() == "2026-09-25T00:00:00+05:30"
    assert week.isoformat() == "2026-09-21T00:00:00+05:30"  # Monday
    assert month.isoformat() == "2026-09-01T00:00:00+05:30"


def test_summarize_buckets_by_close_time():
    trades = [
        _closed("2026-09-24T19:00:00+00:00", Decimal("-100")),  # 00:30 IST today
        _closed("2026-09-24T10:00:00+00:00", Decimal("-200")),  # yesterday IST
        _closed("2026-09-19T10:00:00+00:00", Decimal("50")),    # last week
        _closed("2026-08-31T10:00:00+00:00", Decimal("-999")),  # last month
        _closed("2026-09-24T19:30:00", Decimal("-10")),         # naive = UTC, today
        _closed("2026-09-24T19:40:00+00:00", None),             # no P&L recorded
    ]
    assert summarize(trades, NOW) == RealizedPnl(day=-110.0, week=-310.0, month=-260.0)


def test_seeded_weekly_loss_blocks_a_new_process():
    from skopaq.constants import SafetyRules
    from skopaq.execution.safety_checker import SafetyChecker
    from tests.unit.execution.test_safety_checker import _buy_order

    checker = SafetyChecker(rules=SafetyRules(market_hours_only=False, require_stop_loss=False))
    # 8% down this week, from sessions that have already ended
    with patch.object(pnl_history, "load_realized_pnl",
                      return_value=RealizedPnl(day=0.0, week=-80_000.0, month=-80_000.0)):
        pnl_history.seed_safety_checker(checker, MagicMock())

    result = checker.validate(_buy_order(qty=1, price=100), None, [],
                              Funds(available_margin=1_000_000), 1_000_000)
    assert any("Weekly loss" in r for r in result.rejections)


def test_without_supabase_nothing_is_seeded():
    config = MagicMock(supabase_url="")
    assert pnl_history.load_realized_pnl(config) is None
    checker = MagicMock()
    pnl_history.seed_safety_checker(checker, config)
    checker.seed_realized_pnl.assert_not_called()


def test_reads_closed_buys_for_the_current_mode_since_the_earlier_period():
    config = MagicMock(supabase_url="https://x", trading_mode="live")
    config.supabase_service_key.get_secret_value.return_value = "key"
    repo = MagicMock()
    repo.get_closed_since.return_value = [_closed("2026-09-24T19:00:00+00:00", Decimal("-5"))]

    with patch("supabase.create_client"), \
         patch("skopaq.db.repositories.TradeRepository", return_value=repo):
        realized = pnl_history.load_realized_pnl(config, NOW)

    assert realized == RealizedPnl(day=-5.0, week=-5.0, month=-5.0)
    since, = repo.get_closed_since.call_args.args
    assert since.isoformat() == "2026-09-01T00:00:00+05:30"  # month began before the week
    assert repo.get_closed_since.call_args.kwargs == {"is_paper": False}


def test_supabase_error_leaves_limits_process_local():
    config = MagicMock(supabase_url="https://x", trading_mode="paper")
    config.supabase_service_key.get_secret_value.return_value = "key"
    with patch("supabase.create_client", side_effect=RuntimeError("down")):
        assert pnl_history.load_realized_pnl(config, NOW) is None


def test_repository_reads_closed_buys_only():
    from skopaq.db.repositories import TradeRepository

    client = MagicMock()
    query = client.table.return_value.select.return_value
    query.eq.return_value.eq.return_value.gte.return_value.execute.return_value.data = []

    TradeRepository(client).get_closed_since(NOW, is_paper=True)

    query.eq.assert_called_once_with("side", "BUY")
    query.eq.return_value.eq.assert_called_once_with("is_paper", True)
    query.eq.return_value.eq.return_value.gte.assert_called_once_with(
        "closed_at", NOW.isoformat())
