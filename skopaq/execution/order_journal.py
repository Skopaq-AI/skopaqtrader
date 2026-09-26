"""Per-day journal of Skopaq's own live orders (best effort).

One JSON line per event — ``placed``, ``uncertain``, ``stuck``, ``interrupted``,
``final`` — in ``<SKOPAQ_ORDER_JOURNAL_DIR>/<YYYY-MM-DD>.jsonl`` (default
``~/.skopaq/orders``, on the home volume every container shares). It lets a later
process recognise and resume Skopaq's *own* orders, such as a stuck exit left by a killed
daemon, and never someone else's: a user's GTT SELL leg is never cancelled.

Booking a resumed order's late fill (its trade rows) adds two lines: ``booking`` before
the rows are written and ``booked`` once they are. Only ``booked`` totals count as booked
(``booking_state``): a ``booking`` no ``booked`` line follows — the process died, or the
write failed — booked nothing as far as the journal knows. These lines never change an
order's state (``latest_by_order`` skips them).

The broker's order book stays the source of truth. A failed write is logged and alerted
once, and trading carries on; ``record`` says whether the line was written, so a booking
another process could not learn about is not made.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from skopaq.risk.calendar import now_ist

logger = logging.getLogger(__name__)

_DEFAULT_DIR = "~/.skopaq/orders"
EVENTS = frozenset({"placed", "uncertain", "stuck", "interrupted", "final", "booking",
                    "booked"})
# Lines about booking an order's fill (trade rows), not about the order's state
BOOKING_EVENTS = frozenset({"booking", "booked"})
# Fields a later event may leave empty; they are carried over from earlier lines
_CARRIED = ("internal_id", "symbol", "security_id", "segment", "side", "qty", "purpose",
            "guessed")


def _text(value: object) -> Any:
    """Decimals as strings (exact), None kept, everything else as given."""
    if isinstance(value, Decimal):
        return str(value)
    return value


@dataclass(frozen=True)
class BookingState:
    """What today's journal says Skopaq has booked (trade rows) for one order."""

    booked: Decimal                    # shares booked so far
    avg_price: Optional[Decimal]       # their average price
    final_filled: Optional[Decimal]    # the fill of its latest ``final`` line (None: none)
    has_final: bool                    # a ``final`` line exists
    unconfirmed: tuple[dict, ...]      # ``booking`` lines no later ``booked`` line covers

    @property
    def finished(self) -> bool:
        """Final, and all of its fill is booked."""
        return self.has_final and (self.final_filled is None
                                   or self.booked >= self.final_filled)


def _decimal(value: object) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except ArithmeticError:
        return None


