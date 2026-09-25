"""NSE trading days: weekends, holidays, unknown years (fail closed), the 15:30 close."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from skopaq.risk.calendar import (
    IST,
    NSE_TRADING_HOLIDAYS,
    daemon_block_reason,
    is_trading_day,
    parse_extra_holidays,
    trading_day_status,
)


def test_weekends_are_not_trading_days():
    assert trading_day_status(date(2026, 9, 26)) == (False, "Saturday")
    assert trading_day_status(date(2026, 9, 27)) == (False, "Sunday")


def test_holidays_are_not_trading_days():
    ok, reason = trading_day_status(date(2026, 10, 2))
    assert not ok
    assert "Gandhi" in reason


def test_an_ordinary_weekday_is_a_trading_day():
    assert trading_day_status(date(2026, 9, 28)) == (True, "")
    assert is_trading_day(date(2026, 9, 28))


def test_a_year_without_a_holiday_list_is_not_traded():
    ok, reason = trading_day_status(date(2027, 1, 4))
    assert not ok
    assert "no NSE holiday list for 2027" in reason


def test_extra_holidays_make_a_year_known():
    extra = "2027-01-26"
    assert is_trading_day(date(2027, 1, 4), extra)
    ok, reason = trading_day_status(date(2027, 1, 26), extra)
    assert not ok
    assert "SKOPAQ_NSE_HOLIDAYS" in reason


def test_extra_holidays_accept_commas_and_spaces():
    parsed = parse_extra_holidays("2027-01-26, 2027-03-01  2027-04-02")
    assert set(parsed) == {date(2027, 1, 26), date(2027, 3, 1), date(2027, 4, 2)}
    assert parse_extra_holidays("") == {}


def test_a_bad_extra_entry_is_named():
    with pytest.raises(ValueError, match="2027-13-01"):
        parse_extra_holidays("2027-01-26,2027-13-01")
    with pytest.raises(ValueError, match="20270126"):
        parse_extra_holidays("20270126")


def test_daemon_block_reason():
    assert daemon_block_reason(datetime(2026, 9, 28, 10, 0, tzinfo=IST)) is None
    closed = daemon_block_reason(datetime(2026, 9, 28, 15, 31, tzinfo=IST))
    assert closed == "NSE closed at 15:30 IST"
    assert "Gandhi" in daemon_block_reason(datetime(2026, 10, 2, 10, 0, tzinfo=IST))


def test_daemon_block_reason_converts_to_ist():
    # 04:30 UTC is 10:00 IST
    assert daemon_block_reason(datetime(2026, 9, 28, 4, 30, tzinfo=timezone.utc)) is None
    # 10:05 UTC is 15:35 IST
    assert daemon_block_reason(datetime(2026, 9, 28, 10, 5, tzinfo=timezone.utc)) is not None


def test_the_2026_list_has_15_weekday_closures():
    holidays = NSE_TRADING_HOLIDAYS[2026]
    assert len(holidays) == 15
    assert all(d.weekday() < 5 and d.year == 2026 for d in holidays)
