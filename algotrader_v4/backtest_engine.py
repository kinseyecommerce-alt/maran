"""
backtest_engine.py
Runs any strategy on historical OHLCV data and returns pass / fail.
Only symbols that PASS can be traded by agents.

Features:
  • Walk-forward / out-of-sample validation (avoids overfitting)
  • Trade-level CSV export
  • Equity curve data (for chart generation)
  • Multi-strategy comparison
  • Scheduled weekly auto-backtest (triggered from master_agent_v5)

Usage:
    result = backtest_engine.run("RELIANCE", "NSE", "intraday")
    if result["passed"]:
        # allow this symbol for intraday trading
"""
from __future__ import annotations

import io
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime

import matplotlib
matplotlib.use("Agg")   # non-interactive backend — no display needed
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import ta
from loguru import logger

from config import settings
from market_data import yf_client


# ── Backtest result ──────────────────────────────────────────────────────────────

@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    passed: bool
    total_trades: int       = 0
    wins: int               = 0
    losses: int             = 0
    win_rate: float         = 0.0
    total_pnl: float        = 0.0
    avg_win: float          = 0.0
    avg_loss: float         = 0.0
    max_drawdown_pct: float = 0.0
    sharpe_ratio: float     = 0.0
    profit_factor: float    = 0.0
    best_trade: float       = 0.0
    worst_trade: float      = 0.0
    fail_reasons: list[str] = field(default_factory=list)
    # Walk-forward OOS metrics
    oos_win_rate: float          = 0.0
    oos_total_pnl: float         = 0.0
    oos_trades: int              = 0
    oos_sharpe: float | None     = None
    walk_forward_used: bool      = False
    # Monte Carlo
    mc_pvalue: float             = 1.0
    mc_passed: bool              = False
    sharpe_percentile: float | None  = None   # % of permutations with Sharpe ≤ real
    min_sharpe_5pct: float | None    = None   # 5th pct of permuted Sharpes
    max_drawdown_95pct: float | None = None   # 95th pct of permuted max drawdowns
    calmar_ratio: float = 0.0               # annualised_return / abs(max_drawdown_pct)
    # Raw trade log and equity curve (not serialised by default)
    trades: list[dict]      = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    def to_dict(self, include_trades: bool = False) -> dict:
        d = {
            "symbol":            self.symbol,
            "strategy":          self.strategy,
            "passed":            self.passed,
            "total_trades":      self.total_trades,
            "wins":              self.wins,
            "losses":            self.losses,
            "win_rate":          round(self.win_rate, 1),
            "total_pnl":         round(self.total_pnl, 0),
            "avg_win":           round(self.avg_win, 0),
            "avg_loss":          round(self.avg_loss, 0),
            "max_drawdown_pct":  round(self.max_drawdown_pct, 1),
            "sharpe_ratio":      round(self.sharpe_ratio, 2),
            "profit_factor":     round(self.profit_factor, 2),
            "best_trade":        round(self.best_trade, 0),
            "worst_trade":       round(self.worst_trade, 0),
            "fail_reasons":      self.fail_reasons,
            "walk_forward_used": self.walk_forward_used,
            "oos_win_rate":      round(self.oos_win_rate, 1),
            "oos_total_pnl":     round(self.oos_total_pnl, 0),
            "oos_trades":        self.oos_trades,
            "oos_sharpe":        round(self.oos_sharpe, 2) if self.oos_sharpe is not None else None,
            "calmar_ratio":      round(self.calmar_ratio, 2),
            "mc_pvalue":         round(self.mc_pvalue, 3),
            "mc_passed":         self.mc_passed,
            "monte_carlo": {
                "sharpe_percentile":  self.sharpe_percentile,
                "min_sharpe_5pct":    self.min_sharpe_5pct,
                "max_drawdown_95pct": self.max_drawdown_95pct,
                "mc_pvalue":          round(self.mc_pvalue, 3),
                "is_significant":     self.mc_passed,
            },
        }
        if include_trades:
            d["trades"] = self.trades
        return d

    def to_csv(self) -> str:
        """Return the trade log as a CSV string."""
        if not self.trades:
            return "trade_num,entry,exit,pnl,net_pnl,cost,bars,exit_reason\n"
        rows = ["trade_num,entry,exit,pnl,net_pnl,cost,bars,exit_reason"]
        for i, t in enumerate(self.trades, 1):
            rows.append(
                f"{i},{t['entry']:.2f},{t['exit']:.2f},"
                f"{t['pnl']:.2f},"
                f"{t.get('net_pnl', t['pnl']):.2f},"
                f"{t.get('cost', 0.0):.2f},"
                f"{t['bars']},{t['exit_reason']}"
            )
        return "\n".join(rows)

    def equity_chart_png(self) -> bytes:
        """Render equity curve as PNG bytes."""
        curve = self.equity_curve or [0.0]
        fig, axes = plt.subplots(2, 1, figsize=(10, 6),
                                 gridspec_kw={"height_ratios": [3, 1]})
        fig.suptitle(
            f"{self.symbol} — {self.strategy.upper()} | "
            f"W:{self.win_rate:.0f}%  PnL:₹{self.total_pnl:.0f}  "
            f"Sharpe:{self.sharpe_ratio:.2f}  DD:{self.max_drawdown_pct:.1f}%",
            fontsize=11,
        )

        # Equity curve
        ax = axes[0]
        xs = list(range(len(curve)))
        ax.plot(xs, curve, color="steelblue", linewidth=1.5)
        ax.fill_between(xs, curve, 0, where=[v >= 0 for v in curve],
                        alpha=0.15, color="green")
        ax.fill_between(xs, curve, 0, where=[v < 0 for v in curve],
                        alpha=0.15, color="red")
        ax.axhline(0, color="black", linewidth=0.7, linestyle="--")
        ax.set_ylabel("Cumulative PnL (₹)")
        ax.set_xlabel("Trade #")
        ax.grid(True, alpha=0.3)

        # Drawdown
        arr = np.array(curve)
        peak = np.maximum.accumulate(arr)
        dd   = arr - peak
        axes[1].fill_between(xs, dd, 0, color="red", alpha=0.4)
        axes[1].set_ylabel("Drawdown (₹)")
        axes[1].set_xlabel("Trade #")
        axes[1].grid(True, alpha=0.3)

        plt.tight_layout()
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100)
        plt.close(fig)
        buf.seek(0)
        return buf.read()


