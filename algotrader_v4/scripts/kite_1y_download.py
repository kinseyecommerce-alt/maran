"""
kite_1y_download.py — resumable, rate-limited 1-year Kite history cache (PAPER research only).

Never prints credentials. Every Kite answer is cached per chunk under
logs/kite_1y/raw/<key>/<from>_<to>.csv (an immutable past chunk is cached even
when empty, a chunk touching today is not), so a restart resumes where it left.
Merged series: logs/kite_1y/<key>.csv.  Coverage: logs/kite_1y/coverage.json.

Kite facts this respects:
  • minute candles: max 60 days per request  → 59-day chunks
  • ~3 req/s historical limit                → 2.5 req/s bucket (server shares the limit)
  • continuous=1 is accepted for DAY candles only ("invalid interval for continuous
    data" for minute) → continuous futures are daily; minute futures/options exist
    only for contracts still listed (Kite drops expired contracts).
"""
from __future__ import annotations

import csv
import json
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

from kiteconnect import KiteConnect  # noqa: E402
from kiteconnect.exceptions import InputException, TokenException  # noqa: E402

OUT = Path("logs/kite_1y")
RAW = OUT / "raw"
START = date(2025, 9, 1)            # ~1 month warm-up before the 1-year test window
END = date(2026, 10, 10)            # last full session cached = Fri 2026-10-09
TODAY = date(2026, 10, 11)
RPS = 2.5
MCX_NAMES = ("CRUDEOIL", "CRUDEOILM", "NATURALGAS", "SILVERM", "GOLDM")
OPT_STRIKES_EACH_SIDE = 10
OPT_MAX_DTE = 120

_last = [0.0]
_nreq = [0]


def _throttle():
    gap = 1.0 / RPS
    w = _last[0] + gap - time.monotonic()
    if w > 0:
        time.sleep(w)
    _last[0] = time.monotonic()
    _nreq[0] += 1


def kite() -> KiteConnect:
    from state_store import get_kv
    k = KiteConnect(api_key=os.environ["KITE_API_KEY"])
    k.set_access_token(get_kv("kite_access_token"))
    return k


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def chunks(a: date, b: date, days: int = 59):
    s = a
    while s <= b:
        e = min(s + timedelta(days=days - 1), b)
        yield s, e
        s = e + timedelta(days=1)


FIELDS = ("date", "open", "high", "low", "close", "volume", "oi")


def fetch(k, key: str, token: int, a: date, b: date, interval="minute",
          continuous=False, oi=False) -> list:
    d = RAW / key
    d.mkdir(parents=True, exist_ok=True)
    rows_all = []
    for s, e in chunks(a, b, 59 if "minute" in interval else 1900):
        f = d / f"{interval}_{s:%Y%m%d}_{e:%Y%m%d}.csv"
        if f.exists():
            with open(f) as fh:
                rows_all += list(csv.DictReader(fh))
            continue
        rows = None
        for attempt in range(5):
            _throttle()
            try:
                rows = k.historical_data(token, datetime(s.year, s.month, s.day, 0, 0),
                                         datetime(e.year, e.month, e.day, 23, 59),
                                         interval, continuous=continuous, oi=oi)
                break
            except TokenException:
                log("TOKEN EXPIRED — stopping (re-login needed)")
                raise
            except InputException as exc:
                log(f"{key} {s}-{e}: input error {exc}")
                rows = []
                break
            except Exception as exc:   # network / 429
                log(f"{key} {s}-{e}: {type(exc).__name__} {exc} retry {attempt+1}")
                time.sleep(2 * (attempt + 1))
        if rows is None:
            continue                       # don't cache a failure
        out = [{"date": r["date"].isoformat(), "open": r["open"], "high": r["high"],
                "low": r["low"], "close": r["close"], "volume": r.get("volume", 0),
                "oi": r.get("oi", "")} for r in rows]
        if out or e < TODAY:              # immutable past chunk: cache even if empty
            with open(f, "w", newline="") as fh:
                w = csv.DictWriter(fh, FIELDS)
                w.writeheader()
                w.writerows(out)
        rows_all += out
    return rows_all


