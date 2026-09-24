"""Tests for crypto tool registration and factory function imports."""

import pytest


class TestCryptoToolImports:
    """Verify all 8 crypto tools import correctly and are @tool-decorated."""

    def test_import_onchain_tools(self):
        from tradingagents.agents.crypto_tools import (
            get_blockchain_stats,
            get_address_activity,
        )
        # @tool-decorated functions have a .name attribute
        assert hasattr(get_blockchain_stats, "name")
        assert hasattr(get_address_activity, "name")

    def test_import_defi_tools(self):
        from tradingagents.agents.crypto_tools import (
            get_token_fundamentals,
            get_defi_tvl,
            get_chain_tvl_overview,
        )
        assert hasattr(get_token_fundamentals, "name")
        assert hasattr(get_defi_tvl, "name")
        assert hasattr(get_chain_tvl_overview, "name")

    def test_import_funding_tools(self):
        from tradingagents.agents.crypto_tools import (
            get_funding_rates,
            get_open_interest,
            get_long_short_ratio,
        )
        assert hasattr(get_funding_rates, "name")
        assert hasattr(get_open_interest, "name")
        assert hasattr(get_long_short_ratio, "name")

    def test_tool_count(self):
        """All 8 crypto tools should be importable."""
        from tradingagents.agents import crypto_tools
        tool_names = [
            "get_blockchain_stats", "get_address_activity",
            "get_token_fundamentals", "get_defi_tvl", "get_chain_tvl_overview",
            "get_funding_rates", "get_open_interest", "get_long_short_ratio",
        ]
        for name in tool_names:
            assert hasattr(crypto_tools, name), f"Missing tool: {name}"


class TestAnalystFactoryImports:
    """Verify the 3 crypto analyst factories import from the agents package."""

    def test_import_from_agents(self):
        from tradingagents.agents import (
            create_onchain_analyst,
            create_defi_analyst,
            create_funding_analyst,
        )
        assert callable(create_onchain_analyst)
        assert callable(create_defi_analyst)
        assert callable(create_funding_analyst)

    def test_import_from_individual_modules(self):
        from tradingagents.agents.analysts.onchain_analyst import create_onchain_analyst
        from tradingagents.agents.analysts.defi_analyst import create_defi_analyst
        from tradingagents.agents.analysts.funding_analyst import create_funding_analyst

        assert callable(create_onchain_analyst)
        assert callable(create_defi_analyst)
        assert callable(create_funding_analyst)

    def test_factories_in_all_export(self):
        import tradingagents.agents as agents_pkg
        if hasattr(agents_pkg, "__all__"):
            assert "create_onchain_analyst" in agents_pkg.__all__
            assert "create_defi_analyst" in agents_pkg.__all__
            assert "create_funding_analyst" in agents_pkg.__all__