# ── Strategy parameters ──────────────────────────────────────────────────────────

STRATEGY_PARAMS = {
    "intraday": {
        "interval": "15minute",
        "sl_pct": 1.5,
        "target_pct": 3.0,
        "max_hold_bars": 20,
    },
    "options": {
        "interval": "60minute",
        "sl_pct": 5.0,
        "target_pct": 10.0,
        "max_hold_bars": 40,
    },
    "swing": {
        "interval": "day",
        "sl_pct": 3.0,
        "target_pct": 7.0,
        "max_hold_bars": 15,
    },
    "scalping": {
        "interval": "5minute",
        "sl_pct": 0.3,
        "target_pct": 0.6,
        "max_hold_bars": 12,
    },
}

ALL_STRATEGIES = list(STRATEGY_PARAMS.keys())


# ── Core backtest engine ─────────────────────────────────────────────────────────

class BacktestEngine:

    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], BacktestResult] = {}
        self._cache_lock = threading.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(
        self,
        symbol: str,
        exchange: str = "NSE",
        strategy: str = "intraday",
        lookback_days: int | None = None,
        force: bool = False,
        walk_forward: bool = True,
        n_folds: int | None = None,
        out_of_sample_pct: float | None = None,
    ) -> BacktestResult:
        key = (symbol, strategy)
        with self._cache_lock:
            if not force and key in self._cache:
                return self._cache[key]

        days   = lookback_days or settings.bt_lookback_days
        params = STRATEGY_PARAMS.get(strategy, STRATEGY_PARAMS["intraday"])

        df = self._fetch_data(symbol, exchange, params["interval"], days)
        if df is None or len(df) < 60:
            result = BacktestResult(
                symbol=symbol, strategy=strategy, passed=False,
                fail_reasons=[f"Insufficient data ({len(df) if df is not None else 0} bars)"]
            )
            with self._cache_lock:
                self._cache[key] = result
            return result

        oos_pct = out_of_sample_pct if out_of_sample_pct is not None else 0.30
        if walk_forward and len(df) >= 120:
            result = self._walk_forward_run(
                symbol, strategy, df, params,
                n_splits=n_folds,
                out_of_sample_pct=oos_pct,
            )
        else:
            signals = self._generate_signals(df, strategy)
            trades  = self._simulate_trades(df, signals, params, strategy_name=strategy)
            result  = self._compute_metrics(symbol, strategy, trades)

        result = self._apply_gate(result)
        with self._cache_lock:
            self._cache[key] = result
        logger.info(
            "Backtest {} {} → {} | trades={} win_rate={:.0f}% sharpe={:.2f} dd={:.1f}%{}",
            symbol, strategy, "PASS" if result.passed else "FAIL",
            result.total_trades, result.win_rate, result.sharpe_ratio,
            result.max_drawdown_pct,
            f" OOS={result.oos_win_rate:.0f}%/{result.oos_trades}T" if result.walk_forward_used else "",
        )
        return result

    def run_batch(
        self,
        symbols: list[dict],
        strategy: str,
        walk_forward: bool = True,
        force: bool = False,
    ) -> dict[str, BacktestResult]:
        results = {}
        for item in symbols:
            sym  = item["symbol"]
            exch = item.get("exchange", "NSE")
            results[sym] = self.run(sym, exch, strategy,
                                    walk_forward=walk_forward, force=force)
        return results

    def compare_strategies(
        self,
        symbol: str,
        exchange: str = "NSE",
        lookback_days: int | None = None,
        walk_forward: bool = True,
    ) -> dict:
        """Run all 4 strategies on the same symbol and rank by Sharpe ratio."""
        results: dict[str, BacktestResult] = {}
        for strat in ALL_STRATEGIES:
            results[strat] = self.run(
                symbol, exchange, strat,
                lookback_days=lookback_days,
                force=True,
                walk_forward=walk_forward,
            )

        ranked = sorted(
            results.items(),
            key=lambda kv: (kv[1].passed, kv[1].sharpe_ratio),
            reverse=True,
        )
        best = ranked[0][0] if ranked else None

        return {
            "symbol":  symbol,
            "best_strategy": best,
            "ranking": [
                {
                    "rank":     i + 1,
                    "strategy": strat,
                    "passed":   r.passed,
                    "sharpe":   round(r.sharpe_ratio, 2),
                    "win_rate": round(r.win_rate, 1),
                    "total_pnl": round(r.total_pnl, 0),
                    "oos_win_rate": round(r.oos_win_rate, 1),
                }
                for i, (strat, r) in enumerate(ranked)
            ],
            "details": {s: r.to_dict() for s, r in results.items()},
        }

    def weekly_auto_backtest(self, universe: list[dict] | None = None) -> dict:
        """
        Run full batch across all strategies for the given universe.
        Called by the master agent every Sunday night.
        Refreshes the approved-symbols cache.
        """
        from symbol_scanner import FULL_UNIVERSE
        syms = universe or [{"symbol": s, "exchange": "NSE"} for s in FULL_UNIVERSE]
        summary: dict[str, dict] = {}
        for strat in ALL_STRATEGIES:
            # force=True: the weekly refresh must re-run on fresh data, never
            # serve last week's cached results.
            res = self.run_batch(syms, strat, walk_forward=True, force=True)
            passed = [s for s, r in res.items() if r.passed]
            failed = [s for s, r in res.items() if not r.passed]
            summary[strat] = {
                "passed": passed,
                "failed": failed,
                "pass_count": len(passed),
                "fail_count": len(failed),
            }
            logger.info(
                "[weekly_bt] {} → {}/{} symbols approved",
                strat, len(passed), len(syms),
            )
        return summary

    def get_approved_symbols(self, strategy: str) -> list[str]:
        return [
            sym for (sym, strat), res in self._cache.items()
            if strat == strategy and res.passed
        ]

    def is_approved(self, symbol: str, strategy: str) -> bool:
        key = (symbol, strategy)
        return key in self._cache and self._cache[key].passed

    def clear_cache(self) -> None:
        self._cache.clear()
        logger.info("Backtest cache cleared")

    # ── Walk-forward ───────────────────────────────────────────────────────────

    def _walk_forward_run(
        self,
        symbol: str,
        strategy: str,
        df: pd.DataFrame,
        params: dict,
        n_splits: int | None = None,
        train_frac: float | None = None,
        out_of_sample_pct: float = 0.30,
    ) -> BacktestResult:
        """
        Anchored walk-forward with DISJOINT out-of-sample windows.
        Fold i: OOS = [n*i/k, n*(i+1)/k); train = everything before the OOS
        start (expanding window). Fold 0 is skipped — it has no training data.
        Disjoint OOS folds guarantee no trade is counted twice, so oos_sharpe /
        oos_trades are not inflated by overlapping windows.
        IS result comes from the full dataset; OOS metrics come from OOS folds.
        (train_frac / out_of_sample_pct are retained for API compatibility but
        no longer shape the folds.)
        """
        n_splits    = n_splits or getattr(settings, "bt_wf_folds", 12)
        min_oos_t   = getattr(settings, "bt_min_oos_trades", 15)
        n           = len(df)
        # Ensure at least 2 folds with enough data
        n_splits    = max(2, min(n_splits, n // 20))

        oos_trades_all: list[dict] = []

        for i in range(n_splits):
            oos_start = int(n * i / n_splits)
            oos_end   = int(n * (i + 1) / n_splits)
            if oos_start <= 0:
                # Fold 0: train window [0, oos_start) would be empty — skip.
                continue
            oos_df = df.iloc[oos_start:oos_end]
            if len(oos_df) < 20:
                continue
            sigs = self._generate_signals(oos_df.copy(), strategy)
            fold_trades = self._simulate_trades(oos_df, sigs, params, strategy_name=strategy)
            oos_trades_all.extend(fold_trades)

        # Full-data IS result (for primary metrics)
        signals = self._generate_signals(df, strategy)
        trades  = self._simulate_trades(df, signals, params, strategy_name=strategy)
        result  = self._compute_metrics(symbol, strategy, trades)

        # Attach OOS metrics
        if oos_trades_all:
            oos_pnls = [t.get("net_pnl", t["pnl"]) for t in oos_trades_all]
            oos_wins = [p for p in oos_pnls if p > 0]
            result.oos_win_rate  = len(oos_wins) / len(oos_pnls) * 100
            result.oos_total_pnl = sum(oos_pnls)
            result.oos_trades    = len(oos_pnls)
            if len(oos_pnls) >= 2:
                arr = np.array(oos_pnls, dtype=float)
                std = float(arr.std(ddof=1))  # sample std — population std inflates Sharpe on small OOS folds
                if std > 0:
                    result.oos_sharpe = round(
                        float(arr.mean()) / std * math.sqrt(len(arr)), 2
                    )

        # Gate: require minimum OOS trades. oos_trades == 0 is a FAIL, not a
        # skip — a symbol must never pass on in-sample performance alone.
        if result.oos_trades < min_oos_t:
            result.fail_reasons.append(
                f"Insufficient OOS trades ({result.oos_trades} < {min_oos_t})"
            )

        result.walk_forward_used = True
        return result

    # ── Data fetching ──────────────────────────────────────────────────────────

    _YF_INTERVAL = {
        "15minute": "15m", "5minute": "5m", "60minute": "60m",
        "day": "1d", "minute": "1m",
    }
    _YF_PERIOD = {
        10: "5d", 30: "1mo", 90: "3mo", 180: "6mo", 365: "1y", 730: "2y",
    }

    # ── Slippage BPS per strategy ──────────────────────────────────────────
    _SLIPPAGE_BPS = {
        "intraday": 10,
        "scalping":  5,
        "swing":     3,
        "options":  15,
    }

    def _fetch_data(self, symbol: str, exchange: str, interval: str, days: int):
        yf_interval = self._YF_INTERVAL.get(interval, "15m")
        # Default to "2y" (the max available via yfinance) so that if `days`
        # exceeds 730 the fetch still requests the maximum rather than silently
        # falling back to the loop's starting value of "6mo".
        period = "2y"
        for d, p in sorted(self._YF_PERIOD.items()):
            if days <= d:
                period = p
                break
        df = yf_client.historical(symbol, exchange, yf_interval, period)
        return None if df.empty else df

    # ── Signal generation ──────────────────────────────────────────────────────

    def _generate_signals(self, df: pd.DataFrame, strategy: str) -> pd.Series:
        close  = df["close"]
        high   = df["high"]
        low    = df["low"]
        volume = df["volume"]
        signals = pd.Series(0, index=df.index)

        n = len(df)

        if strategy == "intraday":
            ema9  = ta.trend.EMAIndicator(close, 9).ema_indicator()
            ema21 = ta.trend.EMAIndicator(close, 21).ema_indicator()
            rsi   = ta.momentum.RSIIndicator(close, 14).rsi()
            vwap  = ta.volume.VolumeWeightedAveragePrice(high, low, close, volume).volume_weighted_average_price()
            for i in range(2, n - 1):
                vwap_cross = close.iloc[i-1] < vwap.iloc[i-1] and close.iloc[i] > vwap.iloc[i]
                ema_bull   = ema9.iloc[i] > ema21.iloc[i]
                rsi_ok     = 45 < rsi.iloc[i] < 65
                if vwap_cross and ema_bull and rsi_ok:
                    # Signal confirmed on bar i; entry fills at open of bar i+1 (no look-ahead).
                    signals.iloc[i + 1] = 1

        elif strategy == "options":
            rsi    = ta.momentum.RSIIndicator(close, 14).rsi()
            atr    = ta.volatility.AverageTrueRange(high, low, close, 14).average_true_range()
            atr_ma = atr.rolling(30).mean()
            for i in range(30, n - 1):
                _atr_ma_i = float(atr_ma.iloc[i])
                iv_proxy = (float(atr.iloc[i]) / _atr_ma_i * 50) if (math.isfinite(_atr_ma_i) and _atr_ma_i != 0) else 50
                if iv_proxy < 40 and rsi.iloc[i] < 40:
                    signals.iloc[i + 1] = 1
                elif iv_proxy < 40 and rsi.iloc[i] > 60:
                    signals.iloc[i + 1] = 1

        elif strategy == "swing":
            ema50 = ta.trend.EMAIndicator(close, 50).ema_indicator()
            ema20 = ta.trend.EMAIndicator(close, 20).ema_indicator()
            rsi   = ta.momentum.RSIIndicator(close, 14).rsi()
            for i in range(50, n - 1):
                near   = abs(close.iloc[i] - ema50.iloc[i]) / ema50.iloc[i] < 0.015
                ema_up = ema20.iloc[i] > ema50.iloc[i]
                rsi_ok = 40 < rsi.iloc[i] < 60
                if near and ema_up and rsi_ok:
                    signals.iloc[i + 1] = 1

        elif strategy == "scalping":
            ema9   = ta.trend.EMAIndicator(close, 9).ema_indicator()
            rsi    = ta.momentum.RSIIndicator(close, 7).rsi()
            vol_ma = volume.rolling(10).mean()
            for i in range(10, n - 1):
                cross = close.iloc[i-1] < ema9.iloc[i-1] and close.iloc[i] > ema9.iloc[i]
                spike = volume.iloc[i] > vol_ma.iloc[i] * 1.5
                mom   = 50 < rsi.iloc[i] < 70
                if cross and spike and mom:
                    signals.iloc[i + 1] = 1

        return signals

    # ── Trade simulation ───────────────────────────────────────────────────────

    def _simulate_trades(
        self,
        df: pd.DataFrame,
        signals: pd.Series,
        params: dict,
        strategy_name: str = "",
    ) -> list[dict]:
        sl_pct   = params["sl_pct"] / 100
        tgt_pct  = params["target_pct"] / 100
        max_bars = params["max_hold_bars"]

        # Slippage model
        apply_costs = getattr(settings, "bt_apply_tx_costs", True)
        slippage_bps = self._SLIPPAGE_BPS.get(strategy_name, 10)
        if strategy_name:
            # Allow per-strategy override from settings
            attr = f"bt_slippage_bps_{strategy_name}"
            slippage_bps = getattr(settings, attr, slippage_bps)
        slip = slippage_bps / 10000

        # Infer product from strategy for tx cost calculation
        _product_map = {"swing": "CNC", "intraday": "MIS", "scalping": "MIS", "options": "NRML"}
        product = _product_map.get(strategy_name, "MIS")

        trades: list[dict] = []
        in_trade    = False
        entry_price = 0.0
        entry_fill  = 0.0
        entry_idx   = 0

        for i in range(len(df)):
            if in_trade:
                ltp       = df["close"].iloc[i]
                bars_held = i - entry_idx
                sl        = entry_fill * (1 - sl_pct)
                tgt       = entry_fill * (1 + tgt_pct)
                low_i     = df["low"].iloc[i]
                high_i    = df["high"].iloc[i]

                if low_i <= sl:
                    exit_fill = sl * (1 - slip) if apply_costs else sl
                    gross_pnl = exit_fill - entry_fill
                    qty = max(1, int(100_000 // entry_fill)) if entry_fill > 0 else 1
                    cost = 0.0
                    if apply_costs:
                        from risk_manager import compute_tx_costs
                        cost = compute_tx_costs(qty, entry_fill, exit_fill, product)
                    net_pnl = gross_pnl * qty - cost
                    trades.append({
                        "entry": entry_fill, "exit": exit_fill,
                        "pnl": gross_pnl * qty,
                        "net_pnl": net_pnl, "cost": cost,
                        "bars": bars_held, "exit_reason": "SL",
                    })
                    in_trade = False
                elif high_i >= tgt:
                    exit_fill = tgt * (1 - slip) if apply_costs else tgt
                    gross_pnl = exit_fill - entry_fill
                    qty = max(1, int(100_000 // entry_fill)) if entry_fill > 0 else 1
                    cost = 0.0
                    if apply_costs:
                        from risk_manager import compute_tx_costs
                        cost = compute_tx_costs(qty, entry_fill, exit_fill, product)
                    net_pnl = gross_pnl * qty - cost
                    trades.append({
                        "entry": entry_fill, "exit": exit_fill,
                        "pnl": gross_pnl * qty,
                        "net_pnl": net_pnl, "cost": cost,
                        "bars": bars_held, "exit_reason": "TGT",
                    })
                    in_trade = False
                elif bars_held >= max_bars:
                    exit_fill = ltp * (1 - slip) if apply_costs else ltp
                    gross_pnl = exit_fill - entry_fill
                    qty = max(1, int(100_000 // entry_fill)) if entry_fill > 0 else 1
                    cost = 0.0
                    if apply_costs:
                        from risk_manager import compute_tx_costs
                        cost = compute_tx_costs(qty, entry_fill, exit_fill, product)
                    net_pnl = gross_pnl * qty - cost
                    trades.append({
                        "entry": entry_fill, "exit": exit_fill,
                        "pnl": gross_pnl * qty,
                        "net_pnl": net_pnl, "cost": cost,
                        "bars": bars_held, "exit_reason": "TIMEOUT",
                    })
                    in_trade = False

            elif signals.iloc[i] == 1:
                # Fill at open[i] — earliest executable price after signal confirmed on prior bar close.
                # Using close[i] would be look-ahead bias (bar not yet closed when signal fires).
                entry_price = df["open"].iloc[i] if "open" in df.columns else df["close"].iloc[i]
                # Apply entry slippage (buy at slightly higher price)
                entry_fill  = entry_price * (1 + slip) if apply_costs else entry_price
                entry_idx   = i
                in_trade    = True

        return trades

    # ── Metrics ────────────────────────────────────────────────────────────────

    def _compute_metrics(
        self, symbol: str, strategy: str, trades: list[dict]
    ) -> BacktestResult:
        if not trades:
            return BacktestResult(
                symbol=symbol, strategy=strategy, passed=False,
                fail_reasons=["Zero trades generated"],
            )

        # Prefer net_pnl (after tx costs) when available
        pnls   = [t.get("net_pnl", t["pnl"]) for t in trades]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        total_pnl = sum(pnls)
        win_rate  = len(wins) / len(pnls) * 100 if pnls else 0
        avg_win   = float(np.mean(wins))              if wins   else 0.0
        avg_loss  = float(np.mean([abs(l) for l in losses])) if losses else 0.0

        pnl_arr = np.array(pnls)
        if len(pnl_arr) > 1 and pnl_arr.std(ddof=1) > 0:
            sharpe = float((pnl_arr.mean() / pnl_arr.std(ddof=1)) * math.sqrt(len(pnls)))
        else:
            sharpe = 0.0

        cumulative = np.cumsum(pnls).tolist()
        cum_arr    = np.array(cumulative)
        peak       = np.maximum.accumulate(cum_arr)
        drawdown   = peak - cum_arr
        max_dd     = float(np.max(drawdown)) if len(drawdown) else 0.0
        denom = max(abs(float(peak.max())), abs(float(cum_arr.min())), 1.0)
        max_dd_pct = (max_dd / denom) * 100

        gross_profit = sum(wins)              if wins   else 0.0
        gross_loss   = sum(abs(l) for l in losses) if losses else 0.0
        # When gross_loss == 0 (all-win sequence), profit_factor is mathematically
        # infinite. Cap to 999.0 so to_dict() doesn't emit a raw currency value.
        pf           = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)

        n_trades = max(len(pnls), 1)
        # Annualise using trade count; assume ~5 trading days per trade on average.
        # Cap at 4× (roughly 4 years max annualisation) to prevent extreme blow-up on
        # small samples.
        ann_factor = min(252.0 / max(n_trades / 5, 1), 4.0)
        capital = 100_000.0  # reference capital for return calculation
        ann_return = (total_pnl / capital) * ann_factor
        calmar = ann_return / abs(max_dd_pct / 100) if max_dd_pct != 0 else 0.0

        return BacktestResult(
            symbol=symbol, strategy=strategy, passed=False,
            total_trades=len(trades), wins=len(wins), losses=len(losses),
            win_rate=win_rate, total_pnl=total_pnl, avg_win=avg_win, avg_loss=avg_loss,
            max_drawdown_pct=max_dd_pct, sharpe_ratio=sharpe, profit_factor=pf,
            best_trade=max(pnls) if pnls else 0.0,
            worst_trade=min(pnls) if pnls else 0.0,
            calmar_ratio=round(calmar, 2),
            trades=trades,
            equity_curve=cumulative,
        )

    # ── Pass/fail gate ─────────────────────────────────────────────────────────

    def _apply_gate(self, r: BacktestResult) -> BacktestResult:
        reasons: list[str] = []
        if r.total_trades < settings.bt_min_trades:
            reasons.append(f"Too few trades ({r.total_trades} < {settings.bt_min_trades})")
        if r.win_rate < settings.bt_min_win_rate:
            reasons.append(f"Win rate too low ({r.win_rate:.0f}% < {settings.bt_min_win_rate:.0f}%)")
        if r.sharpe_ratio < settings.bt_min_sharpe:
            reasons.append(f"Sharpe too low ({r.sharpe_ratio:.2f} < {settings.bt_min_sharpe})")
        if r.max_drawdown_pct > settings.bt_max_drawdown_pct:
            reasons.append(f"Drawdown too high ({r.max_drawdown_pct:.1f}% > {settings.bt_max_drawdown_pct:.0f}%)")
        if r.calmar_ratio < settings.bt_min_calmar and r.total_trades >= settings.bt_min_trades:
            reasons.append(f"Calmar too low ({r.calmar_ratio:.2f} < {settings.bt_min_calmar:.1f})")
        if r.total_pnl <= 0:
            reasons.append(f"Negative total P&L (₹{r.total_pnl:.0f})")
        # Walk-forward OOS gate: OOS win rate must not be below 80% of IS win rate
        if r.walk_forward_used and r.oos_trades >= 5:
            oos_floor = r.win_rate * 0.80
            if r.oos_win_rate < oos_floor:
                reasons.append(
                    f"OOS win rate degraded ({r.oos_win_rate:.0f}% < {oos_floor:.0f}% floor)"
                )
        # Carry over OOS trade count failures from walk-forward
        if r.fail_reasons:
            for fr in r.fail_reasons:
                if fr not in reasons:
                    reasons.append(fr)

        # Always run Monte Carlo to populate stats; gate on bt_require_mc_pass
        if r.trades:
            n_perms = getattr(settings, "bt_mc_permutations", 1000)
            mc_result = self._monte_carlo_test(r.trades, n_perms)
            r.mc_pvalue          = mc_result["mc_pvalue"]
            r.mc_passed          = mc_result["mc_passed"]
            r.sharpe_percentile  = mc_result.get("sharpe_percentile")
            r.min_sharpe_5pct    = mc_result.get("min_sharpe_5pct")
            r.max_drawdown_95pct = mc_result.get("max_drawdown_95pct")
            require_mc = getattr(settings, "bt_require_mc_pass", False)
            if require_mc and not r.mc_passed:
                reasons.append(
                    f"Monte Carlo failed (p={r.mc_pvalue:.3f} >= 0.10 threshold)"
                )

        r.passed       = len(reasons) == 0
        r.fail_reasons = reasons
        return r

    # ── Monte Carlo permutation test ───────────────────────────────────────────

    def _monte_carlo_test(
        self,
        trades: list[dict],
        n_permutations: int = 1000,
    ) -> dict:
        """
        Permutation test using Calmar ratio (return / max-drawdown).
        p-value = fraction of shuffled Calmar values >= real Calmar.
        Passes when p-value < 0.10 (real edge is in top 10% of random).
        Also reports Sharpe-based stats across permutations:
          sharpe_percentile  — % of permutations with Sharpe ≤ real Sharpe
          min_sharpe_5pct    — 5th percentile of permuted Sharpes
          max_drawdown_95pct — 95th percentile of permuted max drawdowns
        """
        _no_data = {
            "mc_pvalue": 1.0, "mc_passed": False,
            "sharpe_percentile": None, "min_sharpe_5pct": None, "max_drawdown_95pct": None,
        }
        if len(trades) < 20:
            return _no_data

        def _calmar(pnls: np.ndarray) -> float:
            cumsum = np.cumsum(pnls)
            peak   = np.maximum.accumulate(cumsum)
            max_dd = float(np.max(peak - cumsum)) + 1e-9
            return float(pnls.sum()) / max_dd

        def _sharpe(pnls: np.ndarray) -> float:
            std = float(pnls.std())
            return float(pnls.mean()) / std * math.sqrt(len(pnls)) if std > 1e-9 else 0.0

        def _max_dd(pnls: np.ndarray) -> float:
            cum  = np.cumsum(pnls)
            peak = np.maximum.accumulate(cum)
            return float(np.max(peak - cum))

        real_pnls   = np.array([t.get("net_pnl", t["pnl"]) for t in trades])
        real_calmar = _calmar(real_pnls)
        real_sharpe = _sharpe(real_pnls)

        perm_calmars:   list[float] = []
        perm_sharpes:   list[float] = []
        perm_drawdowns: list[float] = []

        # Use a local RNG instance (not the global numpy state) so concurrent
        # run_batch calls from multiple API requests don't race on shared RNG
        # state, producing non-reproducible and mutually corrupted MC p-values.
        _rng = np.random.default_rng()
        for _ in range(n_permutations):
            p = _rng.permutation(real_pnls)
            perm_calmars.append(_calmar(p))
            perm_sharpes.append(_sharpe(p))
            perm_drawdowns.append(_max_dd(p))

        pvalue = sum(1 for c in perm_calmars if c >= real_calmar) / n_permutations

        ps = np.array(perm_sharpes)
        pd_arr = np.array(perm_drawdowns)
        return {
            "mc_pvalue":          round(pvalue, 3),
            "mc_passed":          pvalue < 0.10,
            "sharpe_percentile":  round(float(np.mean(ps <= real_sharpe) * 100), 1),
            "min_sharpe_5pct":    round(float(np.percentile(ps, 5)), 2),
            "max_drawdown_95pct": round(float(np.percentile(pd_arr, 95)), 2),
        }

    # ── Stress test ────────────────────────────────────────────────────────────

    def stress_test(self, symbol: str, strategy: str = "intraday") -> dict:
        """
        Replay cached trades under 3 adversarial scenarios:
          1. Bear market: shift all PnLs down by 30% (simulate trending loss environment)
          2. High volatility: inflate losses by 50%, reduce wins by 20%
          3. Slippage shock: add 3× normal slippage to every exit
        """
        key    = (symbol, strategy)
        result = self._cache.get(key)
        if not result or not result.trades:
            return {
                "symbol": symbol, "strategy": strategy,
                "error": "No cached backtest. Run /backtest/run first.",
            }

        def _metrics(pnls: list) -> dict:
            if not pnls:
                return {"total_pnl": 0, "win_rate": 0.0, "sharpe": 0.0, "max_dd_pct": 0.0}
            arr  = np.array(pnls)
            wins = [p for p in pnls if p > 0]
            cum  = np.cumsum(pnls)
            peak = np.maximum.accumulate(cum)
            dd   = peak - cum
            mx   = float(np.max(dd)) if len(dd) else 0.0
            pk   = float(peak.max())
            max_dd = round(mx / max(abs(pk), 1) * 100, 1) if pk != 0 else 0.0
            return {
                "total_pnl":   round(float(arr.sum()), 0),
                "win_rate":    round(len(wins) / len(pnls) * 100, 1),
                "sharpe":      round(float(arr.mean() / (arr.std() + 1e-9)), 2),
                "max_dd_pct":  max_dd,
            }

        base_pnls = [t.get("net_pnl", t["pnl"]) for t in result.trades]

        # Scenario 1: Bear market (-30% bias on all trades)
        bear_pnls = [p * 0.7 - abs(p) * 0.1 for p in base_pnls]

        # Scenario 2: High volatility (losses ×1.5, wins ×0.8)
        vol_pnls  = [p * 0.8 if p > 0 else p * 1.5 for p in base_pnls]

        # Scenario 3: Slippage shock (3× slippage applied to each trade)
        slippage_bps = self._SLIPPAGE_BPS.get(strategy, 10) * 3
        slip = slippage_bps / 10000
        shock_pnls = []
        for t in result.trades:
            entry     = t.get("entry", 1.0)
            extra_cost = entry * slip * 2  # extra round-trip slip
            shock_pnls.append(t.get("net_pnl", t["pnl"]) - extra_cost)

        return {
            "symbol":    symbol,
            "strategy":  strategy,
            "base":      _metrics(base_pnls),
            "scenarios": {
                "bear_market": {
                    "description": "30% adverse PnL bias (bear trending environment)",
                    **_metrics(bear_pnls),
                },
                "high_volatility": {
                    "description": "Losses ×1.5, wins ×0.8 (volatile choppy market)",
                    **_metrics(vol_pnls),
                },
                "slippage_shock": {
                    "description": f"3× normal slippage ({slippage_bps} bps) applied",
                    **_metrics(shock_pnls),
                },
            },
        }

    def _max_dd_pct(self, pnls: list) -> float:
        if not pnls:
            return 0.0
        cum  = np.cumsum(pnls)
        peak = np.maximum.accumulate(cum)
        dd   = peak - cum
        mx   = float(np.max(dd))
        pk   = float(peak.max())
        return round(mx / max(abs(pk), 1) * 100, 1) if pk != 0 else 0.0


backtest_engine = BacktestEngine()
