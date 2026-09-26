"""Async REST client for the INDstocks broker API.

Endpoint paths, auth headers, and field names are taken directly from
live API testing — do NOT modify without testing against real API first.

Key differences from typical broker APIs:
    - Auth header: ``Authorization: TOKEN`` (NO "Bearer " prefix)
    - No ``/api/v1/`` path prefix — paths start at root
    - Orders require ``algo_id="99999"`` for regular orders
    - ALL market data endpoints use ``scrip-codes=NSE_2885`` param format
      (exchange underscore security_id from instruments CSV)
    - Historical candles are objects: ``{"ts": epoch_sec, "o":, "h":, ...}``
    - Quote response fields: ``live_price``, ``day_open``, ``day_high``,
      ``day_low``, ``prev_close``, ``day_change``, ``day_change_percentage``
    - Order and portfolio calls use the strict envelope (``_request_envelope``):
      a 2xx ``{"status": "error"|"failure"}`` body raises, and ``data: null`` is
      empty only under ``status: success``. Market data keeps the lenient parser
      (historical data uses ``{"success": true, "data": ...}``).
    - POST /order answers ``data.order_id`` and ``data.order_status`` (not
      ``status``). Acceptance is not a fill: statuses and rows are parsed by
      ``skopaq.broker.order_status``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional

import httpx

from skopaq.broker.models import (
    CancelOrderRequest,
    Funds,
    HistoricalCandle,
    Holding,
    ModifyOrderRequest,
    OptionChain,
    OptionData,
    OrderRequest,
    OrderResponse,
    Position,
    Quote,
    Segment,
    UserProfile,
)
from skopaq.broker.order_status import normalise_status
from skopaq.broker.rate_limiter import RateLimiter, SlidingWindowLimiter
from skopaq.broker.token_manager import TokenExpiredError, TokenManager
from skopaq.config import SkopaqConfig

logger = logging.getLogger(__name__)

# Separate limiters matching INDstocks rate limits
_api_limiter = RateLimiter(max_calls=100, period=1.0)
_order_limiter = RateLimiter(max_calls=10, period=1.0)
# Non-Trading APIs (order history, trades, portfolio, funds, profile) allow 15 requests a
# second: each client stays at 12 in any rolling second, so a burst of live SELLs (each
# reads the book, positions, holdings and funds before it is placed) is not refused
_NON_TRADING_PER_S = 12
_NON_TRADING_PATHS = frozenset({"/order-book", "/order", "/order/trades", "/trade-book",
                                "/funds", "/user/profile"})
_NON_TRADING_PREFIXES = ("/trades/", "/portfolio/")


def _is_non_trading(method: str, path: str) -> bool:
    return method == "GET" and (path in _NON_TRADING_PATHS
                                or path.startswith(_NON_TRADING_PREFIXES))


class BrokerError(Exception):
    """Raised when a broker API call fails.

    ``kind`` says how far the request got:

    - ``not_sent``: it never reached the broker (client not open, expired token,
      connection refused or timed out while connecting). An order was NOT placed.
    - ``transport``: it may have reached the broker (read/write error or timeout
      after sending, the server dropping the connection).
    - ``http``: the broker answered with HTTP >= 400 (``status_code``).
    - ``bad_payload``: a 2xx answer that is not JSON or not the expected shape.
    - ``error_body``: a 2xx answer whose body reports a failure.
    """

    def __init__(
        self, message: str, status_code: int = 0, body: str = "", kind: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.kind = kind


class OrderPlacementUncertain(BrokerError):
    """POST /order outcome unknown — the order may exist.

    Reconcile against the order book before retrying; never re-send blind.
    """


# httpx errors raised before the request left this host: nothing reached the broker.
_NOT_SENT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)


def _error_text(payload: object) -> str:
    """The broker's own words from an error body: ``message``, ``error.msg``, ``error``."""
    if not isinstance(payload, dict):
        return ""
    message = payload.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("msg", "message", "description"):
            if isinstance(error.get(key), str) and error[key].strip():
                return error[key].strip()
    if isinstance(error, str) and error.strip():
        return error.strip()
    for key in ("error_type", "error_code"):
        if payload.get(key) not in (None, ""):
            return str(payload[key])
    return ""


