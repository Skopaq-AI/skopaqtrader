"""Walk-forward validation driver: does any forecaster beat naive on NSE?

Design rules that keep the answer honest:

    Same origins for everyone.  Every forecaster is scored on an identical set
        of (symbol, date) forecast origins.  Letting each model choose where it
        forecasts is the single easiest way to manufacture a good result.
    No lookahead, structurally.  A forecaster only ever receives
        ``df.iloc[:origin + 1]``.  It cannot see the future because the future
        is not in the object it was handed.
    Naive is the denominator.  MASE divides by the naive forecaster's error on
        the same origins, so >= 1.0 literally means "no better than assuming
        nothing changes" — the null hypothesis for any price model.
    Seeded per origin.  Reproducible across runs and machines.

Usage::

    python research/kronos/validate.py fetch --years 3
    python research/kronos/validate.py run --horizon 5 --symbols 50
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import nse_data                      # noqa: E402
from forecasters import BASELINES    # noqa: E402
from metrics import (                # noqa: E402
    ScoreCard,
    circuit_violation_rate,
    crps_from_samples,
    directional_accuracy,
    interval_coverage,
)
from universe import select_universe  # noqa: E402

logger = logging.getLogger("kronos.validate")

PANEL_DIR = Path("data_cache/nse_panel")
RESULTS_DIR = Path("research/kronos/results")


# ── stage 1: data ───────────────────────────────────────────────────────────

def cmd_fetch(args: argparse.Namespace) -> int:
    """Download bhavcopy, adjust for corporate actions, write per-symbol CSVs."""
    end = date.today()
    start = end - timedelta(days=int(365.25 * args.years))

    logger.info("downloading NSE bhavcopy %s .. %s", start, end)
    stats = nse_data.download_range(start, end, delay=args.delay)
    logger.info("download complete — %s", stats)

    if stats.downloaded == 0 and stats.cached == 0:
        logger.error("no bhavcopy files obtained; nothing to build")
        return 1

    df = nse_data.load_long_frame()
    logger.info("parsed %d rows across %d symbols, %d trading days",
                len(df), df["symbol"].nunique(), df["date"].nunique())

    df = nse_data.adjust_for_actions(df)
    symbols = select_universe(df, top_n=args.symbols)
    written = nse_data.build_panel(df, symbols, PANEL_DIR)

    print(f"\n  {len(written)} symbols written to {PANEL_DIR}")
    print(f"  span: {df['date'].min().date()} .. {df['date'].max().date()}")
    print(f"  top 10 by turnover: {', '.join(symbols[:10])}")
    return 0


# ── stage 2: walk-forward ───────────────────────────────────────────────────

def _load_panel(limit: int | None) -> dict[str, pd.DataFrame]:
    files = sorted(PANEL_DIR.glob("*.csv"))
    if not files:
        raise SystemExit(
            f"No panel CSVs in {PANEL_DIR}. Run:  validate.py fetch  first."
        )
    panel = {}
    for p in files:
        panel[p.stem] = pd.read_csv(p, parse_dates=["timestamps"])

    if limit and limit < len(panel):
        # Rank by median traded value, not filename. Slicing sorted(glob())
        # picks the alphabetically-first N — an arbitrary subset that happens
        # to over-weight whichever sectors start with A and B.
        ranked = sorted(panel, key=lambda k: panel[k]["amount"].median(), reverse=True)
        panel = {k: panel[k] for k in ranked[:limit]}
    return panel


def walk_forward(
    panel: dict[str, pd.DataFrame],
    forecasters: list,
    horizon: int,
    context: int,
    n_samples: int,
    step: int,
    test_frac: float,
    seed: int,
    progress_every: int = 50,
) -> list[ScoreCard]:
    """Score every forecaster over identical origins across the panel."""
    # accumulator[name] = dict of lists, one entry per (symbol, origin)
    acc: dict[str, dict[str, list]] = {
        f.name: {"pred": [], "actual": [], "origin": [], "samp": [],
                 "maxmove": [], "symbol": []}
        for f in forecasters
    }
    n_origins = 0
    records: list[dict] = []
    t_start = time.time()

    total_origins = sum(
        len(range(max(context, int(len(d) * (1.0 - test_frac))), len(d) - horizon, step))
        for d in panel.values()
        if len(d) - horizon > max(context, int(len(d) * (1.0 - test_frac)))
    )
    logger.info("planned: %d origins across %d symbols", total_origins, len(panel))

    for sym, df in panel.items():
        closes = df["close"].to_numpy(dtype=float)
        n = len(df)
        first = max(context, int(n * (1.0 - test_frac)))
        last = n - horizon
        if last <= first:
            logger.debug("%s: too short (%d rows), skipped", sym, n)
            continue

        for origin in range(first, last, step):
            history = df.iloc[: origin + 1]
            origin_px = float(closes[origin])
            actual_px = float(closes[origin + horizon])
            origin_dt = pd.Timestamp(df["timestamps"].iloc[origin]).date()

            # Empirical worst single-day move seen *before* the origin only.
            hist_c = closes[max(0, origin - context) : origin + 1]
            rets = np.diff(hist_c) / hist_c[:-1] if hist_c.size > 1 else np.array([0.05])
            max_move = float(np.max(np.abs(rets))) if rets.size else 0.05

            n_origins += 1
            if progress_every and n_origins % progress_every == 0:
                elapsed = time.time() - t_start
                rate = n_origins / max(elapsed, 1e-9)
                logger.info(
                    "  %d origins | %.2f/s | elapsed %.1fm | eta %.1fm",
                    n_origins, rate, elapsed / 60,
                    (total_origins - n_origins) / max(rate, 1e-9) / 60,
                )
            for f in forecasters:
                rng = np.random.default_rng(
                    abs(hash((seed, f.name, sym, origin))) % (2**32)
                )
                try:
                    samples = f.forecast(history, horizon, n_samples, rng)
                except Exception as exc:              # noqa: BLE001
                    logger.warning("%s failed on %s@%d: %s", f.name, sym, origin, exc)
                    continue
                samples = np.asarray(samples, dtype=float)
                if samples.ndim != 2 or samples.shape[1] < horizon:
                    logger.warning("%s returned bad shape %s", f.name, samples.shape)
                    continue

                step_samples = samples[:, horizon - 1]
                pred_px = float(np.median(step_samples))
                a = acc[f.name]
                a["pred"].append(pred_px)
                a["actual"].append(actual_px)
                a["origin"].append(origin_px)
                a["samp"].append(step_samples)
                a["maxmove"].append(max_move)
                a["symbol"].append(sym)

                # Per-origin record. Aggregate scorecards cannot support a
                # paired significance test, and observations sharing a date
                # are cross-sectionally correlated (common market factor) —
                # so inference must cluster on date, which needs this granularity.
                lo80, hi80 = np.quantile(step_samples, [0.1, 0.9])
                pred_ret = (pred_px - origin_px) / origin_px
                true_ret = (actual_px - origin_px) / origin_px
                called = np.sign(pred_ret) != 0
                moved = abs(true_ret) >= 0.002
                records.append({
                    "symbol": sym,
                    "date": origin_dt,
                    "forecaster": f.name,
                    "origin_px": origin_px,
                    "actual_px": actual_px,
                    "pred_px": pred_px,
                    "abs_err": abs(pred_px - actual_px),
                    "crps": crps_from_samples(step_samples, actual_px),
                    "in80": bool(lo80 <= actual_px <= hi80),
                    "scored_dir": bool(called and moved),
                    "dir_correct": bool(called and moved and
                                        np.sign(pred_ret) == np.sign(true_ret)),
                    "spread_pct": float((step_samples.max() - step_samples.min())
                                        / origin_px * 100),
                })

    logger.info("walk-forward: %d origins across %d symbols",
                n_origins, len(panel))

    # Naive MAE is the MASE denominator — compute it before anything else.
    naive_mae = float("nan")
    if "naive" in acc and acc["naive"]["pred"]:
        naive_mae = float(np.mean(
            np.abs(np.array(acc["naive"]["pred"]) - np.array(acc["naive"]["actual"]))
        ))

    cards: list[ScoreCard] = []
    for f in forecasters:
        a = acc[f.name]
        if not a["pred"]:
            cards.append(ScoreCard(forecaster=f.name, horizon=horizon))
            continue

        pred = np.array(a["pred"])
        actual = np.array(a["actual"])
        origin = np.array(a["origin"])
        samp = np.vstack(a["samp"])
        maxmove = np.array(a["maxmove"])

        dir_acc, dir_n = directional_accuracy(pred, actual, origin)
        mae = float(np.mean(np.abs(pred - actual)))
        crps = float(np.mean([
            crps_from_samples(samp[i], actual[i]) for i in range(len(actual))
        ]))

        cards.append(ScoreCard(
            forecaster=f.name,
            horizon=horizon,
            n_origins=len(pred),
            directional_accuracy=dir_acc,
            directional_n=dir_n,
            mae=mae,
            mase=mae / naive_mae if naive_mae and naive_mae > 0 else float("nan"),
            crps=crps,
            coverage_80=interval_coverage(samp, actual),
            circuit_violation_rate=circuit_violation_rate(samp, origin, maxmove),
        ))
    return cards, pd.DataFrame(records)


def _report(cards: list[ScoreCard], horizon: int) -> str:
    rows = [c.as_row() for c in cards]
    hdr = f"{'forecaster':<14}{'dir_acc':>9}{'dir_n':>8}{'MASE':>8}{'CRPS':>10}{'cov80':>8}{'circ':>8}"
    lines = [
        "",
        f"  Walk-forward results — horizon {horizon} trading day(s)",
        "  " + "─" * (len(hdr) - 2),
        "  " + hdr,
        "  " + "─" * (len(hdr) - 2),
    ]
    def fmt(v, nd=4, dash="  —"):
        """Render NaN as an em dash: a metric that does not apply is not zero."""
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        return dash if f != f else f"{f:.{nd}f}"

    for r in rows:
        lines.append(
            f"  {r['forecaster']:<14}{fmt(r['dir_acc']):>9}{r['dir_n']:>8}"
            f"{fmt(r['mase'],3):>8}{fmt(r['crps'],3):>10}"
            f"{fmt(r['cov80'],3):>8}{fmt(r['circuit_viol'],3):>8}"
        )
    lines += [
        "  " + "─" * (len(hdr) - 2),
        "",
        "  dir_acc  fraction of >=0.2% moves whose direction was called right (0.50 = coin flip)",
        "  MASE     mean abs error / naive's. < 1.00 beats 'nothing changes'; >= 1.00 does not",
        "  CRPS     distributional error in rupees, lower better (rewards honest uncertainty)",
        "  cov80    realised coverage of the 80% interval. ~0.80 = calibrated",
        "  circ     share of samples implying a move the stock has never made (NSE band proxy)",
        "",
    ]
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> int:
    panel = _load_panel(args.symbols)
    logger.info("loaded %d symbols from %s", len(panel), PANEL_DIR)

    forecasters = list(BASELINES)

    cards, per_origin = walk_forward(
        panel, forecasters,
        horizon=args.horizon, context=args.context, n_samples=args.samples,
        step=args.step, test_frac=args.test_frac, seed=args.seed,
    )

    print(_report(cards, args.horizon))

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"_{args.tag}" if args.tag else ""
    out = RESULTS_DIR / f"wfo_h{args.horizon}{tag}.json"
    out.write_text(json.dumps([c.as_row() for c in cards], indent=2))
    po_path = RESULTS_DIR / f"origins_h{args.horizon}{tag}.csv"
    per_origin.to_csv(po_path, index=False)
    print(f"  saved -> {out}")
    print(f"  saved -> {po_path}  ({len(per_origin)} per-origin rows)\n")

    winners = [c for c in cards
               if c.forecaster != "naive" and c.mase == c.mase and c.mase < 1.0]
    if not winners:
        print("  VERDICT: nothing beat naive. On this evidence Kronos does not\n"
              "           earn a place in the pipeline.\n")
    else:
        best = min(winners, key=lambda c: c.mase)
        print(f"  VERDICT: '{best.forecaster}' beats naive (MASE {best.mase:.3f}, "
              f"dir_acc {best.directional_accuracy:.3f}).\n"
              f"           Worth a second look before trusting it.\n")
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    p = argparse.ArgumentParser(prog="validate.py", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download NSE bhavcopy and build the panel")
    f.add_argument("--years", type=float, default=3.0)
    f.add_argument("--symbols", type=int, default=50)
    f.add_argument("--delay", type=float, default=0.6,
                   help="seconds between NSE requests (be polite)")
    f.set_defaults(func=cmd_fetch)

    r = sub.add_parser("run", help="walk-forward validation")
    r.add_argument("--horizon", type=int, default=5)
    r.add_argument("--context", type=int, default=512,
                   help="history window fed to the forecaster (Kronos base = 512)")
    r.add_argument("--samples", type=int, default=64)
    r.add_argument("--step", type=int, default=5,
                   help="trading days between forecast origins")
    r.add_argument("--test-frac", type=float, default=0.3,
                   help="fraction of each series held out for testing")
    r.add_argument("--symbols", type=int, default=None)
    r.add_argument("--seed", type=int, default=42)
    r.add_argument("--kronos-model", default="NeoQuasar/Kronos-base",
                   help="hub id OR path to a fine-tuned checkpoint dir")
    r.add_argument("--kronos-name", default="",
                   help="label for this model in the results table")
    r.add_argument("--tag", default="", help="suffix for the results filename")
    r.set_defaults(func=cmd_run)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
