# Upstream Changes Log

Documents all modifications made to files under `tradingagents/` (vendored from TauricResearch/TradingAgents **v0.3.1**).

**Upstream tag:** `upstream-v0.3.1`
**Diff command:** `git diff upstream-v0.3.1..HEAD -- tradingagents/`

> The `upstream-vX` tags point at commits in TauricResearch/TradingAgents and
> are **not** pushed to our origin — pushing one would drag upstream's entire
> history in. Recreate locally with:
> `git fetch upstream --tags && git tag -f upstream-v0.3.1 upstream/main`

## Merge history

| From | To | Branch | Notes |
|---|---|---|---|
| — | v0.2.0 | — | initial vendoring |
| v0.2.0 | **v0.3.1** | `upstream/v0.3.1-merge` | 167 commits; see `docs/maintenance/upstream-merge-v0.3.1.md` |

### What the v0.3.1 merge changed about this list

**Dropped — upstream implemented it independently:**

- `agents/utils/technical_indicators_tools.py` — comma-separated indicator
  handling. Upstream's version splits, lowercases and catches `ValueError` per
  indicator. Ours was removed rather than reapplied.

**Dropped — no longer applicable:**

- `graph/reflection.py` — crypto report injection. Upstream reduced `Reflector`
  to `reflect_on_final_decision`; it no longer assembles analyst reports.
- `agents/managers/risk_manager.py` — upstream renamed this to
  `portfolio_manager.py`. `create_risk_manager` is now
  `create_portfolio_manager`. Our `llm_map` key stays `"risk_manager"`, since
  that key is what routes the judge role to Claude Opus.

**Restructured — same capability, smaller surface:**

- Crypto report injection was duplicated across 13 agent files. Every one
  became a conflict when upstream rewrote prompt assembly. It is now a single
  helper, `agent_utils.get_crypto_reports_block()`, plus a `{crypto_reports}`
  placeholder in the 5 prompts that still read report fields (both researchers,
  all three debators). Future merge surface: one function.

**Added by this merge:**

- `llm_clients/model_catalog.py` — registered `claude-opus-4-6` and
  `gemini-3-flash-preview`. Upstream's rebuilt catalog knows neither, so every
  run logged "not in the known model list", which would mask a genuinely wrong
  model id.
- `pyproject.toml` — `langgraph-checkpoint-sqlite` and `python-dotenv`, both
  newly required by upstream.

**Kept, contrary to expectation:**

- `dataflows/indstocks.py::_normalize_symbol()` — upstream's new
  `dataflows/symbol_utils.normalize_symbol()` looked like a replacement but
  normalises the *opposite* direction (it preserves `.NS` for Yahoo; we strip
  it for INDstocks). Tested, not assumed.

## Changes

### Phase 2: Intelligence Layer

#### 1. `tradingagents/graph/setup.py` — Multi-model tiering

**What:** Added per-role LLM assignment via an optional `llm_map` dict.

- `GraphSetup.__init__` now accepts `llm_map: Optional[Dict[str, Any]]`
- New `_get_llm(role, deep=False)` method looks up role in map, falls back to `_default`, then to the original `quick_thinking_llm`/`deep_thinking_llm`
- All `create_*_analyst`, `create_*_researcher`, `create_*_manager` calls use `self._get_llm("role_name")` instead of hard-coded LLM references

**Why:** Enables multi-model tiering (Gemini Flash for market/fundamentals, Grok for social, Perplexity for news, Claude Sonnet for risk/research) without changing agent code.

**Backward compatible:** Yes — when `llm_map` is `None` (default), behavior is identical to upstream.

---

#### 2. `tradingagents/graph/trading_graph.py` — Pass llm_map to GraphSetup

**What:** Reads `llm_map` from `self.config` and passes it to `GraphSetup` constructor.

**Lines changed:** ~3 lines in `setup_graph()`.

**Backward compatible:** Yes — if config has no `llm_map` key, `None` is passed and upstream behavior is preserved.

---

#### 3. `tradingagents/dataflows/indstocks.py` — NEW FILE (INDstocks data vendor)

