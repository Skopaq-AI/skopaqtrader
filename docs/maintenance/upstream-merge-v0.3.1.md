# Upstream merge plan: TradingAgents v0.2.0 → v0.3.1

**Status:** planned, not started.
**Prepared:** 2026-08-19. **Risk:** medium-high — touches the live daemon path.

## Situation

| | |
|---|---|
| Vendored at | `upstream-v0.2.0` |
| Upstream now | `v0.3.1` |
| Commits behind | 167 total, **135** touching `tradingagents/` |
| Upstream diff | 71 files, **+5,464 / −1,325** |
| Our diff | 27 files, **+1,745 / −73** |
| **Collision files** | **19** |

## What we gain

- `feat(llm)`: **Claude Sonnet 5 and Fable 5** in the catalog
- `feat(llm)`: Amazon Bedrock as a first-class provider (+ `AWS_BEARER_TOKEN_BEDROCK` auth)
- `feat(llm)`: NVIDIA NIM, Kimi, Groq, Mistral providers
- `feat(llm)`: OpenAI-compatible providers unified behind a registry
- `feat(data)`: FRED macro indicators; Polymarket prediction markets
- `feat(reporting)`: report-tree writer shared between CLI and API
- `ci`: test/lint/smoke workflow; Python 3.12 recommended
- Assorted fixes: Alpha Vantage timeouts, ticker normalisation, news-prompt/tool alignment

## Two findings that shape the approach

**1. `llm_map` is NOT superseded — it stays.**
Upstream v0.3.1 still exposes only `deep_think_llm` / `quick_think_llm`. It added
*provider* breadth, not *per-agent role* routing. Our multi-model tiering remains
a genuine local capability, and must be re-applied — but it now has to sit on top
of the new provider registry rather than the old direct client construction.

**2. ~~Upstream's `symbol_utils.py` may let us delete code~~ — WRONG, tested
in Phase 2b.** Upstream's `normalize_symbol()` maps *to* canonical Yahoo
symbols; ours strips suffixes *for* INDstocks. Opposite directions:

    input          upstream            ours
    RELIANCE.NS    RELIANCE.NS         RELIANCE
    TCS.BO         TCS.BO              TCS

Our `_normalize_symbol()` is **not** superseded and must be kept. Recorded as a
correction rather than deleted, so nobody re-derives the same wrong hypothesis.

## Collision inventory

Fifteen of the nineteen are *the same mechanical change*: threading `llm_map`
into an agent factory (`ours 13+/1-` or `14+/2-` each). One decision, replicated.

