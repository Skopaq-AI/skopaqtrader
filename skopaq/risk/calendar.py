"""NSE event calendar — risk-aware date classification.

Flags trading days that carry elevated risk due to known market events.
Used to reduce position size or skip trading entirely.

Event types:
    - F&O monthly expiry (last Thursday of each month)
    - F&O weekly expiry (every Thursday for Bank Nifty / Nifty)
    - RBI policy dates (announced in advance, 6 per year)
    - Union Budget day (Feb 1)
    - General election result dates (known in advance)

Risk levels:
    - NORMAL  → scale = 1.0  (no special events)
    - CAUTION → scale = 0.7  (F&O expiry, minor events)
    - AVOID   → scale = 0.0  (RBI policy, Budget, election results)

Usage::

    calendar = NSEEventCalendar()
    scale = calendar.get_position_scale(date.today())
    # Feed into PositionSizer as calendar_scale multiplier

Trading days (``is_trading_day``, ``daemon_block_reason``) come from the NSE
holiday list below. They fail closed: a year with no list is not a trading
day, so the daemon, the scheduler and the Telegram jobs stay idle until the
list (or ``SKOPAQ_NSE_HOLIDAYS``) is updated.
"""

from __future__ import annotations

import calendar as cal
import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)


# ── Known event dates (update annually) ──────────────────────────────────────
# Format: {(month, day): event_label}
# These are dates that are either fixed or announced well in advance.

# RBI Monetary Policy meeting dates for FY 2025–26 (announced in advance)
# These are the announcement days (last day of each 3-day MPC meeting)
_RBI_POLICY_DATES_2025: set[tuple[int, int]] = {
    (2, 7),    # Feb 2025
    (4, 9),    # Apr 2025
    (6, 6),    # Jun 2025
    (8, 8),    # Aug 2025
    (10, 8),   # Oct 2025
    (12, 5),   # Dec 2025
}

_RBI_POLICY_DATES_2026: set[tuple[int, int]] = {
    (2, 6),    # Feb 2026
    (4, 8),    # Apr 2026
    (6, 5),    # Jun 2026
    (8, 7),    # Aug 2026
    (10, 7),   # Oct 2026
    (12, 4),   # Dec 2026
}

# Union Budget is always Feb 1 (since 2017)
_BUDGET_MONTH_DAY = (2, 1)


def _last_thursday(year: int, month: int) -> date:
    """Compute the last Thursday of a given month.

    The last Thursday is the F&O monthly expiry date on NSE.
    Uses Python's calendar module for correctness across leap years.
    """
    # Get the last day of the month
    _, last_day = cal.monthrange(year, month)
    d = date(year, month, last_day)

    # Walk backwards to find Thursday (weekday=3)
    while d.weekday() != 3:  # 0=Mon, 3=Thu
        d -= timedelta(days=1)

    return d


def _all_thursdays(year: int, month: int) -> list[date]:
    """Return all Thursdays in a given month."""
    thursdays = []
    d = date(year, month, 1)
    # Advance to first Thursday
    while d.weekday() != 3:
        d += timedelta(days=1)
    # Collect all Thursdays
    while d.month == month:
        thursdays.append(d)
        d += timedelta(days=7)
    return thursdays


class NSEEventCalendar:
    """Risk-aware date classification for NSE trading days.

    Classifies each date into a risk level and returns a position-sizing
    multiplier.  Zero means "do not trade".
    """

    def __init__(self, caution_scale: float = 0.7) -> None:
        self._caution_scale = caution_scale

    def get_risk_level(self, d: date) -> str:
        """Classify a date's risk level.

        Args:
            d: The trading date to classify.

        Returns:
            "NORMAL", "CAUTION", or "AVOID".
        """
        # Check AVOID events first (highest priority)
        if self._is_avoid_day(d):
            return "AVOID"

        # Check CAUTION events
        if self._is_caution_day(d):
            return "CAUTION"

        return "NORMAL"

    def get_position_scale(self, d: date) -> float:
        """Return the position-sizing multiplier for a date.

        Returns:
            1.0 for NORMAL, 0.7 for CAUTION, 0.0 for AVOID.
        """
        level = self.get_risk_level(d)
        if level == "AVOID":
            return 0.0
        elif level == "CAUTION":
            return self._caution_scale
        return 1.0

    def get_events(self, d: date) -> list[str]:
        """List all events affecting a date (for logging/display).

        Returns:
            List of event labels (empty for NORMAL days).
        """
        events = []

        # Budget day
        if (d.month, d.day) == _BUDGET_MONTH_DAY:
            events.append("Union Budget Day")

        # RBI policy
        rbi_dates = self._rbi_dates_for_year(d.year)
        if (d.month, d.day) in rbi_dates:
            events.append("RBI Monetary Policy")

        # F&O monthly expiry
        last_thu = _last_thursday(d.year, d.month)
        if d == last_thu:
            events.append("F&O Monthly Expiry")
        elif d.weekday() == 3:  # Any other Thursday
            events.append("F&O Weekly Expiry")

        return events

    def _is_avoid_day(self, d: date) -> bool:
        """Check for AVOID-level events (halt trading)."""
        # Union Budget
        if (d.month, d.day) == _BUDGET_MONTH_DAY:
            return True

        # RBI Monetary Policy announcement
        rbi_dates = self._rbi_dates_for_year(d.year)
        if (d.month, d.day) in rbi_dates:
            return True

        return False

    def _is_caution_day(self, d: date) -> bool:
        """Check for CAUTION-level events (reduce size)."""
        # F&O monthly expiry (last Thursday)
        last_thu = _last_thursday(d.year, d.month)
        if d == last_thu:
            return True

        # F&O weekly expiry (any Thursday that isn't monthly)
        if d.weekday() == 3:
            return True

        # Day before monthly expiry (positioning day)
        if d + timedelta(days=1) == last_thu:
            return True

        return False

    def _rbi_dates_for_year(self, year: int) -> set[tuple[int, int]]:
        """Return RBI policy dates for a given year."""
        if year == 2025:
            return _RBI_POLICY_DATES_2025
        elif year == 2026:
            return _RBI_POLICY_DATES_2026
        else:
            # Unknown year — return empty (no false AVOID signals)
            logger.debug("No RBI dates configured for year %d", year)
            return set()


