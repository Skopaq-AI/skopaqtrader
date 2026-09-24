"""WebSocket client for real-time Binance market data.

Supports:
    - Individual symbol ticker streams
    - Combined ticker streams (multiple symbols)
    - Trade streams (individual trades)
    - Order book (depth) streams
    - Kline/candlestick streams

Usage::

    ws = BinanceWS()

    # Subscribe to BTC ticker
    async for msg in ws.ticker_stream("BTCUSDT"):
        print(msg)

    # Subscribe to multiple tickers
    async for msg in ws.ticker_stream(["BTCUSDT", "ETHUSDT"]):
        print(msg)

    # Trade stream
    async for trade in ws.trade_stream("BTCUSDT"):
        print(trade)

    # Order book
    async for depth in ws.depth_stream("BTCUSDT"):
        print(depth)
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable, Optional

import websockets
from websockets.client import WebSocketClientProtocol

logger = logging.getLogger(__name__)

BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
BINANCE_WS_COMBINED_URL = "wss://stream.binance.com:9443/stream"


@dataclass
class TickerData:
    """Real-time ticker data."""

    symbol: str
    price: float
    price_change: float
    price_change_pct: float
    high_24h: float
    low_24h: float
    volume_24h: float
    quote_volume_24h: float
    bid: float
    ask: float
    timestamp: datetime


@dataclass
class TradeData:
    """Real-time trade data."""

    id: int
    symbol: str
    price: float
    quantity: float
    is_buyer_maker: bool
    timestamp: datetime


@dataclass
class OrderBookEntry:
    """Single order book entry."""

    price: float
    quantity: float


@dataclass
class OrderBookData:
    """Order book snapshot."""

    symbol: str
    bids: list[OrderBookEntry]
    asks: list[OrderBookEntry]
    last_update_id: int
    timestamp: datetime


@dataclass
class KlineData:
    """Kline/candlestick data."""

    symbol: str
    open_time: datetime
    close_time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool


class BinanceWSError(Exception):
    """WebSocket error."""

    pass


class BinanceWS:
    """Async WebSocket client for Binance real-time streams.

    Provides async generators for different stream types.
    Supports automatic reconnection on disconnect.
    """

    def __init__(
        self,
        base_url: str = BINANCE_WS_URL,
        reconnect: bool = True,
        reconnect_delay: float = 5.0,
    ) -> None:
        self._base_url = base_url
        self._reconnect = reconnect
        self._reconnect_delay = reconnect_delay
        self._ws: Optional[WebSocketClientProtocol] = None
        self._subscriptions: set[str] = set()
        self._running = True  # until close(); every stream checks it per message

    async def _connect(self, streams: list[str]) -> WebSocketClientProtocol:
        """Establish WebSocket connection with subscriptions."""
        if len(streams) == 1:
            url = f"{self._base_url}/{streams[0]}"
        else:
            params = "/".join(streams)
            url = f"{BINANCE_WS_COMBINED_URL}?streams={params}"

        logger.debug("Connecting to Binance WS: %s", url)
        ws = await websockets.connect(url, ping_interval=30)
        return ws

    async def ticker_stream(self, symbols: str | list[str]) -> AsyncGenerator[TickerData, None]:
        """Stream 24hr ticker updates for symbol(s).

        Args:
            symbols: Single symbol or list of symbols

        Yields:
            TickerData with real-time price updates
        """
        symbols = [symbols] if isinstance(symbols, str) else symbols
        streams = [f"{s.lower()}@ticker" for s in symbols]

        while True:
            try:
                ws = await self._connect(streams)
                async for msg in ws:
                    if not self._running:
                        return  # closed: end the stream instead of reconnecting

                    data = json.loads(msg)
                    ticker = data.get("data", data)

                    if "e" not in ticker:
                        continue

                    yield TickerData(
                        symbol=ticker["s"],
                        price=float(ticker["c"]),
                        price_change=float(ticker["p"]),
                        price_change_pct=float(ticker["P"]),
                        high_24h=float(ticker["h"]),
                        low_24h=float(ticker["l"]),
                        volume_24h=float(ticker["v"]),
                        quote_volume_24h=float(ticker["q"]),
                        bid=float(ticker["b"]),
                        ask=float(ticker["a"]),
                        timestamp=datetime.fromtimestamp(ticker["E"] / 1000, tz=timezone.utc),
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Ticker stream error: %s", e)
                if self._reconnect:
                    await asyncio.sleep(self._reconnect_delay)
                else:
                    raise BinanceWSError(f"Ticker stream failed: {e}") from e

    async def trade_stream(self, symbol: str) -> AsyncGenerator[TradeData, None]:
        """Stream individual trades for a symbol.

        Args:
            symbol: Trading pair (e.g., "BTCUSDT")

        Yields:
            TradeData for each trade
        """
        stream = f"{symbol.lower()}@trade"

        while True:
            try:
                ws = await self._connect([stream])
                async for msg in ws:
                    if not self._running:
                        return  # closed: end the stream instead of reconnecting

                    data = json.loads(msg)
                    trade = data.get("data", data)

                    if "e" not in trade or trade["e"] != "trade":
                        continue

                    yield TradeData(
                        id=trade["t"],
                        symbol=trade["s"],
                        price=float(trade["p"]),
                        quantity=float(trade["q"]),
                        is_buyer_maker=trade["m"],
                        timestamp=datetime.fromtimestamp(trade["T"] / 1000, tz=timezone.utc),
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Trade stream error: %s", e)
                if self._reconnect:
                    await asyncio.sleep(self._reconnect_delay)
                else:
                    raise BinanceWSError(f"Trade stream failed: {e}") from e

    async def depth_stream(
        self, symbol: str, level: int = 20
    ) -> AsyncGenerator[OrderBookData, None]:
        """Stream order book snapshots for a symbol.

        Uses Binance's partial book depth stream
        (``<symbol>@depth<levels>@100ms``), which offers 5, 10 or 20 levels;
        any other *level* is served as 20.

        Args:
            symbol: Trading pair
            level: Depth level (5, 10 or 20)

        Yields:
            OrderBookData snapshots
        """
        if level not in (5, 10, 20):
            level = 20
        stream = f"{symbol.lower()}@depth{level}@100ms"

        while True:
            try:
                ws = await self._connect([stream])
                async for msg in ws:
                    if not self._running:
                        return  # closed: end the stream instead of reconnecting

                    data = json.loads(msg)
                    depth = data.get("data", data)

                    if "lastUpdateId" not in depth:
                        continue

                    bids = [OrderBookEntry(float(p), float(q)) for p, q in depth.get("bids", [])]
                    asks = [OrderBookEntry(float(p), float(q)) for p, q in depth.get("asks", [])]

                    yield OrderBookData(
                        # Partial depth payloads carry no symbol field.
                        symbol=depth.get("s", symbol.upper()),
                        bids=bids,
                        asks=asks,
                        last_update_id=depth["lastUpdateId"],
                        timestamp=datetime.now(timezone.utc),
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Depth stream error: %s", e)
                if self._reconnect:
                    await asyncio.sleep(self._reconnect_delay)
                else:
                    raise BinanceWSError(f"Depth stream failed: {e}") from e

    async def kline_stream(
        self,
        symbol: str,
        interval: str = "1m",
    ) -> AsyncGenerator[KlineData, None]:
        """Stream kline/candlestick updates.

        Args:
            symbol: Trading pair
            interval: Kline interval (1m, 5m, 15m, 1h, 4h, 1d, etc.)

        Yields:
            KlineData for each candle update
        """
        stream = f"{symbol.lower()}@kline_{interval}"

        while True:
            try:
                ws = await self._connect([stream])
                async for msg in ws:
                    if not self._running:
                        return  # closed: end the stream instead of reconnecting

                    data = json.loads(msg)
                    kline = data.get("data", {}).get("k", {})

                    if "t" not in kline:
                        continue

                    yield KlineData(
                        symbol=kline["s"],
                        open_time=datetime.fromtimestamp(kline["t"] / 1000, tz=timezone.utc),
                        close_time=datetime.fromtimestamp(kline["T"] / 1000, tz=timezone.utc),
                        open=float(kline["o"]),
                        high=float(kline["h"]),
                        low=float(kline["l"]),
                        close=float(kline["c"]),
                        volume=float(kline["v"]),
                        is_closed=kline["x"],
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Kline stream error: %s", e)
                if self._reconnect:
                    await asyncio.sleep(self._reconnect_delay)
                else:
                    raise BinanceWSError(f"Kline stream failed: {e}") from e

    async def close(self) -> None:
        """Close WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
            self._ws = None


