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

**2. Upstream added `dataflows/symbol_utils.py` — we may be able to delete code.**
Our documented change #8 (`_normalize_symbol()` stripping `.NS`/`.BO` in
`indstocks.py`) may be fully covered by upstream's normalizer plus
`fix(data): normalize ticker on the news path`. **Evaluate before re-applying;
deleting a local patch is better than porting it.**

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
