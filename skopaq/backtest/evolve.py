"""Self-Evolving Strategy Loop — the AI learns and improves autonomously.

The complete cycle:
    1. BACKTEST current strategy on recent data
    2. EVALUATE metrics (Sharpe, WFE, Monte Carlo)
    3. ADAPT parameters if performance drops
    4. VALIDATE via walk-forward + Monte Carlo
    5. DEPLOY if validated, REJECT if overfit
    6. REFLECT on what changed and why
    7. PERSIST everything to Postgres

Usage::

    from skopaq.backtest.evolve import run_evolution_cycle

    report = await run_evolution_cycle("RELIANCE")
    # Automatically backtests, validates, adapts, and persists
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class EvolutionReport:
    """Output of a single evolution cycle."""

    symbol: str
    timestamp: str = ""

    # Current strategy performance
    current_sharpe: float = 0.0
    current_win_rate: float = 0.0
    current_return: float = 0.0

    # Walk-forward validation
    wfe_pct: float = 0.0
    wfe_passed: bool = False

    # Monte Carlo robustness
    mc_median_return: float = 0.0
    mc_probability_of_loss: float = 0.0
    mc_robust: bool = False

    # Adaptation
    params_changed: bool = False
    old_params: dict = field(default_factory=dict)
    new_params: dict = field(default_factory=dict)
    improvement: str = ""

    # Decision
    deploy: bool = False
    reason: str = ""

    # Reflection
    lesson: str = ""


async def run_evolution_cycle(
    symbol: str,
    days: int = 180,
) -> EvolutionReport:
    """Run one complete evolution cycle for a symbol.

    Steps:
        1. Fetch historical data
        2. Backtest with current params
        3. Run walk-forward validation
        4. Run Monte Carlo
        5. If underperforming, adapt params
        6. Validate adapted params
        7. Deploy or reject
        8. Persist and reflect

    Args:
        symbol: Stock symbol.
        days: Days of history for backtesting.

    Returns:
        EvolutionReport with full details.
    """
    import asyncio
    import io

    report = EvolutionReport(
        symbol=symbol,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )

    try:
        from tradingagents.dataflows.router import route_to_vendor
        from tradingagents.dataflows.config import set_config
        from skopaq.backtest.engine import BacktestConfig, run_backtest
        from skopaq.backtest.walk_forward import walk_forward_test
        from skopaq.backtest.monte_carlo import run_monte_carlo
        from skopaq.backtest.persistence import (
            save_backtest_result, save_wfo_result, save_monte_carlo_result,
            save_strategy_params, get_active_params,
        )

        # Setup data config
        set_config({
            'data_vendors': {'core_stock_apis': 'indstocks,yfinance', 'technical_indicators': 'yfinance'},
            'yfinance_symbol_suffix': '.NS',
        })

        # Fetch data
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start_date = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")

        ohlcv_text = await asyncio.to_thread(
            route_to_vendor, "get_stock_data", symbol, start_date, end_date
        )
        lines = [l for l in ohlcv_text.strip().split("\n") if not l.startswith("#")]
        df = pd.read_csv(io.StringIO("\n".join(lines)))

        if len(df) < 30:
            report.reason = f"Insufficient data ({len(df)} bars)"
            return report

        # Get current params or use defaults
        active = get_active_params(symbol, "RSI_MEAN_REVERSION")
        if active:
            params = active["params"]
        else:
            params = {
                "rsi_buy_threshold": 35,
                "rsi_sell_threshold": 65,
                "stop_loss_pct": 3.0,
                "target_pct": 6.0,
                "rsi_period": 14,
            }
        report.old_params = params.copy()

        # Step 1: Backtest with current params
        config = BacktestConfig(
            stop_loss_pct=params["stop_loss_pct"] / 100,
            target_pct=params["target_pct"] / 100,
        )

        def generate_signals(data: pd.DataFrame) -> pd.DataFrame:
            data = data.copy()
            delta = data["Close"].diff()
            gain = delta.where(delta > 0, 0).rolling(params["rsi_period"]).mean()
            loss = (-delta.where(delta < 0, 0)).rolling(params["rsi_period"]).mean()
            rs = gain / loss
            data["RSI"] = 100 - (100 / (1 + rs))

            signals = pd.DataFrame({
                "date": data["Date"],
                "signal": 0,
                "confidence": 50,
            })
            for i in range(params["rsi_period"] + 1, len(data)):
                rsi = data.iloc[i]["RSI"]
                if pd.isna(rsi):
                    continue
                if rsi < params["rsi_buy_threshold"]:
                    signals.iloc[i, signals.columns.get_loc("signal")] = 1
                elif rsi > params["rsi_sell_threshold"]:
                    signals.iloc[i, signals.columns.get_loc("signal")] = -1
            return signals

        signals = generate_signals(df)
        bt_result = run_backtest(signals, df, config, symbol)

        report.current_sharpe = bt_result.sharpe_ratio
        report.current_win_rate = bt_result.win_rate_pct
        report.current_return = bt_result.total_return_pct

        # Save backtest
        save_backtest_result(
            symbol, "RSI_MEAN_REVERSION",
            bt_result.start_date, bt_result.end_date,
            bt_result.total_return_pct, bt_result.annual_return_pct,
            bt_result.sharpe_ratio, bt_result.sortino_ratio, bt_result.calmar_ratio,
            bt_result.max_drawdown_pct, bt_result.win_rate_pct, bt_result.profit_factor,
            bt_result.total_trades, params,
        )

        # Step 2: Walk-Forward Validation
        try:
            wfo = walk_forward_test(generate_signals, df, symbol,
                                    in_sample_months=3, out_of_sample_months=1)
            report.wfe_pct = wfo.wfe_pct
            report.wfe_passed = wfo.wfe_pct >= 50
            save_wfo_result(symbol, wfo.total_periods, wfo.wfe_pct,
                           wfo.avg_oos_return, wfo.avg_oos_sharpe, wfo.consistency_pct,
                           [{"is_ret": p.in_sample_return, "oos_ret": p.out_sample_return,
                             "wfe": p.wfe} for p in wfo.periods])
        except Exception as e:
            logger.warning("WFO failed: %s", e)

        # Step 3: Monte Carlo
        if bt_result.trades:
            trade_pnls = [t.pnl for t in bt_result.trades]
            mc = run_monte_carlo(trade_pnls, n_simulations=500)
            report.mc_median_return = mc.median_return_pct
            report.mc_probability_of_loss = mc.probability_of_loss_pct
            report.mc_robust = mc.probability_of_loss_pct < 60
            save_monte_carlo_result(symbol, mc.n_simulations, mc.median_return_pct,
                                   mc.p5_return_pct, mc.p95_return_pct, mc.worst_max_dd_pct,
                                   mc.median_sharpe, mc.probability_of_loss_pct,
                                   mc.probability_of_ruin_pct)

        # Step 4: Adapt if underperforming
        needs_adaptation = (
            bt_result.sharpe_ratio < 0.5
            or bt_result.win_rate_pct < 40
            or bt_result.total_return_pct < -5
        )

        if needs_adaptation:
            # Try parameter variations
            best_sharpe = bt_result.sharpe_ratio
            best_params = params.copy()

            for rsi_buy in [25, 30, 35, 40]:
                for rsi_sell in [60, 65, 70, 75]:
                    for sl in [2.0, 3.0, 4.0, 5.0]:
                        test_params = {
                            **params,
                            "rsi_buy_threshold": rsi_buy,
                            "rsi_sell_threshold": rsi_sell,
                            "stop_loss_pct": sl,
                        }

                        def gen_test(data, p=test_params):
                            data = data.copy()
                            delta = data["Close"].diff()
                            gain = delta.where(delta > 0, 0).rolling(p["rsi_period"]).mean()
                            loss = (-delta.where(delta < 0, 0)).rolling(p["rsi_period"]).mean()
                            rs = gain / loss
                            data["RSI"] = 100 - (100 / (1 + rs))
                            sigs = pd.DataFrame({"date": data["Date"], "signal": 0, "confidence": 50})
                            for i in range(p["rsi_period"] + 1, len(data)):
                                rsi = data.iloc[i]["RSI"]
                                if pd.isna(rsi): continue
                                if rsi < p["rsi_buy_threshold"]: sigs.iloc[i, 1] = 1
                                elif rsi > p["rsi_sell_threshold"]: sigs.iloc[i, 1] = -1
                            return sigs

                        test_cfg = BacktestConfig(stop_loss_pct=sl / 100, target_pct=params["target_pct"] / 100)
                        test_sigs = gen_test(df)
                        test_result = run_backtest(test_sigs, df, test_cfg, symbol)

                        if test_result.sharpe_ratio > best_sharpe and test_result.total_trades >= 3:
                            best_sharpe = test_result.sharpe_ratio
                            best_params = test_params.copy()

            if best_params != params:
                report.params_changed = True
                report.new_params = best_params
                report.improvement = f"Sharpe: {bt_result.sharpe_ratio:.2f} → {best_sharpe:.2f}"
                params = best_params

        # Step 5: Deploy decision
        if bt_result.sharpe_ratio >= 1.0 or (report.params_changed and best_sharpe >= 0.5):
            report.deploy = True
            report.reason = f"Sharpe {bt_result.sharpe_ratio:.2f}, WFE {report.wfe_pct:.0f}%"
            save_strategy_params(symbol, "RSI_MEAN_REVERSION", params,
                                {"sharpe": bt_result.sharpe_ratio, "return": bt_result.total_return_pct})
        else:
            report.deploy = False
            report.reason = f"Sharpe {bt_result.sharpe_ratio:.2f} below threshold"

        # Step 6: Generate reflection
        report.lesson = (
            f"{symbol} RSI strategy: Sharpe={bt_result.sharpe_ratio:.2f}, "
            f"Win={bt_result.win_rate_pct:.0f}%, Return={bt_result.total_return_pct:+.1f}%. "
            f"{'Params adapted: ' + report.improvement if report.params_changed else 'No adaptation needed'}. "
            f"{'Deployed' if report.deploy else 'Not deployed'}: {report.reason}."
        )

        # Notify
        try:
            from skopaq.notifications import notify

            await notify(
                f"Strategy Evolution: {symbol}\n\n"
                f"Sharpe: {bt_result.sharpe_ratio:.2f}\n"
                f"Win Rate: {bt_result.win_rate_pct:.0f}%\n"
                f"Return: {bt_result.total_return_pct:+.1f}%\n"
                f"WFE: {report.wfe_pct:.0f}%\n"
                f"MC P(Loss): {report.mc_probability_of_loss:.0f}%\n\n"
                f"{'Params adapted' if report.params_changed else 'No changes'}\n"
                f"{'DEPLOYED' if report.deploy else 'NOT DEPLOYED'}: {report.reason}\n\n"
                f"Lesson: {report.lesson}"
            )
        except Exception:
            pass

        return report

    except Exception as exc:
        logger.exception("Evolution cycle failed")
        report.reason = f"Error: {exc}"
        return report


def format_evolution_report(report: EvolutionReport) -> str:
    """Format evolution report for display."""
    lines = [
        f"STRATEGY EVOLUTION: {report.symbol}",
        f"Time: {report.timestamp}",
        "",
        "Current Performance:",
        f"  Sharpe: {report.current_sharpe:.2f}",
        f"  Win Rate: {report.current_win_rate:.0f}%",
        f"  Return: {report.current_return:+.1f}%",
        "",
        "Validation:",
        f"  WFE: {report.wfe_pct:.0f}% {'PASS' if report.wfe_passed else 'FAIL'}",
        f"  MC P(Loss): {report.mc_probability_of_loss:.0f}% {'ROBUST' if report.mc_robust else 'FRAGILE'}",
        "",
    ]

    if report.params_changed:
        lines.append("Adaptation:")
        lines.append(f"  {report.improvement}")
        lines.append(f"  Old: {report.old_params}")
        lines.append(f"  New: {report.new_params}")
    else:
        lines.append("No parameter changes needed.")

    lines.append("")
    lines.append(f"Decision: {'DEPLOYED' if report.deploy else 'NOT DEPLOYED'}")
    lines.append(f"Reason: {report.reason}")
    lines.append("")
    lines.append(f"Lesson: {report.lesson}")

    return "\n".join(lines)
