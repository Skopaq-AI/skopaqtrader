"""Pydantic models for INDstocks API requests and responses.

Internal field names use Pythonic conventions (``side``, ``quantity``,
``symbol``).  Translation to INDstocks API field names (``txn_type``,
``qty``, ``security_id``) happens in ``client.py`` only.

API docs: https://api-docs.indstocks.com/
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Optional
from uuid import UUID, uuid4

from pydantic import AliasChoices, BaseModel, Field, field_validator


# ── Enums ───────────────────────────────────────────────────────────────────


class Exchange(StrEnum):
    NSE = "NSE"
    BSE = "BSE"
    BINANCE = "BINANCE"


class Segment(StrEnum):
    """INDstocks segment parameter (required for orders)."""
    EQUITY = "EQUITY"
    DERIVATIVE = "DERIVATIVE"


class Side(StrEnum):
    """BUY or SELL — maps to INDstocks ``txn_type`` in the API layer."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"
    SLM = "SL-M"


class Product(StrEnum):
    """INDstocks product types.

    API docs use INTRADAY/MARGIN/CNC.  We also keep MIS/NRML as aliases
    so internal code can use either naming convention.
    """
    CNC = "CNC"            # Cash and Carry (delivery)
    INTRADAY = "INTRADAY"  # Intraday
    MARGIN = "MARGIN"      # Margin
    # Aliases for internal code that uses Zerodha-style names
    MIS = "INTRADAY"
    NRML = "MARGIN"


class Validity(StrEnum):
    DAY = "DAY"
    IOC = "IOC"


class OrderStatus(StrEnum):
    """Not INDstocks' vocabulary; used only by the unused websocket feed.

    Parse broker statuses with ``skopaq.broker.order_status`` (``normalise_status``,
    ``parse_order_row``), never by comparing against these values.
    """

    PENDING = "PENDING"
    OPEN = "OPEN"
    COMPLETE = "COMPLETE"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    TRIGGER_PENDING = "TRIGGER PENDING"


# ── Request Models ──────────────────────────────────────────────────────────


class OrderRequest(BaseModel):
    """Parameters for placing an order.

    Internal field names are Pythonic (``symbol``, ``side``, ``quantity``).
    The ``client.py`` translates these to INDstocks API names:
        side       → txn_type
        quantity   → qty
        symbol     → (used for logging; ``security_id`` goes to API)
    """

    symbol: str                         # Human-readable (e.g. "RELIANCE")
    exchange: Exchange = Exchange.NSE
    segment: Segment = Segment.EQUITY
    side: Side
    quantity: Decimal = Field(gt=0)
    order_type: OrderType = OrderType.LIMIT
    price: Optional[float] = None       # Limit price
    trigger_price: Optional[float] = None
    product: Product = Product.CNC
    validity: Validity = Validity.DAY
    disclosed_quantity: int = 0
    is_amo: bool = False                # After Market Order

    # INDstocks-specific fields
    security_id: str = ""               # e.g. "3045" from instruments CSV
    algo_id: str = "99999"              # REQUIRED — "99999" for regular orders

    # Internal tracking (not sent to API)
    internal_id: UUID = Field(default_factory=uuid4)
    tag: str = ""                       # User-defined tag


class ModifyOrderRequest(BaseModel):
    """Parameters for modifying a pending order (``POST /order/modify``)."""

    order_id: str
    segment: Segment = Segment.EQUITY
    quantity: Optional[int] = None
    price: Optional[float] = None
    order_type: Optional[OrderType] = None


class CancelOrderRequest(BaseModel):
    """Parameters for cancelling an order (``POST /order/cancel``)."""

    order_id: str
    segment: Segment = Segment.EQUITY


# ── Response Models ─────────────────────────────────────────────────────────


def _blank_to_zero(value: object) -> object:
    """None, ``""`` and ``"null"`` become 0: INDstocks sends null for numbers that
    do not apply (e.g. ``day_buy_val`` on carried-forward rows)."""
    if value is None:
        return 0
    if isinstance(value, str) and value.strip().lower() in ("", "null"):
        return 0
    return value


