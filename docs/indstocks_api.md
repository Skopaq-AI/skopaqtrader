# INDstocks API (Skopaq reference)

Skopaq's notes on the INDstocks REST endpoints it calls: the request and response shapes
it relies on, how it reads order statuses and fills, where the official documentation
contradicts itself, and what still has to be checked against the live API.

The canonical documentation is **<https://api-docs.indstocks.com/>** (a single-file export
is at `https://api-docs.indstocks.com/llms-full.md`, the OpenAPI spec at
`https://api-docs.indstocks.com/openapi-spec.yaml`). If this page disagrees with it, the
official docs win: fix the code and this page. This page was written from the
September 2026 export ("Docs last updated: unknown").

The client is `skopaq/broker/client.py` (`INDstocksClient`); status and row parsing live in
`skopaq/broker/order_status.py`; live order handling in `skopaq/execution/live_orders.py`.

## Basics

- **Base URL:** `https://api.indstocks.com` (`SKOPAQ_INDSTOCKS_BASE_URL`).
- **Auth header:** `Authorization: <token>`, with **no** `Bearer` prefix. A token lasts
  24 hours (`skopaq token set <TOKEN>` every trading day).
- **Instruments** are addressed by scrip code, `{EXCH}_{SECURITY_ID}` (for example
  `NSE_2885` for RELIANCE). Market data takes `scrip-codes=NSE_2885`, never
  `symbols=NSE:RELIANCE`. Orders take the bare `security_id` (`2885`).
- **Static IP:** placing, modifying and cancelling orders needs a whitelisted IPv4 (NSE
  circular NSE/INVG/67858). Read-only calls (quotes, historical data, order book, profile,
  funds) do not, so confirming a fill works from anywhere; placing does not.
- **Timestamps:** historical data takes epoch **milliseconds** and returns candle `ts` in
  epoch **seconds**. Order rows carry ISO-8601 times with a `+05:30` offset.

### Rate limits

| Category | Limit | Covers |
|----------|-------|--------|
| Order APIs | 10/s; at most 25 modifications per order | place, modify, cancel |
| Non-Trading APIs | 15/s, 100,000/day | profile, funds, order history (order book, `GET /order`, trades) |
| Quote APIs | 5/s, 100,000/day | quotes, LTP |
| Data APIs | 5/s, 100,000/day | instruments, historical data, option chain |
| Token generation | 1/min | |

Going over returns HTTP 429. Each client keeps its Non-Trading reads (order book,
`GET /order`, trades, trade book, positions, holdings, funds, profile) to 12 in any rolling
second, so a burst of live SELLs (each reads the book, positions, holdings and funds
before it is placed) is not refused. Its limiter for other calls still allows 100 a
second (the older "10 orders/s, 100 calls/s" figure), which is above the current quote
and data limits. The live order worker stays well inside them: by default it polls each
order once a second, and all the orders it is watching share one order-book read (0.5 s
cache).

## Response envelope

- Success: `{"status": "success", "data": ...}`. Historical data uses
  `{"success": true, "data": ...}`.
- Errors normally come with HTTP 4xx/5xx and `{"status": "error", "message": ...,
  "error_type": ...}` (the conventions page shows `error_code` instead). Other shapes
  occur: `{"message": ..., "success": false}`, `{"message": "Bad Request", "debug_info": ...}`
  and `{"error": "Rate limit exceeded", "success": false}`.
- A **2xx can carry a failure** body, for example `{"status": "failure", "error": {"msg": ...}}`.
  On `POST /order` that has been seen for an order that *was* placed.

Skopaq reads market data leniently (`_request`: unwrap `data` if present) and every order
and portfolio call strictly (`_request_envelope`):

- a 2xx body with `status` error/failure, `success: false`, or a non-empty `error`,
  `error_type` or `error_code` raises `BrokerError(kind="error_body")`, so a failure is
  never read as "no orders" or "no positions";
- `data: null` is accepted (as empty) only under `status: success` or `success: true`;
- a 2xx body that is not JSON, or a list endpoint answering something that is not a
  list, raises `BrokerError(kind="bad_payload")`.

`BrokerError.kind` says how far a request got:

