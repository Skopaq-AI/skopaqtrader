"""OrderJournal: the per-day record of Skopaq's own live orders (best effort)."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

from skopaq.execution.order_journal import OrderJournal
from tests.unit.execution._fakes import AlertSpy, FakeClock


def test_events_are_json_lines_and_unresolved_orders_are_found(tmp_path):
    journal = OrderJournal(tmp_path, wall=FakeClock().wall)
    journal.record("placed", order_id="EQ-1", symbol="TCS", security_id="11536", side="SELL",
                   qty=Decimal(10), purpose="exit", status="INITIATED")
    journal.record("placed", order_id="EQ-2", symbol="INFY", side="BUY", qty=Decimal(5),
                   purpose="entry")
    journal.record("stuck", order_id="EQ-1", filled=Decimal(3), status="PENDING")
    journal.record("final", order_id="EQ-2", filled=Decimal(5), avg_price=Decimal("99.5"),
                   status="SUCCESS")
    journal.record("uncertain", internal_id="abc", symbol="TCS", security_id="11536",
                   side="SELL", qty=Decimal(5))

    lines = (tmp_path / "2026-09-25.jsonl").read_text().splitlines()
    assert len(lines) == 5
    first = json.loads(lines[0])
    assert {"ts", "pid", "event", "order_id", "internal_id", "symbol", "security_id", "side",
            "qty", "purpose", "filled", "avg_price", "status"} <= set(first)
    assert (first["event"], first["qty"], first["ts"][:19]) == (
        "placed", "10", "2026-09-25T11:00:00")

    [unresolved] = journal.today_unresolved()
    assert (unresolved["order_id"], unresolved["event"], unresolved["symbol"]) == (
        "EQ-1", "stuck", "TCS")                                # fields carried from `placed`
    assert journal.own_ids_today() == {"EQ-1", "EQ-2"}
    assert [e["internal_id"] for e in journal.uncertain_today()] == ["abc"]


def test_an_unwritable_directory_warns_once_and_never_raises(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    alerts = AlertSpy()
    journal = OrderJournal(blocker / "orders", wall=FakeClock().wall, alerter=alerts)

    journal.record("placed", order_id="EQ-1")
    journal.record("final", order_id="EQ-1")

    assert alerts.keys() == ["journal-write-failed"]
    assert journal.today_unresolved() == [] and journal.own_ids_today() == set()


def test_the_directory_comes_from_the_env_var_then_the_config(tmp_path, monkeypatch):
    monkeypatch.setenv("SKOPAQ_ORDER_JOURNAL_DIR", str(tmp_path / "env"))
    config = MagicMock(order_journal_dir=str(tmp_path / "cfg"))
    assert OrderJournal.from_config(config).directory == tmp_path / "env"

    monkeypatch.delenv("SKOPAQ_ORDER_JOURNAL_DIR")
    assert OrderJournal.from_config(config).directory == tmp_path / "cfg"
    assert OrderJournal.from_config(MagicMock()).directory == Path(
        "~/.skopaq/orders").expanduser()


def test_tests_never_write_to_the_home_directory(tmp_path):
    # tests/conftest.py points the journal (and the SELL locks) at the test's tmp_path
    journal = OrderJournal.from_config(MagicMock())
    assert journal.directory != Path("~/.skopaq/orders").expanduser()
    assert tmp_path in journal.directory.parents


def test_without_fcntl_the_journal_warns_once_and_never_raises(tmp_path, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "fcntl", None)
    spy = AlertSpy()
    journal = OrderJournal(tmp_path, wall=FakeClock().wall, alerter=spy)
    journal.record("placed", order_id="EQ-1")
    journal.record("placed", order_id="EQ-2")
    assert spy.keys("WARNING") == ["journal-write-failed"]


def test_an_uncertain_placement_is_unresolved_until_a_line_names_its_order(tmp_path):
    journal = OrderJournal(tmp_path, wall=FakeClock().wall)
    journal.record("uncertain", internal_id="abc", symbol="TCS", side="SELL", qty=Decimal(5))
    journal.record("uncertain", internal_id="def", symbol="INFY", side="BUY", qty=Decimal(2))
    journal.record("placed", order_id="EQ-7", internal_id="def", symbol="INFY")
    assert [e["internal_id"] for e in journal.unresolved_uncertain()] == ["abc"]


def test_what_was_reported_is_kept_apart_from_what_filled(tmp_path):
    journal = OrderJournal(tmp_path, wall=FakeClock().wall)
    journal.record("interrupted", order_id="EQ-1", filled=Decimal(6), reported=Decimal(0))
    journal.record("stuck", order_id="EQ-2", filled=Decimal(3))
    lines = [json.loads(line) for line in (tmp_path / "2026-09-25.jsonl").read_text().splitlines()]
    assert (lines[0]["filled"], lines[0]["reported"]) == ("6", "0")
    assert "reported" not in lines[1]


# ── Booking lines, claims ────────────────────────────────────────────────────


def test_record_says_whether_the_line_was_written(tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    assert OrderJournal(tmp_path, wall=FakeClock().wall).record("placed", order_id="EQ-1")
    unwritable = OrderJournal(blocker / "orders", wall=FakeClock().wall, alerter=AlertSpy())
    assert unwritable.record("placed", order_id="EQ-1") is False


def test_only_booked_totals_count_and_a_final_line_is_not_a_booking(tmp_path):
    journal = OrderJournal(tmp_path, wall=FakeClock().wall)
    journal.record("placed", order_id="EQ-1", side="SELL", qty=Decimal(10))
    journal.record("stuck", order_id="EQ-1", filled=Decimal(4), avg_price=Decimal(90))
    state = journal.booking_state("EQ-1")
    assert (state.booked, state.avg_price, state.has_final) == (4, 90, False)

    journal.record("booking", order_id="EQ-1", filled=Decimal(7), avg_price=Decimal("89.9"),
                   reported=Decimal(4), order_final=False, locked=True)
    state = journal.booking_state("EQ-1")
    assert state.booked == 4                                  # started, never confirmed
    [line] = state.unconfirmed
    assert (line["filled"], line["order_final"], line["locked"]) == ("7", False, True)

    journal.record("final", order_id="EQ-1", filled=Decimal(10), avg_price=Decimal("89.8"))
    state = journal.booking_state("EQ-1")
    assert state.booked == 4 and not state.finished           # the worker's line alone
    assert journal.latest_by_order()["EQ-1"]["event"] == "final"   # booking lines skipped

    journal.record("booked", order_id="EQ-1", filled=Decimal(10), avg_price=Decimal("89.8"),
                   reported=Decimal(4), order_final=True, locked=True)
    state = journal.booking_state("EQ-1")
    assert (state.booked, state.avg_price, state.unconfirmed) == (10, Decimal("89.8"), ())
    assert state.finished


def test_a_final_order_whose_booking_was_never_confirmed_is_resumed(tmp_path):
    journal = OrderJournal(tmp_path, wall=FakeClock().wall)
    journal.record("placed", order_id="EQ-1", symbol="TCS", side="BUY", qty=Decimal(10),
                   purpose="entry")
    journal.record("stuck", order_id="EQ-1", filled=Decimal(4), avg_price=Decimal(100))
    journal.record("final", order_id="EQ-2", filled=Decimal(5))       # booked by its caller
    journal.record("final", order_id="EQ-1", filled=Decimal(10), avg_price=Decimal(100))
    assert journal.today_unresolved() == []
    journal.record("booking", order_id="EQ-1", filled=Decimal(10), avg_price=Decimal(100),
                   reported=Decimal(4), order_final=True, locked=True)

    [entry] = journal.today_unresolved()
    assert (entry["order_id"], entry["unbooked"], entry["reported"]) == ("EQ-1", True, "4")
    from skopaq.execution.live_orders import OrderRegistry

    registry = OrderRegistry()
    assert registry.load_journal([entry]) == 1
    tracked = registry.get("EQ-1")
    assert (tracked.filled_reported, tracked.state, tracked.final_seen) == (
        4, "interrupted", True)


def test_a_claim_that_cannot_be_written_is_unknown_but_alerts_still_go_out(tmp_path):
    journal = OrderJournal(tmp_path, wall=FakeClock().wall)
    assert journal.claim("late-fill-EQ-1") is True
    assert journal.claim("late-fill-EQ-1") is False
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    for directory in (blocker, blocker / "orders"):    # a file where the directory goes
        unusable = OrderJournal(directory, wall=FakeClock().wall)
        assert unusable.claim("late-fill-EQ-1") is None
        assert unusable.once_today("positions-left") is True
