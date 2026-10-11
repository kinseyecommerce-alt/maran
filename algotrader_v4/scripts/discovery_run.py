"""
discovery_run.py — one bounded, nice'd strategy-discovery run (PAPER research only).

  python scripts/discovery_run.py --budget-min 120 [--workers 4] [--seed N] [--if-not-trading-hours]

Single instance (logs/discovery/run.lock holds the PID).  Stops by itself at the budget or
when NSE trading hours start.  Never places orders, never touches LIVE gates; the only
registry change it can make is adding a rule that passed EVERY criterion on the sealed
holdout as PAPER probation at half size (profit_eval.add_kept).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.chdir(HERE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget-min", type=float, default=120)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--max-trials", type=int, default=None)
    ap.add_argument("--if-not-trading-hours", action="store_true")
    a = ap.parse_args()
    try:
        os.nice(15)
    except Exception:
        pass
    os.environ.setdefault("ALGOTRADER_LOG_DIR", str(HERE / "logs" / "discovery" / "applog"))
    import strategy_discovery as SD
    if str(os.environ.get("TRADING_MODE", "PAPER")).upper() != "PAPER":
        print("refusing: PAPER only"); return 2
    if a.if_not_trading_hours and SD.trading_hours_now():
        print("skipped: NSE trading hours"); return 0
    SD.DDIR.mkdir(parents=True, exist_ok=True)
    pid = SD.running_pid()
    if pid and pid != os.getpid():
        print(f"another discovery run is active (pid {pid})"); return 0
    SD.LOCK_PATH.write_text(f"{os.getpid()} {datetime.now().isoformat(timespec='seconds')}")
    logf = open(SD.RUNLOG_PATH, "w", buffering=1)

    def log(*x):
        line = " ".join(str(v) for v in x)
        print(datetime.now().strftime("%H:%M:%S"), line, flush=True)
        logf.write(datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
    try:
        SD.run(budget_min=a.budget_min, workers=a.workers, seed=a.seed, log=log, max_trials=a.max_trials)
        return 0
    finally:
        try:
            SD.LOCK_PATH.unlink()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
