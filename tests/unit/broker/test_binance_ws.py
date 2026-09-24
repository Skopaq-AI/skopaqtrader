"""Binance websocket streams, against a fake connection (no network)."""

from __future__ import annotations

import json

import pytest

from skopaq.broker.binance_ws import BinanceWS


class FakeSocket:
    def __init__(self, messages):
        self.messages = [json.dumps(m) for m in messages]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


def _ws(messages, reconnect=False):
    ws = BinanceWS(reconnect=reconnect)
    ws.connected = []

    async def connect(streams):
        ws.connected.append(streams)
        return FakeSocket(messages)

    ws._connect = connect
    return ws


def _ticker(price):
    return {"e": "24hrTicker", "s": "BTCUSDT", "c": price, "p": "1", "P": "0.1", "h": "2",
            "l": "0", "v": "10", "q": "100", "b": "1", "a": "2", "E": 1_700_000_000_000}


async def _take(stream, n):
    out = []
    async for item in stream:
        out.append(item)
        if len(out) == n:
            break
    return out


@pytest.mark.asyncio
async def test_ticker_stream_yields_every_message():
    """Streams used to drop every message: the running flag was never set."""
    ws = _ws([_ticker("100"), _ticker("101")])
    ticks = await _take(ws.ticker_stream("BTCUSDT"), 2)
    assert [t.price for t in ticks] == [100.0, 101.0]
    assert ws.connected == [["btcusdt@ticker"]]


@pytest.mark.asyncio
async def test_depth_stream_uses_a_valid_partial_depth_stream():
    snapshot = {"lastUpdateId": 7, "bids": [["100.5", "2"]], "asks": [["101", "1"]]}
    ws = _ws([snapshot])

    book, = await _take(ws.depth_stream("BTCUSDT", level=100), 1)

    assert ws.connected == [["btcusdt@depth20@100ms"]]  # 100 is not offered; 20 is
    assert book.symbol == "BTCUSDT"  # partial depth payloads carry no "s"
    assert (book.bids[0].price, book.asks[0].quantity, book.last_update_id) == (100.5, 1.0, 7)


@pytest.mark.asyncio
async def test_closed_stream_ends_instead_of_reconnecting():
    ws = _ws([_ticker("100"), _ticker("101")], reconnect=True)
    stream = ws.ticker_stream("BTCUSDT")
    first = await stream.__anext__()
    await ws.close()

    assert first.price == 100.0
    assert [t async for t in stream] == []
    assert len(ws.connected) == 1