**What:** Added `get_stock_data_indstocks()` as a drop-in vendor for fetching OHLCV data from the INDstocks API. Includes:

- `_run_async()` sync-to-async bridge (handles both no-loop and existing-loop scenarios)
- `_fetch_historical()` / `_fetch_quote()` async helpers
- CSV output format matching yfinance for zero-change agent compatibility

**Not a modification** — purely additive new file.

---

#### 4. `tradingagents/dataflows/interface.py` — Register INDstocks vendor

**What:**
- Added `from .indstocks import get_stock_data_indstocks`
- Added `"indstocks"` as first entry in `VENDOR_LIST`
- Registered `get_stock_data_indstocks` in `VENDOR_METHODS["get_stock_data"]`

**Why:** INDstocks is the primary data source for Indian equities. Position as first vendor gives it priority in the fallback chain.

**Backward compatible:** Yes — other vendors still present as fallbacks. If INDstocks token is missing, `route_to_vendor()` falls through to yfinance/alpha_vantage.

---

### Phase 2.5: Paper Trading Enablement

#### 5. `tradingagents/agents/utils/technical_indicators_tools.py` — Handle comma-separated indicators

**What:** Modified `get_indicators()` to split comma-separated indicator strings and fetch each separately.

**Why:** LLMs (Gemini 3 Flash) sometimes batch multiple indicators into a single tool call: `"close_50_sma,close_200_sma,rsi,macd"`. The original code passed this as-is, causing `ValueError: Indicator ... is not supported`.

**Lines changed:** Added ~10 lines of splitting/combining logic after the docstring.

**Backward compatible:** Yes — single indicators work unchanged. Comma-separated strings are transparently split and results combined.

---

#### 6. `tradingagents/dataflows/interface.py` — yfinance symbol suffix for non-US markets

**What:**
- Added `_SYMBOL_ARG_METHODS` frozenset listing methods where first arg is a symbol
- Added `_apply_yfinance_suffix()` helper that appends a configurable suffix (e.g., `.NS`)
- Modified `route_to_vendor()` to call `_apply_yfinance_suffix()` when routing to yfinance

**Why:** yfinance requires exchange suffixes for non-US markets (e.g., `RELIANCE.NS` for NSE India). Upstream passes bare symbols. Rather than modifying every yfinance function, the suffix is applied at the routing layer.

**Backward compatible:** Yes — suffix defaults to empty string (`""`) in `DEFAULT_CONFIG`, so no change for US markets.

---

#### 7. `tradingagents/default_config.py` — Added yfinance_symbol_suffix config key

**What:** Added `"yfinance_symbol_suffix": ""` to `DEFAULT_CONFIG`.

**Why:** Configurable per deployment — set to `.NS` for India, `.L` for London, etc.

**Backward compatible:** Yes — defaults to empty string (no suffix).

---

### Performance: Parallel Analyst Execution

#### 9. `tradingagents/agents/utils/agent_states.py` — State reducers for parallel fan-out

**What:** Replaced string-annotation `Annotated[Type, "description"]` with reducer-annotation `Annotated[Type, reducer_fn]` on all `AgentState` fields (except `messages` which already has `add_messages`).

Added three reducer functions:
- `_last_str(a, b)` — keeps latest non-empty string
- `_last_invest_state(a, b)` — keeps `InvestDebateState` with higher count
- `_last_risk_state(a, b)` — keeps `RiskDebateState` with higher count

**Why:** LangGraph's default `LastValue` channel throws `InvalidUpdateError` when multiple parallel branches converge (fan-in). Custom reducers enable safe state merging during parallel analyst execution.

**Backward compatible:** Yes — reducers are semantically identical to `LastValue` for sequential execution. Only takes effect when parallel branches merge.

---

#### 10. `tradingagents/graph/setup.py` — Parallel analyst fan-out/fan-in

**What:**
- Changed graph wiring from sequential analyst chain to parallel fan-out: `START → [all analysts simultaneously]`
- Replaced per-analyst `Msg Clear` nodes with no-op `_analyst_done` pass-throughs
- Added single `"Clear Analyst Messages"` node after the fan-in point
- Fan-in: all `Done *` nodes → `Clear Analyst Messages` → `Bull Researcher`