| Kind | Meaning | An order sent this way… |
|------|---------|-------------------------|
| `not_sent` | never left the host (client not open, expired token, connect error or timeout) | was not placed |
| `http` | the broker answered HTTP ≥ 400 | 4xx: was rejected; 5xx: may exist |
| `transport` | read/write error or timeout after sending, connection dropped | may exist |
| `bad_payload` | 2xx that is not JSON or not the expected shape | may exist |
| `error_body` | 2xx whose body reports a failure | may exist |

`place_order` raises `OrderPlacementUncertain` whenever the order may exist (and when a 2xx
answer has no order id). The worker then looks for it in the order book by order ids that
were not there before the placement, and never sends it again blind.

## Endpoints Skopaq calls

### Market data

| Endpoint | Client method | Request | Response (`data`) |
|----------|---------------|---------|-------------------|
| `GET /market/quotes/full` | `get_quote`, `get_quotes` | `scrip-codes=NSE_2885,NSE_11536` | dict keyed by scrip code: `live_price`, `day_open`, `day_high`, `day_low`, `prev_close`, `day_change`, `day_change_percentage`, `volume`, `best_bid_price`, `best_ask_price` |
| `GET /market/quotes/ltp` | `get_ltp` | `scrip-codes=NSE_2885` | `{"NSE_2885": {"live_price": 1362}}` |
| `GET /market/historical/{interval}` | `get_historical` | `scrip-codes`, `start_time` and `end_time` in epoch ms; `interval` such as `1minute`, `5minute`, `1day` | `{"NSE_2885": {"candles": [{"ts", "o", "h", "l", "c", "v"}]}}`, `ts` in epoch seconds |
| `GET /market/instruments` | `get_instruments` | `source=equity` | CSV (not JSON): `SECURITY_ID`, `TRADING_SYMBOL`, `CUSTOM_SYMBOL`, `EXCH`, `SEGMENT`, `INSTRUMENT_NAME`, `LOT_UNITS`, `EXPIRY_DATE`, `STRIKE_PRICE`, `OPTION_TYPE`, `TICK_SIZE`, `SYMBOL_NAME` |
| `GET /option-chain` | `get_option_chain` | `symbol` | `calls`, `puts` |

`skopaq/broker/scrip_resolver.py` caches the instruments CSV for an hour:
`resolve_scrip_code` gives the scrip code, `resolve_tick_size` the `TICK_SIZE`.

### Orders

| Endpoint | Client method | Request | Response (`data`) |
|----------|---------------|---------|-------------------|
| `POST /order` | `place_order` | JSON: `txn_type` (BUY/SELL), `exchange`, `segment` (EQUITY), `product` (CNC/INTRADAY/MARGIN), `order_type` (LIMIT/MARKET), `validity` (DAY), `security_id`, `qty` (int), `limit_price` (not for MARKET), `is_amo`, `algo_id`; `remarks` only with `SKOPAQ_INDSTOCKS_ORDER_REMARKS_ENABLED=true` | `{"order_id": "EQ-93586788", "order_status": "INITIATED"}` |
| `POST /order/modify` | `modify_order` | JSON: `order_id`, `segment`, `qty`, `limit_price` (all four mandatory) | `{"order_id", "order_status": "MODIFIED"}` |
| `POST /order/cancel` | `cancel_order` | JSON: `order_id`, `segment` | `{"order_id", "order_status": "CANCELLED"}` |
| `GET /order` | `get_order` | `order_id`, `segment` as a **JSON body on the GET**; Skopaq sends the same as query params too | one order row |
| `GET /order-book` | `get_order_book` | — | list of order rows; `null` (under `status: success`) when there are none |
| `GET /order/trades` | `get_trades` | `order_id`, `segment` as a JSON body on the GET (and query params) | fills: `fill_id`, `exch_order_id`, `quantity`, `price`, `trade_date` |
| `GET /trades/{order_id}` | `get_trades` (fallback) | — | fills (older docs; different field names, `quantity` and `price` in both) |
| `GET /trade-book` | `get_trade_book` | `segment=EQUITY` (required) | today's fills, one row each: `fill_id`, `exch_order_id`, `quantity`, `price`, `trade_date`, `trade_serial_no`, `scrip_code`, `remarks` (if sent); no order id and no side: join on `exch_order_id` |

