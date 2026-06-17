"""
portfolio_backtest.py
True portfolio-level backtesting: shared capital pool, simultaneous multi-symbol
simulation, portfolio-level Sharpe / Calmar / drawdown metrics.

Key difference from run_batch():
  run_batch()           — each symbol gets full capital independently (not a portfolio)
  PortfolioBacktest.run() — ONE shared capital pool, max_open_positions enforced across
                            all symbols simultaneously, unified equity curve
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd
from loguru import logger

from config import settings


@dataclass
class PortfolioResult:
    symbols: list[str]
    strategy: str
    total_capital: float
    max_open_positions: int
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_net_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float = 0.0
    calmar_ratio: float = 0.0
    capital_utilization_avg: float = 0.0
    max_concurrent_positions: int = 0
    per_symbol: dict[str, dict] = field(default_factory=dict)
    equity_curve: list[float] = field(default_factory=list)
    run_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return {
            "symbols":                  self.symbols,
            "strategy":                 self.strategy,
            "total_capital":            self.total_capital,
            "max_open_positions":       self.max_open_positions,
            "total_trades":             self.total_trades,
            "wins":                     self.wins,
            "losses":                   self.losses,
            "win_rate":                 round(self.win_rate, 1),
            "total_net_pnl":            round(self.total_net_pnl, 0),
            "max_drawdown_pct":         round(self.max_drawdown_pct, 1),
            "sharpe_ratio":             round(self.sharpe_ratio, 2),
            "calmar_ratio":             round(self.calmar_ratio, 2),
            "capital_utilization_avg":  round(self.capital_utilization_avg, 1),
            "max_concurrent_positions": self.max_concurrent_positions,
            "per_symbol":               self.per_symbol,
            "equity_curve":             [round(v, 2) for v in self.equity_curve],
            "run_at":                   self.run_at,
        }


class PortfolioBacktest:

    _SLIPPAGE_BPS: dict[str, int] = {
        "intraday": 10, "scalping": 5, "swing": 3, "options": 15,
    }
    _PRODUCT_MAP: dict[str, str] = {
        "swing": "CNC", "intraday": "MIS", "scalping": "MIS", "options": "NRML",
    }

    # ── Public API ─────────────────────────────────────────────────────────

    def run(
        self,
        symbols: list[dict],
        strategy: str = "intraday",
        total_capital: float = 500_000.0,
        max_open_positions: int = 5,
        lookback_days: int | None = None,
    ) -> PortfolioResult:
        """
        Full pipeline: fetch data → generate signals → simulate shared capital pool.
        `symbols` is a list of {"symbol": "RELIANCE", "exchange": "NSE"} dicts.
        """
        from backtest_engine import backtest_engine, STRATEGY_PARAMS

        params = STRATEGY_PARAMS.get(strategy, STRATEGY_PARAMS["intraday"])
        days = lookback_days or settings.bt_lookback_days

        symbol_dfs: dict[str, pd.DataFrame] = {}
        for item in symbols:
            sym  = item["symbol"]
            exch = item.get("exchange", "NSE")
            df   = backtest_engine._fetch_data(sym, exch, params["interval"], days)
            if df is not None and len(df) >= 60:
                symbol_dfs[sym] = df
            else:
                logger.warning("[portfolio_bt] {} skipped — insufficient data", sym)

        if not symbol_dfs:
            logger.warning("[portfolio_bt] No usable data for any symbol — returning empty result")
            return PortfolioResult(
                symbols=[i["symbol"] for i in symbols],
                strategy=strategy,
                total_capital=total_capital,
                max_open_positions=max_open_positions,
            )

        return self.simulate(symbol_dfs, strategy, total_capital, max_open_positions)

    def simulate(
        self,
        symbol_dfs: dict[str, pd.DataFrame],
        strategy: str = "intraday",
        total_capital: float = 500_000.0,
        max_open_positions: int = 5,
        precomputed_signals: dict[str, pd.Series] | None = None,
    ) -> PortfolioResult:
        """
        Core simulation.  Accepts pre-built DataFrames so tests can inject synthetic data.
        `precomputed_signals` bypasses _generate_signals() — useful for unit testing.
        """
        from backtest_engine import backtest_engine, STRATEGY_PARAMS

        params = STRATEGY_PARAMS.get(strategy, STRATEGY_PARAMS["intraday"])

        signals_dict: dict[str, pd.Series] = {}
        for sym, df in symbol_dfs.items():
            if precomputed_signals and sym in precomputed_signals:
                signals_dict[sym] = precomputed_signals[sym]
            else:
                signals_dict[sym] = backtest_engine._generate_signals(df, strategy)

        trades, equity_curve, max_concurrent, util_avg = self._simulate_portfolio(
            symbol_dfs, signals_dict, params, strategy, total_capital, max_open_positions,
        )
        return self._compute_metrics(
            list(symbol_dfs.keys()), strategy, total_capital, max_open_positions,
            trades, equity_curve, max_concurrent, util_avg,
        )

    # ── Core simulation ────────────────────────────────────────────────────

    def _simulate_portfolio(
        self,
        symbol_dfs:   dict[str, pd.DataFrame],
        signals_dict: dict[str, pd.Series],
        params:       dict,
        strategy:     str,
        total_capital:      float,
        max_open_positions: int,
    ) -> tuple[list[dict], list[float], int, float]:
        """
        Bar-by-bar simulation across all symbols on a shared capital pool.
        Returns (trades, equity_curve, max_concurrent_positions, capital_util_avg_pct).
        """
        # Time-align: use intersection of all symbol indices (no look-ahead gaps)
        common_idx = None
        for df in symbol_dfs.values():
            common_idx = df.index if common_idx is None else common_idx.intersection(df.index)

        if common_idx is None or len(common_idx) < 20:
            return [], [], 0, 0.0
        if total_capital <= 0:
            return [], [], 0, 0.0

        sl_pct   = params["sl_pct"] / 100
        tgt_pct  = params["target_pct"] / 100
        max_bars = params["max_hold_bars"]
        slip     = self._SLIPPAGE_BPS.get(strategy, 10) / 10_000
        apply_costs = getattr(settings, "bt_apply_tx_costs", True)
        product  = self._PRODUCT_MAP.get(strategy, "MIS")

        capital_per_pos = total_capital / max(max_open_positions, 1)
        available_capital = total_capital
        open_positions: dict[str, dict] = {}
        trades: list[dict] = []
        equity_curve: list[float] = []
        cumulative_pnl = 0.0
        max_concurrent = 0
        total_util_sum = 0.0
        symbols = list(symbol_dfs.keys())

        for bar_pos, ts in enumerate(common_idx):
            bar_pnl = 0.0

            # ── Step 1: process exits ──────────────────────────────────────
            exited: list[str] = []
            for sym, pos in list(open_positions.items()):
                df = symbol_dfs[sym]
                if ts not in df.index:
                    continue
                bar      = df.loc[ts]
                bars_held = bar_pos - pos["entry_bar_idx"]
                sl  = pos["entry_fill"] * (1 - sl_pct)
                tgt = pos["entry_fill"] * (1 + tgt_pct)

                if bar["low"] <= sl:
                    exit_fill   = sl * (1 - slip) if apply_costs else sl
                    exit_reason = "SL"
                elif bar["high"] >= tgt:
                    exit_fill   = tgt * (1 - slip) if apply_costs else tgt
                    exit_reason = "TGT"
                elif bars_held >= max_bars:
                    exit_fill   = bar["close"] * (1 - slip) if apply_costs else bar["close"]
                    exit_reason = "TIMEOUT"
                else:
                    continue

                qty       = pos["qty"]
                gross_pnl = (exit_fill - pos["entry_fill"]) * qty
                cost      = 0.0
                if apply_costs:
                    try:
                        from risk_manager import compute_tx_costs
                        cost = compute_tx_costs(qty, pos["entry_fill"], exit_fill, product)
                    except Exception:
                        pass
                net_pnl = gross_pnl - cost
                trades.append({
                    "symbol":      sym,
                    "entry":       round(pos["entry_fill"], 4),
                    "exit":        round(exit_fill, 4),
                    "pnl":         gross_pnl,
                    "net_pnl":     net_pnl,
                    "cost":        cost,
                    "bars":        bars_held,
                    "exit_reason": exit_reason,
                })
                bar_pnl += net_pnl
                available_capital += capital_per_pos
                exited.append(sym)

            for sym in exited:
                del open_positions[sym]

            # ── Step 2: process entries ────────────────────────────────────
            for sym in symbols:
                if sym in open_positions:
                    continue
                if len(open_positions) >= max_open_positions:
                    break
                if available_capital < capital_per_pos * 0.9:  # 10% buffer prevents float drift
                    break

                sig = signals_dict.get(sym)
                if sig is None:
                    continue
                try:
                    sig_val = sig.loc[ts] if ts in sig.index else 0
                except Exception:
                    sig_val = 0
                if sig_val != 1:
                    continue

                df = symbol_dfs[sym]
                if ts not in df.index:
                    continue
                entry_price = float(df.loc[ts, "close"])
                if entry_price <= 0:
                    continue
                entry_fill = entry_price * (1 + slip) if apply_costs else entry_price
                qty = max(1, int(capital_per_pos // entry_fill))
                open_positions[sym] = {
                    "entry_fill":    entry_fill,
                    "entry_bar_idx": bar_pos,
                    "qty":           qty,
                }
                available_capital -= capital_per_pos

            n_open = len(open_positions)
            if n_open > max_concurrent:
                max_concurrent = n_open
            deployed = max(0.0, total_capital - available_capital)
            total_util_sum += deployed / total_capital * 100

            cumulative_pnl += bar_pnl
            equity_curve.append(cumulative_pnl)

        # Force-close any still-open positions at the last bar
        if len(common_idx) > 0:
            last_ts  = common_idx[-1]
            last_bar = len(common_idx) - 1
            for sym, pos in list(open_positions.items()):
                df = symbol_dfs[sym]
                if last_ts not in df.index:
                    continue
                lp        = float(df.loc[last_ts, "close"])
                exit_fill = lp * (1 - slip) if apply_costs else lp
                qty       = pos["qty"]
                gross_pnl = (exit_fill - pos["entry_fill"]) * qty
                cost      = 0.0
                if apply_costs:
                    try:
                        from risk_manager import compute_tx_costs
                        cost = compute_tx_costs(qty, pos["entry_fill"], exit_fill, product)
                    except Exception:
                        pass
                trades.append({
                    "symbol":      sym,
                    "entry":       round(pos["entry_fill"], 4),
                    "exit":        round(exit_fill, 4),
                    "pnl":         gross_pnl,
                    "net_pnl":     gross_pnl - cost,
                    "cost":        cost,
                    "bars":        last_bar - pos["entry_bar_idx"],
                    "exit_reason": "EOD",
                })

        n_bars = len(common_idx)
        util_avg = total_util_sum / n_bars if n_bars > 0 else 0.0
        return trades, equity_curve, max_concurrent, util_avg

    # ── Metrics ────────────────────────────────────────────────────────────

    def _compute_metrics(
        self,
        symbols: list[str],
        strategy: str,
        total_capital: float,
        max_open_positions: int,
        trades: list[dict],
        equity_curve: list[float],
        max_concurrent: int,
        util_avg: float,
    ) -> PortfolioResult:
        if total_capital <= 0:
            total_capital = 1.0
        if not trades:
            return PortfolioResult(
                symbols=symbols, strategy=strategy,
                total_capital=total_capital, max_open_positions=max_open_positions,
                equity_curve=equity_curve,
                max_concurrent_positions=max_concurrent,
                capital_utilization_avg=round(util_avg, 1),
            )

        pnls   = [t.get("net_pnl", t["pnl"]) for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total_net_pnl = sum(pnls)
        win_rate = len(wins) / len(pnls) * 100 if pnls else 0.0

        arr = np.array(pnls, dtype=float)
        if len(arr) > 1 and arr.std() > 0:
            sharpe = float(arr.mean() / arr.std() * math.sqrt(len(arr)))
        else:
            sharpe = 0.0

        # Drawdown on cumulative trade P&L
        cum = np.cumsum(pnls)
        peak = np.maximum.accumulate(cum)
        dd   = peak - cum
        max_dd = float(np.max(dd)) if len(dd) else 0.0
        peak_max = float(peak.max()) if len(peak) else 0.0
        max_dd_pct = (max_dd / max(abs(peak_max), total_capital * 0.001)) * 100 if peak_max != 0 else 0.0

        n_trades  = max(len(pnls), 1)
        ann_factor = min(252.0 / max(n_trades / 5, 1), 4.0)
        ann_return = (total_net_pnl / total_capital) * ann_factor
        calmar = ann_return / abs(max_dd_pct / 100) if max_dd_pct != 0 else 0.0

        # Per-symbol breakdown
        per_symbol: dict[str, dict] = {}
        for sym in symbols:
            sym_trades = [t for t in trades if t["symbol"] == sym]
            if not sym_trades:
                continue
            sym_pnls = [t.get("net_pnl", t["pnl"]) for t in sym_trades]
            sym_wins = [p for p in sym_pnls if p > 0]
            per_symbol[sym] = {
                "trades":   len(sym_trades),
                "wins":     len(sym_wins),
                "losses":   len(sym_pnls) - len(sym_wins),
                "win_rate": round(len(sym_wins) / len(sym_pnls) * 100, 1) if sym_pnls else 0.0,
                "net_pnl":  round(sum(sym_pnls), 0),
            }

        return PortfolioResult(
            symbols=symbols,
            strategy=strategy,
            total_capital=total_capital,
            max_open_positions=max_open_positions,
            total_trades=len(trades),
            wins=len(wins),
            losses=len(losses),
            win_rate=win_rate,
            total_net_pnl=total_net_pnl,
            max_drawdown_pct=max_dd_pct,
            sharpe_ratio=sharpe,
            calmar_ratio=round(calmar, 2),
            capital_utilization_avg=round(util_avg, 1),
            max_concurrent_positions=max_concurrent,
            per_symbol=per_symbol,
            equity_curve=equity_curve,
        )


portfolio_backtest = PortfolioBacktest()
