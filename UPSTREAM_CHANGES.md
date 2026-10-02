# Upstream Changes Log

Documents every modification made to files under `tradingagents/`, vendored
from [TauricResearch/TradingAgents](https://github.com/TauricResearch/TradingAgents).

**Upstream base:** v0.5.2 — commit `5eb5085` (2026-09-29). Previous bases: v0.5.1 (`f58a585`), v0.2.0.
**Also vendored unmodified:** `cli/` (upstream's `tradingagents` CLI) and the example `main.py`.

Every change is marked with a `Skopaq:` comment in the source. To see them all
against pristine upstream:

```bash
git clone https://github.com/TauricResearch/TradingAgents /tmp/ta
git -C /tmp/ta checkout 5eb5085
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

**At the v0.5.2 base, `VENDOR_LIST` is re-added by Skopaq.** Upstream deleted
the list when it moved vendor preference into the per-method dicts. Skopaq
brings it back with `indstocks` first, because
`tests/unit/dataflows/test_indstocks_vendor.py` asserts both the membership
and the ordering, and because the ordering is the readable statement of which
vendor wins. The fallback chain itself is `VENDOR_METHODS`, whose insertion
order upstream still honours.

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

### 6. Parallel analysts — **retired at the v0.5.2 base**

This modification existed from the v0.2.0 base through v0.5.1: a
`parallel_analysts` config key (default `False`) that switched
`GraphSetup.setup_graph(..., parallel=True)`, each analyst running as its own
compiled subgraph so they never saw each other's tool calls, and
`_run_signature` appending `parallel=1` so a sequential checkpoint could not
resume into the parallel graph.

**Upstream shipped the same design in v0.5.2 (#1255) and made it the only
mode.** Its `_analyst_graph` is the isolated-subgraph approach above, the
`Msg Clear` nodes are gone, and `_run_signature` pins `analysts=parallel`
itself. Skopaq's contribution is therefore fully redundant, so the
modification was dropped rather than carried:

- the `parallel_analysts` config key is gone from `default_config.py`
- `_isolated_analyst`, the `parallel` parameter and `_chain_analysts` are gone
  from `graph/setup.py`
- `skopaq/graph/skopaq_graph.py` no longer sets the key
- `tests/unit/graph/test_checkpoint_signature.py` now asserts upstream's
  `analysts=parallel` marker instead of Skopaq's

What survives is upstream's improvement over the old Skopaq design: an
analyst is stopped after `max_tool_rounds` and asked to write its report, so a
model that keeps calling tools cannot run the graph into its recursion limit
(#1420). Sequential runs are no longer available at any base from v0.5.2 on.

### 7. Jev endpoint from `TYPESAFE_BASE_URL`

**Files:** `agents/post_screen.py`

- The System One URL is built per request from `TYPESAFE_BASE_URL` (the
  TypeSafe SDK's own variable) plus `/v1/systemone`, instead of the
  hard-coded `https://api.typesafe.ai/v1/systemone`. Unset or blank, it is
  still api.typesafe.ai; a trailing slash is ignored, as in the SDK.

**Why:** gateways such as OpenRouter serve Jev with their own key
(`https://openrouter.ai/api`). With the URL hard-coded, pointing Skopaq at a
gateway would send the gateway key to api.typesafe.ai and screening would
quietly stop. `skopaq/llm/env_bridge.py` copies `SKOPAQ_JEV_BASE_URL` into
`TYPESAFE_BASE_URL`. The default model, `jev-latest`, is also a name
OpenRouter accepts.
**Backward compatible:** yes — without `TYPESAFE_BASE_URL` the request is
unchanged, and upstream's `test_post_screen.py` passes as is.

## Not carried over from the v0.2.0 base

| Former change | Why dropped |
|---|---|
| Comma-separated indicator splitting | Upstream `get_indicators` does it |
| Risk manager fundamentals typo fix | Upstream rewrote the agent (Portfolio Manager) |
| Claude 4.6 in validators / CLI model lists | Upstream accepts unlisted model IDs |
| Parallel analyst fan-out (reducers, `Done *` nodes) | Replaced by isolated per-analyst subgraphs, now upstream's own (modification 6) |
| Crypto reports in memory lookups (managers, trader, reflection) | Upstream removed per-agent memories |
| `parallel_analysts` config key and its sequential mode | Upstream v0.5.2 made the parallel graph the only layout (modification 6) |

## Syncing a newer upstream

1. Add or fetch the upstream remote and extract the tag you are moving to:

   ```bash
   git remote add upstream https://github.com/TauricResearch/TradingAgents.git
   git fetch upstream --tags
   git archive v0.5.2 | tar -x -C /tmp/ta-new     # the new base, pristine
   git archive v0.5.1 | tar -x -C /tmp/ta-old     # the base we forked from
   diff -ru -x __pycache__ /tmp/ta-old/tradingagents tradingagents > /tmp/skopaq.patch
   ```

   That `diff` is the complete record of local modifications; keep it until the
   re-application is verified.

2. Import the new upstream `tradingagents/`, `cli/` and `main.py` verbatim in
   one commit. `cli/` and `main.py` are vendored unmodified, so a `diff -rq`
   against the pristine tree must come back empty for both.
3. Re-apply the modifications above in a second commit. Merge each file
   three-way with the old base, the new upstream and the local version:

   ```bash
   git merge-file -p -L new-upstream -L old-upstream -L skopaq \
       /tmp/ta-new/tradingagents/<file> /tmp/ta-old/tradingagents/<file> \
       <local version> > merged.py
   ```

   `git merge-file` rewrites its **first argument in place** unless you pass
   `-p`, and it exits non-zero on conflict. Check the result for `<<<<<<<`
   markers rather than trusting the exit code alone. Files that merge cleanly
   need no attention; the rest are read and fixed by hand.
4. Run upstream's test suite against the result: copy its `tests/` and
   `pyproject.toml` next to symlinks of our `tradingagents/` and `cli/`, then
   run `pytest tests -m "not integration"` there. Compare the result against
   the same suite run on the pristine tree, so upstream's own
   platform-specific failures are not mistaken for regressions.
5. Run `python3 -m pytest tests/unit/` — `tests/unit/graph/test_pipeline_end_to_end.py`
   runs the whole graph offline and catches broken wiring. Skopaq's own tests
   are where renamed upstream APIs surface first, because they reach further
   into upstream than the CLI does.
6. Update this file, and `UPSTREAM_REF` in `.github/workflows/ci.yml` (CI runs
   step 4 on every pull request).

Steps 3 and 4 are where a sync actually fails. A clean `git merge-file` is not
proof: a hunk can apply and still be wrong because the upstream function it
touched was rewritten. Check for API moves in the diff
(`git diff --stat old-base..new-base`) and look for each renamed or removed
symbol before trusting the merge.
