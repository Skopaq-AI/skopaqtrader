"""Tests for the INDstocks data vendor integration."""

import pytest

from tradingagents.dataflows.interface import VENDOR_LIST, VENDOR_METHODS


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
        from tradingagents.dataflows.indstocks import _run_async

        async def simple_coro():
            return 42

        result = _run_async(simple_coro())
        assert result == 42

    def test_run_async_propagates_exception(self):
        from tradingagents.dataflows.indstocks import _run_async

        async def failing_coro():
            raise ValueError("test error")

        with pytest.raises(ValueError, match="test error"):
            _run_async(failing_coro())


class TestYfinanceSuffix:
    """Regression guard for a silent failure the suffix helper can hit.

    `_apply_yfinance_suffix` reads `yfinance_symbol_suffix` from config. If that
    key goes missing — as it did when tradingagents/ was re-vendored at v0.3.1 —
    the helper degrades to a no-op, every yfinance fallback quietly fetches a US
    ticker instead of the NSE one, and the entire suite still passes.
    """

    def test_config_key_exists(self):
        from tradingagents.default_config import DEFAULT_CONFIG
        assert "yfinance_symbol_suffix" in DEFAULT_CONFIG

    def _with_suffix(self, suffix=".NS"):
        from tradingagents.default_config import DEFAULT_CONFIG
        from tradingagents.dataflows.config import set_config
        cfg = dict(DEFAULT_CONFIG)
        cfg["yfinance_symbol_suffix"] = suffix
        set_config(cfg)

    def test_suffix_is_applied(self):
        from tradingagents.dataflows.interface import _apply_yfinance_suffix
        self._with_suffix()
        assert _apply_yfinance_suffix(("RELIANCE", "d"), "get_stock_data")[0] == "RELIANCE.NS"

    def test_suffix_is_idempotent(self):
        from tradingagents.dataflows.interface import _apply_yfinance_suffix
        self._with_suffix()
        assert _apply_yfinance_suffix(("RELIANCE.NS", "d"), "get_stock_data")[0] == "RELIANCE.NS"

    def test_non_symbol_methods_untouched(self):
        """get_global_news takes a date first — suffixing it would corrupt the call."""
        from tradingagents.dataflows.interface import _apply_yfinance_suffix
        self._with_suffix()
        assert _apply_yfinance_suffix(("2026-01-01",), "get_global_news") == ("2026-01-01",)

    def test_empty_suffix_is_a_noop(self):
        from tradingagents.dataflows.interface import _apply_yfinance_suffix
        self._with_suffix("")
        assert _apply_yfinance_suffix(("AAPL", "d"), "get_stock_data")[0] == "AAPL"

    def test_indstocks_precedes_yfinance_in_the_chain(self):
        """Order is behaviour: route_to_vendor walks dict key order."""
        from tradingagents.dataflows.interface import VENDOR_METHODS
        chain = list(VENDOR_METHODS["get_stock_data"].keys())
        assert chain.index("indstocks") < chain.index("yfinance")
