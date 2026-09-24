"""Multi-exchange abstraction layer for crypto trading.

Provides a unified interface across multiple exchanges:
    - Binance (spot, futures)
    - Coinbase (planned)
    - Kraken (planned)

Usage::

    # Create exchange client
    exchange = create_exchange("binance", api_key="...", api_secret="...")

    # Unified API
    ticker = await exchange.get_ticker("BTCUSDT")
    balance = await exchange.get_balance("USDT")
    order = await exchange.place_order("BTCUSDT", "BUY", 0.001)

    # Real-time via WebSocket
    async for price in exchange.ticker_stream("BTCUSDT"):
        print(price)
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, AsyncGenerator, Optional

from skopaq.broker.models import Exchange, Quote
from skopaq.broker.binance_client import BinanceClient
from skopaq.broker.binance_auth import BinanceAuthClient
from skopaq.broker.binance_ws import BinanceWS, TickerData, TradeData, OrderBookData

logger = logging.getLogger(__name__)


class BaseExchange(ABC):
    """Abstract base class for exchange integrations."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Exchange name."""
        pass

    @property
    @abstractmethod
    def exchange_type(self) -> str:
        """Exchange type: 'spot', 'futures', 'dex'."""
        pass

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Quote:
        """Get current ticker for symbol."""
        pass

    @abstractmethod
    async def get_balance(self, asset: str) -> dict[str, Any]:
        """Get account balance for asset."""
        pass

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "LIMIT",
    ) -> dict[str, Any]:
        """Place an order."""
        pass

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        """Cancel an order."""
        pass

    @abstractmethod
    async def get_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        """Get order status."""
        pass

    @abstractmethod
    async def get_open_orders(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        """Get open orders."""
        pass

    @abstractmethod
    async def get_order_history(
        self, symbol: Optional[str] = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        """Get order history."""
        pass

    @abstractmethod
    async def get_trades(self, symbol: str, limit: int = 500) -> list[dict[str, Any]]:
        """Get trade history."""
        pass

    @abstractmethod
    async def get_depth(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        """Get order book depth."""
        pass


class BinanceSpotExchange(BaseExchange):
    """Binance spot exchange implementation."""

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        testnet: bool = False,
    ) -> None:
        self._api_key = api_key
        self._api_secret = api_secret
        self._testnet = testnet
        self._public_client: Optional[BinanceClient] = None
        self._auth_client: Optional[BinanceAuthClient] = None

    @property
    def name(self) -> str:
        return "Binance"

    @property
    def exchange_type(self) -> str:
        return "spot"

    async def _get_public_client(self) -> BinanceClient:
        if self._public_client is None:
            self._public_client = BinanceClient()
        return self._public_client

    async def _get_auth_client(self) -> BinanceAuthClient:
        if not self._api_key or not self._api_secret:
            raise ValueError("API key and secret required for authenticated calls")

        if self._auth_client is None:
            self._auth_client = BinanceAuthClient(
                api_key=self._api_key,
                api_secret=self._api_secret,
                testnet=self._testnet,
            )
        return self._auth_client

    async def get_ticker(self, symbol: str) -> Quote:
        client = await self._get_public_client()
        return await client.get_quote(symbol)

    async def get_balance(self, asset: str) -> dict[str, Any]:
        client = await self._get_auth_client()
        return await client.get_balance(asset)

    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
        order_type: str = "LIMIT",
    ) -> dict[str, Any]:
        client = await self._get_auth_client()
        return await client.place_order(symbol, side, quantity, price, order_type)

    async def cancel_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        client = await self._get_auth_client()
        return await client.cancel_order(symbol, order_id)

    async def get_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        client = await self._get_auth_client()
        return await client.get_order(symbol, order_id)

    async def get_open_orders(self, symbol: Optional[str] = None) -> list[dict[str, Any]]:
        client = await self._get_auth_client()
        return await client.get_open_orders(symbol)

    async def get_order_history(
        self, symbol: Optional[str] = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        client = await self._get_auth_client()
        return await client.get_order_history(symbol, limit)

    async def get_trades(self, symbol: str, limit: int = 500) -> list[dict[str, Any]]:
        client = await self._get_auth_client()
        return await client.get_my_trades(symbol, limit)

    async def get_depth(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        client = await self._get_auth_client()
        return await client.get_depth(symbol, limit)

    def ticker_stream(self, symbol: str) -> AsyncGenerator[TickerData, None]:
        """Stream real-time ticker updates."""
        ws = BinanceWS()
        return ws.ticker_stream(symbol)

    def trade_stream(self, symbol: str) -> AsyncGenerator[TradeData, None]:
        """Stream real-time trades."""
        ws = BinanceWS()
        return ws.trade_stream(symbol)

    def depth_stream(self, symbol: str, level: int = 20) -> AsyncGenerator[OrderBookData, None]:
        """Stream order book updates."""
        ws = BinanceWS()
        return ws.depth_stream(symbol, level)


class CoinbaseExchange(BaseExchange):
    """Coinbase exchange (placeholder for future implementation)."""

    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        raise NotImplementedError("Coinbase exchange coming soon")

    @property
    def name(self) -> str:
        return "Coinbase"

    @property
    def exchange_type(self) -> str:
        return "spot"


class KrakenExchange(BaseExchange):
    """Kraken exchange (placeholder for future implementation)."""

    def __init__(self, api_key: str = "", api_secret: str = "") -> None:
        raise NotImplementedError("Kraken exchange coming soon")

    @property
    def name(self) -> str:
        return "Kraken"

    @property
    def exchange_type(self) -> str:
        return "spot"


def create_exchange(
    exchange: str,
    api_key: str = "",
    api_secret: str = "",
    testnet: bool = False,
) -> BaseExchange:
    """Factory function to create exchange client.

    Args:
        exchange: Exchange name (binance, coinbase, kraken)
        api_key: API key for authenticated access
        api_secret: API secret for authenticated access
        testnet: Use testnet (where available)

    Returns:
        Exchange client instance

    Raises:
        ValueError: If exchange is not supported
    """
    exchange = exchange.lower()

    if exchange in ("binance", "binance_spot"):
        return BinanceSpotExchange(api_key, api_secret, testnet)
    elif exchange == "coinbase":
        return CoinbaseExchange(api_key, api_secret)
    elif exchange == "kraken":
        return KrakenExchange(api_key, api_secret)
    else:
        raise ValueError(f"Unsupported exchange: {exchange}. Supported: binance, coinbase, kraken")


SUPPORTED_EXCHANGES = {
    "binance": {
        "name": "Binance",
        "type": "spot",
        "testnet": True,
        "fees": {"maker": 0.001, "taker": 0.001},
    },
    "coinbase": {
        "name": "Coinbase",
        "type": "spot",
        "testnet": False,
        "fees": {"maker": 0.006, "taker": 0.006},
    },
    "kraken": {
        "name": "Kraken",
        "type": "spot",
        "testnet": False,
        "fees": {"maker": 0.0016, "taker": 0.0026},
    },
}


def get_exchange_info(exchange: str) -> Optional[dict[str, Any]]:
    """Get exchange information and fees."""
    return SUPPORTED_EXCHANGES.get(exchange.lower())


def list_supported_exchanges() -> list[str]:
    """List all supported exchanges."""
    return list(SUPPORTED_EXCHANGES.keys())
