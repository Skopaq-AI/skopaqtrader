"""INDstocksClient order and portfolio calls against a mocked transport
(httpx.MockTransport): request shapes, the strict response envelope, and which
failures mean "not placed" vs "may have been placed"."""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest

from skopaq.broker import client as client_mod
from skopaq.broker.client import BrokerError, INDstocksClient, OrderPlacementUncertain
from skopaq.broker.models import (
    CancelOrderRequest,
    ModifyOrderRequest,
    OrderRequest,
    OrderType,
    Side,
)
from skopaq.broker.rate_limiter import RateLimiter
from skopaq.broker.token_manager import TokenExpiredError


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    """The module limiters are shared across tests; give each test fresh, roomy ones."""
    monkeypatch.setattr(client_mod, "_api_limiter", RateLimiter(max_calls=10_000))
    monkeypatch.setattr(client_mod, "_order_limiter", RateLimiter(max_calls=10_000))


class Recorder:
    """A MockTransport handler that answers from a script and records every request."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = answer(request)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def body(self, i: int = -1) -> dict:
        return json.loads(self.requests[i].content)


def ok(data) -> httpx.Response:
    return httpx.Response(200, json={"status": "success", "data": data})


def _config(remarks=None) -> MagicMock:
    cfg = MagicMock()
    cfg.indstocks_base_url = "https://api.indstocks.test"
    if remarks is not None:
        cfg.indstocks_order_remarks_enabled = remarks
    return cfg


def _client(handler, *, remarks=None, token="TOKEN-123") -> INDstocksClient:
    token_mgr = MagicMock()
    token_mgr.get_token.return_value = token
    return INDstocksClient(_config(remarks), token_mgr, transport=httpx.MockTransport(handler))


def _order(**kw) -> OrderRequest:
    values = dict(symbol="RELIANCE", side=Side.SELL, quantity=Decimal("5"),
                  order_type=OrderType.LIMIT, price=1400.5, security_id="2885")
    values.update(kw)
    return OrderRequest(**values)


PLACED = ok({"order_id": "EQ-93586788", "order_status": "INITIATED"})


# ── C1: place_order ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_place_order_reads_order_status_and_keeps_the_payload():
    rec = Recorder(PLACED)
    async with _client(rec) as client:
        resp = await client.place_order(_order())
    assert resp.order_id == "EQ-93586788"
    assert resp.status == "INITIATED"
    req = rec.requests[0]
    assert (req.method, req.url.path) == ("POST", "/order")
    assert req.headers["Authorization"] == "TOKEN-123"          # no "Bearer "
    assert rec.body() == {
        "txn_type": "SELL", "exchange": "NSE", "segment": "EQUITY", "product": "CNC",
        "order_type": "LIMIT", "validity": "DAY", "security_id": "2885", "qty": 5,
        "is_amo": False, "algo_id": "99999", "limit_price": 1400.5,
    }


@pytest.mark.asyncio
async def test_place_order_sends_remarks_only_when_enabled():
    order = _order()
    for remarks, expect in ((True, True), (False, False), (None, False)):
        rec = Recorder(PLACED)
        async with _client(rec, remarks=remarks) as client:
            await client.place_order(order)
        if expect:
            assert rec.body()["remarks"] == f"skopaq-{order.internal_id.hex[:24]}"
        else:
            assert "remarks" not in rec.body()           # None: a MagicMock attribute, not True


@pytest.mark.asyncio
async def test_place_order_falls_back_to_status_inside_data():
    rec = Recorder(ok({"order_id": "EQ-1", "status": "o-pending"}))
    async with _client(rec) as client:
        resp = await client.place_order(_order())
    assert resp.status == "O-PENDING"


# ── C2: get_order ───────────────────────────────────────────────────────────


ROW = {"id": "EQ-1", "status": "PENDING", "txn_type": "SELL", "requested_qty": 5,
       "traded_qty": 0, "security_id": "2885"}


@pytest.mark.asyncio
async def test_get_order_sends_body_and_params():
    rec = Recorder(ok(ROW))
    async with _client(rec) as client:
        data = await client.get_order("EQ-1")
    assert data == ROW
    req = rec.requests[0]
    assert (req.method, req.url.path) == ("GET", "/order")
    assert dict(req.url.params) == {"order_id": "EQ-1", "segment": "EQUITY"}
    assert rec.body() == {"order_id": "EQ-1", "segment": "EQUITY"}


@pytest.mark.asyncio
async def test_get_order_list_response_picks_the_id():
    rec = Recorder(ok([{"id": "EQ-0", "status": "SUCCESS"}, ROW]), ok([{"id": "EQ-0"}]), ok(None))
    async with _client(rec) as client:
        assert await client.get_order("EQ-1") == ROW
        assert await client.get_order("EQ-1") == {}
        assert await client.get_order("EQ-1") == {}


# ── C3: get_order_book ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_order_book_null_data_under_success_is_empty():
    rec = Recorder(ok(None))
    async with _client(rec) as client:
        assert await client.get_order_book() == []
    assert rec.requests[0].url.path == "/order-book"


@pytest.mark.asyncio
async def test_order_book_rows():
    rec = Recorder(ok([ROW, "junk", None]), ok({"orders": [ROW]}))
    async with _client(rec) as client:
        assert await client.get_order_book() == [ROW]
        assert await client.get_order_book() == [ROW]


# ── C4 / C5: cancel and modify ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_reads_order_status():
    rec = Recorder(ok({"order_id": "EQ-1", "order_status": "CANCELLED"}), ok({"order_id": "EQ-1"}))
    async with _client(rec) as client:
        resp = await client.cancel_order(CancelOrderRequest(order_id="EQ-1"))
        assert (resp.order_id, resp.status) == ("EQ-1", "CANCELLED")
        resp = await client.cancel_order(CancelOrderRequest(order_id="EQ-1"))
        assert (resp.status, resp.message) == ("", "")    # no fake "CANCELLED"
    assert rec.requests[0].url.path == "/order/cancel"
    assert rec.body(0) == {"order_id": "EQ-1", "segment": "EQUITY"}


@pytest.mark.asyncio
async def test_cancel_failure_body_raises():
    rec = Recorder(httpx.Response(200, json={"status": "error",
                                             "message": "OrderCannotBeCancelled"}))
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await client.cancel_order(CancelOrderRequest(order_id="EQ-1"))
    assert info.value.kind == "error_body"
    assert "OrderCannotBeCancelled" in str(info.value)


@pytest.mark.asyncio
async def test_modify_needs_quantity_and_price():
    rec = Recorder(ok({"order_id": "EQ-1", "order_status": "MODIFIED"}))
    async with _client(rec) as client:
        with pytest.raises(ValueError):
            await client.modify_order(ModifyOrderRequest(order_id="EQ-1", quantity=5))
        with pytest.raises(ValueError):
            await client.modify_order(ModifyOrderRequest(order_id="EQ-1", price=1400.0))
        assert rec.requests == []
        resp = await client.modify_order(
            ModifyOrderRequest(order_id="EQ-1", quantity=5, price=1400.0))
    assert resp.status == "MODIFIED"
    assert rec.body() == {"order_id": "EQ-1", "segment": "EQUITY", "qty": 5, "limit_price": 1400.0}


# ── C6: positions and holdings ──────────────────────────────────────────────


def _position_row(product: str, **kw) -> dict:
    base = {
        "position_id": "86016462", "security_id": "2885", "symbol": "RELIANCE",
        "segment": "EQUITY", "product": product, "exchange": "NSE", "isin": "INE002A01018",
        "net_qty": 5, "avg_price": 1400.0, "buy_qty": 5, "buy_avg": 1400.0, "sell_qty": 0,
        "sell_avg": 0, "realized_profit": 0, "day_buy_qty": 5, "day_buy_val": 7000.0,
        "day_sell_qty": 0, "day_sell_val": 0, "cf_buy_qty": None, "cf_buy_val": None,
        "cf_sell_qty": None, "cf_sell_val": None,
    }
    base.update(kw)
    return base


def _by_product(cnc, intraday):
    def answer(request: httpx.Request):
        return cnc if request.url.params.get("product") == "cnc" else intraday
    return answer


@pytest.mark.asyncio
async def test_positions_two_calls_with_lowercase_params():
    cnc_row = _position_row("", day_buy_val=None, day_sell_val=None)   # a null value is accepted
    intraday_row = _position_row("", security_id="1521", symbol="INDIAGLYCO", net_qty=0)
    rec = Recorder(_by_product(ok([cnc_row]), ok([intraday_row])))
    async with _client(rec) as client:
        positions = await client.get_positions()
    params = [dict(r.url.params) for r in rec.requests]
    assert params == [{"segment": "equity", "product": "cnc"},
                      {"segment": "equity", "product": "intraday"}]
    assert all(r.url.path == "/portfolio/positions" for r in rec.requests)
    assert [(p.symbol, p.product) for p in positions] == [("RELIANCE", "CNC"),
                                                          ("INDIAGLYCO", "INTRADAY")]
    assert positions[0].quantity == Decimal("5")
    assert positions[0].buy_value == 0.0


@pytest.mark.asyncio
async def test_positions_wrapper_and_empty_data():
    wrapped = ok({"net_positions": [_position_row("CNC")], "day_positions": []})
    rec = Recorder(_by_product(wrapped, ok(None)))
    async with _client(rec) as client:
        positions = await client.get_positions()
    assert [(p.symbol, p.product) for p in positions] == [("RELIANCE", "CNC")]


@pytest.mark.asyncio
async def test_positions_fall_back_to_the_legacy_call_when_both_are_refused():
    def answer(request: httpx.Request):
        if request.url.params:
            return httpx.Response(400, json={"status": "error", "message": "bad params"})
        return ok([_position_row("CNC"), _position_row("", security_id="1", symbol="X")])
    rec = Recorder(answer)
    async with _client(rec) as client:
        positions = await client.get_positions()
    assert len(rec.requests) == 3
    assert not rec.requests[2].url.params
    assert [(p.symbol, p.product) for p in positions] == [("RELIANCE", "CNC"), ("X", "")]


@pytest.mark.asyncio
async def test_positions_server_error_propagates():
    rec = Recorder(_by_product(httpx.Response(503, text="unavailable"), ok([])))
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await client.get_positions()
    assert info.value.status_code == 503


@pytest.mark.asyncio
async def test_positions_cnc_refused_alone_raises():
    refused = httpx.Response(404, json={"status": "error", "message": "not found"})
    rec = Recorder(_by_product(refused, ok([_position_row("")])))
    async with _client(rec) as client:
        with pytest.raises(BrokerError):
            await client.get_positions()


@pytest.mark.asyncio
async def test_positions_intraday_refused_alone_keeps_cnc_rows():
    refused = httpx.Response(400, json={"status": "error", "message": "bad product"})
    rec = Recorder(_by_product(ok([_position_row("")]), refused))
    async with _client(rec) as client:
        positions = await client.get_positions()
    assert [(p.symbol, p.product) for p in positions] == [("RELIANCE", "CNC")]


@pytest.mark.asyncio
async def test_holdings_parse_total_qty():
    rec = Recorder(ok([{"security_id": "18520", "symbol": "CUPID", "isin": "INE509F01029",
                        "total_qty": 1, "used_qty": 0, "avg_price": 217.3, "t1_qty": 1,
                        "t1_avg_price": 217.3, "dp_qty": 0, "dp_avg_price": 0}]),
                   ok(None))
    async with _client(rec) as client:
        holdings = await client.get_holdings()
        assert await client.get_holdings() == []
    assert holdings[0].quantity == Decimal("1")
    assert holdings[0].average_price == 217.3
    assert holdings[0].security_id == "18520"


# ── C7: per-order trades ────────────────────────────────────────────────────


FILL = {"fill_id": 1, "exch_order_id": "11", "quantity": 5, "price": 1400.5,
        "trade_date": "2026-09-25T10:15:00+05:30"}
NOT_FOUND = httpx.Response(404, json={"status": "error", "message": "Not Found"})


@pytest.mark.asyncio
async def test_trades_order_trades_path_with_body_and_params():
    rec = Recorder(ok([FILL]))
    async with _client(rec) as client:
        assert await client.get_trades("EQ-1") == [FILL]
    req = rec.requests[0]
    assert (req.method, req.url.path) == ("GET", "/order/trades")
    assert dict(req.url.params) == {"order_id": "EQ-1", "segment": "EQUITY"}
    assert rec.body() == {"order_id": "EQ-1", "segment": "EQUITY"}


@pytest.mark.asyncio
async def test_trades_404_falls_back_for_that_call_only():
    def answer(request: httpx.Request):
        return NOT_FOUND if request.url.path == "/order/trades" else ok([])
    rec = Recorder(answer)
    async with _client(rec) as client:
        assert await client.get_trades("EQ-1") == []
        assert await client.get_trades("EQ-1") == []
    # An empty /trades/{id} result does not make it the preferred path
    assert [r.url.path for r in rec.requests] == ["/order/trades", "/trades/EQ-1",
                                                  "/order/trades", "/trades/EQ-1"]


@pytest.mark.asyncio
async def test_trades_path_with_rows_becomes_the_preference():
    def answer(request: httpx.Request):
        return NOT_FOUND if request.url.path == "/order/trades" else ok([FILL])
    rec = Recorder(answer)
    async with _client(rec) as client:
        assert await client.get_trades("EQ-1") == [FILL]
        assert await client.get_trades("EQ-2") == [FILL]
    assert [r.url.path for r in rec.requests] == ["/order/trades", "/trades/EQ-1", "/trades/EQ-2"]


@pytest.mark.asyncio
async def test_trades_other_errors_propagate():
    rec = Recorder(httpx.Response(500, text="boom"))
    async with _client(rec) as client:
        with pytest.raises(BrokerError):
            await client.get_trades("EQ-1")
    assert len(rec.requests) == 1


@pytest.mark.asyncio
async def test_trade_book():
    rec = Recorder(ok([FILL]), ok(None))
    async with _client(rec) as client:
        assert await client.get_trade_book() == [FILL]
        assert await client.get_trade_book() == []
    assert dict(rec.requests[0].url.params) == {"segment": "EQUITY"}


# ── C8 / C9: a 2xx failure body is an error, never an empty book ────────────


FAILURE_BODIES = [
    {"status": "error", "data": None, "message": "Something went wrong"},
    {"status": "failure", "data": None},
    {"status": "FAILURE", "error": {"msg": "no data"}},
    {"success": False, "data": None, "message": "nope"},
    {"data": None},                                   # null without a success marker
    {"status": "success", "data": [], "error_type": "DataException"},
    {"status": "success", "data": [], "error_code": "E42"},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", FAILURE_BODIES)
async def test_order_book_failure_body_raises(body):
    rec = Recorder(httpx.Response(200, json=body))
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await client.get_order_book()
    assert info.value.kind == "error_body"
    assert info.value.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("body", FAILURE_BODIES)
async def test_positions_failure_body_raises(body):
    rec = Recorder(httpx.Response(200, json=body))
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await client.get_positions()
    assert info.value.kind == "error_body"


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [None, False, 0, "", {}])
async def test_empty_error_key_on_a_success_is_not_an_error(empty):
    body = {"status": "success", "data": [ROW], "error": empty, "error_code": empty}
    rec = Recorder(httpx.Response(200, json=body))
    async with _client(rec) as client:
        assert await client.get_order_book() == [ROW]


@pytest.mark.asyncio
async def test_success_flag_envelope():
    rec = Recorder(httpx.Response(200, json={"success": True, "data": None}))
    async with _client(rec) as client:
        assert await client.get_order_book() == []


# ── C10 / C11: payloads that are not what the endpoint returns ──────────────


@pytest.mark.asyncio
async def test_non_json_read_is_bad_payload():
    rec = Recorder(httpx.Response(200, text="<html>gateway</html>"))
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await client.get_order_book()
    assert info.value.kind == "bad_payload"


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [{"foo": 1}, "rows", 5])
async def test_order_book_wrong_shape_is_bad_payload(data):
    rec = Recorder(ok(data))
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await client.get_order_book()
    assert info.value.kind == "bad_payload"


# ── C12: outcomes of POST /order that may have placed the order ─────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("answer,kind", [
    (httpx.Response(200, json={"status": "failure",
                               "error": {"msg": "no order number in rs response"}}), "error_body"),
    (httpx.Response(200, json={"status": "success", "data": {"order_status": "INITIATED"}}),
     "bad_payload"),
    (httpx.Response(200, json={"status": "success", "data": None}), "bad_payload"),
    (httpx.Response(500, text="Internal Server Error"), "http"),
    (httpx.Response(502, json={"status": "error", "message": "Bad gateway"}), "http"),
    (httpx.ReadTimeout("read timed out"), "transport"),
    (httpx.RemoteProtocolError("server disconnected"), "transport"),
    (httpx.Response(200, text="not json"), "bad_payload"),
])
async def test_place_order_uncertain(answer, kind):
    rec = Recorder(answer)
    async with _client(rec) as client:
        with pytest.raises(OrderPlacementUncertain) as info:
            await client.place_order(_order())
    assert info.value.kind == kind
    assert len(rec.requests) == 1                          # never retried here


@pytest.mark.asyncio
async def test_place_order_uncertain_keeps_the_broker_message():
    rec = Recorder(httpx.Response(200, json={"status": "failure",
                                             "error": {"msg": "no order number in rs response"}}))
    async with _client(rec) as client:
        with pytest.raises(OrderPlacementUncertain, match="no order number in rs response"):
            await client.place_order(_order())


@pytest.mark.asyncio
@pytest.mark.parametrize("code,message", [
    (400, "RMS: Margin exceeds the available balance"),
    (429, "Rate limit exceeded"),
])
async def test_place_order_4xx_is_a_definite_rejection(code, message):
    rec = Recorder(httpx.Response(code, json={"status": "error", "message": message,
                                              "error_type": "OrderException"}))
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await client.place_order(_order())
    assert not isinstance(info.value, OrderPlacementUncertain)
    assert info.value.kind == "http"
    assert info.value.status_code == code
    assert message in str(info.value)


# ── C13: nothing was sent ───────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [httpx.ConnectError("refused"), httpx.ConnectTimeout("timeout"),
                                 httpx.PoolTimeout("pool")])
async def test_connection_failures_are_not_sent(exc):
    rec = Recorder(exc)
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await client.place_order(_order())
    assert not isinstance(info.value, OrderPlacementUncertain)
    assert info.value.kind == "not_sent"


@pytest.mark.asyncio
async def test_client_not_initialised_is_not_sent():
    client = _client(Recorder(PLACED))                     # never entered
    with pytest.raises(BrokerError) as info:
        await client.place_order(_order())
    assert not isinstance(info.value, OrderPlacementUncertain)
    assert info.value.kind == "not_sent"


@pytest.mark.asyncio
async def test_expired_token_is_not_sent():
    rec = Recorder(PLACED)
    token_mgr = MagicMock()
    token_mgr.get_token.side_effect = TokenExpiredError("token expired")
    client = INDstocksClient(_config(), token_mgr, transport=httpx.MockTransport(rec))
    async with client:
        with pytest.raises(BrokerError) as info:
            await client.place_order(_order())
    assert not isinstance(info.value, OrderPlacementUncertain)
    assert info.value.kind == "not_sent"
    assert rec.requests == []


# ── Market data keeps the lenient parser ────────────────────────────────────


@pytest.mark.asyncio
async def test_market_data_still_unwraps_any_envelope():
    rec = Recorder(httpx.Response(200, json={"success": True,
                                             "data": {"NSE_2885": {"live_price": 1362}}}))
    async with _client(rec) as client:
        assert await client.get_ltp("NSE_2885") == 1362.0
    assert dict(rec.requests[0].url.params) == {"scrip-codes": "NSE_2885"}


@pytest.mark.asyncio
async def test_http_error_message_prefers_the_broker_text():
    rec = Recorder(httpx.Response(401, json={"status": "error", "message": "Invalid token",
                                             "error_type": "TokenException"}))
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await client.get_ltp("NSE_2885")
    assert info.value.status_code == 401
    assert info.value.kind == "http"
    assert "Invalid token" in str(info.value)


# ── Review fixes: answers that are not an envelope ──────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ["get_order_book", "get_positions", "get_holdings"])
@pytest.mark.parametrize("body", [b"null", b"5", b'"rows"', b"true"])
async def test_a_bare_json_scalar_is_bad_payload_not_empty(call, body):
    # A 2xx whose whole body is `null` must not read as an empty order book (no open
    # SELLs), no positions or no holdings: only `data: null` under a success marker is
    rec = Recorder(httpx.Response(200, content=body,
                                  headers={"content-type": "application/json"}))
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await getattr(client, call)()
    assert info.value.kind == "bad_payload"


@pytest.mark.asyncio
async def test_get_order_without_data_is_bad_payload():
    # An envelope without `data` is not the order row: its `status: success` would read
    # as a full fill the broker never confirmed
    rec = Recorder(httpx.Response(200, json={"status": "success", "order_id": "EQ-1",
                                             "message": "ok"}))
    async with _client(rec) as client:
        with pytest.raises(BrokerError) as info:
            await client.get_order("EQ-1")
    assert info.value.kind == "bad_payload"


# ── Non-Trading reads have their own limit (15/s documented; 12 used) ───────


@pytest.mark.asyncio
async def test_non_trading_reads_go_through_the_read_limiter_and_orders_do_not():
    def answer(request):
        if request.url.path == "/market/quotes/ltp":
            return httpx.Response(200, json={"status": "success",
                                             "data": {"NSE_2885": {"live_price": 100}}})
        if request.url.path == "/funds":
            return ok({"detailed_avl_balance": {"eq_cnc": 1}})
        if request.url.path == "/order" and request.method == "POST":
            return PLACED
        return ok([])

    acquired = []

    class Spy:
        async def acquire(self):
            acquired.append(True)

    async with _client(answer) as client:
        client._read_limiter = Spy()
        await client.get_order_book()
        await client.get_positions()                  # two queries: cnc and intraday
        await client.get_holdings()
        await client.get_funds()
        await client.get_trade_book()
        reads = len(acquired)
        await client.place_order(_order())
        try:
            await client.get_ltp("NSE_2885")
        except BrokerError:
            pass
    assert reads == 6
    assert len(acquired) == reads                     # neither the order nor market data


@pytest.mark.asyncio
async def test_a_burst_of_reads_stays_under_the_documented_non_trading_limit():
    import asyncio
    from collections import deque

    from skopaq.broker.rate_limiter import SlidingWindowLimiter
    from tests.unit.execution._fakes import FakeClock

    clock = FakeClock()
    window: deque = deque()
    refused = []

    def broker(request):
        # INDstocks: 15 Non-Trading requests per second, then 429
        while window and clock.t - window[0] >= 1.0:
            window.popleft()
        if len(window) >= 15:
            refused.append(clock.t)
            return httpx.Response(429, json={"status": "error", "message": "Too Many Requests"})
        window.append(clock.t)
        return ok([])

    async with _client(broker) as client:
        client._read_limiter = SlidingWindowLimiter(12, 1.0, clock=clock.clock,
                                                    sleep=clock.sleep)
        # Five concurrent exits, each reading the book, positions (2 queries) and holdings
        await asyncio.gather(*(read() for read in [client.get_order_book, client.get_positions,
                                                   client.get_holdings] * 5))
    assert refused == []
    assert clock.t >= 1.0                            # the 20 reads were spread out