| File | ours | theirs | Notes |
|---|---|---|---|
| `graph/trading_graph.py` | 30+/0− | **332+/87−** | **Highest risk.** Upstream largely rewrote it; our `llm_map` read may have no home |
| `graph/setup.py` | 79+/36− | 82+/128− | Parallel analysts + crypto agents vs upstream restructure |
| `dataflows/interface.py` | 37+/2− | 130+/30− | INDstocks routing + suffix handling |
| `graph/reflection.py` | 13+/1− | 42+/**106−** | Upstream deleted heavily — check the file still exists in shape |
| `agents/managers/risk_manager.py` | 19+/1− | 0+/**66−** | Pure deletion upstream — likely moved/refactored |
| `agents/utils/agent_states.py` | 43+/19− | 8+/8− | Our confidence-scoring fields |
| `graph/conditional_logic.py` | 28+/4− | 9+/3− | Our parallel-execution logic |
| `default_config.py` | 4+/0− | 141+/11− | Mostly additive upstream |
| `agents/__init__.py` | 6+/0− | 11+/14− | Export list |
| `llm_clients/validators.py` | 3+/0− | 15+/64− | Upstream simplified |
| 9 × agent factories | ~13+/1− each | small | Mechanical `llm_map` threading |

Files we *added* (`indstocks.py`, crypto analysts, etc.) do not collide.

## Phased plan

### Phase 0 — Safety net (30 min)
1. `git checkout -b upstream/v0.3.1-merge`
2. Record a **behavioural baseline**, not just tests:
   - `python -m pytest tests/unit/ -q` → expect **566 passed**
   - `skopaq daemon --once --paper` → save the full report tree
   - `skopaq analyze RELIANCE` → save output
3. Tag the pre-merge state: `git tag pre-upstream-v0.3.1`

**Rollback at any point:** `git checkout main` — the live daemon runs from `main`.

### Phase 1 — Re-vendor clean (1 hr)
Do **not** `git merge`. Re-vendor wholesale, then re-apply our changes deliberately:

```bash
git rm -r --cached tradingagents/
rm -rf tradingagents/
git checkout upstream/main -- tradingagents/
git tag -f upstream-v0.3.1 upstream/main
```

Rationale: our diff is 96% additive (+1,745 / −73). Re-applying additions onto a
clean base is far more tractable than resolving 19 three-way conflicts, and it
forces an explicit decision on every change rather than letting `git` guess.

### Phase 2 — Re-apply by category, not by file (3–4 hrs)

Order matters — each step is independently testable.

| Step | Change | Files | Risk |
|---|---|---|---|
| 2a | INDstocks vendor + crypto analysts (new files) | ~8 | **low** — no collision |
| 2b | `symbol_utils` evaluation — delete our `_normalize_symbol()` if covered | 1 | low |
| 2c | `agent_states.py` confidence fields | 1 | low |
| 2d | `llm_map` threading into agent factories | 15 | **low, tedious** — mechanical |
| 2e | `trading_graph.py` — wire `llm_map` onto the new provider registry | 1 | **HIGH** |
| 2f | `setup.py` + `conditional_logic.py` — parallel analysts, crypto routing | 2 | **HIGH** |
| 2g | `interface.py` — INDstocks routing | 1 | medium |
| 2h | `reflection.py`, `risk_manager.py` — reconcile against upstream deletions | 2 | medium |

Commit after each step so a bad one reverts alone.

### Phase 3 — Verify (1–2 hrs)
1. `pytest tests/unit/ -q` → must be **≥566 passed**
2. `pytest tests/integration/ -v -m integration` (needs `.env`)
3. `skopaq analyze RELIANCE` — diff against the Phase-0 baseline
4. **`skopaq daemon --once --paper`** — the real acceptance test; compare the
   report tree against baseline. Every FSM state must be reached:
   `PRE_OPEN → SCANNING → ANALYZING → TRADING → MONITORING → CLOSING → REPORTING`
5. Confirm per-agent model routing is live — check logs show Gemini 3 Flash for
   analysts, Grok for social, Claude Opus for the two judge roles. **A silent
   fallback to `quick_think_llm` everywhere is the most likely failure mode and
   the tests will not catch it.**

### Phase 4 — Land (30 min)
1. Rewrite `UPSTREAM_CHANGES.md`: new base tag, drop anything upstream absorbed
2. Update `CLAUDE.md` — it says "vendored TradingAgents v0.2.0"
3. Update `mkdocs.yml` / docs if provider lists are documented
4. Merge to `main` **only after** a clean paper daemon run

**Estimate: 6–8 focused hours.** Not a background task.

## Top risks

1. **Silent LLM-routing regression.** If `llm_map` fails to thread through the new
   registry, every agent quietly falls back to one model. Cost and quality both
   change; no test fails. → Phase 3.5 exists specifically for this.
2. **`trading_graph.py` rewrite.** 332 upstream insertions. Our 30 lines may need
   redesigning, not porting.
3. **Live daemon.** Railway cron fires 09:10 IST weekdays. Merge outside market
   hours; keep `main` deployable throughout.
4. **Python version.** Upstream now recommends 3.12; we run 3.14. Verify the new
   provider SDKs have 3.14 wheels before assuming parity.

## Decision checkpoint

If Phase 2e/2f prove harder than a day's work, a defensible fallback is to take
**only the LLM catalog updates** (Sonnet 5, Fable 5, Bedrock) by cherry-picking
`llm_clients/`, and defer the graph restructure. That captures most of the
practical value at a fraction of the risk.

---

## Phase 0 executed — 2026-08-20

| Artifact | Value |
|---|---|
| Tag | `pre-upstream-v0.3.1` at `4f6dd89` |
| Branch | `upstream/v0.3.1-merge` (from `main`) |
| Unit-test baseline | **540 passed** |
| LLM routing baseline | `docs/maintenance/baseline/llm_routing.txt` — 15 roles |
| Graph shape baseline | `docs/maintenance/baseline/graph_shape.txt` |

**Correction to Phase 0 above:** the expected count is **540**, not 566. The
566 figure includes `skopaq/risk/volatility.py` tests, which live on
`feat/volatility-forecasting` — a separate workstream not on this branch.

### The routing baseline is the important artifact

```
role                      client                              model
_default                  NormalizedChatGoogleGenerativeAI    gemini-3-flash-preview
chat_brain                ChatAnthropic                       claude-opus-4-6
research_manager          ChatAnthropic                       claude-opus-4-6
risk_manager              ChatAnthropic                       claude-opus-4-6
social_analyst            UnifiedChatOpenAI                   x-ai/grok-3-mini
<10 others>               NormalizedChatGoogleGenerativeAI    gemini-3-flash-preview
                                                              roles mapped: 15
```

Regenerate this after every Phase-2 step and diff it. **If `roles mapped` drops
to 1, `llm_map` has broken and every agent is silently running on `_default`.**

That failure was observed for real during Phase 0: a capture run without `.env`
loaded printed `No LLM API keys available — build_llm_map returning empty
_default` and mapped 1 role instead of 15. It is a genuine, quiet, easily-missed
regression, and it is why this baseline exists. Reproduce with:

```bash
python3 -c "
from dotenv import load_dotenv; load_dotenv('.env')
from skopaq.llm.env_bridge import bridge_env_vars; bridge_env_vars()
from skopaq.llm import build_llm_map
m = build_llm_map(); print(f'roles mapped: {len(m)}')
for r in sorted(m): print(r, getattr(m[r],'model_name',None) or getattr(m[r],'model','?'))
"
```

### BLOCKER: behavioural baseline not captured

`skopaq status` reports **`Token ✗ INVALID — Token EXPIRED`**, so the two runtime
baselines in Phase 0 could not be taken:

- `skopaq daemon --once --paper` (the real acceptance test)
- `skopaq analyze RELIANCE`

**Phase 3 cannot be completed without these.** Regenerate the INDstocks token
from their dashboard, then capture both against `pre-upstream-v0.3.1` *before*
starting Phase 1 — a post-merge run with nothing to compare against proves
nothing.

Also noted: `skopaq status` shows **Mode: LIVE**. Use explicit `--paper` on every
baseline and verification run.

---

## Phase 1 executed — 2026-08-20

Re-vendored `tradingagents/` at `upstream/main`; tagged `upstream-v0.3.1`.
Our pre-merge tree and a 2,292-line patch of our changes were preserved first.

    files      56 -> 71
    diff       69 files changed, +5,496 / -1,548

**This branch is intentionally broken until Phase 2 completes.** All 27 of our
modifications are gone by design — re-applying them deliberately is the point.

### Measured breakage

| Check | Result |
|---|---|
| Unit tests | **3 collection errors** (crypto dataflow modules we added, now absent) |
| `build_llm_map()` | still 15 roles — **misleading, see below** |
| `llm_map` threading into agent factories | **0 present, 5 missing** |
| Our files removed by the re-vendor | 10 |

### The routing check from Phase 0 was insufficient

`build_llm_map()` lives in `skopaq/llm/`, not `tradingagents/`, so it kept
returning 15 roles even with every agent-side modification stripped out. It
verifies the map is *built*, not that it is *threaded*. Use this instead — it
inspects the factory signatures that actually consume it:

```bash
python3 -c "
import inspect, importlib
targets = [('tradingagents.agents.trader.trader','create_trader'),
           ('tradingagents.agents.managers.research_manager','create_research_manager'),
           ('tradingagents.agents.researchers.bull_researcher','create_bull_researcher')]
for mod, fn in targets:
    f = getattr(importlib.import_module(mod), fn)
    p = list(inspect.signature(f).parameters)
    print(('OK  ' if 'llm_map' in p else 'GONE'), fn, p)
"
```

### Structural changes decoded

Two upstream edits that the diff stats made look alarming:

- **`agents/managers/risk_manager.py` → `agents/managers/portfolio_manager.py`.**
  A rename, not a deletion — that is the `theirs 0+/66-` line in the collision
  table. `create_risk_manager` is now **`create_portfolio_manager`**.
- **Risk debators renamed to match their filenames**:
  `create_aggressive_debator` / `create_conservative_debator` /
  `create_neutral_debator`.

Also noted: upstream's model catalog warns
`Model 'claude-opus-4-6' is not in the known model list for provider 'anthropic'`.
Non-fatal ("Continuing anyway"), but our judge roles depend on it — check
whether the catalog needs an entry in Phase 2.

### Phase 2a worklist — our files to restore

```
tradingagents/dataflows/indstocks.py
tradingagents/dataflows/crypto_onchain.py
tradingagents/dataflows/crypto_funding.py
tradingagents/dataflows/crypto_defi.py
tradingagents/agents/analysts/onchain_analyst.py
tradingagents/agents/analysts/funding_analyst.py
tradingagents/agents/analysts/defi_analyst.py
tradingagents/agents/utils/crypto_tools.py
tradingagents/llm_clients/TODO.md
```

`agents/managers/risk_manager.py` also shows as deleted, but that one is
upstream's rename — do **not** restore it; retarget our callers onto
`create_portfolio_manager` instead.

---

## Phases 2 and 4 executed — 2026-08-21

All of Phase 2 landed; Phase 4 docs updated. **546 unit tests passing**
(540 baseline + 6 new suffix guards), 0 model warnings.

| Step | Outcome | Commit |
|---|---|---|
| 2a | 8 of our files restored; all import against v0.3.1 | `8e45bc3` |
| 2b | `_normalize_symbol` KEPT — not superseded (tested) | `8e45bc3` |
| 2c | AgentState reducers merged; 18 fields, 18 reducers | `4b293b4` |
| 2d | Crypto reports → one shared helper, 5 call sites | `4b293b4` |
| 2e | Crypto graph wiring + 2 new upstream deps | `11e8495` |
| 2g | INDstocks vendor registration + suffix guards | `8d9742e` |
| 2f | `llm_map` routing + parallel analyst fan-out | `4eb8784`, `b246db3` |
| 2h | Audit sweep; models registered, 1 patch dropped | `3651365` |
| 4 | `UPSTREAM_CHANGES.md`, `CLAUDE.md` | this commit |

### Corrections this merge forced on the plan

The plan was wrong in three places, each caught by measurement rather than
review. Recorded because the pattern matters more than the specifics: **every
one was an assumption that a green test suite would have carried through.**

1. **"15 collision files are mechanical `llm_map` changes."** False. `llm_map`
   appears in exactly two files. The other 13 were crypto report injection, and
   the Phase-1 check for `llm_map` in `create_trader`'s signature was therefore
   testing something that never existed.
2. **"`build_llm_map()` returning 15 roles proves routing works."** False. It
   lives in `skopaq/llm/` and kept returning 15 with every agent-side change
   stripped out. It proves the map is *built*, not *consumed*.
3. **"`symbol_utils.py` may let us delete `_normalize_symbol`."** False — it
   normalises the opposite direction.

### Three silent failures the test suite did not catch

- **`yfinance_symbol_suffix` dropped from `default_config.py`.** The suffix
  helper degraded to a no-op; every yfinance fallback would have fetched
  `RELIANCE` (a US ticker) instead of `RELIANCE.NS`. All 540 tests passed.
  Now guarded by `TestYfinanceSuffix`.
- **Parallel analyst fan-out missing.** Suite passed at 546 both with and
  without it — a 4x slowdown with byte-identical output. No test covers graph
  topology; verified by compiling the graph and inspecting edges instead.
- **Unregistered model ids.** Every run logged "not in the known model list",
  which is exactly what a genuinely wrong model id would look like.

## Remaining before merging to main

**Phase 3 is NOT complete.** Two runtime baselines were never captured, because
the INDstocks token was expired during Phase 0:

```bash
git checkout pre-upstream-v0.3.1
skopaq analyze RELIANCE --paper       # save
skopaq daemon --once --paper          # save the full report tree
git checkout upstream/v0.3.1-merge
# re-run both, diff against the baselines
```

`skopaq status` reports **Mode: LIVE** — pass `--paper` explicitly every time.

These cost real LLM credits, which is why they were left for a human decision
rather than run unattended. Until they pass, what is verified is: unit tests,
per-role routing, graph topology, and vendor registration — everything except
an actual end-to-end trading session.

**Do not merge to `main` until the paper daemon run is clean.**
