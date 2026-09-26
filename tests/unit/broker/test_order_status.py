"""INDstocks order rows and statuses (skopaq/broker/order_status.py): normalisation,
classification, filled quantities, row parsing, fills and rejection kinds."""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from skopaq.broker.order_status import (
    TERMINAL_STATES,
    OrderState,
    RejectKind,
    classify,
    classify_rejection,
    find_order,
    is_non_cnc_product,
    normalise_status,
    parse_broker_time,
    parse_fills,
    parse_order_book,
    parse_order_row,
    to_decimal,
)


def row(status: str, traded=0, requested=10, price="", **kw) -> dict:
    """A book row in the documented shape (GET /order-book, GET /order)."""
    base = {
        "created_at": "2026-09-25T10:15:00.123456+05:30",
        "updated_at": "2026-09-25T10:15:01.654321+05:30",
        "user_id": "710354",
        "security_id": "2885",
        "isin": "INE002A01018",
        "name": "Reliance Industries",
        "id": "EQ-1001",
        "exch_order_id": "1100000017281712",
        "txn_type": "SELL",
        "exchange": "NSE",
        "segment": "EQUITY",
        "product": "CNC",
        "order_type": "LIMIT",
        "validity": "DAY",
        "traded_qty": traded,
        "requested_qty": requested,
        "requested_price": "1400",
        "traded_price": price,
        "status": status,
        "extra_info": "",
    }
    base.update(kw)
    return base


# ── S1: the 15 documented statuses ──────────────────────────────────────────


@pytest.mark.parametrize("status", [
    "QUEUED", "O-PENDING", "SL-PENDING", "PROCESSING", "INITIATED", "MODIFIED",
    "PENDING", "PARTIALLY FILLED",
])
def test_working_statuses(status):
    assert classify(status, Decimal("0"), Decimal("10")) is OrderState.WORKING
    assert classify(status, Decimal("4"), Decimal("10")) is OrderState.WORKING


@pytest.mark.parametrize("status,state", [
    ("SUCCESS", OrderState.FILLED),
    ("CANCELLED", OrderState.CANCELLED),
    ("EXPIRED", OrderState.CANCELLED),
    ("FAILED", OrderState.REJECTED),
    ("ABORTED", OrderState.REJECTED),
    ("PARTIALLY FILLED - CANCELLED", OrderState.PARTIAL_DONE),
    ("PARTIALLY FILLED - EXPIRED", OrderState.PARTIAL_DONE),
])
def test_final_statuses(status, state):
    traded = Decimal("3") if state is OrderState.PARTIAL_DONE else Decimal("0")
    assert classify(status, traded, Decimal("10")) is state
    assert state in TERMINAL_STATES


def test_working_and_unrecognised_are_not_terminal():
    assert OrderState.WORKING not in TERMINAL_STATES
    assert OrderState.UNRECOGNISED not in TERMINAL_STATES


def test_aliases_from_other_brokers_and_feeds():
    assert classify("COMPLETE", None, None) is OrderState.FILLED
    assert classify("OPEN", None, Decimal("10")) is OrderState.WORKING
    assert classify("TRIGGER PENDING", None, Decimal("10")) is OrderState.WORKING
    assert classify("REJECTED", None, Decimal("10")) is OrderState.REJECTED


# ── S2: normalisation ───────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("success", "SUCCESS"),
    ("  Success ", "SUCCESS"),
    ("PARTIALLY_EXECUTED", "PARTIALLY FILLED"),
    ("partially   filled", "PARTIALLY FILLED"),
    ("PARTIALLY FILLED-CANCELLED", "PARTIALLY FILLED - CANCELLED"),
    ("partially filled  -  expired", "PARTIALLY FILLED - EXPIRED"),
    ("PARTIALLY_FILLED_CANCELLED", "PARTIALLY FILLED - CANCELLED"),
    ("PF-CANCELLED", "PARTIALLY FILLED - CANCELLED"),
    ("PFC", "PARTIALLY FILLED - CANCELLED"),
    ("PF-EXPIRED", "PARTIALLY FILLED - EXPIRED"),
    ("RJ", "REJECTED"),
    ("O-PENDING", "O-PENDING"),
    ("o-pending", "O-PENDING"),
    ("SL-PENDING", "SL-PENDING"),
    ("TRIGGER_PENDING", "TRIGGER PENDING"),
])
def test_normalise_status(raw, expected):
    assert normalise_status(raw) == expected


