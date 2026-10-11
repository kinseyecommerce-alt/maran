"""
kite_forward_update.py — append the newest sessions to the 1-year Kite cache (logs/kite_1y):
NIFTY 50 + India VIX (minute + day) and the Nifty-50 stocks (minute).  These new sessions are
the discovery loop's expanding FORWARD holdout.  PAPER research only; never prints credentials;
needs a logged-in Kite session (state_store token).  Run after the close (scheduler 16:30 IST).
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "scripts"))

import kite_1y_download as KD  # noqa: E402  (chdir + rate limiter + chunk cache)


def _last_date(p: Path):
    last = None
    with open(p) as f:
        for r in csv.DictReader(f):
            last = r["date"][:10]
    return date.fromisoformat(last) if last else None


def main() -> int:
    from ist_clock import now_ist
    now = now_ist()
    today = now.date()
    end = today if now.hour * 60 + now.minute >= 15 * 60 + 45 else today - timedelta(days=1)
    KD.TODAY = today                      # chunks touching today are never cached as immutable
    cov_f = KD.OUT / "coverage.json"
    cov = json.loads(cov_f.read_text())
    keys = [("NIFTY50_IDX", "minute"), ("NIFTY50_IDX", "day"), ("INDIAVIX", "minute"), ("INDIAVIX", "day")]
    keys += [(k, "minute") for k in cov if k.startswith("EQ_") and (cov[k] or {}).get("token")]
    k = KD.kite()
    added = {}
    for key, iv in keys:
        ck = key + ("" if iv == "minute" else "_" + iv)
        c = cov.get(ck) or {}
        tok = c.get("token")
        p = KD.OUT / f"{ck}.csv"
        if not tok or not p.exists():
            continue
        last = _last_date(p)
        if last is None or last >= end:
            continue
        rows = KD.fetch(k, key, tok, last + timedelta(days=1), end, iv)
        if not rows:
            continue
        with open(p) as f:
            old = list(csv.DictReader(f))
        c.update(KD.merge(key, old + rows, iv))
        cov[ck] = c
        added[ck] = len(rows)
    cov_f.write_text(json.dumps(cov, indent=1, default=str))
    print(f"forward update to {end}: {len(added)} series extended, {sum(added.values())} bars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
