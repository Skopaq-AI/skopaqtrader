"""Re-pricing a protective exit never waits on an instruments-CSV download: a cached tick is
used even when the resolver's cache is stale (tick sizes do not change intraday), and a
lookup that has to download is bounded, falling back to the coarse tick.

Virtual time (FakeClock) over a scripted broker whose instruments download is slow.
"""

from __future__ import annotations

import time
from decimal import Decimal

from skopaq.broker import scrip_resolver
from tests.unit.execution._fakes import Script
from tests.unit.execution.test_live_orders import Rig, exit_sell, held, sig

CSV = "EXCH,TRADING_SYMBOL,SECURITY_ID,TICK_SIZE\nNSE,TCS,11536,0.10\n"
FILLED = {"status": "SUCCESS", "traded_qty": 10, "traded_price": "1228"}


def _slow_csv(rig, seconds=25.0):
    async def get_instruments(source="equity"):
        rig.client.calls.append(("get_instruments", rig.clock.t))
        await rig.clock.sleep(seconds)          # a slow multi-MB download
        return CSV
    return get_instruments


async def test_a_stale_cache_still_gives_its_tick_without_a_download(monkeypatch):
    monkeypatch.setattr(scrip_resolver, "_cache", {"NSE:TCS": "NSE_11536"})
    monkeypatch.setattr(scrip_resolver, "_tick_cache", {"NSE:TCS": Decimal("0.10")})
    monkeypatch.setattr(scrip_resolver, "_cache_ts", time.time() - 3700)   # over an hour old
    rig = Rig(positions=held(), ltp=1234.57)
    rig.client.get_instruments = _slow_csv(rig)
    rig.client.place_effects = [Script(), Script(timeline=[(0, FILLED)])]

    result = await rig.worker.execute(exit_sell(), sig("SELL", 1234.0))

    first, second = rig.client.placed()
    assert result.success
    assert second[3] == 1228.3                  # the cached 0.10 tick
    assert second[4] < 15                       # re-placed right after the cancel (t≈10)
    assert "get_instruments" not in rig.client.names()


async def test_a_lookup_that_must_download_is_bounded(monkeypatch):
    monkeypatch.setattr(scrip_resolver, "_cache", {})
    monkeypatch.setattr(scrip_resolver, "_tick_cache", {})
    monkeypatch.setattr(scrip_resolver, "_cache_ts", 0.0)
    rig = Rig(positions=held(), ltp=1234.57)
    rig.client.get_instruments = _slow_csv(rig)
    rig.client.place_effects = [Script(), Script(timeline=[(0, FILLED)])]

    result = await rig.worker.execute(exit_sell(), sig("SELL", 1234.0))

    first, second = rig.client.placed()
    assert result.success
    assert second[3] == 1228.0                  # the coarse 1.00 tick at ₹1,000–20,000
    assert second[4] < 15
