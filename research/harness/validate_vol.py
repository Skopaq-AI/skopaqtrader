"""Walk-forward validation of realized-volatility forecasts on NSE.

Same discipline as the price harness — identical origins for every model, no
lookahead, a naive control as the denominator, date-clustered inference — with
two changes that matter for volatility.

QLIKE instead of MSE
    Realized variance is a *noisy proxy* for latent volatility, not the thing
    itself.  Under MSE a model is rewarded for chasing that proxy noise, and
    the ranking becomes an artefact of the estimator.  QLIKE (Patton 2011),

        L = RV/F - ln(RV/F) - 1

    is one of the few losses whose ranking is unchanged by proxy noise.  It is
    also asymmetric in the right direction for a trader: under-forecasting
    volatility is penalised much harder than over-forecasting it, which is the
    correct risk posture.

No pretraining, so no train/test cutoff
    HAR refits by OLS at every origin on past data only.  Nothing is learned
    across origins, so the test window is limited only by the lag structure —
    about 490 usable dates per symbol instead of the 194 the Kronos runs had.

Usage::

    python research/harness/validate_vol.py --horizon 1 --symbols 50
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import significance as sig            # noqa: E402
from har import VOL_MODELS, annualize, realized_variance  # noqa: E402

logger = logging.getLogger("har.validate")

PANEL_DIR = Path("data_cache/nse_panel_train")
RESULTS_DIR = Path("research/harness/results")


def qlike(actual: np.ndarray, pred: np.ndarray) -> np.ndarray:
    """Patton's QLIKE loss. Both inputs are variances and must be positive."""
    a = np.maximum(np.asarray(actual, dtype=float), 1e-12)
    f = np.maximum(np.asarray(pred, dtype=float), 1e-12)
    r = a / f
    return r - np.log(r) - 1.0


def walk_forward_vol(
    panel: dict[str, pd.DataFrame],
    models: list,
    horizon: int,
    estimator: str,
    warmup: int,
    step: int,
    seed: int = 42,
) -> pd.DataFrame:
    """Score every model on identical origins. Returns per-origin rows."""
    records: list[dict] = []
    t0 = time.time()

    for si, (symbol, df) in enumerate(panel.items(), 1):
        rv = realized_variance(df, estimator)
        good = np.isfinite(rv)
        if good.sum() < warmup + horizon + 10:
            continue
        dates = pd.to_datetime(df["timestamps"]).dt.date.to_numpy()
        n = len(rv)

        for origin in range(warmup, n - horizon, step):
            hist = rv[: origin + 1]
            if not np.all(np.isfinite(hist[-22:])):
                continue
            actual = float(np.mean(rv[origin + 1 : origin + 1 + horizon]))
            if not np.isfinite(actual):
                continue
            current = float(hist[-1])

            for m in models:
                try:
                    pred = float(m.forecast(hist, horizon))
                except Exception as exc:                      # noqa: BLE001
                    logger.debug("%s failed on %s@%d: %s", m.name, symbol, origin, exc)
                    continue
                if not np.isfinite(pred) or pred <= 0:
                    continue

                # Direction = will volatility rise or fall from here?
                pred_up = pred > current
                true_up = actual > current
                records.append({
                    "symbol": symbol,
                    "date": dates[origin],
                    "forecaster": m.name,
                    "origin_px": current,
                    "actual_px": actual,
                    "pred_px": pred,
                    "abs_err": abs(pred - actual),
                    "qlike": float(qlike(actual, pred)),
                    "log_sq_err": float((np.log(pred) - np.log(actual)) ** 2),
                    "crps": abs(pred - actual),   # point models: CRPS == MAE
                    "in80": False,
                    "scored_dir": True,
                    "dir_correct": bool(pred_up == true_up),
                    "spread_pct": 0.0,
                    "pred_vol_ann": float(annualize(pred)),
                    "actual_vol_ann": float(annualize(actual)),
                })

        if si % 10 == 0:
            logger.info("  %d/%d symbols | %d rows | %.0fs",
                        si, len(panel), len(records), time.time() - t0)

    return pd.DataFrame(records)