@pytest.mark.parametrize("raw", [None, 5, MagicMock(), ["SUCCESS"]])
def test_normalise_status_non_strings(raw):
    assert normalise_status(raw) == ""


def test_single_letter_feed_codes_are_not_mapped():
    assert normalise_status("S") == "S"
    assert classify("S", None, Decimal("10")) is OrderState.UNRECOGNISED


# ── S3: filled quantity ─────────────────────────────────────────────────────


def test_filled_qty_success_without_traded_is_requested():
    snap = parse_order_row(row("SUCCESS", traded=None))
    assert snap.state is OrderState.FILLED
    assert snap.filled_qty == Decimal("10")


def test_filled_qty_partial_final_without_traded_is_unknown():
    snap = parse_order_row(row("PARTIALLY FILLED - CANCELLED", traded=None))
    assert snap.state is OrderState.PARTIAL_DONE
    assert snap.filled_qty is None


@pytest.mark.parametrize("status", ["PARTIALLY FILLED - CANCELLED",
                                    "PARTIALLY FILLED - EXPIRED"])
def test_filled_qty_partial_final_reporting_zero_traded_is_unknown(status):
    # The status says something executed; the row says nothing did: unknown, never 0
    snap = parse_order_row(row(status, traded=0))
    assert snap.state is OrderState.PARTIAL_DONE
    assert snap.filled_qty is None
    assert parse_order_row(row(status, traded=4)).filled_qty == Decimal("4")


def test_filled_qty_cancelled_without_traded_is_zero():
    snap = parse_order_row(row("CANCELLED", traded=None))
    assert snap.state is OrderState.CANCELLED
    assert snap.filled_qty == Decimal("0")


def test_filled_qty_working_without_traded_is_unknown():
    snap = parse_order_row(row("PENDING", traded=None))
    assert snap.filled_qty is None


def test_filled_qty_capped_at_requested():
    snap = parse_order_row(row("SUCCESS", traded=12, requested=10))
    assert snap.filled_qty == Decimal("10")


def test_filled_qty_without_requested():
    snap = parse_order_row(row("PARTIALLY FILLED - CANCELLED", traded=4, requested=None))
    assert snap.filled_qty == Decimal("4")
    assert snap.remaining_qty is None


def test_remaining_qty():
    snap = parse_order_row(row("PARTIALLY FILLED", traded=4, requested=10))
    assert snap.remaining_qty == Decimal("6")
    assert parse_order_row(row("PENDING", traded=None)).remaining_qty == Decimal("10")
    assert parse_order_row(row("SUCCESS", traded=12)).remaining_qty == Decimal("0")


# ── S4: the quantities cross-check the status ───────────────────────────────


def test_pending_with_everything_traded_is_filled():
    assert parse_order_row(row("PENDING", traded=10)).state is OrderState.FILLED


def test_nofill_status_with_a_fill_is_partial():
    assert parse_order_row(row("CANCELLED", traded=4)).state is OrderState.PARTIAL_DONE
    assert parse_order_row(row("FAILED", traded=2)).state is OrderState.PARTIAL_DONE


def test_zero_requested_is_not_filled():
    assert classify("PENDING", Decimal("0"), Decimal("0")) is OrderState.WORKING


# ── S5: unrecognised statuses and operator extras ───────────────────────────


