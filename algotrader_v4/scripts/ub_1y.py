"""
ub_1y.py — run unified_backtest (Nifty 50 stocks, NIFTY futures, MCX agents) over the
1-year Kite cache (logs/kite_1y) without touching the live caches. PAPER research only.

  • NSE stocks / NIFTY index: Kite 1-minute bars 2025-09-01 … 2026-10-09.
  • MCX: Kite has minute history only for still-listed contracts (continuous=1 is
    daily-only), so the minute series is stitched per day from the NEAREST-EXPIRY
    live contract that traded that day — before the current front month became
    front, that is a deferred (thinner) month. Stated in the report.
  • Owner focus mode pauses these agents live; this research process evaluates the
    owner's kept segments (Nifty 50 stocks, NIFTY F&O, MCX) in-process only — the
    persisted owner_universe / gates are not modified.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.chdir(HERE)
K1Y = HERE / "logs" / "kite_1y"
UBD = K1Y / "ub_data"


def prep() -> dict:
    cov = {}
    (UBD / "_native").mkdir(parents=True, exist_ok=True)
    for p in K1Y.glob("EQ_*.csv"):
        sym = p.stem[3:]
        (UBD / sym).mkdir(exist_ok=True)
        dst = UBD / sym / "1m.csv"
        if not dst.exists() or dst.stat().st_mtime < p.stat().st_mtime:
            with open(p) as fi, open(dst, "w", newline="") as fo:
                w = csv.writer(fo); w.writerow(["date", "open", "high", "low", "close", "volume"])
                for r in csv.DictReader(fi):
                    w.writerow([r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]])
    (UBD / "NIFTY").mkdir(exist_ok=True)
    with open(K1Y / "NIFTY50_IDX.csv") as fi, open(UBD / "NIFTY" / "1m.csv", "w", newline="") as fo:
        w = csv.writer(fo); w.writerow(["date", "open", "high", "low", "close", "volume"])
        for r in csv.DictReader(fi):
            w.writerow([r["date"], r["open"], r["high"], r["low"], r["close"], 0])
    meta = {r["tradingsymbol"]: r for r in json.loads(sorted(K1Y.glob("instruments_MCX_*.json"))[-1].read_text())}
    for name in ("CRUDEOIL", "CRUDEOILM", "NATURALGAS", "SILVERM", "GOLDM"):
        per_day: dict = {}
        files = sorted(K1Y.glob(f"MCX_{name}2*FUT.csv"),
                       key=lambda f: meta.get(f.stem[4:], {}).get("expiry") or "9999")
        used: dict = {}
        for f in files:
            exp = (meta.get(f.stem[4:]) or {}).get("expiry")
            if not exp:
                continue
            exp = date.fromisoformat(exp[:10])
            with open(f) as fh:
                rows = list(csv.DictReader(fh))
            byd: dict = {}
            for r in rows:
                byd.setdefault(r["date"][:10], []).append(r)
            for d, rs in byd.items():
                if d in per_day or date.fromisoformat(d) > exp:
                    continue
                if len(rs) < 60:
                    continue
                per_day[d] = rs
                used[d] = f.stem[4:]
        with open(UBD / "_native" / f"{name}-FUT_MCX_1m.csv", "w", newline="") as fo:
            w = csv.writer(fo); w.writerow(["date", "open", "high", "low", "close", "volume"])
            for d in sorted(per_day):
                for r in per_day[d]:
                    w.writerow([r["date"], r["open"], r["high"], r["low"], r["close"], r["volume"]])
        ds = sorted(per_day)
        by_contract: dict = {}
        for d, c in used.items():
            by_contract[c] = by_contract.get(c, 0) + 1
        cov[name] = {"days": len(ds), "first": ds[0] if ds else None, "last": ds[-1] if ds else None,
                     "contracts_days": by_contract}
    (K1Y / "mcx_stitch_coverage.json").write_text(json.dumps(cov, indent=1))
    return cov


def main():
    import unified_backtest as U
    cov = prep()
    print("MCX stitch:", json.dumps({k: (v["days"], v["first"], v["last"]) for k, v in cov.items()}), flush=True)
    U.DATA_DIR = UBD
    U.NATIVE_DIR = UBD / "_native"
    U.CACHE_DIR = K1Y / "ub_cache"
    import owner_universe as OU
    from nifty100 import NIFTY_50
    o = OU.owner_universe
    kept = set(NIFTY_50)
    o.allows = lambda s, segment="NSE_EQ", *a, **k: ((s in kept or s == "NIFTY" or segment == "MCX"), "research view")
    o.segment_enabled = lambda seg, *a, **k: seg in ("NSE_EQ", "NSE_FO", "MCX")
    o.fo_underlying_allowed = lambda u, *a, **k: u == "NIFTY"
    OU._nifty50 = lambda: list(NIFTY_50)
    days = int(os.environ.get("UB_DAYS", "250"))
    U.main(["--agents", "all", "--days", str(days), "--workers", "6",
            "--out", str(HERE / "logs" / "backtests" / "unified_backtest_1y.json")])


if __name__ == "__main__":
    main()
