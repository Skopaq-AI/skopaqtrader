"""Realized P&L from stored trades, for the daily/weekly/monthly loss limits.

``SafetyChecker`` keeps its loss totals in memory, and every CLI command, MCP
call and daemon session (a fresh process each morning) builds a new checker.
This module reads the P&L of positions closed earlier from Supabase and
seeds the checker with it, so a 3% daily, 7% weekly or 12% monthly loss
stops trading whichever process it happened in.

Periods follow the IST calendar: the day from midnight, the week from Monday,
the month from the 1st. Paper and live trades are counted separately.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable, Optional

if TYPE_CHECKING:
    from skopaq.db.models import TradeRecord
    from skopaq.execution.safety_checker import SafetyChecker

logger = logging.getLogger(__name__)

_IST = timezone(timedelta(hours=5, minutes=30))


@dataclass(frozen=True)
class RealizedPnl:
    day: float = 0.0
    week: float = 0.0
    month: float = 0.0


def period_starts(now: datetime) -> tuple[datetime, datetime, datetime]:
    """IST midnight today, Monday of this week, and the 1st of this month."""
    ist = now.astimezone(_IST)
    day = ist.replace(hour=0, minute=0, second=0, microsecond=0)
    return day, day - timedelta(days=day.weekday()), day.replace(day=1)


def summarize(trades: Iterable[TradeRecord], now: datetime) -> RealizedPnl:
    """Sum closed positions' P&L into today, this week and this month."""
    day_start, week_start, month_start = period_starts(now)
    day = week = month = 0.0
    for trade in trades:
        if trade.pnl is None or trade.closed_at is None:
            continue
        closed = trade.closed_at
        if closed.tzinfo is None:
            closed = closed.replace(tzinfo=timezone.utc)
        pnl = float(trade.pnl)
        if closed >= month_start:
            month += pnl
        if closed >= week_start:
            week += pnl
        if closed >= day_start:
            day += pnl
    return RealizedPnl(day=day, week=week, month=month)


def load_realized_pnl(config, now: Optional[datetime] = None) -> Optional[RealizedPnl]:
    """Realized P&L for the current trading mode, or ``None`` if unavailable."""
    if not config.supabase_url or not config.supabase_service_key.get_secret_value():
        return None
    now = now or datetime.now(timezone.utc)
    try:
        from skopaq.db.repositories import TradeRepository
        from supabase import create_client

        client = create_client(
            config.supabase_url, config.supabase_service_key.get_secret_value()
        )
        _, week_start, month_start = period_starts(now)
        trades = TradeRepository(client).get_closed_since(
            min(week_start, month_start), is_paper=config.trading_mode != "live"
        )
    except Exception:
        logger.warning(
            "Could not read realized P&L from Supabase — loss limits see only "
            "this process's trades", exc_info=True,
        )
        return None
    return summarize(trades, now)


def seed_safety_checker(safety: SafetyChecker, config, now: Optional[datetime] = None) -> None:
    """Seed *safety* with this day's, week's and month's realized P&L."""
    realized = load_realized_pnl(config, now)
    if realized is None:
        logger.info(
            "Loss limits count only this process's trades (Supabase not available)"
        )
        return
    safety.seed_realized_pnl(realized.day, realized.week, realized.month)
    logger.info(
        "Loss limits seeded: day %.2f, week %.2f, month %.2f",
        realized.day, realized.week, realized.month,
    )