def _is_error_body(payload: dict) -> bool:
    """A body that reports a failure, whatever its HTTP status.

    ``status`` error/failure, ``success: false``, or a non-empty ``error`` /
    ``error_type`` / ``error_code`` (a null, false, 0 or empty value is no error).
    """
    status = payload.get("status")
    if isinstance(status, str) and status.strip().lower() in ("error", "failure"):
        return True
    if payload.get("success") is False:
        return True
    for key in ("error", "error_type", "error_code"):
        value = payload.get(key)
        if value.strip() if isinstance(value, str) else value:
            return True
    return False


def _order_status_of(data: object) -> str:
    """The order status in a place/modify/cancel answer (``order_status``, else ``status``)."""
    if not isinstance(data, dict):
        return ""
    return normalise_status(data.get("order_status")) or normalise_status(data.get("status"))


def _dict_rows(rows: list) -> list[dict[str, Any]]:
    return [row for row in rows if isinstance(row, dict)]


class INDstocksClient:
    """Async HTTP client for INDstocks REST API.

    Usage::

        async with INDstocksClient(config, token_mgr) as client:
            quote = await client.get_quote("NSE_2885", symbol="RELIANCE")
    """

    def __init__(
        self,
        config: SkopaqConfig,
        token_manager: TokenManager,
        *,
        timeout: float = 30.0,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ) -> None:
        self._base_url = config.indstocks_base_url.rstrip("/")
        self._token_manager = token_manager
        self._timeout = timeout
        self._transport = transport   # tests pass httpx.MockTransport
        self._client: Optional[httpx.AsyncClient] = None
        # `remarks` on POST /order is listed as unreleased in the docs: off unless enabled
        self._remarks_enabled = (
            getattr(config, "indstocks_order_remarks_enabled", False) is True
        )
        # Which per-order trades path works: "order" (/order/trades) or "legacy"
        # (/trades/{id}); set only after a path returned at least one fill.
        self._trades_path: Optional[str] = None
        self._read_limiter = SlidingWindowLimiter(_NON_TRADING_PER_S, 1.0)

    async def __aenter__(self) -> INDstocksClient:
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            transport=self._transport,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Internal helpers ─────────────────────────────────────────────────

    def _headers(self) -> dict[str, str]:
        """Build auth headers.

        INDstocks uses ``Authorization: TOKEN`` — NO "Bearer " prefix.
        """
        try:
            token = self._token_manager.get_token()
        except TokenExpiredError as exc:
            raise BrokerError(str(exc), kind="not_sent") from exc
        return {
            "Authorization": token,
            "Content-Type": "application/json",
        }

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        is_order: bool = False,
    ) -> httpx.Response:
        """Send one request (rate limited) and return the 2xx response.

        Raises ``BrokerError`` with a ``kind`` saying whether the request could have
        reached the broker (see ``BrokerError``), or ``kind="http"`` for HTTP >= 400.
        """
        if self._client is None:
            raise BrokerError(
                "Client not initialised. Use `async with` context manager.", kind="not_sent",
            )

        if is_order:
            await _order_limiter.acquire()
        elif _is_non_trading(method, path):
            await self._read_limiter.acquire()
        else:
            await _api_limiter.acquire()

        try:
            resp = await self._client.request(
                method,
                path,
                headers=self._headers(),
                params=params,
                json=json_body,
            )
        except _NOT_SENT_ERRORS as exc:
            raise BrokerError(f"HTTP error (not sent): {exc!r}", kind="not_sent") from exc
        except httpx.HTTPError as exc:
            raise BrokerError(f"HTTP error: {exc!r}", kind="transport") from exc

        if resp.status_code >= 400:
            try:
                detail = _error_text(resp.json()) or resp.text
            except ValueError:
                detail = resp.text
            raise BrokerError(
                f"API error {resp.status_code}: {detail}",
                status_code=resp.status_code,
                body=resp.text,
                kind="http",
            )
        return resp

    @staticmethod
    def _json(resp: httpx.Response, path: str) -> Any:
        try:
            return resp.json()
        except ValueError as exc:
            raise BrokerError(
                f"{path}: response is not JSON: {resp.text[:200]!r}",
                status_code=resp.status_code,
                body=resp.text,
                kind="bad_payload",
            ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        is_order: bool = False,
    ) -> Any:
        """Send an API request and return its data, leniently (market data).

        Returns the parsed JSON response. If the response has a
        ``{"data": ...}`` wrapper, returns the inner ``data`` value. Order and
        portfolio calls use ``_request_envelope`` instead.
        """
        resp = await self._send(
            method, path, params=params, json_body=json_body, is_order=is_order,
        )
        data = self._json(resp, path)

        # INDstocks wraps responses in {"status": ..., "data": ...} (historical data in
        # {"success": ..., "data": ...})
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    async def _request_envelope(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        is_order: bool = False,
        require_data: bool = False,
    ) -> Any:
        """Send an order/portfolio request and return its ``data``, strictly.

        A body reporting a failure (``status`` error/failure, ``success: false``, or
        an ``error`` / ``error_type`` / ``error_code`` value) raises
        ``BrokerError(kind="error_body")`` even with HTTP 200 — otherwise it would
        read as an empty order book or no positions. ``data: null`` is returned only
        under a success marker. A list is returned as-is, and so is a dict without
        ``data`` unless ``require_data`` (then it is ``bad_payload``, like a bare JSON
        ``null`` or other scalar, which is never read as "nothing").
        """
        resp = await self._send(
            method, path, params=params, json_body=json_body, is_order=is_order,
        )
        payload = self._json(resp, path)
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            raise BrokerError(
                f"{path}: unexpected payload {type(payload).__name__}: {str(payload)[:200]}",
                status_code=resp.status_code,
                body=resp.text,
                kind="bad_payload",
            )

        def failure(reason: str) -> BrokerError:
            text = _error_text(payload)
            return BrokerError(
                f"{path}: {reason}" + (f": {text}" if text else ""),
                status_code=resp.status_code,
                body=resp.text,
                kind="error_body",
            )

        if _is_error_body(payload):
            reported = payload.get("status", payload.get("success"))
            raise failure(f"broker reported a failure (status={reported!r})")
        if "data" not in payload:
            if require_data:
                raise BrokerError(
                    f"{path}: the answer has no data: {str(payload)[:200]}",
                    status_code=resp.status_code,
                    body=resp.text,
                    kind="bad_payload",
                )
            return payload

        status = payload.get("status")
        success = (isinstance(status, str) and status.strip().lower() == "success") or (
            payload.get("success") is True
        )
        unmarked = "status" not in payload and "success" not in payload
        if payload["data"] is None and not success:
            raise failure("data is null without a success status")
        if not (success or unmarked):
            raise failure(f"unexpected status {status!r}")
        return payload["data"]

    async def _request_text(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
    ) -> str:
        """Send a request expecting text/CSV response (instruments endpoint)."""
        resp = await self._send(method, path, params=params)
        return resp.text

    # ── Market Data ──────────────────────────────────────────────────────

    async def get_quote(self, scrip_code: str, symbol: str = "") -> Quote:
        """Fetch full quote for a single scrip code.

        Endpoint: ``GET /market/quotes/full?scrip-codes=NSE_2885``

        Args:
            scrip_code: Instrument identifier like ``NSE_2885`` from instruments CSV.
            symbol: Optional human-readable name for the returned Quote object.

        Real response::

            {"NSE_2885": {"live_price": 1361.3, "day_change": -32.6,
             "day_change_percentage": -2.34, "day_open": 1375.5,
             "day_high": 1378.6, "day_low": 1358.6, "prev_close": 1393.9,
             "52week_high": 1611.8, "52week_low": 1114.85, ...}}
        """
        data = await self._request(
            "GET", "/market/quotes/full",
            params={"scrip-codes": scrip_code},
        )

        # Response is dict keyed by scrip_code (e.g. "NSE_2885")
        if isinstance(data, dict):
            quote_data = data.get(scrip_code, data)
            if isinstance(quote_data, dict):
                # Parse exchange from scrip_code (e.g. "NSE_2885" → "NSE")
                exchange = scrip_code.split("_")[0] if "_" in scrip_code else "NSE"
                return Quote(
                    symbol=symbol or scrip_code,
                    exchange=exchange,
                    ltp=float(quote_data.get("live_price", 0)),
                    open=float(quote_data.get("day_open", 0)),
                    high=float(quote_data.get("day_high", 0)),
                    low=float(quote_data.get("day_low", 0)),
                    close=float(quote_data.get("prev_close", 0)),
                    volume=int(quote_data.get("volume", 0)),
                    change=float(quote_data.get("day_change", 0)),
                    change_pct=float(quote_data.get("day_change_percentage", 0)),
                    bid=float(quote_data.get("best_bid_price", 0)),
                    ask=float(quote_data.get("best_ask_price", 0)),
                )

        return Quote(symbol=symbol or scrip_code)

    async def get_quotes(
        self, scrip_codes: list[str], symbols: list[str] | None = None,
    ) -> list[Quote]:
        """Fetch full quotes for multiple scrip codes.

        Endpoint: ``GET /market/quotes/full?scrip-codes=NSE_2885,NSE_11536``

        Args:
            scrip_codes: List of scrip codes like ``["NSE_2885", "NSE_11536"]``.
            symbols: Optional human-readable names (same order as scrip_codes).
        """
        joined = ",".join(scrip_codes)
        data = await self._request(
            "GET", "/market/quotes/full",
            params={"scrip-codes": joined},
        )

        quotes: list[Quote] = []
        if isinstance(data, dict):
            for i, sc in enumerate(scrip_codes):
                qd = data.get(sc, {})
                if isinstance(qd, dict):
                    exchange = sc.split("_")[0] if "_" in sc else "NSE"
                    sym = symbols[i] if symbols and i < len(symbols) else sc
                    quotes.append(Quote(
                        symbol=sym,
                        exchange=exchange,
                        ltp=float(qd.get("live_price", 0)),
                        open=float(qd.get("day_open", 0)),
                        high=float(qd.get("day_high", 0)),
                        low=float(qd.get("day_low", 0)),
                        close=float(qd.get("prev_close", 0)),
                        volume=int(qd.get("volume", 0)),
                        change=float(qd.get("day_change", 0)),
                        change_pct=float(qd.get("day_change_percentage", 0)),
                    ))
        return quotes

    async def get_ltp(self, scrip_code: str) -> float:
        """Fetch just the last traded price.

        Endpoint: ``GET /market/quotes/ltp?scrip-codes=NSE_2885``

        Real response: ``{"NSE_2885": {"live_price": 1362}}``
        """
        data = await self._request(
            "GET", "/market/quotes/ltp",
            params={"scrip-codes": scrip_code},
        )

        if isinstance(data, dict):
            ltp_data = data.get(scrip_code, data)
            if isinstance(ltp_data, dict):
                return float(ltp_data.get("live_price", 0))
            if isinstance(ltp_data, (int, float)):
                return float(ltp_data)
        return 0.0

    async def get_historical(
        self,
        scrip_code: str,
        interval: str = "1day",
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> list[HistoricalCandle]:
        """Fetch OHLCV candles.

        Endpoint: ``GET /market/historical/{interval}``

        Args:
            scrip_code: Security identifier like ``NSE_2885`` (from instruments CSV).
            interval: Candle interval — ``1day``, ``1week``, ``1month``,
                ``1minute``, ``5minute``, ``15minute``, ``30minute``,
                ``60minute``, ``1second``, etc.
            start_time: Unix epoch **milliseconds** in IST.
            end_time: Unix epoch **milliseconds** in IST.

        Real response (after ``_request`` unwraps ``data``)::

            {"NSE_2885": {"candles": [
                {"ts": 1740960000, "o": 1204, "h": 1206.45,
                 "l": 1156, "c": 1171.25, "v": 17944938},
                ...
            ]}}

        Input timestamps are epoch **milliseconds**.  Response candle ``ts``
        values are epoch **seconds**.
        """
        params: dict[str, str] = {"scrip-codes": scrip_code}
        if start_time is not None:
            params["start_time"] = str(start_time)
        if end_time is not None:
            params["end_time"] = str(end_time)

        data = await self._request(
            "GET", f"/market/historical/{interval}",
            params=params,
        )

        # Data is nested under scrip_code key: {"NSE_2885": {"candles": [...]}}
        candles_raw: list[Any] = []
        if isinstance(data, dict):
            scrip_data = data.get(scrip_code, data)
            if isinstance(scrip_data, dict):
                candles_raw = scrip_data.get("candles") or []
            elif isinstance(scrip_data, list):
                candles_raw = scrip_data
        elif isinstance(data, list):
            candles_raw = data

        candles: list[HistoricalCandle] = []
        for row in candles_raw:
            if isinstance(row, dict):
                # Object candles: {"ts": epoch_sec, "o":, "h":, "l":, "c":, "v":}
                ts = row.get("ts", 0)
                dt = datetime.fromtimestamp(int(ts))
                candles.append(HistoricalCandle(
                    timestamp=dt,
                    open=float(row.get("o", 0)),
                    high=float(row.get("h", 0)),
                    low=float(row.get("l", 0)),
                    close=float(row.get("c", 0)),
                    volume=int(row.get("v", 0)),
                ))
            elif isinstance(row, list) and len(row) >= 6:
                # Fallback for array candles [ts, o, h, l, c, v]
                ts = row[0]
                if isinstance(ts, (int, float)) and ts > 1e12:
                    dt = datetime.fromtimestamp(ts / 1000)
                else:
                    dt = datetime.fromtimestamp(int(ts))
                candles.append(HistoricalCandle(
                    timestamp=dt,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=int(row[5]),
                ))
        return candles

    async def get_instruments(self, source: str = "equity") -> str:
        """Fetch instruments master as CSV text.

        Endpoint: ``GET /market/instruments?source=equity``

        Returns raw CSV with columns:
        SECURITY_ID, TRADING_SYMBOL, CUSTOM_SYMBOL, EXCH, SEGMENT,
        INSTRUMENT_NAME, LOT_UNITS, EXPIRY_DATE, STRIKE_PRICE,
        OPTION_TYPE, TICK_SIZE, SYMBOL_NAME
        """
        return await self._request_text(
            "GET", "/market/instruments",
            params={"source": source},
        )

    # ── Orders ───────────────────────────────────────────────────────────

    async def place_order(self, order: OrderRequest) -> OrderResponse:
        """Place a new order.

        Endpoint: ``POST /order``

        Translates Pythonic field names to INDstocks API names:
            side       → txn_type
            quantity   → qty
            price      → limit_price
        """
        payload: dict[str, Any] = {
            "txn_type": order.side.value,           # side → txn_type
            "exchange": order.exchange.value,
            "segment": order.segment.value,
            "product": order.product.value,
            "order_type": order.order_type.value,
            "validity": order.validity.value,
            "security_id": order.security_id,
            "qty": int(order.quantity),               # quantity → qty (int for JSON)
            "is_amo": order.is_amo,
            "algo_id": order.algo_id,
        }
        if order.price is not None:
            payload["limit_price"] = order.price     # price → limit_price
        if order.trigger_price is not None:
            payload["trigger_price"] = order.trigger_price
        if self._remarks_enabled:
            # Echoed in the order book, so an uncertain placement can be found by it
            payload["remarks"] = f"skopaq-{order.internal_id.hex[:24]}"

        try:
            data = await self._request_envelope(
                "POST", "/order", json_body=payload, is_order=True,
            )
        except BrokerError as exc:
            if exc.kind == "not_sent" or (exc.kind == "http" and 400 <= exc.status_code < 500):
                raise                               # definitely not placed / rejected
            # 5xx, a dropped connection, a non-JSON or failure body: it may exist
            logger.error(
                "Order placement uncertain: %s %s qty=%d security_id=%s — %s",
                order.side, order.symbol, order.quantity, order.security_id, exc,
            )
            raise OrderPlacementUncertain(
                str(exc), status_code=exc.status_code, body=exc.body, kind=exc.kind,
            ) from exc

        order_id = data.get("order_id") if isinstance(data, dict) else None
        if isinstance(order_id, int) and not isinstance(order_id, bool):
            order_id = str(order_id)
        if not isinstance(order_id, str) or not order_id.strip():
            logger.error(
                "Order placement uncertain: %s %s qty=%d — no order id in %r",
                order.side, order.symbol, order.quantity, data,
            )
            raise OrderPlacementUncertain(
                f"POST /order: no order id in the answer ({_error_text(data) or data!r})",
                status_code=200,
                body=repr(data),
                kind="bad_payload",
            )

        status = _order_status_of(data)
        logger.info(
            "Order accepted: %s %s qty=%d security_id=%s → %s %s",
            order.side, order.symbol, order.quantity, order.security_id, order_id, status,
        )
        return OrderResponse(
            order_id=order_id.strip(),
            status=status,
            message=str(data.get("message") or ""),
        )

    async def modify_order(self, req: ModifyOrderRequest) -> OrderResponse:
        """Modify a pending order.

        Endpoint: ``POST /order/modify``. ``qty`` and ``limit_price`` are both
        mandatory, so both must be given. At most 25 modifications per order.
        """
        if req.quantity is None or req.price is None:
            raise ValueError("modify_order needs both quantity and price (INDstocks requires both)")
        payload: dict[str, Any] = {
            "order_id": req.order_id,
            "segment": req.segment.value,
            "qty": req.quantity,                    # quantity → qty
            "limit_price": req.price,               # price → limit_price
        }

        data = await self._request_envelope(
            "POST", "/order/modify", json_body=payload, is_order=True,
        )
        message = data.get("message") if isinstance(data, dict) else None
        return OrderResponse(
            order_id=req.order_id,
            status=_order_status_of(data),
            message=str(message or ""),
        )

    async def cancel_order(self, req: CancelOrderRequest) -> OrderResponse:
        """Cancel a pending order.

        Endpoint: ``POST /order/cancel``
        """
        payload = {
            "order_id": req.order_id,
            "segment": req.segment.value,
        }
        data = await self._request_envelope(
            "POST", "/order/cancel", json_body=payload, is_order=True,
        )
        # A cancel races the order filling: re-read the order for its final state
        message = data.get("message") if isinstance(data, dict) else None
        return OrderResponse(
            order_id=req.order_id,
            status=_order_status_of(data),
            message=str(message or ""),
        )

    @staticmethod
    def _rows(data: Any, path: str, *wrapper_keys: str) -> list[dict[str, Any]]:
        """The rows of a list answer: a list, ``None`` (empty under a success status),
        or a dict holding the list under one of ``wrapper_keys``. Anything else raises
        ``BrokerError(kind="bad_payload")`` — never read as "no rows"."""
        if data is None:
            return []
        if isinstance(data, list):
            return _dict_rows(data)
        if isinstance(data, dict):
            for key in wrapper_keys:
                if isinstance(data.get(key), list):
                    return _dict_rows(data[key])
        raise BrokerError(
            f"{path}: unexpected payload {type(data).__name__}: {str(data)[:200]}",
            kind="bad_payload",
        )

    async def get_order_book(self) -> list[dict[str, Any]]:
        """Fetch all orders for the day (raw rows; parse with ``order_status``).

        Endpoint: ``GET /order-book``. An empty book is ``data: null`` under
        ``status: success``; a failure body or an unexpected payload raises.
        """
        data = await self._request_envelope("GET", "/order-book")
        return self._rows(data, "/order-book", "orders")

    async def get_order(self, order_id: str, segment: str = "EQUITY") -> dict[str, Any]:
        """Fetch a single order by ID (a raw row; ``{}`` if the broker has none).

        Endpoint: ``GET /order``. The docs send ``order_id`` and ``segment`` as a JSON
        body on the GET; query params are sent too, in case only one form works.
        """
        body = {"order_id": order_id, "segment": segment}
        # The row must come from `data`: an envelope's own `status: success` is not an
        # order status (it would read as a full fill)
        data = await self._request_envelope("GET", "/order", params=body, json_body=body,
                                            require_data=True)
        if isinstance(data, dict):
            return data
        if isinstance(data, list):
            for row in _dict_rows(data):
                if order_id in (str(row.get("id", "")), str(row.get("order_id", ""))):
                    return row
        return {}

    async def get_trades(self, order_id: str, segment: str = "EQUITY") -> list[dict[str, Any]]:
        """Fetch the fills of one order.

        The docs disagree on the path: ``GET /order/trades`` with a JSON body
        ``{order_id, segment}`` (Orders page, OpenAPI) or ``GET /trades/{order_id}``
        (API overview). A 404/405 from one falls back to the other for this call; a
        path becomes the preference only once it returned a fill, because a 404 can
        also mean "no trades yet".
        """
        body = {"order_id": order_id, "segment": segment}
        first, second = ("order", "legacy")
        if self._trades_path == "legacy":
            first, second = second, first
        for which in (first, second):
            try:
                if which == "order":
                    data = await self._request_envelope(
                        "GET", "/order/trades", params=body, json_body=body,
                    )
                else:
                    data = await self._request_envelope("GET", f"/trades/{order_id}")
            except BrokerError as exc:
                if which == first and exc.kind == "http" and exc.status_code in (404, 405):
                    continue                        # try the other path for this call
                raise
            rows = self._rows(data, "trades", "trades")
            if rows:
                self._trades_path = which
            return rows
        return []                                   # not reached: the second path returns or raises

    async def get_trade_book(self, segment: str = "EQUITY") -> list[dict[str, Any]]:
        """Fetch all of today's fills (one row per fill; join on ``exch_order_id``).

        Endpoint: ``GET /trade-book?segment=...``
        """
        data = await self._request_envelope(
            "GET", "/trade-book",
            params={"segment": segment},
        )
        return self._rows(data, "/trade-book", "trades")

    # ── Portfolio ─────────────────────────────────────────────────────────

    async def get_positions(self) -> list[Position]:
        """Fetch today's equity positions (CNC and INTRADAY).

        Endpoint: ``GET /portfolio/positions?segment=equity&product=cnc|intraday``
        (both params required, lowercase). Each row's ``product`` is set to the
        product queried. If the broker refuses both queries (400/404/422), the call
        is retried without params and rows keep their own product. A failed read
        raises — it never becomes "no positions".
        """
        positions: list[Position] = []
        refused: dict[str, BrokerError] = {}
        for product in ("cnc", "intraday"):
            try:
                data = await self._request_envelope(
                    "GET", "/portfolio/positions",
                    params={"segment": "equity", "product": product},
                )
            except BrokerError as exc:
                if exc.kind == "http" and exc.status_code in (400, 404, 422):
                    refused[product] = exc
                    continue
                raise
            for row in self._rows(data, "/portfolio/positions", "net_positions"):
                position = Position(**row)
                position.product = product.upper()
                positions.append(position)

        if len(refused) == 2:
            logger.warning(
                "Positions query with segment/product refused (%s); retrying without",
                refused["cnc"],
            )
            data = await self._request_envelope("GET", "/portfolio/positions")
            return [
                Position(**row)
                for row in self._rows(data, "/portfolio/positions", "net_positions")
            ]
        if "cnc" in refused:
            # CNC rows are the ones Skopaq trades: without them the read has failed
            raise refused["cnc"]
        if "intraday" in refused:
            # Intraday rows never count for a CNC SELL, so CNC rows alone are safe
            logger.warning(
                "Intraday positions query refused (%s); using CNC rows only", refused["intraday"],
            )
        return positions

    async def get_holdings(self) -> list[Holding]:
        """Fetch delivery holdings (``total_qty`` / ``avg_price`` parse via aliases).

        Endpoint: ``GET /portfolio/holdings``. A failed read raises.
        """
        data = await self._request_envelope("GET", "/portfolio/holdings")
        return [Holding(**h) for h in self._rows(data, "/portfolio/holdings", "holdings")]

    async def get_funds(self) -> Funds:
        """Fetch available funds and margin.

        Endpoint: ``GET /funds``
        """
        data = await self._request("GET", "/funds")
        if isinstance(data, dict):
            # INDstocks returns nested structure — map to our flat model.
            # Key fields: detailed_avl_balance.eq_cnc (equity CNC buying power),
            # funds_added, sod_balance, pledge_received.
            avl = data.get("detailed_avl_balance", {})
            eq_cnc = float(avl.get("eq_cnc", 0))
            pledge = float(data.get("pledge_received", 0))

            return Funds(
                available_cash=eq_cnc,
                available_margin=eq_cnc,
                used_margin=0.0,
                total_collateral=eq_cnc + pledge,
            )
        return Funds()

    # ── User ─────────────────────────────────────────────────────────────

    async def get_profile(self) -> UserProfile:
        """Fetch authenticated user's profile.

        Endpoint: ``GET /user/profile``
        """
        data = await self._request("GET", "/user/profile")
        if isinstance(data, dict):
            return UserProfile(**data)
        return UserProfile()

    # ── Options ──────────────────────────────────────────────────────────

    async def get_option_chain(self, symbol: str) -> OptionChain:
        """Fetch option chain for a symbol.

        Endpoint: ``GET /option-chain``
        """
        data = await self._request(
            "GET", "/option-chain",
            params={"symbol": symbol},
        )
        if isinstance(data, dict):
            calls = [OptionData(**c) for c in data.get("calls", [])]
            puts = [OptionData(**p) for p in data.get("puts", [])]
            return OptionChain(
                symbol=symbol,
                calls=calls,
                puts=puts,
                spot_price=float(data.get("spot_price", 0)),
                pcr=float(data.get("pcr", 0)),
            )
        return OptionChain(symbol=symbol)