**Why:** All 4 analysts are completely independent (separate tools, separate state fields). Running them in parallel saves time proportional to the non-longest analyst phase. Measured ~18% improvement (4m 46s → 3m 55s).

**Backward compatible:** Yes — same graph semantics, same outputs. Analysts just run concurrently.

---

#### 11. `tradingagents/graph/conditional_logic.py` — Done node routing

**What:** Changed `should_continue_*` return values from `"Msg Clear X"` to `"Done X"` to match the new no-op Done nodes.

**Why:** Per-analyst `Msg Clear` nodes were replaced with `Done *` pass-throughs to avoid `RemoveMessage` conflicts during parallel execution (multiple branches trying to delete the same initial message ID).

**Backward compatible:** Yes — no semantic change. Just different node names in the graph.

---

### Bugfix: Symbol suffix stripping

#### 8. `tradingagents/dataflows/indstocks.py` — Strip `.NS`/`.BO` suffixes

**What:** Added `_normalize_symbol()` helper and call it at the top of `_resolve_scrip_code()`.

**Why:** LLM agents (trained on Yahoo Finance conventions) often generate `RELIANCE.NS` instead of bare `RELIANCE`. The yfinance suffix logic in `interface.py` only *adds* `.NS` for the yfinance vendor — it doesn't *remove* it for INDstocks. This caused `ValueError: Symbol 'RELIANCE.NS' not found in NSE instruments`.

**Backward compatible:** Yes — bare symbols pass through unchanged.

---

### Phase 3: Crypto-Specific Analyst Agents

Three new analysts (on-chain, DeFi/tokenomics, funding rates) that run alongside the 4 base analysts during crypto trades. Activated only when `asset_class == "crypto"`.

#### New Files (7) — all under `tradingagents/`

| # | File | Purpose |
|---|------|---------|
| 12 | `tradingagents/dataflows/crypto_onchain.py` | Blockchair + Blockchain.info data fetching (network stats, address activity) |
| 13 | `tradingagents/dataflows/crypto_defi.py` | DeFiLlama + CoinGecko data fetching (TVL, token fundamentals, chain overview) |
| 14 | `tradingagents/dataflows/crypto_funding.py` | Binance Futures data fetching (funding rates, open interest, long/short ratios) |
| 15 | `tradingagents/agents/utils/crypto_tools.py` | 8 `@tool`-decorated LangGraph functions wrapping the 3 dataflow modules |
| 16 | `tradingagents/agents/analysts/onchain_analyst.py` | On-chain analyst factory (`create_onchain_analyst`) — blockchain network health |
| 17 | `tradingagents/agents/analysts/defi_analyst.py` | DeFi analyst factory (`create_defi_analyst`) — TVL, supply dynamics, protocol metrics |
| 18 | `tradingagents/agents/analysts/funding_analyst.py` | Funding rate analyst factory (`create_funding_analyst`) — derivatives sentiment |

**Not modifications** — purely additive new files. All follow existing patterns (analyst factory pattern from `market_analyst.py`, dataflow pattern from `yfin_utils.py`).

#### Modified Files (15)

#### 19. `tradingagents/agents/utils/agent_states.py` — +3 crypto report state fields

**What:** Added 3 new fields to `AgentState`:
- `onchain_report: Annotated[str, _last_str]`
- `defi_report: Annotated[str, _last_str]`
- `funding_report: Annotated[str, _last_str]`

**Why:** Each crypto analyst writes its output to a dedicated state field, read by downstream debate consumers.

**Backward compatible:** Yes — uses existing `_last_str` reducer, defaults to `""` for equity trades.

---

#### 20. `tradingagents/graph/propagation.py` — +3 initial state fields

**What:** Added `"onchain_report": "", "defi_report": "", "funding_report": ""` to `create_initial_state()`.

**Backward compatible:** Yes — empty strings have no effect on equity debate prompts.

---

#### 21. `tradingagents/graph/setup.py` — +3 analyst registration blocks