def _id_to_str(value: object) -> object:
    """Accept a numeric ``security_id`` (``2885``) as the string ``"2885"``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return "" if value is None else value


class OrderResponse(BaseModel):
    """Response from INDstocks after placing/modifying/cancelling an order."""

    order_id: str = ""
    status: str = ""
    message: str = ""
    exchange_order_id: Optional[str] = None
    timestamp: Optional[datetime] = None


class Position(BaseModel):
    """Open position from ``GET /portfolio/positions``.

    INDstocks uses shortened field names (``net_qty``, ``avg_price``,
    ``buy_qty``, etc.).  Pydantic ``validation_alias`` lets the API
    response hydrate our canonical field names automatically, while
    ``extra = "allow"`` keeps any additional fields the API may add.
    """

    symbol: str = Field("", validation_alias=AliasChoices("symbol", "trading_symbol"))
    exchange: str = ""
    # The client overwrites this with the product it queried (row values are unreliable)
    product: str = ""
    quantity: Decimal = Field(
        Decimal("0"), validation_alias=AliasChoices("net_qty", "net_quantity"),
    )
    average_price: float = Field(0.0, validation_alias="avg_price")
    last_price: float = 0.0
    pnl: float = Field(0.0, validation_alias="realized_profit")
    day_pnl: float = 0.0
    buy_quantity: Decimal = Field(Decimal("0"), validation_alias="buy_qty")
    sell_quantity: Decimal = Field(Decimal("0"), validation_alias="sell_qty")
    buy_value: float = Field(0.0, validation_alias="day_buy_val")
    sell_value: float = Field(0.0, validation_alias="day_sell_val")
    day_sell_quantity: Decimal = Field(
        Decimal("0"), validation_alias=AliasChoices("day_sell_qty", "day_sell_quantity"),
    )
    security_id: str = ""
    # The same shares have a different security id on each exchange; the ISIN is shared
    isin: str = ""

    model_config = {"extra": "allow", "populate_by_name": True}

    @field_validator(
        "quantity", "average_price", "last_price", "pnl", "day_pnl", "buy_quantity",
        "sell_quantity", "buy_value", "sell_value", "day_sell_quantity", mode="before",
    )
    @classmethod
    def _blank_numbers(cls, value: object) -> object:
        return _blank_to_zero(value)

    @field_validator("security_id", "exchange", "isin", mode="before")
    @classmethod
    def _security_id_text(cls, value: object) -> object:
        return _id_to_str(value)


class Holding(BaseModel):
    """Delivery holding from ``GET /portfolio/holdings``.

    INDstocks rows carry ``symbol``, ``security_id``, ``total_qty`` (T1 + DP),
    ``used_qty`` ("pledged, sold, or otherwise blocked") and ``avg_price``; there is no
    product, LTP or P&L. ``used_quantity`` is kept but not subtracted: it may include
    shares sold today, which the negative net position already subtracts.
    """

    symbol: str = Field("", validation_alias=AliasChoices("symbol", "trading_symbol"))
    security_id: str = ""
    exchange: str = ""
    isin: str = ""
    quantity: Decimal = Field(Decimal("0"), validation_alias=AliasChoices("quantity", "total_qty"))
    average_price: float = Field(
        0.0, validation_alias=AliasChoices("average_price", "avg_price"),
    )
    used_quantity: Decimal = Field(
        Decimal("0"), validation_alias=AliasChoices("used_quantity", "used_qty"),
    )
    last_price: float = 0.0
    pnl: float = 0.0
    day_change: float = 0.0
    day_change_pct: float = 0.0

    model_config = {"extra": "allow", "populate_by_name": True}

    @field_validator(
        "quantity", "average_price", "used_quantity", "last_price", "pnl", "day_change",
        "day_change_pct", mode="before",
    )
    @classmethod
    def _blank_numbers(cls, value: object) -> object:
        return _blank_to_zero(value)

    @field_validator("security_id", "exchange", "isin", mode="before")
    @classmethod
    def _security_id_text(cls, value: object) -> object:
        return _id_to_str(value)


class Quote(BaseModel):
    """Market quote from ``GET /market/quotes/full``."""

    symbol: str = ""
    exchange: str = ""
    ltp: float = 0.0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0
    change: float = 0.0
    change_pct: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    timestamp: Optional[datetime] = None

    model_config = {"extra": "allow"}


class OptionData(BaseModel):
    """Single option contract in an option chain."""

    strike_price: float = 0.0
    expiry: str = ""
    option_type: str = ""
    ltp: float = 0.0
    open_interest: int = 0
    change_in_oi: int = 0
    volume: int = 0
    iv: float = 0.0
    bid: float = 0.0
    ask: float = 0.0

    model_config = {"extra": "allow"}


class OptionChain(BaseModel):
    """Option chain from ``GET /option-chain``."""

    symbol: str = ""
    expiry: str = ""
    calls: list[OptionData] = Field(default_factory=list)
    puts: list[OptionData] = Field(default_factory=list)
    spot_price: float = 0.0
    pcr: float = 0.0

    model_config = {"extra": "allow"}


class Greeks(BaseModel):
    """Option Greeks from ``POST /greeks``."""

    delta: float = 0.0
    gamma: float = 0.0
    theta: float = 0.0
    vega: float = 0.0
    iv: float = 0.0

    model_config = {"extra": "allow"}


class Funds(BaseModel):
    """Available funds from ``GET /funds``."""

    available_cash: float = 0.0
    used_margin: float = 0.0
    available_margin: float = 0.0
    total_collateral: float = 0.0

    model_config = {"extra": "allow"}


class UserProfile(BaseModel):
    """User profile from ``GET /user/profile``."""

    user_id: str = ""
    name: str = ""
    email: str = ""
    broker: str = "INDstocks"

    model_config = {"extra": "allow"}


class HistoricalCandle(BaseModel):
    """Single OHLCV candle."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


