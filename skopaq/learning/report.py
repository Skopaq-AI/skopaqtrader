"""Track record — does the system have an edge? (`skopaq report`)

Backtesting an LLM on past dates is contaminated: the models already know
what happened next. The honest test is forward: score every call made in
paper or live trading against what the market then did. This module reads
what Skopaq already stores:

- **AI calls** — upstream's decision log (mirrored to Supabase). Each call is
  settled once its holding window has traded, with its raw return and its
  return over the benchmark (NIFTY 50 for ``.NS`` tickers).
- **Executed trades** — closed positions in the ``trades`` table, with the
  realized P&L and the signal's confidence at entry.
- **Calibration** — whether higher confidence actually won more often.

Numbers stay in code; nothing here asks an LLM.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Below this many settled calls, hit rates are mostly noise.
MIN_SAMPLE = 30

_LONG = {"Buy", "Overweight"}
_SHORT_OR_EXIT = {"Sell", "Underweight"}
_BUCKETS = ((0, 50), (50, 60), (60, 70), (70, 80), (80, 101))


def _pct(text: Optional[str]) -> Optional[float]:
    """``"+2.0%"`` → ``0.02``; anything else → ``None``."""
    if not text:
        return None
    try:
        return float(text.strip().rstrip("%")) / 100
    except ValueError:
        return None


def parse_tag(entry: str) -> Optional[dict[str, Any]]:
    """The tag line of a decision-log entry, as upstream writes it.

    ``[date | ticker | rating | pending]`` or
    ``[date | ticker | rating | +2.0% | +1.0% | 5d | resolved:...]``.
    """
    line = entry.strip().splitlines()[0].strip() if entry.strip() else ""
    if not (line.startswith("[") and line.endswith("]")):
        return None
    fields = [f.strip() for f in line[1:-1].split("|")]
    if len(fields) < 4:
        return None
    pending = fields[3] == "pending"
    return {
        "date": fields[0],
        "ticker": fields[1],
        "rating": fields[2],
        "pending": pending,
        "raw": None if pending else _pct(fields[3]),
        "alpha": None if pending or len(fields) < 5 else _pct(fields[4]),
    }


@dataclass
class RatingStats:
    calls: int = 0
    settled: int = 0
    avg_return: Optional[float] = None
    avg_alpha: Optional[float] = None


@dataclass
class CallStats:
    """How the AI's calls did, whether or not they were traded."""

    total: int = 0
    pending: int = 0
    settled: int = 0
    # Buy/Overweight that rose + Sell/Underweight that fell, over those settled
    hit_rate: Optional[float] = None
    directional: int = 0
    # Mean return over the benchmark of settled Buy/Overweight calls
    long_alpha: Optional[float] = None
    by_rating: dict[str, RatingStats] = field(default_factory=dict)

    @property
    def enough_data(self) -> bool:
        return self.directional >= MIN_SAMPLE


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def summarize_calls(entries: Iterable[str]) -> CallStats:
    tags = [t for t in (parse_tag(e) for e in entries) if t is not None]
    stats = CallStats(total=len(tags))
    hits = 0
    long_alphas: list[float] = []
    returns: dict[str, list[float]] = {}
    alphas: dict[str, list[float]] = {}

    for tag in tags:
        rating = tag["rating"]
        by = stats.by_rating.setdefault(rating, RatingStats())
        by.calls += 1
        if tag["pending"] or tag["raw"] is None:
            stats.pending += tag["pending"]
            continue
        stats.settled += 1
        by.settled += 1
        returns.setdefault(rating, []).append(tag["raw"])
        if tag["alpha"] is not None:
            alphas.setdefault(rating, []).append(tag["alpha"])
        if rating in _LONG:
            stats.directional += 1
            hits += tag["raw"] > 0
            if tag["alpha"] is not None:
                long_alphas.append(tag["alpha"])
        elif rating in _SHORT_OR_EXIT:
            stats.directional += 1
            hits += tag["raw"] < 0

    for rating, by in stats.by_rating.items():
        by.avg_return = _mean(returns.get(rating, []))
        by.avg_alpha = _mean(alphas.get(rating, []))
    stats.hit_rate = hits / stats.directional if stats.directional else None
    stats.long_alpha = _mean(long_alphas)
    return stats


@dataclass
class TradeStats:
    """Realized results of executed trades (closed positions)."""

    closed: int = 0
    wins: int = 0
    total_pnl: float = 0.0
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    profit_factor: Optional[float] = None  # gross profit / gross loss
    max_drawdown: float = 0.0  # worst peak-to-trough of cumulative P&L (INR)
    avg_return: Optional[float] = None  # per trade, on the capital it used
    return_std: Optional[float] = None

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.closed if self.closed else None