def test_unrecognised_status_logged_once_per_order(caplog):
    caplog.set_level(logging.WARNING, logger="skopaq.broker.order_status")
    first = parse_order_row(row("WEIRD STATE", id="EQ-7"))
    again = parse_order_row(row("WEIRD STATE", id="EQ-7"))
    other = parse_order_row(row("WEIRD STATE", id="EQ-8"))
    assert first.state is again.state is other.state is OrderState.UNRECOGNISED
    warnings = [r for r in caplog.records if "WEIRD STATE" in r.getMessage()]
    assert len(warnings) == 2
    assert "EQ-7" in warnings[0].getMessage()
    assert "EQ-8" in warnings[1].getMessage()


def test_extra_terminal_status():
    extra = frozenset({"WEIRD STATE"})
    assert parse_order_row(row("weird state"), extra_terminal=extra).state is OrderState.CANCELLED
    snap = parse_order_row(row("WEIRD STATE", traded=3), extra_terminal=extra)
    assert snap.state is OrderState.PARTIAL_DONE
    assert snap.filled_qty == Decimal("3")


# ── S6: row parsing ─────────────────────────────────────────────────────────

# The documented GET /order example (a MARKET order that filled), verbatim.
DOC_MARKET_SUCCESS = {
    "created_at": "2025-07-02T09:18:40.446948+05:30",
    "updated_at": "2025-07-02T09:18:40.498595+05:30",
    "user_id": "710354",
    "security_id": "56998",
    "isin": "",
    "name": "NIFTY 3 JUL 25700 CE",
    "id": "DRV-28131451",
    "exch_order_id": "1300000002340881",
    "txn_type": "BUY",
    "exchange": "NSE",
    "segment": "DERIVATIVE",
    "product": "MARGIN",
    "order_type": "MARKET",
    "validity": "DAY",
    "mkt_type": "NL",
    "off_mkt_flag": "false",
    "traded_qty": 75,
    "requested_qty": 75,
    "requested_price": "43.55",
    "traded_price": "43.55",
    "sl_trigger_price": "",
    "sl_limit_price": "",
    "tgt_trigger_price": "",
    "tgt_limit_price": "",
    "status": "SUCCESS",
    "extra_info": "",
    "remarks": "momentum-v2/sig-4471",
}

DOC_OPENDING = {
    "created_at": "2025-07-02T17:59:57.799576+05:30",
    "updated_at": "2025-07-02T18:05:03.660538+05:30",
    "user_id": "710354",
    "security_id": "56888",
    "isin": "",
    "name": "NIFTY 03 Jul ₹25550 Call",
    "id": "DRV-28209665",
    "exch_order_id": "",
    "txn_type": "BUY",
    "exchange": "NSE",
    "segment": "DERIVATIVE",
    "product": "MARGIN",
    "order_type": "LIMIT",
    "validity": "DAY",
    "mkt_type": "NL",
    "off_mkt_flag": "true",
    "traded_qty": 0,
    "requested_qty": 225,
    "requested_price": "32.1",
    "traded_price": "",
    "status": "O-PENDING",
    "extra_info": "",
}

DOC_GTT = {
    "created_at": "2025-07-02T15:47:07.079035+05:30",
    "updated_at": "2025-07-02T17:43:02.635379+05:30",
    "security_id": "58757",
    "name": "NIFTY 3 JUL 27400 CE",
    "id": "GTT-2914581",
    "exch_order_id": "",
    "txn_type": "SELL",
    "exchange": "NSE",
    "segment": "DERIVATIVE",
    "product": "MARGIN",
    "order_type": "OCO",
    "validity": "",
    "traded_qty": 0,
    "requested_qty": 75,
    "requested_price": "",
    "traded_price": "",
    "status": "CANCELLED",
    "extra_info": "",
}


