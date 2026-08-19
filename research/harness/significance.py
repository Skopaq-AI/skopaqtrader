"""Statistical inference on walk-forward results.

An aggregate scorecard cannot tell you whether a forecaster is *really* better
than naive — only that it scored better on one particular sample. This module
supplies the missing half.

The central correction: **observations sharing a date are not independent.**
Fifteen symbols forecast on the same origin date all load on the same market
factor, so when NIFTY gaps down they miss together. Treating those 15 rows as
15 independent draws overstates precision by roughly sqrt(n_symbols). Every
test here therefore resamples *dates*, not rows — the cluster bootstrap. With
44 distinct dates, that is the honest sample size regardless of how many
individual forecasts were made.

Tests provided:
    paired_bootstrap()  — is model's loss lower than baseline's? (Diebold-
                          Mariano in spirit, cluster-bootstrapped rather than
                          relying on an asymptotic HAC variance that 44
                          clusters will not support)
    directional_ci()    — confidence interval on hit rate, clustered
    per_symbol()        — is any edge broad, or driven by two lucky names?
    stability()         — does it survive splitting the sample in half?
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def _date_groups(df: pd.DataFrame, col: str) -> list[np.ndarray]:
    """Pre-split one column into per-date arrays.

    The bootstrap draws thousands of resamples, so the per-date grouping must
    happen once, not once per draw.  Filtering the DataFrame inside the loop
    is O(dates x draws) pandas operations and turns a 2-second job into a
    10-minute one on 19k rows.
    """
    return [g.to_numpy() for _, g in df.groupby("date", sort=True)[col]]


def _boot_mean(groups: list[np.ndarray], n_boot: int, rng: np.random.Generator):
    """Cluster bootstrap of a pooled mean over pre-split date groups."""
    k = len(groups)
    sums = np.array([g.sum() for g in groups], dtype=float)
    counts = np.array([g.size for g in groups], dtype=float)
    idx = rng.integers(0, k, size=(n_boot, k))
    # Pooled mean of the drawn clusters = sum(sums)/sum(counts), vectorised.
    return sums[idx].sum(axis=1) / np.maximum(counts[idx].sum(axis=1), 1e-9)


def _cluster_resample(
    df: pd.DataFrame, rng: np.random.Generator
) -> pd.DataFrame:
    """Resample whole dates with replacement, preserving within-date structure."""
    dates = df["date"].unique()
    picked = rng.choice(dates, size=len(dates), replace=True)
    return pd.concat([df[df["date"] == d] for d in picked], ignore_index=True)


def paired_bootstrap(
    df: pd.DataFrame,
    model: str,
    baseline: str = "naive",
    metric: str = "abs_err",
    n_boot: int = 2000,
    seed: int = 0,
) -> dict:
    """Cluster-bootstrapped test of whether ``model`` has lower loss.

    Pairs the two forecasters on identical (symbol, date) origins, forms the
    loss differential ``d = loss_baseline - loss_model`` (positive favours the
    model), then bootstraps the mean of ``d`` over dates.

    Returns a dict with the observed mean differential, its 95% CI, a
    one-sided p-value for "model is better", and the cluster count.
    """
    piv = df.pivot_table(index=["symbol", "date"], columns="forecaster",
                         values=metric).reset_index()
    if model not in piv or baseline not in piv:
        return {"error": f"missing {model} or {baseline}"}

    piv = piv.dropna(subset=[model, baseline])
    piv["d"] = piv[baseline] - piv[model]

    observed = float(piv["d"].mean())
    rng = np.random.default_rng(seed)
    boots = _boot_mean(_date_groups(piv, "d"), n_boot, rng)

    lo, hi = np.percentile(boots, [2.5, 97.5])
    # One-sided: how often does the bootstrap say the model is NOT better?
    p = float(np.mean(boots <= 0.0))
    return {
        "model": model, "baseline": baseline, "metric": metric,
        "mean_diff": observed,
        "ci95": (float(lo), float(hi)),
        "p_one_sided": p,
        "n_clusters": int(piv["date"].nunique()),
        "n_pairs": int(len(piv)),
        "better": bool(observed > 0 and lo > 0),
    }


def directional_ci(
    df: pd.DataFrame, model: str, n_boot: int = 2000, seed: int = 0
) -> dict:
    """Cluster-bootstrapped CI on directional hit rate for one forecaster."""
    sub = df[(df["forecaster"] == model) & (df["scored_dir"])]
    if sub.empty:
        return {"model": model, "n": 0, "acc": float("nan"),
                "ci95": (float("nan"), float("nan")), "beats_chance": False}

    observed = float(sub["dir_correct"].mean())
    rng = np.random.default_rng(seed)
    boots = _boot_mean(_date_groups(sub, "dir_correct"), n_boot, rng)

    lo, hi = np.percentile(boots, [2.5, 97.5])
    return {
        "model": model, "n": int(len(sub)),
        "n_clusters": int(sub["date"].nunique()),
        "acc": observed, "ci95": (float(lo), float(hi)),
        "beats_chance": bool(lo > 0.5),
    }


def per_symbol(df: pd.DataFrame, model: str, baseline: str = "naive") -> pd.DataFrame:
    """Per-symbol mean loss differential — is an edge broad or concentrated?

    A model that beats naive on 12 of 15 symbols is telling a different story
    from one that wins overall because it was spectacular on two and mediocre
    on thirteen. The aggregate number cannot distinguish them.
    """
    piv = df.pivot_table(index=["symbol", "date"], columns="forecaster",
                         values="abs_err").reset_index()
    piv = piv.dropna(subset=[model, baseline])
    piv["d"] = piv[baseline] - piv[model]
    out = (piv.groupby("symbol")["d"]
              .agg(mean_diff="mean", n="size")
              .reset_index()
              .sort_values("mean_diff", ascending=False))
    out["model_wins"] = out["mean_diff"] > 0
    return out


def stability(df: pd.DataFrame, model: str, baseline: str = "naive") -> pd.DataFrame:
    """Split the sample chronologically — does the result survive both halves?"""
    piv = df.pivot_table(index=["symbol", "date"], columns="forecaster",
                         values="abs_err").reset_index()
    piv = piv.dropna(subset=[model, baseline])
    piv["d"] = piv[baseline] - piv[model]

    dates = np.sort(piv["date"].unique())
    mid = dates[len(dates) // 2]
    piv["half"] = np.where(piv["date"] < mid, "first", "second")
    return (piv.groupby("half")
               .agg(mean_diff=("d", "mean"), n=("d", "size"),
                    dates=("date", "nunique"))
               .reset_index())


def directional_stability(df: pd.DataFrame, model: str) -> pd.DataFrame:
    """Directional hit rate split chronologically — does an edge persist?

    A single aggregate hit rate cannot distinguish a durable edge from one
    regime's worth of luck.  If the first half is 0.58 and the second 0.49,
    the pooled 0.535 is an artefact of averaging, not a finding.
    """
    sub = df[(df["forecaster"] == model) & (df["scored_dir"])].copy()
    if sub.empty:
        return pd.DataFrame()
    dates = np.sort(sub["date"].unique())
    mid = dates[len(dates) // 2]
    sub["half"] = np.where(sub["date"] < mid, "first", "second")
    return (sub.groupby("half")
               .agg(acc=("dir_correct", "mean"), n=("dir_correct", "size"),
                    dates=("date", "nunique"))
               .reset_index())


def directional_by_symbol(df: pd.DataFrame, model: str) -> pd.DataFrame:
    """Per-symbol hit rate — breadth check for a directional edge."""
    sub = df[(df["forecaster"] == model) & (df["scored_dir"])]
    if sub.empty:
        return pd.DataFrame()
    return (sub.groupby("symbol")
               .agg(acc=("dir_correct", "mean"), n=("dir_correct", "size"))
               .reset_index()
               .sort_values("acc", ascending=False))


def report(path: str, model: str = "kronos", baseline: str = "naive") -> str:
    """Full inference report from a per-origin CSV."""
    df = pd.read_csv(path)
    lines: list[str] = []
    add = lines.append

    add("")
    add(f"  Inference report — {model} vs {baseline}")
    add("  " + "=" * 66)
    add(f"  rows {len(df)} | symbols {df['symbol'].nunique()} | "
        f"distinct dates {df['date'].nunique()}")
    add(f"  Dates are the clustering unit: effective n = {df['date'].nunique()}, "
        f"not {len(df[df.forecaster == model])}.")
    add("")

    for metric, label in (("abs_err", "point accuracy"), ("crps", "distributional")):
        r = paired_bootstrap(df, model, baseline, metric=metric)
        if "error" in r:
            add(f"  {label}: {r['error']}")
            continue
        lo, hi = r["ci95"]
        verdict = "BETTER" if r["better"] else "not distinguishable"
        add(f"  {label:<16} ({metric})")
        if metric == "crps" and baseline == "naive":
            # naive emits a degenerate (zero-width) distribution, so its CRPS
            # reduces to plain MAE. ANY distributional forecaster "wins" this
            # comparison by construction; it says nothing about skill.
            verdict += "  [INVALID — naive has no distribution; compare vs bootstrap]"
        add(f"    mean loss reduction vs {baseline}: {r['mean_diff']:+.4f}")
        add(f"    95% CI (cluster bootstrap):        [{lo:+.4f}, {hi:+.4f}]")
        add(f"    one-sided p:                       {r['p_one_sided']:.4f}")
        add(f"    verdict:                           {verdict}")
        add("")

    if baseline == "naive" and "bootstrap" in set(df["forecaster"]) and model != "bootstrap":
        r = paired_bootstrap(df, model, "bootstrap", metric="crps")
        if "error" not in r:
            lo, hi = r["ci95"]
            add("  distributional vs bootstrap (the fair distributional peer)")
            add(f"    mean CRPS reduction:               {r['mean_diff']:+.4f}")
            add(f"    95% CI (cluster bootstrap):        [{lo:+.4f}, {hi:+.4f}]")
            add(f"    verdict:                           "
                f"{'BETTER' if r['better'] else 'not distinguishable'}")
            add("")

    for m in (model, baseline):
        d = directional_ci(df, m)
        if d["n"] == 0:
            add(f"  direction {m:<12} abstains (no directional calls)")
            continue
        lo, hi = d["ci95"]
        add(f"  direction {m:<12} {d['acc']:.4f}  95% CI [{lo:.4f}, {hi:.4f}]"
            f"  {'BEATS CHANCE' if d['beats_chance'] else 'indistinguishable from chance'}")
    add("")

    ds = directional_stability(df, model)
    if not ds.empty:
        add("  directional stability (chronological halves):")
        for _, r in ds.iterrows():
            add(f"    {r['half']:<7} acc {r['acc']:.4f}  "
                f"({int(r['n'])} calls, {int(r['dates'])} dates)")
        add("")

    dbs = directional_by_symbol(df, model)
    if not dbs.empty:
        above = int((dbs["acc"] > 0.5).sum())
        add(f"  directional breadth: above 0.50 on {above}/{len(dbs)} symbols")
        add(f"    best  {dbs.iloc[0]['symbol']:<12} {dbs.iloc[0]['acc']:.4f}")
        add(f"    worst {dbs.iloc[-1]['symbol']:<12} {dbs.iloc[-1]['acc']:.4f}")
        add("")

    ps = per_symbol(df, model, baseline)
    if not ps.empty:
        wins = int(ps["model_wins"].sum())
        add(f"  breadth: {model} beats {baseline} on {wins}/{len(ps)} symbols")
        add(f"    best  {ps.iloc[0]['symbol']:<12} {ps.iloc[0]['mean_diff']:+.3f}")
        add(f"    worst {ps.iloc[-1]['symbol']:<12} {ps.iloc[-1]['mean_diff']:+.3f}")
        add("")

    st = stability(df, model, baseline)
    if not st.empty:
        add("  stability (chronological halves):")
        for _, r in st.iterrows():
            add(f"    {r['half']:<7} mean_diff {r['mean_diff']:+.3f}  "
                f"({int(r['n'])} pairs, {int(r['dates'])} dates)")
    add("")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    src = sys.argv[1] if len(sys.argv) > 1 else \
        "research/kronos/results/origins_h5_kronos-base.csv"
    mdl = sys.argv[2] if len(sys.argv) > 2 else "kronos"
    print(report(src, mdl))