class OrderJournal:
    """Append-only JSON-lines record of this host's live orders, one file per IST day."""

    def __init__(self, directory: Path | str, *, wall: Callable[[], datetime] = now_ist,
                 alerter: Any = None) -> None:
        self.directory = Path(directory).expanduser()
        self._wall = wall
        self._alerter = alerter
        self._warned = False

    @classmethod
    def from_config(cls, config: object, **kwargs: Any) -> OrderJournal:
        """Directory: ``SKOPAQ_ORDER_JOURNAL_DIR``, else ``config.order_journal_dir`` (if a
        string), else ``~/.skopaq/orders``."""
        env = os.environ.get("SKOPAQ_ORDER_JOURNAL_DIR", "").strip()
        value = getattr(config, "order_journal_dir", None)
        configured = value.strip() if isinstance(value, str) else ""
        return cls(env or configured or _DEFAULT_DIR, **kwargs)

    def path_for(self, day: date) -> Path:
        return self.directory / f"{day.isoformat()}.jsonl"

    def record(
        self,
        event: str,
        *,
        order_id: str = "",
        internal_id: str = "",
        symbol: str = "",
        security_id: str = "",
        segment: str = "",
        side: str = "",
        qty: Optional[Decimal] = None,
        purpose: str = "",
        filled: Optional[Decimal] = None,
        avg_price: Optional[Decimal] = None,
        status: str = "",
        note: str = "",
        reported: Optional[Decimal] = None,
        reported_avg: Optional[Decimal] = None,
        before_ids: Optional[Iterable[str]] = None,
        remark: str = "",
        guessed: bool = False,
        order_final: Optional[bool] = None,
        locked: Optional[bool] = None,
    ) -> bool:
        """Append one event line (flock + fsync); True if it was written. Never raises.

        ``reported`` is what has been reported (booked) as filled for the order: an
        interrupted order's (nothing, so a later process books it all), or a resumed
        order's that is still working (what the resuming process had booked of it, which
        the next process to resume it reads under the order lock); ``reported_avg`` is
        that part's average price.
        An ``uncertain`` line carries ``before_ids`` (the order ids already in the book
        when it was sent: none of them can be it) and its ``remark`` tag. ``guessed``
        marks an order matched to an uncertain placement by its look alone (never
        cancelled by Skopaq, and it does not resolve the placement).

        A ``booking``/``booked`` line's ``filled`` and ``avg_price`` are the order's total
        booked once it succeeds, ``reported`` the total booked before it; ``order_final``
        says whether the order was final, ``locked`` whether the process held its order
        lock (so no other process could be booking it meanwhile).
        """
        now = self._wall()
        line = {
            "ts": now.isoformat(), "pid": os.getpid(), "event": event,
            "order_id": order_id, "internal_id": internal_id, "symbol": symbol,
            "security_id": security_id, "segment": segment, "side": side,
            "qty": _text(qty), "purpose": purpose, "filled": _text(filled),
            "avg_price": _text(avg_price), "status": status,
        }
        if reported is not None:
            line["reported"] = _text(reported)
            if reported_avg is not None:
                line["reported_avg"] = _text(reported_avg)
        if before_ids is not None:
            line["before_ids"] = sorted(before_ids)
        if remark:
            line["remark"] = remark
        if guessed:
            line["guessed"] = True
        if order_final is not None:
            line["order_final"] = order_final
        if locked is not None:
            line["locked"] = locked
        if note:
            line["note"] = note
        try:
            # Imported here: paper mode never writes the journal, and fcntl is POSIX-only
            import fcntl

            self.directory.mkdir(parents=True, exist_ok=True)
            with open(self.path_for(now.date()), "a", encoding="utf-8") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)
                try:
                    fh.write(json.dumps(line) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        except Exception as exc:
            self._write_failed(exc)
            return False
        return True

    def _write_failed(self, exc: Exception) -> None:
        if self._warned:
            logger.debug("Order journal write failed again: %s", exc)
            return
        self._warned = True
        text = (f"Order journal {self.directory} is not writable ({exc}); trading continues, "
                "but a restarted process will not recognise this process's open orders, and "
                "a late fill booked while it cannot be written may be booked twice (another "
                "Skopaq process cannot see the booking) — check the trade rows")
        try:
            alerter = self._alerter
            if alerter is None:
                from skopaq.execution.order_alerts import get_alerter

                alerter = get_alerter()
            alerter.alert("WARNING", "journal-write-failed", text)
        except Exception:
            logger.warning(text)

    def entries(self, day: Optional[date] = None) -> list[dict]:
        """The day's lines (today by default), in order; unreadable lines are skipped."""
        path = self.path_for(day or self._wall().date())
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return []
        lines: list[dict] = []
        for raw in text.splitlines():
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if isinstance(item, dict):
                lines.append(item)
        return lines

    def latest_by_order(self, entries: Optional[list[dict]] = None) -> dict[str, dict]:
        """The last event of each order today (booking lines aside), with fields carried
        from its earlier lines and ``first_ts``: the time of its first line (when it was
        placed or found)."""
        latest: dict[str, dict] = {}
        for item in self.entries() if entries is None else entries:
            order_id = item.get("order_id")
            if (not isinstance(order_id, str) or not order_id
                    or item.get("event") in BOOKING_EVENTS):
                continue
            merged = dict(item)
            earlier = latest.get(order_id, {})
            for key in _CARRIED:
                if merged.get(key) in (None, "") and earlier.get(key) not in (None, ""):
                    merged[key] = earlier[key]
            merged["first_ts"] = earlier.get("first_ts") or item.get("ts")
            latest[order_id] = merged
        return latest

    def today_unresolved(self) -> list[dict]:
        """Today's own orders to resume: those whose last event is not ``final``, and
        final ones whose late fill a process started booking without confirming it
        (``unbooked``, with ``reported`` set to what is booked of it)."""
        entries = self.entries()
        found = []
        for order_id, line in self.latest_by_order(entries).items():
            if line.get("event") != "final":
                found.append(line)
                continue
            state = self.booking_state(order_id, entries)
            if state.unconfirmed and not state.finished:
                found.append({**line, "unbooked": True, "reported": _text(state.booked),
                              "reported_avg": _text(state.avg_price)})
        return found

    def booking_state(self, order_id: str,
                      entries: Optional[list[dict]] = None) -> BookingState:
        """What Skopaq has booked for ``order_id`` today.

        Booked: the most any ``booked`` line totals; or what a state line says was
        reported (``reported``: a resumed order's booked total, an interrupted order's
        nothing); or — the latest state line being a ``stuck`` line without one — that
        line's fill, which the process that placed the order reported to its caller.
        A ``final`` line's fill is never counted: the process that read it may not have
        booked it. ``unconfirmed``: the ``booking`` lines no later ``booked`` line of at
        least their total follows.
        """
        lines = [e for e in (self.entries() if entries is None else entries)
                 if e.get("order_id") == order_id]
        best: Optional[tuple[Decimal, Optional[Decimal]]] = None

        def take(qty: Optional[Decimal], avg: object) -> None:
            nonlocal best
            if qty is not None and (best is None or qty > best[0]):
                best = (qty, _decimal(avg))

        state_lines = [e for e in lines
                       if e.get("event") not in BOOKING_EVENTS and e.get("event") != "final"]
        for line in state_lines:
            if line.get("reported") is not None:
                take(_decimal(line.get("reported")), line.get("reported_avg"))
        if (state_lines and state_lines[-1].get("event") == "stuck"
                and state_lines[-1].get("reported") is None):
            take(_decimal(state_lines[-1].get("filled")), state_lines[-1].get("avg_price"))
        unconfirmed = []
        for i, line in enumerate(lines):
            if line.get("event") == "booked":
                take(_decimal(line.get("filled")), line.get("avg_price"))
            elif line.get("event") == "booking":
                total = _decimal(line.get("filled")) or Decimal(0)
                if not any(later.get("event") == "booked"
                           and (_decimal(later.get("filled")) or Decimal(0)) >= total
                           for later in lines[i + 1:]):
                    unconfirmed.append(line)
        finals = [e for e in lines if e.get("event") == "final"]
        booked, avg = best if best is not None else (Decimal(0), None)
        return BookingState(
            booked=booked, avg_price=avg if booked else None,
            final_filled=_decimal(finals[-1].get("filled")) if finals else None,
            has_final=bool(finals), unconfirmed=tuple(unconfirmed))

    def own_ids_today(self) -> set[str]:
        """Every order id this host's processes placed today."""
        return set(self.latest_by_order())

    def uncertain_today(self) -> list[dict]:
        """Today's placements whose outcome was unknown (no order id to follow)."""
        return [e for e in self.entries() if e.get("event") == "uncertain"]

    def unresolved_uncertain(self) -> list[dict]:
        """Today's uncertain placements no later line has named their order for (an order
        only guessed to be one does not resolve it)."""
        entries = self.entries()
        found = {e.get("internal_id") for e in entries
                 if e.get("order_id") and e.get("internal_id") and not e.get("guessed")}
        return [e for e in entries if e.get("event") == "uncertain"
                and e.get("internal_id") and e.get("internal_id") not in found]

    def claim(self, key: str) -> Optional[bool]:
        """Claim ``key`` for today across the processes sharing the journal (a marker
        file): True if this call claimed it, False if it was claimed already, None when
        the marker cannot be written (nobody can tell)."""
        name = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120] or "_"
        path = self.directory / f"{self._wall().date().isoformat()}.{name}.once"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Order journal marker %s not written: %s", path, exc)
            return None
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            return False
        except OSError as exc:
            logger.warning("Order journal marker %s not written: %s", path, exc)
            return None
        os.close(fd)
        return True

    def once_today(self, key: str) -> bool:
        """True the first time ``key`` is claimed today by any process sharing the journal
        (``claim``); True as well when the marker cannot be written (never silence an
        alert because of the journal)."""
        return self.claim(key) is not False