Notes:

- **`POST /order` only acknowledges the order.** The status key is `order_status`, not
  `status`. Validation and RMS rejections come back at once as HTTP 400 (below); whether
  the order fills is decided at the exchange afterwards. `skopaq/execution/live_orders.py`
  confirms every live fill by reading the order back.
- Order ids: `EQ-…` (equity), `DRV-…` (derivatives), `GTT-…` (smart orders). The id
  `POST /order` returns is the order row's `id` and what `GET /order`, cancel and trades
  take.
- A cancel races the order filling: always re-read the order afterwards; the final status
  can be `SUCCESS` or `PARTIALLY FILLED - CANCELLED`. `Position could not be found.`
  means the order does not exist or has already completed; `The order is already
  pending…` means try again shortly. Stop and GTT orders are cancelled on
  `/smart/order/cancel`, which Skopaq does not use.
- Skopaq never modifies an order: a resting exit is cancelled and the rest re-placed.
- Skopaq also sends `trigger_price` on `POST /order` when the order has one. It is not a
  documented field there (stops exist only on `/smart/order`); whether it is ignored or
  refused is unverified.

### Portfolio and account

| Endpoint | Client method | Request | Response (`data`) |
|----------|---------------|---------|-------------------|
| `GET /portfolio/positions` | `get_positions` | `segment=equity` and `product=cnc` / `product=intraday`, **both required, lowercase** | flat list: `position_id`, `security_id`, `symbol`, `segment`, `product`, `exchange`, `isin`, `net_qty`, `avg_price`, `buy_qty`, `buy_avg`, `sell_qty`, `sell_avg`, `realized_profit`, `day_buy_qty`, `day_buy_val`, `day_sell_qty`, `day_sell_val`, `cf_*` (the `day_*` and `cf_*` values can be null) |
| `GET /portfolio/holdings` | `get_holdings` | — | `security_id`, `symbol`, `isin`, `total_qty` (T1 + DP), `used_qty`, `avg_price`, `t1_qty`, `t1_avg_price`, `dp_qty`, `dp_avg_price`; no product, LTP or P&L |
| `GET /funds` | `get_funds` | — | `detailed_avl_balance.eq_cnc` (CNC buying power), `pledge_received`, … |
| `GET /user/profile` | `get_profile` | — | profile |

- Positions: seeing CNC and intraday rows takes two calls. Each row's `product` echoes
  the query, so Skopaq sets it from the query rather than trusting the row. If both queries
  are refused (400/404/422) it retries without parameters; a failed read always raises,
  it never becomes "no positions". A `{"net_positions": [...]}` wrapper is accepted.
  Null numbers read as 0.
- Holdings: `Holding.quantity` is `total_qty` and `Holding.average_price` is `avg_price`.
  `used_qty` ("pledged, sold, or otherwise blocked") is kept as `used_quantity` but not
  subtracted, because a sale today is taken to show already as a negative CNC `net_qty`
  in positions (still to be verified, below).
- **Read the order book before positions and holdings** (`read_broker_snapshot` in
  `skopaq/broker/book_snapshot.py`). An order that fills between the reads is then counted
  twice (still open in the book, already sold in positions), which understates what can
  be sold; the other order would count it zero times and allow a double sell.

## Order rows (`GET /order`, `GET /order-book`)

| Field | Notes |
|-------|-------|
| `id` | the order id (not `order_id`) |
| `exch_order_id` | exchange id; `""` until the order reaches the exchange |
| `txn_type` | `BUY` / `SELL` |
| `security_id` | the instrument **on that exchange**: the same stock has another security id on BSE, so compare it only within one exchange |
| `isin` | the instrument across exchanges (`""` for derivatives); positions and holdings carry it too, and Skopaq matches the same shares on NSE and BSE by it |
| `name` | display name (for example `NIFTY 3 JUL 25700 CE`): **not** a trading symbol, and rows have no trading symbol |
| `exchange`, `segment`, `product`, `validity`, `mkt_type`, `off_mkt_flag` | as sent |
| `order_type` | a MARKET order keeps `MARKET` after INDstocks turns it into a LIMIT |
| `requested_qty`, `traded_qty` | integers; `traded_qty` is the quantity filled so far |
| `requested_price` | string; for a MARKET order the limit it was converted to |
| `traded_price` | string, `""` until something fills; read as the average fill price (the docs do not say) |
| `sl_trigger_price`, `sl_limit_price`, `tgt_trigger_price`, `tgt_limit_price` | strings, `""` on regular orders |
| `status` | see the table below |
| `extra_info` | rejection reason or exchange message; empty for pending or successful orders |
| `remarks` | only if sent at placement |
| `created_at`, `updated_at` | ISO-8601 with `+05:30` |

