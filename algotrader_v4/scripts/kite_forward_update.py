"""
kite_forward_update.py — append the newest sessions to the 1-year Kite cache (logs/kite_1y):
NIFTY 50 + India VIX (minute + day), the Nifty-50 stocks (minute) and REAL NIFTY option minute
bars (every cached contract not yet expired + the two nearest weekly expiries, ATM ±10 strikes) —
so the forward holdout of option strategies is priced on recorded premiums, not the model.  These new sessions are
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
    # option contracts: extend every cached contract that has not expired, and add the two
    # nearest weekly expiries ATM ±10 strikes (instrument snapshot saved for option_meta)
    inst_f = KD.OUT / f"instruments_NFO_{today:%Y%m%d}.json"
    if inst_f.exists():
        nfo = json.loads(inst_f.read_text())
    else:
        KD._throttle()
        nfo = k.instruments("NFO")
        for r in nfo:
            for c in ("expiry", "last_price"):
                r[c] = str(r.get(c)) if r.get(c) is not None else None
        inst_f.write_text(json.dumps(nfo))
    opts = [r for r in nfo if r.get("name") == "NIFTY" and r.get("instrument_type") in ("CE", "PE")
            and r.get("expiry") and date.fromisoformat(r["expiry"][:10]) >= end - timedelta(days=1)]
    exps = sorted({r["expiry"][:10] for r in opts})[:2]
    spot = None
    try:
        with open(KD.OUT / "NIFTY50_IDX_day.csv") as f:
            for r in csv.DictReader(f):
                spot = float(r["close"])
    except Exception:
        pass
    if spot:
        for r in opts:
            if r["expiry"][:10] in exps and abs(float(r["strike"]) - spot) <= 10 * 50 + 1e-6:
                ck = f"OPT_{r['tradingsymbol']}"
                if ck not in cov:
                    cov[ck] = {"token": r["instrument_token"], "new": True, "interval": "minute"}
    opt_keys = [ck for ck, c in cov.items() if ck.startswith("OPT_") and (c or {}).get("token")]
    live_opt = {f"OPT_{r['tradingsymbol']}" for r in opts}
    keys += [(ck, "minute") for ck in opt_keys if ck in live_opt]
    added = {}
    for key, iv in keys:
        ck = key + ("" if iv == "minute" else "_" + iv)
        c = cov.get(ck) or {}
        tok = c.get("token")
        p = KD.OUT / f"{ck}.csv"
        is_opt = key.startswith("OPT_")
        if not tok or (not p.exists() and not is_opt):
            continue
        last = _last_date(p) if p.exists() else end - timedelta(days=5)
        if last is None or last >= end:
            continue
        rows = KD.fetch(k, key, tok, last + timedelta(days=1), end, iv, False, is_opt)
        if not rows:
            continue
        old = []
        if p.exists():
            with open(p) as f:
                old = list(csv.DictReader(f))
        c.update(KD.merge(key, old + rows, iv))
        c.pop("new", None)
        cov[ck] = c
        added[ck] = len(rows)
    cov_f.write_text(json.dumps(cov, indent=1, default=str))
    print(f"forward update to {end}: {len(added)} series extended, {sum(added.values())} bars")
    return 0


if __name__ == "__main__":
    sys.exit(main())
