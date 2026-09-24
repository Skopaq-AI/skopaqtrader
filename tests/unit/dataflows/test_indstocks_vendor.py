"""Tests for the INDstocks data vendor integration."""

import pytest

from tradingagents.dataflows.router import VENDOR_LIST, VENDOR_METHODS


class TestVendorRegistration:
    def test_indstocks_in_vendor_list(self):
        assert "indstocks" in VENDOR_LIST

    def test_indstocks_is_first_vendor(self):
        assert VENDOR_LIST[0] == "indstocks"

    def test_indstocks_registered_for_stock_data(self):
        assert "indstocks" in VENDOR_METHODS["get_stock_data"]

    def test_indstocks_function_is_callable(self):
        func = VENDOR_METHODS["get_stock_data"]["indstocks"]
        assert callable(func)

    def test_indstocks_function_has_correct_name(self):
        func = VENDOR_METHODS["get_stock_data"]["indstocks"]
        assert func.__name__ == "get_stock_data_indstocks"


class TestRunAsync:
    """Test the async-to-sync bridge utility."""

    def test_run_async_executes_coroutine(self):
        from tradingagents.dataflows.vendors.indstocks import _run_async

        async def simple_coro():
            return 42

        result = _run_async(simple_coro())
        assert result == 42

    def test_run_async_propagates_exception(self):
        from tradingagents.dataflows.vendors.indstocks import _run_async

        async def failing_coro():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            _run_async(failing_coro())


class TestRouting:
    """route_to_vendor with the INDstocks → yfinance chain Skopaq configures."""

    @pytest.fixture(autouse=True)
    def _config(self):
        from tradingagents.dataflows.config import run_config

        with run_config({
            "data_vendors": {"core_stock_apis": "indstocks,yfinance"},
            "yfinance_symbol_suffix": ".NS",
        }):
            yield

    def test_indstocks_serves_bare_symbol(self, monkeypatch):
        from tradingagents.dataflows import router

        calls = []
        monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], "indstocks",
                            lambda *a: calls.append(("indstocks", a)) or "CSV")
        result = router.route_to_vendor("get_stock_data", "RELIANCE", "2026-09-01", "2026-09-20")
        assert result == "CSV"
        assert calls == [("indstocks", ("RELIANCE", "2026-09-01", "2026-09-20"))]

    def test_no_data_falls_back_to_yfinance_with_suffix(self, monkeypatch):
        from tradingagents.dataflows import router
        from tradingagents.dataflows.errors import NoMarketDataError

        def no_data(symbol, *_):
            raise NoMarketDataError(symbol, detail="no INDstocks candles")

        calls = []
        monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], "indstocks", no_data)
        monkeypatch.setitem(router.VENDOR_METHODS["get_stock_data"], "yfinance",
                            lambda *a: calls.append(a) or "YF")
        result = router.route_to_vendor("get_stock_data", "RELIANCE", "2026-09-01", "2026-09-20")
        assert result == "YF"
        assert calls == [("RELIANCE.NS", "2026-09-01", "2026-09-20")]

    def test_suffix_not_doubled(self):
        from tradingagents.dataflows.router import _apply_yfinance_suffix

        assert _apply_yfinance_suffix(("RELIANCE.NS",), "get_news") == ("RELIANCE.NS",)
        assert _apply_yfinance_suffix(("2026-09-20",), "get_global_news") == ("2026-09-20",)


class TestNormalizeSymbol:
    def test_strips_exchange_suffix(self):
        from tradingagents.dataflows.vendors.indstocks import _normalize_symbol

        assert _normalize_symbol("RELIANCE.NS") == "RELIANCE"
        assert _normalize_symbol("TCS") == "TCS"