There is no remaining-quantity or average-price field: remaining is
`requested_qty − traded_qty`. Parse rows with `parse_order_row` / `parse_order_book` from
`skopaq.broker.order_status` (they accept a few alternate key names, never use `name` as
the symbol, and drop only rows without an id); never compare raw status strings.

## Order statuses

The 15 documented statuses, and how `skopaq.broker.order_status.classify` reads each when
the quantities say nothing more:

| Status | Skopaq state | Documented meaning |
|--------|--------------|--------------------|
| `QUEUED` | `working` | queued for processing |
| `O-PENDING` | `working` | after-market order pending (also returned for ordinary placements) |
| `SL-PENDING` | `working` | stop-loss pending trigger |
| `PROCESSING` | `working` | being processed |
| `INITIATED` | `working` | initiated and sent to the exchange |
| `MODIFIED` | `working` | successfully modified |
| `PENDING` | `working` | pending execution at the exchange |
| `PARTIALLY FILLED` | `working` | partly executed, rest still working |
| `SUCCESS` | `filled` | fully executed |
| `PARTIALLY FILLED - CANCELLED` | `partial` | partly executed, rest cancelled (with `traded_qty` 0 or missing: fill unknown, never "nothing") |
| `PARTIALLY FILLED - EXPIRED` | `partial` | partly executed, rest expired (the same) |
| `CANCELLED` | `cancelled` | cancelled by the user or the system |
| `EXPIRED` | `cancelled` | expired without execution |
| `FAILED` | `rejected` | failed (technical or other reasons) |
| `ABORTED` | `rejected` | aborted (system or validation issues) |

- There is no REST status called REJECTED, COMPLETE, OPEN or TRIGGER PENDING: rejections
  show as `FAILED` or `ABORTED`. Those names, `PARTIALLY_EXECUTED`, `PF-CANCELLED`/`PFC`,
  `PF-EXPIRED` and `RJ` are still accepted as aliases, and case, underscores and dash
  spacing are normalised.
- Quantities overrule the status: `traded_qty ≥ requested_qty > 0` is filled whatever the
  status says, and a no-fill final status with `traded_qty > 0` is partial.
- A status in none of these lists is `unrecognised`: treated as still working until the
  timeout (then cancelled and re-read) and logged once per order.
  `SKOPAQ_ORDER_EXTRA_TERMINAL_STATUSES` declares extra statuses final without a deploy.
- The filled quantity is **unknown**, not 0, when a partial or working status comes without
  `traded_qty`. A protective exit then stops with a CRITICAL alert instead of re-selling.

The order-updates WebSocket (`wss://ws-order-updates.indstocks.com/...`) uses short codes
(`R`, `P`, `S`, `F`, `C`, `RJ`, `PF`, `PFC`) and a disputed payload shape. Skopaq does not
use it; it polls REST.

## Fills

- Filled quantity: the larger of `traded_qty` and the sum of the order's trade
  quantities, capped at `requested_qty`.
- Average price, in this order: the volume-weighted average of the order's trades
  (`Σ quantity × price / Σ quantity`) when they add up to the filled quantity;
  `traded_price`; the trade book's fills joined on `exch_order_id`; the average of
  whatever trades were found; else Skopaq's reference price
  (`fill_price_source="estimate"`, with a WARNING).
