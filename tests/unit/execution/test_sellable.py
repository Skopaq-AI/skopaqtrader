"""sellable_quantity: holdings + positions − open SELLs − fills positions don't show yet.

The pure calculation behind the worker's re-place check and the SafetyChecker's
no-short-sale check for live SELLs.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from skopaq.broker.models import Holding, Position
from skopaq.broker.order_status import parse_order_book
from skopaq.execution.sellable import SellContext, same_instrument, sellable_quantity
from skopaq.risk.calendar import IST
from tests.unit.execution._fakes import row

READ_AT = datetime(2026, 9, 25, 15, 21, tzinfo=IST)


def ctx(*rows, own_recent=0, own_ids=(), lag=600.0) -> SellContext:
    return SellContext(orders=parse_order_book(list(rows)), read_at=READ_AT,
                       own_recent_exit_qty=Decimal(own_recent), lag_window_s=lag,
                       own_order_ids=frozenset(own_ids))


def pos(net, *, sell=0, day_sell=0, product="CNC", symbol="TCS", sid="11536") -> Position:
    return Position(symbol=symbol, security_id=sid, quantity=Decimal(net),
                    sell_quantity=Decimal(sell), day_sell_quantity=Decimal(day_sell),
                    product=product)


def hold(n, symbol="TCS", sid="11536") -> Holding:
    return Holding(symbol=symbol, security_id=sid, quantity=Decimal(n))


def view(positions=(), holdings=(), context=None, qty=5, product="CNC", symbol="TCS",
         sid="11536"):
    return sellable_quantity(symbol=symbol, security_id=sid, product=product,
                             positions=list(positions), holdings=list(holdings),
                             context=context or ctx(), order_qty=Decimal(qty))


def at(hh, mm) -> str:
    return datetime(2026, 9, 25, hh, mm, tzinfo=IST).isoformat()


def test_holdings_plus_positions_minus_open_sells():   # T1, T2
    v = view([pos(5)], [hold(10)], ctx(row("PENDING", traded=0, requested=6, id="EQ-1")))
    assert (v.holding_qty, v.position_qty, v.pending_qty, v.sellable) == (10, 5, 6, 9)
    assert [o.order_id for o in v.pending] == ["EQ-1"]


def test_only_the_open_remainder_is_pending_and_its_fill_is_unshown():   # T2
    working = row("PARTIALLY FILLED", traded=4, requested=10, id="EQ-1",
                  updated_at=at(15, 20))
    lagging = view([pos(10)], [], ctx(working))
    assert (lagging.pending_qty, lagging.unshown_fill_qty, lagging.sellable) == (6, 4, 0)

    caught_up = view([pos(6, sell=4)], [], ctx(working))
    assert (caught_up.pending_qty, caught_up.unshown_fill_qty, caught_up.sellable) == (6, 0, 0)


def test_other_instruments_are_ignored_and_unattributable_rows_count():   # T3
    other = row("PENDING", id="EQ-2", security_id="1594")
    anonymous = row("PENDING", id="EQ-3", security_id="", requested=2)
    v = view([pos(10)], [], ctx(other, anonymous))
    assert (v.pending_qty, v.sellable) == (2, 8)


def test_an_open_sell_of_unknown_size_blocks_the_whole_order():   # T4
    v = view([pos(10)], [], ctx(row("PENDING", id="EQ-1", requested=None)), qty=5)
    assert (v.pending_qty, v.sellable) == (5, 5)


def test_positions_lagging_a_filled_exit_leave_nothing_to_sell():   # T5
    filled = row("SUCCESS", traded=10, id="EQ-1", updated_at=at(15, 20))
    lagging = view([pos(10)], [], ctx(filled))
    assert (lagging.unshown_fill_qty, lagging.sellable) == (10, 0)

    caught_up = view([pos(0, sell=10)], [], ctx(filled))
    assert (caught_up.unshown_fill_qty, caught_up.sellable) == (0, 0)


def test_only_sales_positions_do_not_show_yet_are_subtracted():   # T6
    earlier = row("SUCCESS", traded=5, requested=5, id="EQ-1", updated_at=at(10, 0))
    new = row("SUCCESS", traded=6, requested=6, id="EQ-2", updated_at=at(15, 20))
    v = view([pos(-5, sell=5)], [hold(20)], ctx(earlier, new))
    assert (v.unshown_fill_qty, v.sellable) == (6, 9)


def test_an_old_sale_never_shown_stops_counting_after_the_lag_window():   # T6
    old = row("SUCCESS", traded=5, requested=5, id="EQ-1", updated_at=at(10, 0))
    v = view([pos(10)], [], ctx(old))
    assert (v.unshown_fill_qty, v.sellable) == (0, 10)


def test_an_own_confirmed_exit_counts_even_without_a_book_row():   # T6
    v = view([pos(10)], [], ctx(own_recent=4))
    assert (v.unshown_fill_qty, v.sellable) == (4, 6)
    shown = view([pos(6, day_sell=4)], [], ctx(own_recent=4))
    assert (shown.unshown_fill_qty, shown.sellable) == (0, 6)


def test_an_unparseable_update_time_is_recent_only_for_own_orders():
    filled = row("SUCCESS", traded=3, requested=3, id="EQ-1", updated_at="yesterday-ish")
    assert view([pos(10)], [], ctx(filled)).unshown_fill_qty == 0
    assert view([pos(10)], [], ctx(filled, own_ids=["EQ-1"])).unshown_fill_qty == 3


def test_cnc_sell_product_rules():
    v = view([pos(10), pos(5, product="INTRADAY"), pos(2, product="")], [],
             ctx(row("PENDING", id="EQ-9", product="INTRADAY"),
                 row("PENDING", id="EQ-8", product="", requested=1)))
    assert (v.position_qty, v.pending_qty, v.sellable) == (12, 1, 11)
    assert "5 INTRADAY position" in v.excluded and "EQ-9 INTRADAY SELL" in v.excluded


def test_a_non_cnc_sell_ignores_holdings_and_other_products():
    v = view([pos(10), pos(4, product="INTRADAY")], [hold(20)], product="INTRADAY")
    assert (v.holding_qty, v.position_qty, v.sellable) == (0, 4, 4)


def test_buys_never_count():
    v = view([pos(10)], [], ctx(row("PENDING", id="EQ-1", txn_type="BUY"),
                                row("SUCCESS", traded=10, id="EQ-2", txn_type="BUY",
                                    updated_at=at(15, 20))))
    assert (v.pending_qty, v.unshown_fill_qty, v.sellable) == (0, 0, 10)


def test_same_instrument():
    assert same_instrument("TCS", "11536", "TCS-EQ", "11536")
    assert not same_instrument("TCS", "11536", "TCS", "1594")      # ids decide when both known
    assert same_instrument("NSE:TCS-EQ", "", "tcs", "11536")        # else the base symbol
    assert not same_instrument("", "", "TCS", "")


def test_success_reporting_nothing_traded_counts_as_sold_in_full():
    # The broker contradicting itself (SUCCESS, traded_qty 0 or missing) is read the
    # conservative way, as the worker reads it: the order may have sold everything
    for traded in (0, None, 10):
        done = row("SUCCESS", traded=traded, requested=10, id="EQ-1", updated_at=at(15, 20))
        v = view([pos(0)], [hold(10)], ctx(done), qty=10)
        assert (v.unshown_fill_qty, v.sellable) == (10, 0), traded


def test_an_uncertain_sell_placement_counts_until_the_lag_window_ends():
    from skopaq.execution.sellable import UncertainPlacement

    sent = datetime(2026, 9, 25, 15, 20, tzinfo=IST)
    placement = UncertainPlacement(internal_id="abc", symbol="TCS", security_id="11536",
                                   qty=Decimal(10), at=sent)

    def with_book(*rows, own_ids=(), uncertain=(placement,)):
        return SellContext(orders=parse_order_book(list(rows)), read_at=READ_AT,
                           own_order_ids=frozenset(own_ids), uncertain=uncertain)

    # Not in the book yet: it may be working, so the shares are not sold again
    v = view([pos(10)], [], with_book(), qty=10)
    assert (v.uncertain_qty, v.sellable) == (10, 0)
    # An order that merely looks like it may be someone else's SELL: both count
    looks_like_it = row("PENDING", requested=10, id="EQ-5", created_at=at(15, 20))
    v = view([pos(10), pos(10)], [], with_book(looks_like_it), qty=10)
    assert (v.uncertain_qty, v.pending_qty, v.sellable) == (10, 10, 0)
    # An order we already know is not it (e.g. the exit's own earlier, cancelled attempt)
    earlier = row("CANCELLED", requested=10, id="EQ-4", created_at=at(15, 19))
    v = view([pos(10)], [], with_book(earlier, own_ids={"EQ-4"}), qty=10)
    assert (v.uncertain_qty, v.sellable) == (10, 0)
    # Another instrument, or older than the lag window: not counted
    assert view([pos(10, symbol="INFY", sid="1594")], [], with_book(), qty=10,
                symbol="INFY", sid="1594").uncertain_qty == 0
    old = UncertainPlacement(internal_id="x", symbol="TCS", security_id="11536",
                             qty=Decimal(10), at=datetime(2026, 9, 25, 15, 0, tzinfo=IST))
    assert view([pos(10)], [], with_book(uncertain=(old,)), qty=10).sellable == 10


def test_an_order_carrying_the_placements_remark_is_counted_instead():
    from skopaq.execution.sellable import UncertainPlacement

    placement = UncertainPlacement(internal_id="abc", symbol="TCS", security_id="11536",
                                   qty=Decimal(10), at=datetime(2026, 9, 25, 15, 20, tzinfo=IST),
                                   remark="skopaq-abc")
    tagged = row("PENDING", requested=10, id="EQ-5", created_at=at(15, 20),
                 remarks="skopaq-abc")
    context = SellContext(orders=parse_order_book([tagged]), read_at=READ_AT,
                          uncertain=(placement,))
    v = view([pos(10)], [], context, qty=10)
    assert (v.uncertain_qty, v.pending_qty, v.sellable) == (0, 10, 0)


def test_could_be_placement_needs_a_new_order_created_about_then():
    from skopaq.execution.sellable import UncertainPlacement, could_be_placement

    sent = datetime(2026, 9, 25, 15, 20, tzinfo=IST)
    placement = UncertainPlacement(internal_id="abc", symbol="TCS", security_id="11536",
                                   qty=Decimal(10), at=sent, before_ids=frozenset({"EQ-1"}))
    [new, old, later, other_qty] = parse_order_book([
        row("PENDING", requested=10, id="EQ-2", created_at=at(15, 21)),
        row("PENDING", requested=10, id="EQ-1", created_at=at(15, 20)),     # in the book before
        row("PENDING", requested=10, id="EQ-3", created_at=at(15, 30)),     # 10 min later
        row("PENDING", requested=6, id="EQ-4", created_at=at(15, 20)),
    ])
    assert could_be_placement(new, placement, set())
    assert not could_be_placement(new, placement, {"EQ-2"})               # already ours
    assert not could_be_placement(old, placement, set())
    assert not could_be_placement(later, placement, set())
    assert not could_be_placement(other_qty, placement, set())


# ── The same shares on another exchange (security ids differ per exchange) ────

TCS_ISIN = "INE467B01029"


def _cross(net, *, exchange, sid, isin=TCS_ISIN, sell=0) -> Position:
    return Position(symbol="TCS", security_id=sid, exchange=exchange, isin=isin,
                    product="CNC", quantity=Decimal(net), sell_quantity=Decimal(sell))


def test_a_sale_of_the_same_shares_on_bse_is_counted():
    # Skopaq bought 10 TCS on NSE today; the user sold them in the app on BSE
    positions = [_cross(10, exchange="NSE", sid="11536"),
                 _cross(-10, exchange="BSE", sid="532540", sell=10)]
    book = SellContext(orders=parse_order_book([
        row("SUCCESS", traded=10, requested=10, id="EQ-2", security_id="532540",
            exchange="BSE", isin=TCS_ISIN, updated_at=at(15, 20))]), read_at=READ_AT)
    v = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                          positions=positions, holdings=[], context=book,
                          order_qty=Decimal(10), exchange="NSE")
    assert (v.position_qty, v.sellable) == (0, 0)


def test_an_open_bse_sell_of_the_same_isin_counts_and_another_isin_does_not():
    positions = [_cross(10, exchange="NSE", sid="11536")]
    same = row("O-PENDING", requested=10, id="BSE-1", security_id="532540",
               exchange="BSE", isin=TCS_ISIN)
    other = row("O-PENDING", requested=10, id="BSE-2", security_id="500325",
                exchange="BSE", isin="INE002A01018")
    v = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                          positions=positions, holdings=[],
                          context=SellContext(orders=parse_order_book([same, other]),
                                              read_at=READ_AT),
                          order_qty=Decimal(10), exchange="NSE")
    assert [o.order_id for o in v.pending] == ["BSE-1"] and v.sellable == 0


def test_an_open_sell_on_another_exchange_without_an_isin_counts_against_it():
    positions = [_cross(10, exchange="NSE", sid="11536")]
    unknown = row("O-PENDING", requested=10, id="BSE-3", security_id="532540",
                  exchange="BSE")                          # no isin: cannot tell, so it counts
    v = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                          positions=positions, holdings=[],
                          context=SellContext(orders=parse_order_book([unknown]),
                                              read_at=READ_AT),
                          order_qty=Decimal(10), exchange="NSE")
    assert v.sellable == 0


def test_same_exchange_rows_still_match_by_security_id():
    positions = [_cross(10, exchange="NSE", sid="11536", isin=""),
                 Position(symbol="TCS-BE", security_id="99999", exchange="NSE",
                          product="CNC", quantity=Decimal(5))]
    v = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                          positions=positions, holdings=[],
                          context=SellContext(orders=(), read_at=READ_AT),
                          order_qty=Decimal(10), exchange="NSE")
    assert v.position_qty == 10


def test_a_bse_sale_of_the_same_shares_counts_by_symbol_without_isins():
    positions = [_cross(10, exchange="NSE", sid="11536", isin=""),
                 _cross(-10, exchange="BSE", sid="532540", isin="", sell=10)]
    v = sellable_quantity(symbol="TCS", security_id="11536", product="CNC",
                          positions=positions, holdings=[],
                          context=SellContext(orders=(), read_at=READ_AT),
                          order_qty=Decimal(10), exchange="NSE")
    assert (v.position_qty, v.sellable) == (0, 0)


def test_a_partially_filled_final_sell_reporting_zero_traded_counts_in_full():
    # "PARTIALLY FILLED - CANCELLED" with traded_qty 0: something sold, how much is unknown
    # — counted as all of it (like SUCCESS reporting nothing traded), never as nothing
    for status in ("SUCCESS", "PARTIALLY FILLED - CANCELLED", "PARTIALLY FILLED - EXPIRED"):
        sold = row(status, traded=0, requested=10, id="EQ-1", updated_at=at(15, 20))
        v = view([pos(10)], [], ctx(sold), qty=10)
        assert (v.unshown_fill_qty, v.sellable) == (10, 0), status
