# Upstream Changes Log

Documents every modification made to files under `tradingagents/`, vendored
from [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents).

**Upstream base:** v0.5.1 — commit `f58a585` (2026-09-24). Previous base: v0.2.0.
**Also vendored unmodified:** `cli/` (upstream's `tradingagents` CLI) and the example `main.py`.

Every change is marked with a `Skopaq:` comment in the source. To see them all
against pristine upstream:

```bash
git clone https://github.com/TauricResearch/TradingAgents /tmp/ta
git -C /tmp/ta checkout f58a585
diff -ru -x __pycache__ /tmp/ta/tradingagents tradingagents
diff -ru -x __pycache__ /tmp/ta/cli cli        # expected: no differences
```

## Modifications

### 1. Per-role LLMs

**Files:** `graph/setup.py`, `graph/trading_graph.py`

- `TradingAgentsGraph(..., llm_map=None)` accepts a `{role: llm}` dict and
  passes it to `GraphSetup`. An `"llm_map"` key in `config` is accepted too,
  and is removed from `config`: the data layer deep-copies config on every
  read, and live LLM clients must not be copied.
- `GraphSetup._get_llm(*roles, deep=False)` returns the first role found in
  the map, then `_default`, then upstream's quick/deep LLM. Every agent node
  uses it. Role keys: `market_analyst`, `sentiment_analyst` (or
  `social_analyst`), `news_analyst`, `fundamentals_analyst`,
  `onchain_analyst`, `defi_analyst`, `funding_analyst`, `bull_researcher`,
  `bear_researcher`, `research_manager`, `trader`, `aggressive_debator`,
  `neutral_debator`, `conservative_debator`, `portfolio_manager` (or its
  pre-v0.2.2 name `risk_manager`).

**Why:** `skopaq/llm/model_tier.py` assigns Gemini, Grok and Claude per role.
**Backward compatible:** yes — without `llm_map`, behavior is upstream's.

### 2. INDstocks data vendor

**Files:** `dataflows/vendors/indstocks.py` (new), `dataflows/router.py`

- New vendor for NSE OHLCV from the INDstocks broker API, returning the same
  CSV shape as yfinance. Strips `.NS`/`.BO` suffixes, bridges the async
  client to sync, includes the `end_date` candle (like the yfinance vendor),
  and raises `NoMarketDataError` on an empty result so the router can try
  the next configured vendor.
- Registered first in `VENDOR_LIST` and in `VENDOR_METHODS["get_stock_data"]`.

**Backward compatible:** yes — only used when a config names `indstocks`
(Skopaq uses `"core_stock_apis": "indstocks,yfinance"`).

### 3. yfinance symbol suffix

**Files:** `dataflows/router.py`, `default_config.py`

- `route_to_vendor` appends `config["yfinance_symbol_suffix"]` (e.g. `.NS`)
  to a bare symbol before calling a yfinance function
  (`_apply_yfinance_suffix`). New config key, default `""`.

**Why:** Skopaq code calls `route_to_vendor` directly with bare NSE symbols
(ATR sizing, MCP data tools, backtests). Graph runs already pass
`RELIANCE.NS`, which is left alone.
**Backward compatible:** yes — the empty default changes nothing.

### 4. Crypto analysts (on-chain, DeFi, funding)

**New files:** `agents/analysts/onchain_analyst.py`, `agents/analysts/defi_analyst.py`,
`agents/analysts/funding_analyst.py`, `agents/crypto_tools.py`,
`dataflows/vendors/crypto_onchain.py`, `dataflows/vendors/crypto_defi.py`,
`dataflows/vendors/crypto_funding.py`

**Modified:** `agents/__init__.py` (exports), `graph/analyst_execution.py`
(specs `onchain`/`defi`/`funding`), `agents/state.py` (`onchain_report`,
`defi_report`, `funding_report`), `graph/propagation.py` (empty initial
reports), `graph/setup.py` (factories), `graph/trading_graph.py` (state log),
`agents/context.py` (`crypto_reports_section`), and the five report readers
`agents/researchers/{bull,bear}_researcher.py`,
`agents/risk_mgmt/{aggressive,conservative,neutral}_debator.py`, which append
`crypto_reports_section(state)` after the fundamentals report.

The crypto vendors accept yfinance-style pairs (`BTC-USD`), the form the
analysis runs on, as well as Binance pairs (`BTCUSDT`) and bare coins.

**Why:** Skopaq selects these analysts when `asset_class == "crypto"`
(Blockchair/Blockchain.info, DeFiLlama/CoinGecko, Binance Futures data).
**Backward compatible:** yes — analysts run only when selected, and the
report section is empty for equity runs, so those prompts are unchanged.

### 5. Portfolio Manager confidence

**Files:** `agents/schemas.py`, `agents/managers/portfolio_manager.py`

- `PortfolioDecision.confidence: int | None` (0–100; a value between 0 and
  1 is read as a fraction, anything else becomes `None`), rendered as
  `**Confidence**: N` by
  `render_pm_decision`, and listed in the prompt's output sections for the
  free-text fallback.

**Why:** `skopaq/graph/skopaq_graph.py` parses it for position sizing and
the minimum-confidence safety gate.
**Backward compatible:** yes — optional field; the rendered decision gains
one line, which upstream's rating parser ignores.

### 6. Parallel analysts (opt-in)

**Files:** `graph/setup.py`, `graph/trading_graph.py`, `default_config.py`

- New config key `parallel_analysts` (default `False`). When true,
  `GraphSetup.setup_graph(..., parallel=True)` starts every selected analyst
  at once and joins them before the Bull Researcher.
- Each analyst and its tool loop run in their own compiled subgraph
  (`_isolated_analyst`), seeded with the run's opening message, so analysts
  never see or route on each other's tool calls; only the analyst's report
  is written back. No `Msg Clear` nodes are needed in this mode.
- `TradingAgentsGraph._run_signature` appends `parallel=1` in this mode, so
  a checkpoint from a sequential run never resumes into the parallel graph
  (whose nodes differ). Sequential signatures are unchanged.

**Why:** the analysts are independent, and running them together cuts
analysis time (about 18% with the four equity analysts on the v0.2.0 base).
The v0.2.0 fan-out shared one message list between analysts; the subgraphs
avoid that. `SkopaqTradingGraph` turns it on.
**Backward compatible:** yes — off by default; upstream's CLI and tests run
the sequential graph unchanged.

## Not carried over from the v0.2.0 base

| Former change | Why dropped |
|---|---|
| Comma-separated indicator splitting | Upstream `get_indicators` does it |
| Risk manager fundamentals typo fix | Upstream rewrote the agent (Portfolio Manager) |
| Claude 4.6 in validators / CLI model lists | Upstream accepts unlisted model IDs |
| Parallel analyst fan-out (reducers, `Done *` nodes) | Replaced by isolated per-analyst subgraphs (modification 6) |
| Crypto reports in memory lookups (managers, trader, reflection) | Upstream removed per-agent memories |

## Syncing a newer upstream

1. Import the new upstream `tradingagents/`, `cli/` and `main.py` verbatim
   in one commit.
2. Re-apply the modifications above in a second commit (search the old tree
   for `Skopaq:`).
3. Run upstream's test suite against the result: copy its `tests/` and
   `pyproject.toml` next to symlinks of our `tradingagents/` and `cli/`, then
   run `pytest tests -m "not integration"` there.
4. Run `python3 -m pytest tests/unit/` — `tests/unit/graph/test_pipeline_end_to_end.py`
   runs the whole graph offline and catches broken wiring.
5. Update this file, and `UPSTREAM_REF` in `.github/workflows/ci.yml` (CI runs
   step 3 on every pull request).