def merge(key: str, rows: list, interval="minute"):
    seen = {}
    for r in rows:
        seen[r["date"]] = r
    out = [seen[t] for t in sorted(seen)]
    with open(OUT / f"{key}{'' if interval == 'minute' else '_' + interval}.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, FIELDS)
        w.writeheader()
        w.writerows(out)
    days = sorted({r["date"][:10] for r in out})
    return {"bars": len(out), "days": len(days), "first": days[0] if days else None,
            "last": days[-1] if days else None, "interval": interval}


def main():
    k = kite()
    OUT.mkdir(parents=True, exist_ok=True)
    cov_f = OUT / "coverage.json"
    cov = json.loads(cov_f.read_text()) if cov_f.exists() else {}

    def save():
        cov_f.write_text(json.dumps(cov, indent=1, default=str))

    inst = {}
    for ex in ("NSE", "NFO", "MCX"):
        f = OUT / f"instruments_{ex}_{TODAY:%Y%m%d}.json"
        if f.exists():
            inst[ex] = json.loads(f.read_text())
        else:
            _throttle()
            rows = k.instruments(ex)
            for r in rows:
                for c in ("expiry", "last_price"):
                    r[c] = str(r.get(c)) if r.get(c) is not None else None
            f.write_text(json.dumps(rows))
            inst[ex] = rows
    log("instruments", {e: len(v) for e, v in inst.items()})

    jobs = []   # (key, token, interval, continuous, oi, start)
    nse = {r["tradingsymbol"]: r for r in inst["NSE"]}
    jobs.append(("NIFTY50_IDX", nse["NIFTY 50"]["instrument_token"], "minute", False, False, START))
    jobs.append(("NIFTY50_IDX", nse["NIFTY 50"]["instrument_token"], "day", False, False, date(2024, 1, 1)))
    jobs.append(("INDIAVIX", nse["INDIA VIX"]["instrument_token"], "minute", False, False, START))
    jobs.append(("INDIAVIX", nse["INDIA VIX"]["instrument_token"], "day", False, False, date(2024, 1, 1)))

    # NIFTY futures: continuous DAILY + minute for each live contract
    futs = sorted([r for r in inst["NFO"] if r["name"] == "NIFTY" and r["instrument_type"] == "FUT"],
                  key=lambda r: r["expiry"])
    jobs.append(("NIFTY_FUT_CONT", futs[0]["instrument_token"], "day", True, True, date(2024, 1, 1)))
    for r in futs:
        jobs.append((f"NFO_{r['tradingsymbol']}", r["instrument_token"], "minute", False, True, START))

    # MCX: continuous daily + minute for live contracts expiring within ~6 months
    for name in MCX_NAMES:
        cs = sorted([r for r in inst["MCX"] if r["name"] == name and r["instrument_type"] == "FUT"
                     and r["segment"] == "MCX-FUT"], key=lambda r: r["expiry"])
        if not cs:
            log("MCX no contracts", name)
            continue
        jobs.append((f"MCX_{name}_CONT", cs[0]["instrument_token"], "day", True, True, date(2024, 1, 1)))
        for r in cs:
            if (date.fromisoformat(r["expiry"][:10]) - TODAY).days <= 190:
                jobs.append((f"MCX_{r['tradingsymbol']}", r["instrument_token"], "minute", False, True, START))

    from nifty100 import NIFTY_50
    for s in NIFTY_50:
        r = nse.get(s)
        if not r:
            log("no NSE token", s)
            cov[f"EQ_{s}"] = {"error": "no-token"}
            continue
        jobs.append((f"EQ_{s}", r["instrument_token"], "minute", False, False, START))

    # NIFTY options: live expiries within OPT_MAX_DTE, strikes ±10 steps around any
    # daily close of the last 60 days (= "ATM ±10" at the time each bar traded)
    idx_daily = fetch(k, "NIFTY50_IDX", nse["NIFTY 50"]["instrument_token"], date(2024, 1, 1), END, "day")
    recent = [float(r["close"]) for r in idx_daily if r["date"][:10] >= str(TODAY - timedelta(days=62))]
    lo, hi = min(recent), max(recent)
    opts = [r for r in inst["NFO"] if r["name"] == "NIFTY" and r["instrument_type"] in ("CE", "PE")
            and 0 <= (date.fromisoformat(r["expiry"][:10]) - TODAY).days <= OPT_MAX_DTE]
    step = 50
    opts = [r for r in opts if lo - OPT_STRIKES_EACH_SIDE * step <= float(r["strike"]) <= hi + OPT_STRIKES_EACH_SIDE * step]
    log(f"options: {len(opts)} contracts, spot range {lo:.0f}-{hi:.0f}, expiries "
        f"{sorted({r['expiry'][:10] for r in opts})}")
    for r in sorted(opts, key=lambda r: (r["expiry"], abs(float(r["strike"]) - recent[-1]))):
        jobs.append((f"OPT_{r['tradingsymbol']}", r["instrument_token"], "minute", False, True,
                     TODAY - timedelta(days=59)))

    log(f"{len(jobs)} jobs")
    for i, (key, tok, iv, cont, oi, st) in enumerate(jobs):
        ck = key + ("" if iv == "minute" else "_" + iv)
        if ck in cov and cov[ck].get("complete"):
            continue
        rows = fetch(k, key, tok, st, END, iv, cont, oi)
        c = merge(key, rows, iv)
        c.update(complete=True, token=tok, continuous=cont)
        cov[ck] = c
        if i % 10 == 0:
            save()
            log(f"[{i+1}/{len(jobs)}] {ck}: {c['days']}d {c['bars']} bars ({_nreq[0]} req)")
    save()
    log(f"DONE {_nreq[0]} requests")


if __name__ == "__main__":
    main()
