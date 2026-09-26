"""Tick sizes from the INDstocks instruments CSV (skopaq/broker/scrip_resolver.py)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from skopaq.broker import scrip_resolver
from skopaq.broker.scrip_resolver import resolve_scrip_code, resolve_tick_size

CSV = (
    "SECURITY_ID,TRADING_SYMBOL,CUSTOM_SYMBOL,EXCH,SEGMENT,INSTRUMENT_NAME,LOT_UNITS,"
    "EXPIRY_DATE,STRIKE_PRICE,OPTION_TYPE,TICK_SIZE,SYMBOL_NAME\n"
    "2885,RELIANCE,Reliance,NSE,E,EQUITY,1,,,,0.10,RELIANCE\n"
    "11536,TCS,TCS,NSE,E,EQUITY,1,,,,,TCS\n"
    "1594,INFY,Infosys,NSE,E,EQUITY,1,,,,abc,INFY\n"
    "3045,SBIN,SBI,NSE,E,EQUITY,1,,,,0,SBIN\n"
    "500325,RELIANCE,Reliance,BSE,E,EQUITY,1,,,,0.05,RELIANCE\n"
)


@pytest.fixture(autouse=True)
def _empty_cache(monkeypatch):
    monkeypatch.setattr(scrip_resolver, "_cache", {})
    monkeypatch.setattr(scrip_resolver, "_cache_ts", 0.0)
    monkeypatch.setattr(scrip_resolver, "_tick_cache", {})


def _client(csv_text: str = CSV) -> MagicMock:
    client = MagicMock()
    client.get_instruments = AsyncMock(return_value=csv_text)
    return client


@pytest.mark.asyncio
async def test_tick_size_from_the_csv():
    client = _client()
    assert await resolve_tick_size(client, "RELIANCE") == Decimal("0.10")
    assert await resolve_tick_size(client, "RELIANCE", "BSE") == Decimal("0.05")
    client.get_instruments.assert_awaited_once()          # one download serves every lookup


@pytest.mark.asyncio
@pytest.mark.parametrize("symbol", ["TCS", "INFY", "SBIN", "NOSUCH"])
async def test_missing_unparseable_or_zero_tick_is_none(symbol):
    assert await resolve_tick_size(_client(), symbol) is None


@pytest.mark.asyncio
async def test_download_error_is_none():
    client = MagicMock()
    client.get_instruments = AsyncMock(side_effect=RuntimeError("network down"))
    assert await resolve_tick_size(client, "RELIANCE") is None


@pytest.mark.asyncio
async def test_scrip_code_cache_keeps_its_shape():
    client = _client()
    assert await resolve_scrip_code(client, "RELIANCE") == "NSE_2885"
    assert scrip_resolver._cache["NSE:TCS"] == "NSE_11536"
    assert scrip_resolver._tick_cache == {"NSE:RELIANCE": Decimal("0.10"),
                                          "BSE:RELIANCE": Decimal("0.05")}
    # The tick lookup reuses the cache the scrip lookup loaded
    assert await resolve_tick_size(client, "RELIANCE") == Decimal("0.10")
    client.get_instruments.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_cache_is_reloaded(monkeypatch):
    client = _client()
    assert await resolve_tick_size(client, "RELIANCE") == Decimal("0.10")
    monkeypatch.setattr(scrip_resolver, "_cache_ts", 0.0)   # an hour and more ago
    client.get_instruments = AsyncMock(return_value=CSV.replace(",0.10,", ",0.50,"))
    assert await resolve_tick_size(client, "RELIANCE") == Decimal("0.50")
