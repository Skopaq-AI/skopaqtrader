"""Liquidity-screened NSE universe selection.

Picking symbols by hand invites bias: you reach for the names you already
believe in, and a forecaster then looks good (or bad) for reasons that have
nothing to do with the model.  This module derives the universe from the data
instead — top-N by median daily turnover, with a coverage floor so thinly
listed or recently-listed names cannot sneak in.

Turnover is the right liquidity proxy rather than raw volume: a Rs 5 stock
trading 10M shares is far less liquid than a Rs 3,000 stock trading 1M, and
Kronos is being asked to forecast *prices*, not share counts.
"""

from __future__ import annotations

import logging

import pandas as pd

logger = logging.getLogger(__name__)


def select_universe(
    df: pd.DataFrame,
    top_n: int = 50,
    min_coverage: float = 0.95,
    min_median_turnover: float = 5e7,
) -> list[str]:
    """Choose the most liquid, best-covered symbols in the frame.

    Args:
        df: Long frame (adjusted or raw) with ``symbol``, ``date``, ``turnover``.
        top_n: How many symbols to return.
        min_coverage: Fraction of the sample's trading days a symbol must be
            present on.  Filters new listings, suspensions and delistings —
            all of which would otherwise leave holes a walk-forward test reads
            as price moves.
        min_median_turnover: Absolute floor in rupees (default Rs 5 crore/day).

    Returns:
        Symbols sorted by descending median turnover.
    """
    total_days = df["date"].nunique()
    if total_days == 0:
        return []

    stats = (
        df.groupby("symbol")
        .agg(days=("date", "nunique"), median_turnover=("turnover", "median"))
        .reset_index()
    )
    stats["coverage"] = stats["days"] / total_days

    eligible = stats[
        (stats["coverage"] >= min_coverage)
        & (stats["median_turnover"] >= min_median_turnover)
    ].sort_values("median_turnover", ascending=False)

    logger.info(
        "universe: %d symbols seen, %d pass coverage>=%.0f%% and "
        "turnover>=Rs %.1f cr, taking top %d",
        len(stats), len(eligible), min_coverage * 100,
        min_median_turnover / 1e7, top_n,
    )
    return eligible.head(top_n)["symbol"].tolist()