- Skopaq books ₹20 brokerage per order that filled (INDstocks' flat fee).

## MARKET orders and tick size

"API trading does not support pure MARKET orders": INDstocks converts a MARKET order to a
LIMIT at the live price before it reaches the exchange, so it can rest unfilled in a
falling market. RMS can also refuse it outright ("Market orders are blocked for this
instrument."). Skopaq therefore cancels a resting protective exit and re-places the rest
as a LIMIT a little below the LTP, rounded **down** to the tick:

- the tick is the instruments CSV `TICK_SIZE` when it is plausible (above 0 and at most
  0.2 % of the price; its unit is not documented);
- otherwise a coarse tick valid in every NSE price band: 0.05 below ₹1,000, 1.00 below
  ₹20,000, 5.00 above.

A LIMIT refused for its price (tick, circuit, price band) is retried as MARKET in the same
attempt; "Market orders are blocked" makes the next attempt a LIMIT.

## Synchronous rejections

`POST /order` answers HTTP 400 for validation failures (`DayValidityAllowed`,
`QtyWithinFreezeQty`, `PriceWithinRange`, …) and RMS rejections, with a free-text message:
`RMS: Margin exceeds …`, `Market orders are blocked for this instrument.`, tick size,
circuit. `classify_rejection` sorts them into `rate_limited` (429), `price`,
`market_blocked`, `auth` (401/403, token), `not_sent` and `other`. A protective exit backs
off on `rate_limited` and `not_sent` (1, 2, 4, 8 s, at most 15 s per exit) without using an
attempt, and stops with a CRITICAL alert on `auth` and `other`.

## Where the official docs disagree

| Topic | The docs | What Skopaq does |
|-------|----------|------------------|
| `GET /order` request | a JSON body on a GET; no example with query params | sends both; falls back to the order-book row; sticks to whichever source answered |
| Per-order fills | Orders page and OpenAPI: `GET /order/trades` with a JSON body; API overview and older docs: `GET /trades/{order_id}` | tries `/order/trades`, falls back on 404/405 for that call; a path becomes the preference only after it returned a fill |
| Positions | `segment` and `product` required and lowercase, flat array; older pages show a `net_positions` / `day_positions` wrapper and no parameters | two queries (cnc, intraday); the call without parameters if both are refused; the wrapper accepted |
| `validity` | the enum is `DAY`, `IOC`, but the validations say "Order should be placed with DAY validity" | always `DAY`; resting re-placements are cancelled by Skopaq instead |
| `O-PENDING` | described as an after-market order pending | also returned for ordinary placements: treated as working |
| `traded_price` | not defined | read as the average fill price; the trades' VWAP is preferred |
| `remarks` | listed under "[Unreleased]" in the changelog | sent only when `SKOPAQ_INDSTOCKS_ORDER_REMARKS_ENABLED=true` |
| Rate limits | the conventions table (above) vs "10 orders/s, 100 API calls/s" in the v1.0.0 changelog | follows the table for polling |
| Holdings | `total_qty` / `avg_price` now; an older page showed `quantity` / `average_price` | accepts both |

## To verify live

None of this has been checked against the live API yet
(`tests/integration/test_indstocks.py` covers the token and market data only). Check it by
hand with 1 share from the whitelisted host, or as new `-m integration` tests, and record
the answers here:

1. A 1-share LIMIT BUY far below the market: the status while it rests, then cancel it and
   read `CANCELLED`; the real status strings and row keys (`id`, `updated_at`, `product`).
2. Which `GET /order` form works (JSON body or query), which trades path works, and the
   trades fields.
3. `/portfolio/positions` with and without parameters; the equity CNC row after a same-day
   BUY and SELL (`sell_qty`, `day_sell_qty`, `net_qty`).
4. Holdings vs positions for T1 shares (no double counting); whether selling earlier
   holdings today shows a negative CNC `net_qty`; `total_qty` / `used_qty` before and after.
5. How soon positions reflect a filled SELL (this sets
   `SKOPAQ_ORDER_SELL_FILL_LAG_WINDOW_SECONDS`).
6. Whether `validity=IOC` on `POST /order` is accepted.
7. The unit of `TICK_SIZE` in the instruments CSV (rupees or paise) for a ₹200, ₹2,000 and
   ₹6,000 stock.
8. A MARKET SELL's row: does `order_type` stay `MARKET`, and is `requested_price` the
   converted price?
9. The answer to cancelling a completed order (`Position could not be found.`) and to a
   cancel while "already pending".
10. Whether `remarks` is accepted and echoed in the order book.