**What:** Added 3 conditional registration blocks in `setup_graph()`:
```python
if "onchain" in selected_analysts:
    analyst_nodes["onchain"] = create_onchain_analyst(self._get_llm("onchain_analyst"))
    tool_nodes["onchain"] = self.tool_nodes["onchain"]
```
Same pattern for `"defi"` and `"funding"`.

**Why:** The existing dynamic loop automatically handles fan-out, tool loops, and fan-in for any analyst in `analyst_nodes` — no additional wiring code needed.

**Backward compatible:** Yes — when `selected_analysts` is the default 4, these blocks are skipped.

---

#### 22. `tradingagents/graph/conditional_logic.py` — +3 should_continue methods

**What:** Added `should_continue_onchain()`, `should_continue_defi()`, `should_continue_funding()` following the exact pattern of `should_continue_market()`.

**Backward compatible:** Yes — methods are only called if the corresponding analyst is registered.

---

#### 23. `tradingagents/graph/trading_graph.py` — +3 ToolNodes + imports

**What:**
- Imported 8 crypto tool functions from `tradingagents.agents.utils.crypto_tools`
- Added 3 `ToolNode` entries in `_create_tool_nodes()` for onchain, defi, funding
- Added 3 fields to `_log_state()` debug output

**Backward compatible:** Yes — ToolNodes are only registered if the analyst is selected.

---

#### 24. `tradingagents/agents/__init__.py` — +3 factory imports/exports

**What:** Added imports and `__all__` entries for `create_onchain_analyst`, `create_defi_analyst`, `create_funding_analyst`.

**Backward compatible:** Yes — additive exports only.

---

#### 25-33. Debate consumers (9 files) — Append crypto reports to prompts

**What:** All 9 downstream consumers now conditionally append crypto analyst reports when available:

```python
onchain_report = state.get("onchain_report", "")
defi_report = state.get("defi_report", "")
funding_report = state.get("funding_report", "")
crypto_section = ""
if onchain_report or defi_report or funding_report:
    crypto_section = (
        f"\n\nOn-Chain Network Analysis:\n{onchain_report}"
        f"\n\nDeFi/Tokenomics Analysis:\n{defi_report}"
        f"\n\nFunding Rate/Derivatives Analysis:\n{funding_report}"
    )
```

**Files modified:**
1. `tradingagents/agents/researchers/bull_researcher.py` — `curr_situation` + prompt injection
2. `tradingagents/agents/researchers/bear_researcher.py` — `curr_situation` + prompt injection
3. `tradingagents/agents/managers/research_manager.py` — `curr_situation` for memory lookup
4. `tradingagents/agents/trader/trader.py` — `curr_situation` for memory lookup
5. `tradingagents/agents/risk_mgmt/aggressive_debator.py` — prompt injection after fundamentals
6. `tradingagents/agents/risk_mgmt/conservative_debator.py` — prompt injection after fundamentals
7. `tradingagents/agents/risk_mgmt/neutral_debator.py` — prompt injection after fundamentals
8. `tradingagents/agents/managers/risk_manager.py` — `curr_situation` for memory lookup
9. `tradingagents/graph/reflection.py` — `_extract_current_situation()` helper

**Why:** Debate participants need all analyst data to make informed arguments. Crypto reports are only included when non-empty, so equity trades see identical prompts.

**Backward compatible:** Yes — `state.get("onchain_report", "")` returns empty string for equity trades, `crypto_section` stays empty, prompts unchanged.

---

### Confidence Scoring

#### 34. `tradingagents/agents/managers/risk_manager.py` — Add confidence score deliverable

**What:** Added a third deliverable to the risk manager prompt requesting a numeric confidence score in the format `CONFIDENCE: <0-100>` on the final line of the response.

**Why:** The risk manager's narrative output contained no structured confidence indicator, causing all trade signals to default to 50% confidence. The new format preserves the free-text narrative while appending a parseable score.

**Lines changed:** ~4 lines added to the prompt deliverables section.

**Backward compatible:** Yes — the prompt addition is purely additive. If the LLM ignores it, downstream parsing in `skopaq/graph/skopaq_graph.py` falls back to a debater-agreement heuristic (35–85 range) or the previous default of 50.
