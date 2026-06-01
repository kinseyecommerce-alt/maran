"""
profit_optimizer.py
Three-phase profit maximisation pipeline powered by walk-forward backtests.

Phase 1 — Swarm parameter optimisation
  Grid-search SL%, target%, min_score for each strategy via walk-forward backtest.
  Saves best params to optimised_params.json.

Phase 2 — Gate threshold sweep
  Test claude_gate_threshold 45–65 against trade history win-rate model.
  Saves best threshold to optimised_params.json.

Phase 3 — Regime-aware config
  Combines Phase 1 + 2 results per detected regime (bull/bear/sideways/volatile).
  Saves regime_config.json — loaded by master_agent_v5 on each regime change.

Usage:
    python profit_optimizer.py                 # full pipeline
    python profit_optimizer.py --phase 1       # param optimisation only
    python profit_optimizer.py --phase 2       # threshold sweep only
    python profit_optimizer.py --phase 3       # build regime config only
    python profit_optimizer.py --apply         # apply best config to live settings
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any

from loguru import logger

from backtest_engine import BacktestEngine, BacktestResult
from config import settings

_OUT_DIR   = Path("logs/optimizer")
_PARAMS_F  = _OUT_DIR / "optimised_params.json"
_REGIME_F  = _OUT_DIR / "regime_config.json"
_OUT_DIR.mkdir(parents=True, exist_ok=True)

_ENGINE = BacktestEngine()

# ── Symbols with confirmed simulation data ─────────────────────────────────
_SYMBOLS = [
    d for d in os.listdir("logs/historical_data")
    if (Path("logs/historical_data") / d / "15m.csv").exists()
][:10]   # top 10 to keep runtime reasonable

# ── Parameter grids per strategy ─────────────────────────────────────────────
_GRIDS: dict[str, dict[str, list]] = {
    "intraday": {
        "sl_pct":    [1.0, 1.5, 2.0],
        "target_pct":[2.5, 3.0, 3.5, 4.0],
        "min_score": [3, 4, 5],
    },
    "scalping": {
        "sl_pct":    [0.2, 0.3, 0.4],
        "target_pct":[0.5, 0.7, 0.9],
        "min_score": [2, 3, 4],
    },
    "swing": {
        "sl_pct":    [2.0, 3.0, 4.0],
        "target_pct":[6.0, 8.0, 10.0],
        "min_score": [1, 2, 3],
    },
    "options": {
        "sl_pct":    [20.0, 25.0, 30.0],
        "target_pct":[50.0, 65.0, 80.0],
        "min_score": [5, 6, 7],
    },
    "futures": {
        "sl_pct":    [0.75, 1.0, 1.25],
        "target_pct":[1.5, 2.0, 2.5],
        "min_score": [3, 4, 5],
    },
}

_STRATEGY_TF = {
    "intraday": "15m",
    "scalping": "5m",
    "swing":    "1d",
    "options":  "1h",
    "futures":  "15m",
}


def _score(r: BacktestResult) -> float:
    """Composite profit score: penalise low trades, reward Sharpe×WinRate×Calmar."""
    if r.total_trades < 5:
        return -999.0
    sharpe  = max(r.sharpe_ratio, 0.0)
    calmar  = max(r.calmar_ratio, 0.0)
    wr      = r.win_rate / 100.0
    pnl_pts = r.total_pnl / max(abs(r.total_pnl) + 1, 1)
    return sharpe * wr * (1 + calmar * 0.3) + pnl_pts * 0.1


# ─────────────────────────────────────────────────────────────────────────────
# Phase 1 — Parameter optimisation
# ─────────────────────────────────────────────────────────────────────────────

def phase1_optimise(verbose: bool = True) -> dict[str, dict]:
    """Grid-search best SL/target/min_score per strategy."""
    best_params: dict[str, dict] = {}

    for strategy, grid in _GRIDS.items():
        tf = _STRATEGY_TF[strategy]
        symbols = [s for s in _SYMBOLS
                   if (Path("logs/historical_data") / s / f"{tf}.csv").exists()]
        if not symbols:
            logger.warning("Phase 1: no {} data for {}, skipping", tf, strategy)
            continue

        keys   = list(grid.keys())
        combos = list(itertools.product(*grid.values()))
        best_score = -999.0
        best_combo: dict = {}
        best_result_summary: dict = {}

        if verbose:
            print(f"\n  [{strategy}] testing {len(combos)} combos × {len(symbols)} symbols")

        for combo in combos:
            params = dict(zip(keys, combo))
            # Temporarily patch settings
            orig = {k: getattr(settings, f"{k.replace('pct','pct_')}{strategy}", None)
                    for k in keys}
            _patch_settings(strategy, params)

            combo_scores = []
            for sym in symbols:
                try:
                    r = _ENGINE.run(sym, strategy, use_walk_forward=True,
                                    lookback_days=settings.bt_lookback_days,
                                    n_folds=settings.bt_wf_folds)
                    combo_scores.append(_score(r))
                except Exception:
                    pass

            _restore_settings(strategy, orig)

            if not combo_scores:
                continue
            avg = sum(combo_scores) / len(combo_scores)
            if avg > best_score:
                best_score = avg
                best_combo = params
                best_result_summary = {"avg_score": round(avg, 3)}

        if best_combo:
            best_params[strategy] = {**best_combo, **best_result_summary}
            if verbose:
                print(f"    ✅  {strategy}: {best_combo}  score={best_score:.3f}")
        else:
            # No combo produced enough trades — keep current config as best known
            current = {
                "sl_pct":     getattr(settings, f"sl_pct_{strategy}", 1.5),
                "target_pct": getattr(settings, f"tgt_pct_{strategy}", 3.0),
                "min_score":  getattr(settings, f"min_score_{strategy}", 4),
                "avg_score":  0.0,
                "note":       "insufficient_backtest_data_kept_current",
            }
            best_params[strategy] = current
            if verbose:
                print(f"    ⚠   {strategy}: insufficient trades — kept current config")

    _save_params("phase1", best_params)
    return best_params


def _patch_settings(strategy: str, params: dict) -> None:
    for k, v in params.items():
        attr = f"{k.replace('pct', 'pct_')}{strategy}"
        if hasattr(settings, attr):
            setattr(settings, attr, v)
        elif k == "min_score":
            attr2 = f"min_score_{strategy}"
            if hasattr(settings, attr2):
                setattr(settings, attr2, int(v))


def _restore_settings(strategy: str, orig: dict) -> None:
    for k, v in orig.items():
        if v is None:
            continue
        attr = f"{k.replace('pct', 'pct_')}{strategy}"
        if hasattr(settings, attr):
            setattr(settings, attr, v)
        elif k == "min_score":
            attr2 = f"min_score_{strategy}"
            if hasattr(settings, attr2):
                setattr(settings, attr2, int(v))


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 — Gate threshold sweep
# ─────────────────────────────────────────────────────────────────────────────

def phase2_threshold_sweep(verbose: bool = True) -> dict:
    """
    Test claude_gate_threshold values 45–65 against simulated trade outcomes.
    Uses a simple model: lower threshold → more trades, but win_rate drops
    proportionally to how far below the 'natural' approval level we go.
    Calibrated from historical trade P&L distribution in state_store.
    """
    from state_store import get_trade_history

    trades = get_trade_history(days=90)
    real_pnl = [t for t in trades if abs(t.get("net_pnl", 0)) > 1]
    wins_pct = sum(1 for t in real_pnl if t.get("net_pnl", 0) > 0) / max(len(real_pnl), 1)
    if len(real_pnl) < 20 or wins_pct < 0.10:
        # Too few real trades or all losses — synthetic baseline gives a useful signal
        logger.info("Phase 2: using synthetic P&L model (real trades={}, win_pct={:.0%})",
                    len(real_pnl), wins_pct)
        trades = _synthetic_trades()
    else:
        trades = real_pnl
        logger.info("Phase 2: using {} real trades (win_pct={:.0%})", len(trades), wins_pct)

    best_threshold = 55
    best_score = -999.0
    results = []

    for threshold in range(45, 66, 5):
        wins = [t for t in trades if t.get("net_pnl", 0) > 0]
        losses = [t for t in trades if t.get("net_pnl", 0) <= 0]

        # Model: each 5pt below natural threshold adds ~8% more trades (marginal ones)
        # but their win rate is ~40% (vs 55% baseline for high-confidence trades)
        extra_trade_pct = max(0, (60 - threshold) / 5) * 0.08
        extra_trades_n  = int(len(trades) * extra_trade_pct)
        marginal_win_pct = 0.40 - max(0, (60 - threshold) / 5) * 0.02

        extra_wins   = int(extra_trades_n * marginal_win_pct)
        extra_losses = extra_trades_n - extra_wins

        total_wins   = len(wins)   + extra_wins
        total_losses = len(losses) + extra_losses
        total        = total_wins  + total_losses

        avg_win  = sum(t.get("net_pnl", 0) for t in wins)  / max(len(wins),  1)
        avg_loss = sum(t.get("net_pnl", 0) for t in losses) / max(len(losses), 1)

        simulated_pnl = total_wins * avg_win + total_losses * avg_loss
        win_rate = total_wins / max(total, 1) * 100

        # Profit factor (gross wins / gross losses)
        pf = abs(total_wins * avg_win) / max(abs(total_losses * avg_loss), 1)

        score = simulated_pnl * (win_rate / 100) * min(pf, 3.0)
        results.append({
            "threshold":     threshold,
            "simulated_pnl": round(simulated_pnl, 0),
            "win_rate":      round(win_rate, 1),
            "total_trades":  total,
            "profit_factor": round(pf, 2),
            "score":         round(score, 1),
        })

        if score > best_score:
            best_score = score
            best_threshold = threshold

    if verbose:
        print("\n  Gate threshold sweep:")
        print(f"  {'Threshold':>9} | {'Win%':>5} | {'Trades':>6} | {'SimPnL':>9} | {'PF':>4} | {'Score':>8}")
        print("  " + "-" * 55)
        for r in results:
            marker = " ◄ BEST" if r["threshold"] == best_threshold else ""
            print(f"  {r['threshold']:>9} | {r['win_rate']:>5.1f} | {r['total_trades']:>6} | "
                  f"₹{r['simulated_pnl']:>7.0f} | {r['profit_factor']:>4.2f} | {r['score']:>8.0f}{marker}")

    result = {"best_threshold": best_threshold, "sweep_results": results}
    _save_params("phase2", result)
    return result


def _synthetic_trades() -> list[dict]:
    """Generate synthetic P&L distribution for threshold sweep when no real trades exist."""
    import random
    random.seed(42)
    trades = []
    for _ in range(60):
        is_win = random.random() < 0.55
        pnl = random.uniform(500, 3000) if is_win else random.uniform(-2000, -300)
        trades.append({"net_pnl": round(pnl, 2)})
    return trades


# ─────────────────────────────────────────────────────────────────────────────
# Phase 3 — Regime-aware config
# ─────────────────────────────────────────────────────────────────────────────

def phase3_regime_config(p1: dict, p2: dict, verbose: bool = True) -> dict:
    """
    Build regime_config.json mapping each market regime to its optimal settings.

    Bull  → lower threshold, wider targets (capture full trend moves)
    Bear  → tighter SL, higher threshold (protect capital)
    Sideways → medium threshold, tighter targets (quick scalps)
    Volatile → highest threshold, smallest size (max protection)
    """
    base = deepcopy(p1)
    threshold = p2.get("best_threshold", 55)

    regime_config = {
        "BULL_TREND": {
            "claude_gate_threshold": max(45, threshold - 5),
            "description": "Strong uptrend — capture moves, lower bar for long entries",
            "strategy_overrides": {
                s: {
                    "sl_pct":     round(v.get("sl_pct", 1.5) * 0.9, 2),
                    "target_pct": round(v.get("target_pct", 3.0) * 1.2, 2),
                    "min_score":  max(2, v.get("min_score", 4) - 1),
                }
                for s, v in base.items()
            },
        },
        "BEAR_TREND": {
            "claude_gate_threshold": min(72, threshold + 10),
            "description": "Downtrend — protect capital, only highest-conviction shorts",
            "strategy_overrides": {
                s: {
                    "sl_pct":     round(v.get("sl_pct", 1.5) * 0.8, 2),
                    "target_pct": round(v.get("target_pct", 3.0) * 0.9, 2),
                    "min_score":  min(7, v.get("min_score", 4) + 1),
                }
                for s, v in base.items()
            },
        },
        "SIDEWAYS": {
            "claude_gate_threshold": threshold,
            "description": "Range-bound — take quick scalps, avoid swing positions",
            "strategy_overrides": {
                s: {
                    "sl_pct":     v.get("sl_pct", 1.5),
                    "target_pct": round(v.get("target_pct", 3.0) * 0.8, 2),
                    "min_score":  v.get("min_score", 4),
                }
                for s, v in base.items()
            },
        },
        "HIGH_VOLATILE": {
            "claude_gate_threshold": min(75, threshold + 15),
            "description": "High volatility — maximum protection, smallest size",
            "strategy_overrides": {
                s: {
                    "sl_pct":     round(v.get("sl_pct", 1.5) * 0.7, 2),
                    "target_pct": round(v.get("target_pct", 3.0) * 1.3, 2),
                    "min_score":  min(8, v.get("min_score", 4) + 2),
                }
                for s, v in base.items()
            },
        },
    }

    with open(_REGIME_F, "w") as f:
        json.dump({"generated_at": datetime.now().isoformat(),
                   "base_threshold": threshold,
                   "regimes": regime_config}, f, indent=2)

    if verbose:
        print(f"\n  Regime config → {_REGIME_F}")
        for regime, cfg in regime_config.items():
            print(f"    {regime:<16} threshold={cfg['claude_gate_threshold']}  "
                  f"— {cfg['description']}")

    return regime_config


# ─────────────────────────────────────────────────────────────────────────────
# Apply best config to live settings
# ─────────────────────────────────────────────────────────────────────────────

def apply_optimised_config(regime: str | None = None) -> dict:
    """
    Apply the optimised parameters to the live settings object.
    If regime is given, use regime-specific overrides; otherwise use base optimised params.
    """
    applied: dict[str, Any] = {}

    # Load regime config if available
    if _REGIME_F.exists() and regime:
        with open(_REGIME_F) as f:
            rc = json.load(f)
        regime_data = rc["regimes"].get(regime)
        if regime_data:
            t = regime_data["claude_gate_threshold"]
            settings.claude_gate_threshold = t
            applied["claude_gate_threshold"] = t
            for strategy, overrides in regime_data.get("strategy_overrides", {}).items():
                for k, v in overrides.items():
                    attr = f"{k.replace('pct', 'pct_')}{strategy}" if "pct" in k else f"{k}_{strategy}"
                    if hasattr(settings, attr):
                        setattr(settings, attr, v)
                        applied[attr] = v

    # Load base optimised params
    if _PARAMS_F.exists():
        with open(_PARAMS_F) as f:
            saved = json.load(f)
        p1 = saved.get("phase1", {})
        for strategy, params in p1.items():
            for k, v in params.items():
                if k in ("avg_score",):
                    continue
                attr = f"{k.replace('pct', 'pct_')}{strategy}" if "pct" in k else f"{k}_{strategy}"
                if hasattr(settings, attr):
                    setattr(settings, attr, v)
                    applied[attr] = v

    if not applied:
        return {"status": "nothing_to_apply", "hint": "Run profit_optimizer.py first"}

    logger.info("[optimizer] Applied {} optimised settings (regime={})", len(applied), regime)
    return {"status": "applied", "regime": regime, "settings": applied}


# ─────────────────────────────────────────────────────────────────────────────
# Persistence helpers
# ─────────────────────────────────────────────────────────────────────────────

def _save_params(phase: str, data: dict) -> None:
    existing: dict = {}
    if _PARAMS_F.exists():
        with open(_PARAMS_F) as f:
            existing = json.load(f)
    existing[phase] = data
    existing["updated_at"] = datetime.now().isoformat()
    with open(_PARAMS_F, "w") as f:
        json.dump(existing, f, indent=2)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="AlgoTrader profit optimiser")
    parser.add_argument("--phase", type=int, choices=[1, 2, 3], help="Run single phase only")
    parser.add_argument("--apply", action="store_true", help="Apply best config to live settings and exit")
    args = parser.parse_args()

    if args.apply:
        result = apply_optimised_config()
        print(json.dumps(result, indent=2))
        return

    t_start = time.monotonic()

    if args.phase == 1:
        print("── Phase 1: Parameter optimisation ─────────────────────────────")
        phase1_optimise()
    elif args.phase == 2:
        print("── Phase 2: Gate threshold sweep ────────────────────────────────")
        phase2_threshold_sweep()
    elif args.phase == 3:
        if not _PARAMS_F.exists():
            print("ERROR: run --phase 1 and --phase 2 first")
            return
        with open(_PARAMS_F) as f:
            saved = json.load(f)
        print("── Phase 3: Regime config ───────────────────────────────────────")
        phase3_regime_config(saved.get("phase1", {}), saved.get("phase2", {}))
    else:
        print("═" * 62)
        print("  AlgoTrader Profit Optimiser — full pipeline")
        print("═" * 62)

        print("\n── Phase 1: Parameter optimisation ─────────────────────────────")
        p1 = phase1_optimise()

        print("\n── Phase 2: Gate threshold sweep ────────────────────────────────")
        p2 = phase2_threshold_sweep()

        print("\n── Phase 3: Regime-aware config ─────────────────────────────────")
        phase3_regime_config(p1, p2)

        elapsed = time.monotonic() - t_start
        print(f"\n  Done in {elapsed:.1f}s")
        print(f"  Results → {_PARAMS_F}")
        print(f"  Regime config → {_REGIME_F}")
        print(f"\n  Apply to live settings:  python profit_optimizer.py --apply")


if __name__ == "__main__":
    main()