class BinanceWSManager:
    """Manager for multiple WebSocket streams with shared connection.

    Efficiently handles multiple subscriptions over fewer connections.
    """

    def __init__(self) -> None:
        self._streams: dict[str, Callable] = {}
        self._ws: Optional[WebSocketClientProtocol] = None

    async def subscribe(
        self,
        stream_type: str,
        symbols: str | list[str],
        callback: Callable[[Any], None],
    ) -> None:
        """Subscribe to a stream type.

        Args:
            stream_type: "ticker", "trade", "depth", "kline"
            symbols: Symbol or list of symbols
            callback: Async callback function for messages
        """
        symbols = [symbols] if isinstance(symbols, str) else symbols
        streams = [f"{s.lower()}@{stream_type}" for s in symbols]
        self._streams.update({s: callback for s in streams})

    async def start(self) -> None:
        """Start consuming all subscribed streams."""
        if not self._streams:
            return

        stream_list = list(self._streams.keys())
        url = f"{BINANCE_WS_COMBINED_URL}?streams={'/'.join(stream_list)}"

        async with websockets.connect(url, ping_interval=30) as ws:
            async for msg in ws:
                data = json.loads(msg)
                stream = data.get("stream", "")
                callback = self._streams.get(stream)

                if callback and data.get("data"):
                    await callback(data["data"])
