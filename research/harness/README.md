# NSE Forecast Validation Harness

Answers one question before any integration work happens:

> **Does a candidate model actually forecast Indian equities better than
> assuming nothing changes?**

Nothing in here touches the trading pipeline, places orders, or needs broker
credentials.

## Verdicts so far

| Target | Model | Result |
|---|---|---|
| Daily price | Kronos-base zero-shot | MASE 2.846 — **rejected** |
| Daily price | Kronos-base fine-tuned on NSE | MASE 1.344 — **rejected** |
| Daily price direction | Kronos fine-tuned | 0.4941, CI [0.4729, 0.5147] — **chance** |
| **Realized volatility** | **Log-HAR** | **QLIKE 0.542x random walk — works** |

Kronos is retired: its venv, vendored source, fine-tuned checkpoint and HF
weights were deleted (1.68 GB). `results/` retains the evidence. The harness
itself is model-agnostic and is what carried over.

---

## Data provenance & compliance

**Source: NSE's own public UDiFF Bhavcopy** — the exchange's authoritative
end-of-day publication.

| | |
|---|---|
| **Cost** | Free, no account, no API key |
| **Permitted** | Analysis, research, backtesting — NSE's [Data Usage & Sharing Policy](https://www.nseindia.com/static/market-data/nse-data-policy) |
| **Restricted** | **Redistribution.** Ownership of the data remains with NSE/NSE Data at all times |
| **Our posture** | Cache is local-only and gitignored (`**/data_cache/`). Nothing is ever committed or republished |

**Politeness is built in, not optional.** `download_range()` sleeps ~0.35–0.6s
between requests with jitter, caches every response, and writes a `.holiday`
marker on 404 so a re-run never re-asks for a non-trading day. Please do not
lower the delay — sustained scraping is how public data access gets withdrawn
for everyone.

### Why not yfinance
The repo already depends on it, but it is the wrong tool *here*:
- Yahoo's terms do not permit automated extraction; `yfinance` uses
  undocumented endpoints.
- More importantly, its NSE coverage has known split-adjustment and volume
  defects. Benchmarking a forecaster on defective data produces a confident,
  wrong verdict — the worst possible outcome for this exercise.

### Why not INDstocks (for this)
Perfectly legitimate — it is your entitled broker feed and consumption is its
intended use. Two practical reasons it is not the default: the token expires
(it was expired when this was built), and bhavcopy gives the **entire
2,400-symbol EQ universe** per request rather than one symbol at a time.

### Corporate actions
Bhavcopy prices are **unadjusted**. A 1:2 split reads as a 50% overnight crash
and would score every forecaster as catastrophically wrong on a quiet day.

`adjust_for_actions()` fixes this using NSE's own restated `PrvsClsgPric`:

```
factor[t] = prev_close[t] / close[t-1]      # 0.5 on a 1:2 split
```

The exchange publishes the adjustment itself on the ex-date, so no separate
corporate-actions feed is needed and no guessing is involved.

### Not covered by backtesting, but adjacent
SEBI's retail algo-trading framework became **fully mandatory 1 April 2026**:
exchange-assigned Algo-IDs on every algorithmic order, orders only from a
**static IP registered with your broker**, broker as legal principal. There is
an exemption below **10 orders/second**. This harness places no orders and
contacts no exchange, so it is entirely out of scope — but it is worth a direct
question to INDstocks about the live path.

---

## Why two virtualenvs

This is the constraint that shapes everything:

| | Main repo env | Kronos env |
|---|---|---|
| Python | **3.14.2** | **3.12** |
| pandas | **3.0.1** | **2.2.2** (Kronos pins it) |
| torch | absent | ~445 MB (+2.7 GB CUDA libs unless CPU index used) |

They cannot merge. torch has no 3.14 wheels, and pandas 3.0 → 2.2.2 is a major
downgrade that would break the trading code.

So the harness splits at a **CSV boundary**, which crosses venvs where a live
DataFrame cannot:

```
  main env (3.14)                    kronos env (3.12)
  ───────────────                    ─────────────────
  fetch  → data_cache/nse_panel/*.csv ──→ run --kronos
  run    (baselines only)                 (same CSVs, same origins)
```

Everything except `kronos_runner.py` runs in your existing environment today.

---

## Usage

### 1. Fetch data (main env, no extra installs)

```bash
python research/kronos/validate.py fetch --years 3 --symbols 50
```

Downloads ~750 bhavcopy files (~10 min, resumable), adjusts for corporate
actions, picks the top 50 by median turnover with a 95% coverage floor, and
writes one Kronos-shaped OHLCV CSV per symbol.

### 2. Baseline run (main env)

```bash
python research/kronos/validate.py run --horizon 5 --context 250
```

Establishes the bar. **Read this number before installing torch** — if the
bootstrap baseline is already at chance, that tells you something about daily
NSE predictability that no model will overturn.

### 3. Kronos run (isolated env)

```bash
python3.12 -m venv .venv-kronos
.venv-kronos/bin/pip install torch          # macOS arm64: MPS build, no CUDA payload
.venv-kronos/bin/pip install numpy "pandas==2.2.2" "einops==0.8.1" \
    "huggingface_hub==0.33.1" "matplotlib==3.9.3" "tqdm==4.67.1" "safetensors==0.6.2"
git clone https://github.com/shiyu-coder/Kronos research/kronos/vendor/Kronos

.venv-kronos/bin/python research/kronos/validate.py run \
    --horizon 5 --context 512 --samples 16 --step 11 \
    --kronos --device mps --batch-size 16
```

**On Linux/CUDA hosts** add `--index-url https://download.pytorch.org/whl/cpu`
to the torch install to skip ~2.7 GB of `nvidia/*` packages. On macOS arm64
that flag is unnecessary and counter-productive: the default wheel is already
CUDA-free and is the one that carries MPS support.

Weights download on first use (~5 GB into `~/.cache/huggingface`, ~2 min).
Model sizes: `Kronos-mini` (4.1M params, 2048 context), `Kronos-small`
(24.7M, 512), `Kronos-base` (102.3M, 512).

### Measured cost (Apple M4 Pro, MPS, horizon 5)

| | context 512 | context 256 |
|---|---|---|
| 16 samples | 3.8 s/origin | ~1.8 s/origin |
| 32 samples | 7.8 s/origin | 3.6 s/origin |
| 64 samples | 15.7 s/origin | — |

**Batching is not free here** — cost scales linearly in sample count, so MPS is
already saturated at batch 16. Buy statistical power with *origins*, not
samples. Halving the context is the only cheap 2x available.

### The `sample_count` trap

`KronosPredictor.predict(..., sample_count=N)` generates N stochastic paths and
then runs `preds = np.mean(preds, axis=1)` before returning. **The public API
hands back a point forecast; the ensemble is averaged away.** Scoring that
distributionally would give Kronos a zero-width interval — CRPS collapsing to
MAE and coverage to ~0 — silently, with no error raised.

`kronos_runner.py` works around this without patching vendored source: each
batch row in `auto_regressive_inference` is sampled independently
(`torch.multinomial` over `(B, vocab)`), so submitting the *same* history N
times through `predict_batch` with `sample_count=1` returns N genuinely
independent trajectories. Verified empirically: 16/16 unique paths.

---

## Reading the output

```
forecaster      dir_acc   dir_n    MASE      CRPS   cov80    circ
naive            0.5012    4821  1.0000   18.4213  0.0000  0.0000
bootstrap        0.4987    4821  1.0140   17.9902  0.7913  0.0021
kronos           ...
```

| Column | Meaning | Bar to clear |
|---|---|---|
| `dir_acc` | Direction called right, on moves ≥0.2% | **> 0.50** (0.50 = coin flip) |
| `MASE` | Error ÷ naive's error | **< 1.00** |
| `CRPS` | Distributional error, rupees | lower than baselines |
| `cov80` | Realised coverage of the 80% interval | **≈ 0.80** = calibrated |
| `circ` | Samples implying a move the stock has never made | **≈ 0** |