def _entry_value(trade) -> Optional[float]:
    price = trade.fill_price or trade.price
    if price is None or not trade.quantity:
        return None
    return float(price) * float(trade.quantity)


def summarize_trades(trades: Iterable[Any]) -> TradeStats:
    """*trades*: closed opening BUY rows (``TradeRepository.get_closed_since``)."""
    closed = sorted(
        (t for t in trades if t.pnl is not None and t.closed_at is not None),
        key=lambda t: t.closed_at,
    )
    stats = TradeStats(closed=len(closed))
    pnls = [float(t.pnl) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    stats.wins = len(wins)
    stats.total_pnl = sum(pnls)
    stats.avg_win = _mean(wins)
    stats.avg_loss = _mean(losses)
    if losses:
        stats.profit_factor = sum(wins) / -sum(losses)

    peak = equity = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        stats.max_drawdown = max(stats.max_drawdown, peak - equity)

    returns = [
        float(t.pnl) / value for t in closed
        if (value := _entry_value(t))
    ]
    stats.avg_return = _mean(returns)
    if len(returns) > 1:
        mean = stats.avg_return
        stats.return_std = math.sqrt(sum((r - mean) ** 2 for r in returns) / (len(returns) - 1))
    return stats


@dataclass
class CalibrationBucket:
    low: int
    high: int
    trades: int = 0
    wins: int = 0

    @property
    def win_rate(self) -> Optional[float]:
        return self.wins / self.trades if self.trades else None

    @property
    def label(self) -> str:
        return f"{self.low}–{min(self.high, 100)}"


def calibration(trades: Iterable[Any]) -> list[CalibrationBucket]:
    """Win rate of closed trades by the signal's confidence at entry."""
    buckets = [CalibrationBucket(low, high) for low, high in _BUCKETS]
    for trade in trades:
        if trade.pnl is None:
            continue
        confidence = (trade.agent_decision or {}).get("confidence")
        if confidence is None:
            continue
        for bucket in buckets:
            if bucket.low <= confidence < bucket.high:
                bucket.trades += 1
                bucket.wins += float(trade.pnl) > 0
                break
    return buckets


@dataclass
class Report:
    days: int
    mode: str
    calls: CallStats
    trades: TradeStats
    calibration: list[CalibrationBucket]
    sources: list[str] = field(default_factory=list)


def _decision_log_entries(config, client) -> tuple[list[str], list[str]]:
    """Decision-log entries from Supabase and the local log, merged."""
    from pathlib import Path

    from skopaq.memory.store import DECISION_LOG_ROLE, _split_entries, merge_entries
    from tradingagents.default_config import DEFAULT_CONFIG

    sources, remote, local = [], [], []
    if client is not None:
        try:
            from skopaq.db.repositories import MemoryRepository

            record = MemoryRepository(client).get_by_role(DECISION_LOG_ROLE)
            remote = record.documents if record else []
            sources.append("supabase:decision_log")
        except Exception:
            logger.warning("Could not read the decision log from Supabase", exc_info=True)
    path = Path(DEFAULT_CONFIG["memory_log_path"])
    if path.exists():
        local = _split_entries(path.read_text(encoding="utf-8"))
        sources.append(str(path))
    return merge_entries(remote, local), sources


def build_report(config, days: int = 90, now: Optional[datetime] = None) -> Report:
    """Collect the track record for the last *days* days in the current mode."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    is_paper = config.trading_mode != "live"

    client = None
    if config.supabase_url and config.supabase_service_key.get_secret_value():
        from supabase import create_client

        client = create_client(config.supabase_url, config.supabase_service_key.get_secret_value())

    entries, sources = _decision_log_entries(config, client)
    cutoff = since.date().isoformat()
    entries = [e for e in entries if (t := parse_tag(e)) and t["date"] >= cutoff]

    trades: list[Any] = []
    if client is not None:
        try:
            from skopaq.db.repositories import TradeRepository

            trades = TradeRepository(client).get_closed_since(since, is_paper=is_paper)
            sources.append("supabase:trades")
        except Exception:
            logger.warning("Could not read trades from Supabase", exc_info=True)

    return Report(
        days=days,
        mode="paper" if is_paper else "live",
        calls=summarize_calls(entries),
        trades=summarize_trades(trades),
        calibration=calibration(trades),
        sources=sources,
    )