def summarise(po: pd.DataFrame, horizon: int) -> str:
    rw = po[po.forecaster == "rw"]
    rw_qlike = rw["qlike"].mean() if not rw.empty else np.nan
    rw_mae = rw["abs_err"].mean() if not rw.empty else np.nan

    lines = ["", f"  Volatility walk-forward — horizon {horizon} trading day(s)",
             "  " + "─" * 68,
             f"  {'model':<10}{'QLIKE':>10}{'QL ratio':>10}{'MASE':>9}"
             f"{'logMSE':>9}{'dir_acc':>9}{'n':>8}",
             "  " + "─" * 68]
    for name, g in po.groupby("forecaster"):
        ql = g["qlike"].mean()
        lines.append(
            f"  {name:<10}{ql:>10.4f}{ql / rw_qlike:>10.4f}"
            f"{g['abs_err'].mean() / rw_mae:>9.4f}"
            f"{g['log_sq_err'].mean():>9.4f}"
            f"{g['dir_correct'].mean():>9.4f}{len(g):>8}"
        )
    lines += ["  " + "─" * 68, "",
              "  QLIKE     Patton loss, robust to RV being a noisy proxy. Lower better.",
              "  QL ratio  QLIKE / random-walk's. < 1.00 beats 'volatility stays put'.",
              "  MASE      mean abs error / random-walk's.",
              "  dir_acc   fraction of times the up/down move in vol was called right.",
              ""]
    return "\n".join(lines)


def main() -> int:
    logging.basicConfig(level=logging.INFO, datefmt="%H:%M:%S",
                        format="%(asctime)s %(levelname)-7s %(message)s")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--horizon", type=int, default=1)
    p.add_argument("--estimator", default="garman_klass",
                   choices=["close_to_close", "parkinson",
                            "garman_klass", "rogers_satchell"])
    p.add_argument("--warmup", type=int, default=150,
                   help="bars of history before the first origin")
    p.add_argument("--step", type=int, default=0,
                   help="0 = use horizon (non-overlapping windows)")
    p.add_argument("--symbols", type=int, default=50)
    p.add_argument("--tag", default="")
    args = p.parse_args()

    step = args.step or args.horizon      # non-overlapping keeps clusters honest

    files = sorted(PANEL_DIR.glob("*.csv"))
    if not files:
        raise SystemExit(f"No panel CSVs in {PANEL_DIR}")
    panel = {f.stem: pd.read_csv(f, parse_dates=["timestamps"])
             for f in files[: args.symbols]}
    logger.info("panel: %d symbols | estimator %s | horizon %d | step %d",
                len(panel), args.estimator, args.horizon, step)

    po = walk_forward_vol(panel, VOL_MODELS, args.horizon,
                          args.estimator, args.warmup, step)
    if po.empty:
        raise SystemExit("no origins scored")

    print(summarise(po, args.horizon))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"_{args.tag}" if args.tag else ""
    path = RESULTS_DIR / f"vol_h{args.horizon}{tag}.csv"
    po.to_csv(path, index=False)
    print(f"  saved -> {path}  ({len(po)} rows, {po['date'].nunique()} dates)\n")

    for m in ("log-har", "har", "ewma", "ma22"):
        if m not in set(po.forecaster):
            continue
        r = sig.paired_bootstrap(po, m, "rw", metric="qlike", n_boot=4000)
        lo, hi = r["ci95"]
        verdict = "BEATS random walk" if r["better"] else "not distinguishable"
        print(f"  {m:<9} QLIKE reduction vs rw: {r['mean_diff']:+.5f}  "
              f"CI [{lo:+.5f}, {hi:+.5f}]  ({r['n_clusters']} clusters)  {verdict}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