`circ` is the NSE-specific tell. Kronos was trained on global exchange data;
Indian equities have hard price bands (5/10/20%) that most markets lack. A
non-trivial `circ` means the model has not internalised this market's
microstructure — regardless of how its averaged error looks.

**Beating naive on MASE alone is not sufficient.** A model can win on MASE by
shrinking toward the mean while being directionally useless. `dir_acc > 0.50`
and `MASE < 1.00` together, consistently across horizons, is the real bar.

---

## Measured baselines (recorded 2026-08-19)

Real NSE data. 50 most-liquid symbols, 648 trading days (2024-01-01 → 2026-08-18),
context 512, 128 samples, ~2,000 forecast origins per horizon.

| horizon | forecaster | dir_acc | MASE | CRPS | cov80 | circ |
|---|---|---|---|---|---|---|
| 1d | naive | — | **1.000** | 34.08 | — | 0.000 |
| 1d | drift | 0.517 | 1.007 | 26.73 | 0.779 | 0.001 |
| 1d | bootstrap | 0.493 | 1.005 | 25.30 | 0.710 | 0.000 |
| 5d | naive | — | **1.000** | 77.69 | — | 0.000 |
| 5d | drift | 0.481 | 1.043 | 61.80 | 0.779 | 0.008 |
| 5d | bootstrap | 0.510 | 1.007 | 58.03 | 0.729 | 0.002 |
| 20d | naive | — | **1.000** | 163.38 | — | 0.000 |
| 20d | drift | 0.475 | 1.083 | 131.68 | 0.740 | 0.059 |
| 20d | bootstrap | 0.482 | 1.053 | 125.27 | 0.701 | 0.049 |

**This is the bar Kronos must clear: MASE < 1.000 and dir_acc > 0.50, together.**

Reading these:

- **Nothing beats naive on MASE at any horizon.** Directional accuracy sits at
  0.475–0.517 — a coin flip. This is the expected result on daily equity data
  and it is why the harness exists: it establishes that "beats naive" is a real
  achievement here, not a formality.
- **The MASE gap widens with horizon** (1.005 → 1.053 for bootstrap). Longer
  horizons give a model more rope, and every baseline hangs itself with it.
- **CRPS is lower for drift/bootstrap than naive, and that is not a win.**
  Naive emits a zero-width distribution, so its CRPS degenerates to plain MAE
  and is not comparable. Compare CRPS *between distributional* forecasters
  only — Kronos vs bootstrap, never Kronos vs naive.
- **cov80 ≈ 0.70–0.78 against a 0.80 target** means the baselines are mildly
  overconfident. If Kronos lands materially below 0.70 its intervals are not
  trustworthy regardless of its point accuracy.
- **`circ` rises with horizon** (0.000 → 0.059). Baselines that know nothing
  about NSE price bands start implying impossible moves as the horizon grows.
  Kronos scoring materially *worse* than ~0.05 at 20d is the signature of a
  model that never saw this market.

### Data horizon limit
NSE's UDiFF bhavcopy archive begins at **2024-01-01** — all 95 weekdays tested
in 2023 returned 404. Roughly **2.6 years** is the maximum obtainable via this
route. For longer history you would need INDstocks (token permitting) or a
licensed vendor.

## Files

| File | Runs in | Purpose |
|---|---|---|
| `nse_data.py` | main | Bhavcopy download, parse, corporate-action adjustment, panel build |
| `universe.py` | main | Liquidity + coverage screen (no hand-picked symbols) |
| `forecasters.py` | main | `Forecaster` protocol + naive / drift / block-bootstrap baselines |
| `metrics.py` | main | CRPS, MASE, directional accuracy, calibration, circuit diagnostic |
| `validate.py` | main | Walk-forward driver + CLI |
| `kronos_runner.py` | **kronos venv** | Lazy-torch Kronos adapter |