def test_parse_documented_market_success_row():
    snap = parse_order_row(DOC_MARKET_SUCCESS)
    assert snap.order_id == "DRV-28131451"
    assert snap.status == snap.status_raw == "SUCCESS"
    assert snap.state is OrderState.FILLED
    assert snap.side == "BUY"
    assert snap.security_id == "56998"
    assert snap.symbol == ""                       # `name` is never the symbol
    assert snap.name == "NIFTY 3 JUL 25700 CE"
    assert (snap.product, snap.segment) == ("MARGIN", "DERIVATIVE")
    assert (snap.order_type, snap.validity) == ("MARKET", "DAY")
    assert snap.requested_qty == snap.traded_qty == Decimal("75")
    assert snap.traded_price == Decimal("43.55")
    assert snap.exch_order_id == "1300000002340881"
    assert snap.remarks == "momentum-v2/sig-4471"
    assert snap.message == ""
    assert snap.created_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert snap.updated_at > snap.created_at


def test_parse_documented_pending_row():
    snap = parse_order_row(DOC_OPENDING)
    assert snap.state is OrderState.WORKING
    assert snap.traded_price is None                # "" until filled
    assert snap.traded_qty == Decimal("0")
    assert snap.remaining_qty == Decimal("225")
    assert snap.remarks == ""


def test_parse_documented_gtt_row():
    snap = parse_order_row(DOC_GTT)
    assert snap.order_id == "GTT-2914581"
    assert snap.side == "SELL"
    assert snap.order_type == "OCO"
    assert snap.validity == ""
    assert snap.state is OrderState.CANCELLED
    assert snap.filled_qty == Decimal("0")


def test_parse_row_alternate_keys():
    snap = parse_order_row({
        "order_id": "EQ-5",
        "transaction_type": "sell",
        "scrip_code": "NSE_2885",
        "trading_symbol": "RELIANCE",
        "name": "Reliance Industries",
        "quantity": 5,
        "filled_quantity": "2",
        "average_price": "100.5",
        "order_status": "partially_executed",
        "product": "cnc",
        "message": "exchange said hello",
    })
    assert snap.order_id == "EQ-5"
    assert snap.side == "SELL"
    assert snap.security_id == "2885"
    assert snap.symbol == "RELIANCE"
    assert snap.product == "CNC"
    assert snap.requested_qty == Decimal("5")
    assert snap.traded_qty == Decimal("2")
    assert snap.traded_price == Decimal("100.5")
    assert snap.status == "PARTIALLY FILLED"
    assert snap.status_raw == "partially_executed"
    assert snap.state is OrderState.WORKING
    assert snap.message == "exchange said hello"


def test_parse_row_numeric_ids():
    snap = parse_order_row(row("PENDING", id=None, orderId=12345, security_id=2885))
    assert snap.order_id == "12345"
    assert snap.security_id == "2885"


def test_parse_row_rejection_message():
    snap = parse_order_row(row("FAILED", extra_info="RMS: Margin exceeds"))
    assert snap.state is OrderState.REJECTED
    assert snap.message == "RMS: Margin exceeds"


@pytest.mark.parametrize("bad", [
    {"status": "PENDING", "requested_qty": 10},   # no order id
    {"id": "", "status": "PENDING"},
    {"id": "   ", "status": "PENDING"},
    MagicMock(),
    None,
    ["EQ-1", "PENDING"],
    "EQ-1",
])
def test_parse_row_without_id_is_none(bad):
    assert parse_order_row(bad) is None


def test_parse_order_book_keeps_the_good_rows():
    rows = [
        row("PENDING", id="EQ-1"),
        {"status": "PENDING", "requested_qty": 4},   # no id: dropped
        "junk",
        None,
        row("SUCCESS", id="EQ-2", traded=10),
    ]
    book = parse_order_book(rows)
    assert isinstance(book, tuple)
    assert [s.order_id for s in book] == ["EQ-1", "EQ-2"]
    assert find_order(book, "EQ-2").state is OrderState.FILLED
    assert find_order(book, "EQ-3") is None


@pytest.mark.parametrize("bad", [None, {"orders": []}, MagicMock(), "rows"])
def test_parse_order_book_non_list(bad):
    assert parse_order_book(bad) == ()


# ── S7: numbers and times ───────────────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [
    (5, Decimal("5")),
    (5.5, Decimal("5.5")),
    (Decimal("7.25"), Decimal("7.25")),
    ("43.55", Decimal("43.55")),
    (" 12 ", Decimal("12")),
])
def test_to_decimal(value, expected):
    assert to_decimal(value) == expected


