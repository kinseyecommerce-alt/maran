"""
historical_learner.py
One-time pre-learning script. Run ONCE before going live.

Backtests all Nifty 100 × 4 strategies using historical data,
then pre-populates the adaptive engine so the bot starts with
2 years of learned knowledge instead of cold defaults.

Usage:
    cd algotrader_v4
    python3 historical_learner.py

Or with strategy filter:
    python3 historical_learner.py --strategies intraday,scalping
    python3 historical_learner.py --resume       # skip already-done symbols
    python3 historical_learner.py --symbols RELIANCE,TCS,HDFCBANK
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime

from ist_clock import now_ist as _now_ist
from pathlib import Path

from loguru import logger

# Configure clean console output for this script
logger.remove()
logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> {message}", level="INFO")

PROGRESS_FILE = Path("logs/learning_progress.json")
APPROVED_FILE = Path("logs/approved_symbols.json")
Path("logs").mkdir(exist_ok=True)

# Keys must match live agent names (ALL_AGENTS) — filter_watchlist and the
# adaptive engine key off them. Pairs is excluded: its two-symbol spread has
# no single-symbol backtest representation (evaluated in live paper only).
ALL_STRATEGIES = ["intraday", "scalping", "options", "swing",
                  "momentum", "mean_reversion", "futures"]


def _load_progress() -> dict:
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text())
    return {}


def _save_progress(progress: dict) -> None:
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(progress, f, indent=2)
        tmp.replace(PROGRESS_FILE)
    except Exception as exc:
        logger.warning("_save_progress failed: {}", exc)
        tmp.unlink(missing_ok=True)


def _load_approved() -> dict:
    if APPROVED_FILE.exists():
        return json.loads(APPROVED_FILE.read_text())
    return {s: [] for s in ALL_STRATEGIES}


def _save_approved(approved: dict) -> None:
    tmp = APPROVED_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(approved, f, indent=2, default=str)
        tmp.replace(APPROVED_FILE)
    except Exception as exc:
        logger.warning("_save_approved failed: {}", exc)
        tmp.unlink(missing_ok=True)
    # Mirror to state_store — approved_symbols.json gates filter_watchlist()
    # (SKIP_STARTUP_BACKTEST) and the container filesystem is ephemeral.
    try:
        from state_store import set_kv
        set_kv("approved_symbols", json.dumps(approved, default=str))
    except Exception as exc:
        logger.debug("_save_approved kv mirror failed (non-critical): {}", exc)


async def _run_one(symbol: str, strategy: str, sem: asyncio.Semaphore,
                   approved: dict, progress: dict) -> tuple[str, str, bool]:
    key = f"{strategy}::{symbol}"
    async with sem:
        try:
            from backtest_engine import backtest_engine
            from adaptive_engine import adaptive_engine, AdaptiveParams

            result = await asyncio.to_thread(
                backtest_engine.run, symbol, "NSE", strategy
            )

            # Pre-populate adaptive engine with learned params
            params_key = f"{strategy}::{symbol}"
            with adaptive_engine._lock:
                existing = adaptive_engine._params.get(params_key)
            if existing is None or existing.win_rate_20 < 1e-9:
                # Seed with backtest results
                from adaptive_engine import STRATEGY_CONFIG, GATE_THRESHOLDS
                cfg  = STRATEGY_CONFIG.get(strategy, {})
                gate = GATE_THRESHOLDS.get(strategy, {})
                new_params = AdaptiveParams(
                    strategy=strategy, symbol=symbol,
                    sl_pct=result.optimal_sl if hasattr(result, "optimal_sl") else cfg.get("sl", 1.5),
                    target_pct=result.optimal_target if hasattr(result, "optimal_target") else cfg.get("t1", 3.0),
                    trail_pct=(result.optimal_sl if hasattr(result, "optimal_sl") else cfg.get("sl", 1.5)) * 0.33,
                    win_rate_20=result.win_rate / 100.0,
                    sharpe_20=result.sharpe_ratio,
                    avg_win_pct=result.avg_win_pct if hasattr(result, "avg_win_pct") else cfg.get("t1", 3.0) * 0.6,
                    avg_loss_pct=result.avg_loss_pct if hasattr(result, "avg_loss_pct") else cfg.get("sl", 1.5) * 0.7,
                    min_rsi=45.0 if result.win_rate >= 55 else 48.0,
                    max_rsi=67.0 if result.win_rate >= 55 else 63.0,
                    min_adx=20.0,
                    status="ACTIVE" if result.passed else "CAUTIOUS",
                )
                with adaptive_engine._lock:
                    adaptive_engine._params[params_key] = new_params

            # Hard pass = the full LIVE gate (win≥55%, sharpe≥1, dd≤15%, OOS).
            # Soft pass = positive-expectancy edge — approved for trading with
            # CAUTIOUS adaptive status (conviction sizing halves position size).
            # Without the soft tier the strict gate can approve ZERO symbols
            # market-wide, and filter_watchlist would then leave every agent
            # with nothing to trade.
            soft_pass = (not result.passed
                         and result.total_trades >= 5
                         and result.total_pnl > 0
                         and result.profit_factor >= 1.0)
            if result.passed or soft_pass:
                if symbol not in approved[strategy]:
                    approved[strategy].append(symbol)
                status = "✅ PASS" if result.passed else "☑  SOFT-PASS (edge, cautious size)"
            else:
                status = f"⚠  FAIL ({', '.join(result.fail_reasons[:1])})"

            progress[key] = {
                "done": True, "passed": result.passed,
                "win_rate": round(result.win_rate, 1),
                "sharpe": round(result.sharpe_ratio, 2),
                "trades": result.total_trades,
                "ts": _now_ist().isoformat(),
            }
            return symbol, strategy, result.passed, status

        except Exception as exc:
            logger.warning("  {} {} error: {}", symbol, strategy, exc)
            progress[key] = {"done": False, "passed": False, "error": str(exc),
                             "ts": _now_ist().isoformat()}
            return symbol, strategy, False, f"❌ ERROR"


async def learn(symbols: list[str], strategies: list[str],
                resume: bool, concurrency: int) -> None:
    from adaptive_engine import adaptive_engine
    from agents.base_agent import send_telegram

    progress = _load_progress() if resume else {}
    approved = _load_approved() if resume else {s: [] for s in ALL_STRATEGIES}

    total   = len(symbols) * len(strategies)
    done    = 0
    sem     = asyncio.Semaphore(concurrency)
    t_start = time.time()

    print(f"\n{'═'*60}")
    print(f"  AlgoTrader Pro — Historical Learner")
    print(f"  {len(symbols)} symbols × {len(strategies)} strategies = {total} backtests")
    print(f"  Concurrency: {concurrency} | Resume: {resume}")
    print(f"{'═'*60}\n")

    # Collect telegram tasks to await at end (fire-and-forget loses messages on loop shutdown)
    _tg_tasks: list = []

    # Notify start
    _tg_tasks.append(asyncio.ensure_future(send_telegram(
        f"🧠 <b>Historical Learning Started</b>\n"
        f"{len(symbols)} Nifty-100 symbols × {len(strategies)} strategies\n"
        f"Expected time: ~{total//concurrency//6} min"
    )))

    tasks = []
    for symbol in symbols:
        for strategy in strategies:
            key = f"{strategy}::{symbol}"
            if resume and progress.get(key, {}).get("done"):
                done += 1
                # Re-add to approved if was passing
                if progress[key].get("passed") and symbol not in approved.get(strategy, []):
                    approved.setdefault(strategy, []).append(symbol)
                continue
            tasks.append(_run_one(symbol, strategy, sem, approved, progress))

    skipped = total - len(tasks)
    if skipped:
        print(f"  Skipping {skipped} already-completed backtests (--resume)\n")

    milestone_pct = 0
    newly_done = 0   # count only freshly completed (not pre-skipped) for accurate ETA
    for coro in asyncio.as_completed(tasks):
        sym, strat, passed, status = await coro
        done += 1
        newly_done += 1
        elapsed = time.time() - t_start
        rate    = newly_done / elapsed if elapsed > 0 else 1
        eta_s   = int((total - done) / rate) if rate > 0 else 0
        eta     = f"{eta_s//60}m{eta_s%60:02d}s"

        print(f"  [{done:3d}/{total}] {status:30s} {sym:15s} {strat:10s}  ETA {eta}")

        # Save progress every 10 completions
        if done % 10 == 0:
            _save_progress(progress)
            _save_approved(approved)
            adaptive_engine._save_state()

        # Telegram milestone every 25%
        pct = (done * 100 // total) if total > 0 else 100
        if pct >= milestone_pct + 25:
            milestone_pct = (pct // 25) * 25
            pass_counts = {s: len(v) for s, v in approved.items()}
            _tg_tasks.append(asyncio.ensure_future(send_telegram(
                f"🧠 Learning {milestone_pct}% done ({done}/{total})\n"
                + "\n".join(f"  {s}: {n} approved" for s, n in pass_counts.items())
            )))

    # Final save
    _save_progress(progress)
    _save_approved(approved)
    adaptive_engine._save_state()

    elapsed = int(time.time() - t_start)
    pass_counts = {s: len(v) for s, v in approved.items()}
    total_approved = sum(pass_counts.values())

    print(f"\n{'═'*60}")
    print(f"  ✅ Learning complete in {elapsed//60}m{elapsed%60:02d}s")
    print(f"  Approved symbols per strategy:")
    for strat, syms in approved.items():
        print(f"    {strat:12s}: {len(syms):3d} / {len(symbols)}")
    print(f"\n  Adaptive params saved → logs/adaptive/adaptive_params.json")
    print(f"  Approved list saved  → logs/approved_symbols.json")
    print(f"\n  Now set in .env:")
    print(f"    SKIP_STARTUP_BACKTEST=true")
    print(f"    USE_NIFTY100_WATCHLIST=true")
    print(f"{'═'*60}\n")

    await send_telegram(
        f"✅ <b>Historical Learning Complete</b>\n"
        f"Time: {elapsed//60}m{elapsed%60:02d}s\n"
        + "\n".join(f"  {s}: {n} symbols approved" for s, n in pass_counts.items()) +
        f"\n\nSet SKIP_STARTUP_BACKTEST=true to use pre-learned params."
    )
    # Flush any pending milestone notifications
    if _tg_tasks:
        await asyncio.gather(*_tg_tasks, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="AlgoTrader Historical Learner")
    parser.add_argument("--strategies", default=",".join(ALL_STRATEGIES),
                        help="Comma-separated strategies (default: all)")
    parser.add_argument("--symbols", default="",
                        help="Comma-separated symbols (default: full Nifty 100)")
    parser.add_argument("--resume", action="store_true",
                        help="Skip already-completed backtests")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="Parallel backtests (default: 5)")
    args = parser.parse_args()

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]

    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        from nifty100 import NIFTY_100
        symbols = NIFTY_100

    asyncio.run(learn(symbols, strategies, args.resume, args.concurrency))


if __name__ == "__main__":
    main()