# ── Composite Models ────────────────────────────────────────────────────────


class PortfolioSnapshot(BaseModel):
    """Complete portfolio state at a point in time."""

    timestamp: datetime = Field(default_factory=datetime.now)
    total_value: Decimal = Decimal("0")
    cash: Decimal = Decimal("0")
    positions_value: Decimal = Decimal("0")
    day_pnl: Decimal = Decimal("0")
    positions: list[Position] = Field(default_factory=list)
    open_orders: int = 0


class TradingSignal(BaseModel):
    """Parsed trading signal from the agent graph."""

    symbol: str
    exchange: Exchange = Exchange.NSE
    action: str  # BUY, SELL, HOLD
    confidence: int = Field(ge=0, le=100, default=50)
    # The order's limit price; with order_type=MARKET, the reference price
    # (e.g. the LTP an exit was decided at), used as the fill estimate.
    entry_price: Optional[float] = None
    # None: LIMIT at entry_price when it is set, else MARKET. Protective exits
    # set MARKET so a stop-loss below the entry price still fills.
    order_type: Optional[OrderType] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    quantity: Optional[Decimal] = None
    reasoning: str = ""
    agent_state: dict = Field(default_factory=dict)
    # Live: a protective exit of the day's position (the monitor, CLOSING). The Executor
    # re-checks it under the SELL lock against what that position still holds after
    # Skopaq's own open, unconfirmed and not-yet-shown SELLs, so it never sells older
    # delivery holdings. An analysis SELL leaves it False (it may sell holdings). Paper
    # ignores it.
    position_only: bool = False


class ExecutionResult(BaseModel):
    """Result of order execution (paper or live)."""

    success: bool
    order: Optional[OrderResponse] = None
    signal: Optional[TradingSignal] = None
    mode: str = "paper"
    safety_passed: bool = True
    rejection_reason: str = ""
    fill_price: Optional[float] = None
    slippage: float = 0.0
    brokerage: float = 5.0  # INR flat per order
    timestamp: datetime = Field(default_factory=datetime.now)

    # Live only (paper keeps the defaults; consumers then use the ordered quantity).
    # Read them through the helpers below, which tolerate mock results.
    filled_quantity: Optional[Decimal] = None   # broker-confirmed total across the result's orders
    requested_quantity: Optional[Decimal] = None
    # filled | partial | rejected | cancelled | open | unknown | not_placed | late_fill
    outcome: str = ""
    order_ids: list[str] = Field(default_factory=list)   # every broker order placed, in order
    remaining_open: bool = False    # an order may still be working at the broker
    fill_unconfirmed: bool = False  # a final order whose filled quantity the broker did not report
    fill_price_source: str = ""     # trades | order | trade_book | estimate
    broker_message: str = ""        # last extra_info / broker or cancel message


# Readers for ExecutionResult's live fields. Tests pass MagicMock results whose
# attributes are truthy mocks, so every consumer reads through these.


def filled_quantity_of(result: object, default: Decimal | int) -> Decimal:
    """The broker-confirmed filled quantity, else ``default`` (paper, mocks, unknown)."""
    value = getattr(result, "filled_quantity", None)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return default if isinstance(default, Decimal) else Decimal(default)


def is_remaining_open(result: object) -> bool:
    """True only when the result says an order may still be working at the broker."""
    return getattr(result, "remaining_open", False) is True


def is_unconfirmed(result: object) -> bool:
    """An order may still be working, or a final order's fill was not reported."""
    return is_remaining_open(result) or getattr(result, "fill_unconfirmed", False) is True


def outcome_of(result: object) -> str:
    """The live outcome string, or ``""``."""
    value = getattr(result, "outcome", "")
    return value if isinstance(value, str) else ""


def order_ids_of(result: object) -> list[str]:
    """The broker order ids, or ``[]``."""
    value = getattr(result, "order_ids", None)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return list(value)
    return []


def fill_status_of(result: object) -> str:
    """FILLED, PARTIAL (a live order filled in part), UNCONFIRMED (a live order may still
    be working, or its fill was not reported) or FAILED. Paper: FILLED or FAILED."""
    if getattr(result, "success", False):
        return "FILLED" if outcome_of(result) in ("", "filled") else "PARTIAL"
    return "UNCONFIRMED" if is_unconfirmed(result) else "FAILED"