@pytest.mark.parametrize("value", ["", None, True, False, MagicMock(), "abc", float("nan"),
                                   float("inf"), "NaN", [], {}])
def test_to_decimal_rejects(value):
    assert to_decimal(value) is None


def test_parse_broker_time():
    ist = timedelta(hours=5, minutes=30)
    aware = parse_broker_time("2025-07-02T17:59:57.799576+05:30")
    assert aware.utcoffset() == ist
    assert (aware.hour, aware.microsecond) == (17, 799576)
    naive = parse_broker_time("2025-07-02T17:59:57")
    assert naive.utcoffset() == ist
    assert parse_broker_time("2025-07-02T12:29:57Z").utcoffset() == timedelta(0)
    for junk in ("junk", "", None, 123, MagicMock()):
        assert parse_broker_time(junk) is None


# ── S8: fills ───────────────────────────────────────────────────────────────


def test_parse_fills_vwap_over_two_fills():
    fills = parse_fills([
        {"fill_id": 1, "exch_order_id": "11", "quantity": 6, "price": 100,
         "trade_date": "2026-09-25T10:15:00+05:30"},
        {"fill_id": 2, "exch_order_id": "11", "quantity": 4, "price": 99,
         "trade_date": "2026-09-25T10:15:02+05:30"},
    ])
    assert fills.qty == Decimal("10")
    assert fills.vwap == Decimal("99.6")
    assert fills.count == 2


def test_parse_fills_alternate_keys_and_junk():
    fills = parse_fills([{"qty": "3", "trade_price": "50.5"}, "junk", {"quantity": 0, "price": 9},
                         {"traded_qty": 1, "traded_price": 52.5}])
    assert fills.qty == Decimal("4")
    assert fills.vwap == Decimal("51")
    assert fills.count == 2


def test_parse_fills_without_a_price_has_no_vwap():
    fills = parse_fills([{"quantity": 6, "price": 100}, {"quantity": 4}])
    assert fills.qty == Decimal("10")
    assert fills.vwap is None


@pytest.mark.parametrize("bad", [None, {}, MagicMock(), []])
def test_parse_fills_empty(bad):
    fills = parse_fills(bad)
    assert (fills.qty, fills.vwap, fills.count) == (Decimal("0"), None, 0)


# ── S9: rejection kinds ─────────────────────────────────────────────────────


@pytest.mark.parametrize("status_code,message,kind,expected", [
    (429, "", "", RejectKind.RATE_LIMITED),
    (400, "Too Many Requests", "", RejectKind.RATE_LIMITED),
    (400, "Rate limit exceeded", "", RejectKind.RATE_LIMITED),
    (400, "Market orders are blocked for this instrument.", "", RejectKind.MARKET_BLOCKED),
    (400, "Price should be a multiple of the tick size", "", RejectKind.PRICE),
    (400, "PriceWithinRange: Limit price must be within the allowed range", "", RejectKind.PRICE),
    (400, "Order price is outside the circuit limit", "", RejectKind.PRICE),
    (400, "RMS: Margin exceeds the available balance", "", RejectKind.OTHER),
    (401, "", "", RejectKind.AUTH),
    (403, "Forbidden", "", RejectKind.AUTH),
    (400, "Invalid token", "", RejectKind.AUTH),
    (0, "Client not initialised", "not_sent", RejectKind.NOT_SENT),
    (500, "Internal error", "http", RejectKind.OTHER),
])
def test_classify_rejection(status_code, message, kind, expected):
    assert classify_rejection(status_code, message, kind) is expected


def test_non_cnc_products():
    for product in ("INTRADAY", "intraday", "MIS", "MARGIN", "NRML", "MTF", "CO", "BO"):
        assert is_non_cnc_product(product)
    for product in ("CNC", "cnc", "", "DELIVERY?"):
        assert not is_non_cnc_product(product)