# ── Trading days (NSE holidays) ──────────────────────────────────────────────

IST = timezone(timedelta(hours=5, minutes=30))
NSE_CLOSE = time(15, 30)

# Weekday closures of the NSE equity segment, from the exchange's holiday circular.
# Add next year's list every December. Special sessions (Muhurat trading, a Budget
# Sunday) are not traded by the daemon, so they are not listed.
NSE_TRADING_HOLIDAYS: dict[int, dict[date, str]] = {
    2026: {
        date(2026, 1, 26): "Republic Day",
        date(2026, 3, 3): "Holi",
        date(2026, 3, 26): "Shri Ram Navami",
        date(2026, 3, 31): "Shri Mahavir Jayanti",
        date(2026, 4, 3): "Good Friday",
        date(2026, 4, 14): "Dr. Baba Saheb Ambedkar Jayanti",
        date(2026, 5, 1): "Maharashtra Day",
        date(2026, 5, 28): "Bakri Id",
        date(2026, 6, 26): "Muharram",
        date(2026, 9, 14): "Ganesh Chaturthi",
        date(2026, 10, 2): "Mahatma Gandhi Jayanti",
        date(2026, 10, 20): "Dussehra",
        date(2026, 11, 10): "Diwali Balipratipada",
        date(2026, 11, 24): "Prakash Gurpurb Sri Guru Nanak Dev",
        date(2026, 12, 25): "Christmas",
    },
}


def now_ist() -> datetime:
    """The current time in IST (a function so tests can patch it)."""
    return datetime.now(IST)


def parse_extra_holidays(spec: str) -> dict[date, str]:
    """Parse ``SKOPAQ_NSE_HOLIDAYS``: YYYY-MM-DD dates separated by commas and/or spaces.

    Raises:
        ValueError: naming the first entry that is not a valid date.
    """
    extra: dict[date, str] = {}
    for part in re.split(r"[,\s]+", spec or ""):
        if not part:
            continue
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
                raise ValueError(part)
            extra[date.fromisoformat(part)] = "SKOPAQ_NSE_HOLIDAYS"
        except ValueError:
            raise ValueError(
                f"SKOPAQ_NSE_HOLIDAYS: {part!r} is not a YYYY-MM-DD date"
            ) from None
    return extra


def trading_day_status(d: date, extra: str = "") -> tuple[bool, str]:
    """Whether NSE trades on *d*, and if not, why.

    A year counts as known when it has a built-in list or any extra date falls
    in it; a date in an unknown year is not a trading day (fail closed).
    """
    if d.weekday() >= 5:
        return False, d.strftime("%A")
    extras = parse_extra_holidays(extra)
    known = d.year in NSE_TRADING_HOLIDAYS or any(x.year == d.year for x in extras)
    if not known:
        return False, (
            f"no NSE holiday list for {d.year}: add it to skopaq/risk/calendar.py "
            "or SKOPAQ_NSE_HOLIDAYS"
        )
    name = NSE_TRADING_HOLIDAYS.get(d.year, {}).get(d) or extras.get(d)
    if name:
        return False, f"NSE holiday: {name}"
    return True, ""


def is_trading_day(d: date, extra: str = "") -> bool:
    return trading_day_status(d, extra)[0]


def daemon_block_reason(now: datetime, extra: str = "") -> Optional[str]:
    """Why a daemon session must not start at *now*, or ``None`` if it may."""
    now = now.astimezone(IST)
    ok, reason = trading_day_status(now.date(), extra)
    if not ok:
        return reason
    if now.time() >= NSE_CLOSE:
        return "NSE closed at 15:30 IST"
    return None