`kronos_runner._to_samples()` carries an explicit **UNVERIFIED** note: the exact
return shape of `KronosPredictor.predict` was never executed here (torch and
huggingface.co were both unreachable at authoring time). It accepts several
plausible shapes and raises loudly otherwise, rather than silently mis-scoring.
Confirm on first real run.

---

## Volatility results (recorded 2026-08-19)

Garman-Klass realized variance, 50 symbols, 648 daily bars, horizon 1,
non-overlapping origins. **124,250 rows across 497 independent date clusters.**

| model | QLIKE | QL ratio | MASE | dir_acc (noisy ref) | dir_acc (smoothed ref) |
|---|---|---|---|---|---|
| **log-har** | **0.3092** | **0.5420** | 0.8983 | 0.6370 | 0.5268 |
| ewma (λ=0.94) | 0.3176 | 0.5568 | 0.8526 | 0.6788 | 0.5685 |
| ma22 | 0.3317 | 0.5814 | 0.8668 | 0.6799 | 0.5746 |
| har (levels) | blown up | — | 0.9520 | 0.6402 | 0.5334 |
| rw (control) | 0.5704 | 1.0000 | 1.0000 | 0.5097 | 0.5612 |

Cluster-bootstrap, 497 clusters:

```
log-har  QLIKE reduction vs rw: +0.26127  CI [+0.23108, +0.29774]  BEATS random walk
ewma     QLIKE reduction vs rw: +0.25279  CI [+0.22248, +0.29038]  BEATS random walk
ma22     QLIKE reduction vs rw: +0.23878  CI [+0.20844, +0.27579]  BEATS random walk
log-har  QLIKE reduction vs ewma: +0.00848  CI [+0.00176, +0.01549]  BETTER (barely)
```

### Three caveats that matter

**1. The directional number is inflated by the reference, not by skill.**
Scoring "will vol rise or fall" against *today's* RV gives 0.68 — but a single
day's RV is a noisy estimate, so a smoothed forecast beats it largely by
reverting that noise. Re-scored against a trailing 5-day reference the same
models fall to **0.53–0.57**, and the random walk itself scores 0.5612. The
genuine directional edge over the control is 1–2 points, not 17.

**2. Log-HAR beats EWMA by almost nothing.** +0.008 QLIKE on a 0.31 base — a
one-line exponential average captures ~97% of what the regression delivers.
This reproduces the [2026 TSFM volatility survey](https://arxiv.org/abs/2607.05291)
finding that classical benchmarks stay competitive.

**3. Level-HAR is unusable under QLIKE.** Exactly **1 row of 24,850** produced a
non-positive variance forecast, and QLIKE → ∞ as F → 0, so that single row
dominates the mean. Its *median* QLIKE ratio is 1.0158 — merely on par with the
random walk. This is a real property of level-OLS on positive skewed data, not
an implementation bug, and it is precisely why log-HAR is the standard.

### What is trustworthy here

The QLIKE result: **log-HAR roughly halves random-walk loss**, on 497
independent date clusters, with a CI nowhere near zero. That is a genuine,
well-powered finding — and the contrast with the price work is the whole point:
identical harness, identical data, price direction landed at 0.4941 while
volatility loss halved.

## Files

| File | Purpose |
|---|---|
| `nse_data.py` | Bhavcopy download, parse, corporate-action adjustment, panel build |
| `universe.py` | Liquidity + coverage screen |
| `forecasters.py` | Price baselines: naive / drift / block-bootstrap |
| `har.py` | RV estimators (close-to-close, Parkinson, Garman-Klass, Rogers-Satchell) + HAR models |
| `metrics.py` | CRPS, MASE, directional accuracy, calibration, circuit diagnostic |
| `significance.py` | Vectorised date-clustered bootstrap, stability, breadth |
| `validate.py` | Price walk-forward + CLI |
| `validate_vol.py` | Volatility walk-forward + QLIKE + CLI |
| `test_harness.py` | 39 tests |
