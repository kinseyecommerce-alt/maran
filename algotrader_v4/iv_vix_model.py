"""
iv_vix_model.py — NIFTY option IV surface driven by INDIA VIX, calibrated on REAL
Kite 1-minute option bars (logs/kite_1y/OPT_*.csv, cached by
scripts/kite_1y_download.py). PAPER research only — no Kite calls here.

Model (same Black-Scholes frame the research harness prices in: spot S, r=6.5%,
T = years to 15:30 on expiry):
    ln(IV / (VIX/100)) = b0 + b1·L + b2·L² + b3·x + b4·x² + b5·x·L + b6·x·1[x<0]
      L = ln(days-to-expiry, floored at 0.05 d),  x = ln(K/S) / ((VIX/100)·√T)  (clipped ±4)
Samples: OTM (and ATM ±1 step both sides) contracts, every 5 minutes 09:20–15:25,
traded minute (volume > 0), premium ≥ ₹1, |K−S| ≤ 10 strikes.

validate(): fit on the earlier real sessions, test on the last `hold` sessions;
reports price error (MAPE / median abs % / bias, premium ≥ ₹5) and IV RMSE (vol pts),
and the same numbers for the OLD model (flat IV = k × RV20, k = 1.0-ish) so the
improvement is measured, not asserted. Also the intraday ATM-IV-vs-VIX relation.
"""
from __future__ import annotations

import csv
import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).parent
K1Y = ROOT / "logs" / "kite_1y"
MODEL_PATH = K1Y / "iv_model.json"
R_ = 0.065
STEP = 50


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=None)


def load_minute(path: Path, need_volume: bool = False) -> dict:
    out = {}
    with open(path) as f:
        for r in csv.DictReader(f):
            if need_volume and float(r.get("volume") or 0) <= 0:
                continue
            out[_ts(r["date"])] = float(r["close"])
    return out


def years_to(expiry: date, now: datetime) -> float:
    end = datetime(expiry.year, expiry.month, expiry.day, 15, 30)
    return max((end - now).total_seconds(), 1800.0) / (365.0 * 86400.0)


# ── vectorised Black-Scholes ────────────────────────────────────────────────
def _ncdf(x):
    from scipy.special import ndtr
    return ndtr(x)


def bs_vec(S, K, T, sig, is_call):
    sq = sig * np.sqrt(T)
    d1 = (np.log(S / K) + (R_ + 0.5 * sig * sig) * T) / sq
    d2 = d1 - sq
    c = S * _ncdf(d1) - K * np.exp(-R_ * T) * _ncdf(d2)
    p = K * np.exp(-R_ * T) * _ncdf(-d2) - S * _ncdf(-d1)
    return np.where(is_call, c, p)


def iv_vec(px, S, K, T, is_call, iters: int = 50):
    lo = np.full_like(px, 0.01); hi = np.full_like(px, 3.0)
    for _ in range(iters):
        m = (lo + hi) / 2
        over = bs_vec(S, K, T, m, is_call) > px
        hi = np.where(over, m, hi); lo = np.where(over, lo, m)
    iv = (lo + hi) / 2
    intr = np.where(is_call, np.maximum(S - K, 0), np.maximum(K - S, 0))
    return np.where((px > intr + 0.05) & (iv > 0.015) & (iv < 2.9), iv, np.nan)


# ── features / model ─────────────────────────────────────────────────────────
def feats(S, K, T, vix):
    v = np.asarray(vix, float) / 100.0
    days = np.maximum(np.asarray(T, float) * 365.0, 0.05)
    L = np.log(days)
    x = np.clip(np.log(np.asarray(K, float) / np.asarray(S, float)) / (v * np.sqrt(T)), -4, 4)
    return np.column_stack([np.ones_like(L), L, L * L, x, x * x, x * L, x * (x < 0)])


def predict_iv(coef, S, K, T, vix):
    X = feats(np.atleast_1d(S), np.atleast_1d(K), np.atleast_1d(T), np.atleast_1d(vix))
    v = np.atleast_1d(np.asarray(vix, float)) / 100.0
    return v * np.exp(X @ np.asarray(coef))


def predict_iv1(coef, S: float, K: float, T: float, vix: float) -> float:
    """Scalar fast path (the harness calls this per bar)."""
    v = vix / 100.0
    days = max(T * 365.0, 0.05)
    L = math.log(days)
    x = max(-4.0, min(4.0, math.log(K / S) / (v * math.sqrt(T))))
    b = coef
    z = b[0] + b[1] * L + b[2] * L * L + b[3] * x + b[4] * x * x + b[5] * x * L + (b[6] * x if x < 0 else 0.0)
    return v * math.exp(z)


# ── samples from the real option cache ──────────────────────────────────────
def option_meta() -> dict:
    """NIFTY option contracts from every saved NFO instrument snapshot (newest wins), so contracts
    that have since expired keep their strike/expiry metadata as forward data accumulates."""
    out: dict = {}
    for f in sorted(K1Y.glob("instruments_NFO_*.json")):
        for r in json.loads(f.read_text()):
            if r.get("name") == "NIFTY" and r.get("instrument_type") in ("CE", "PE"):
                out[r["tradingsymbol"]] = r
    return out


def load_samples(every_min: int = 5) -> dict:
    """Arrays of real option observations (+ spot, VIX) for calibration."""
    idx = load_minute(K1Y / "NIFTY50_IDX.csv")
    vix = load_minute(K1Y / "INDIAVIX.csv")
    meta = option_meta()
    rows = []
    for p in sorted(K1Y.glob("OPT_*.csv")):
        sym = p.stem[4:]
        m = meta.get(sym)
        if not m:
            continue
        exp = date.fromisoformat(m["expiry"][:10]); K = float(m["strike"]); call = m["instrument_type"] == "CE"
        with open(p) as f:
            for r in csv.DictReader(f):
                if float(r.get("volume") or 0) <= 0:
                    continue
                t = _ts(r["date"])
                if t.minute % every_min or not (9 * 60 + 20 <= t.hour * 60 + t.minute <= 15 * 60 + 25):
                    continue
                S = idx.get(t); v = vix.get(t)
                px = float(r["close"])
                if not S or not v or px < 1.0 or abs(K - S) > 10 * STEP:
                    continue
                otm = (call and K >= S - STEP) or ((not call) and K <= S + STEP)
                if not otm:
                    continue
                rows.append((t, S, K, years_to(exp, t + timedelta(minutes=1)), v, px, call, exp))
    if not rows:
        return {"n": 0}
    t, S, K, T, V, P, C, E = zip(*rows)
    S = np.array(S); K = np.array(K); T = np.array(T); V = np.array(V); P = np.array(P); C = np.array(C)
    iv = iv_vec(P, S, K, T, C)
    ok = np.isfinite(iv)
    return {"n": int(ok.sum()), "t": np.array(t)[ok], "S": S[ok], "K": K[ok], "T": T[ok], "vix": V[ok],
            "px": P[ok], "call": C[ok], "iv": iv[ok], "exp": np.array(E)[ok]}


def fit(sm: dict, mask=None) -> list:
    m = np.ones(sm["n"], bool) if mask is None else mask
    X = feats(sm["S"][m], sm["K"][m], sm["T"][m], sm["vix"][m])
    y = np.log(sm["iv"][m] / (sm["vix"][m] / 100.0))
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return [float(c) for c in coef]


def _err(sm, m, sig) -> dict:
    model = bs_vec(sm["S"][m], sm["K"][m], sm["T"][m], sig, sm["call"][m])
    real = sm["px"][m]
    keep = real >= 5.0
    e = (model[keep] - real[keep]) / real[keep]
    ive = (sig - sm["iv"][m])[keep] * 100
    return {"n": int(keep.sum()), "mape_pct": round(float(np.mean(np.abs(e))) * 100, 1),
            "median_abs_pct": round(float(np.median(np.abs(e))) * 100, 1),
            "bias_pct": round(float(np.mean(e)) * 100, 1),
            "iv_rmse_volpts": round(float(np.sqrt(np.mean(ive ** 2))), 2)}


def validate(sm: dict, rv20_of: dict, k_old: float, hold: int = 10, atm_weekly: bool = True) -> dict:
    days = sorted({t.date() for t in sm["t"]})
    if len(days) < hold + 5:
        hold = max(1, len(days) // 3)
    test_days = set(days[-hold:])
    dd = np.array([t.date() for t in sm["t"]])
    te = np.array([d in test_days for d in dd])
    coef = fit(sm, ~te)
    sig_new = predict_iv(coef, sm["S"][te], sm["K"][te], sm["T"][te], sm["vix"][te])
    rv = np.array([rv20_of.get(d, np.nan) for d in dd[te]])
    sig_old = k_old * rv
    out = {"train_sessions": [str(days[0]), str(days[-hold - 1]), len(days) - hold],
           "test_sessions": [str(min(test_days)), str(max(test_days)), hold],
           "coef_train_only": coef,
           "new_vix_surface": _err({**sm, "S": sm["S"][te], "K": sm["K"][te], "T": sm["T"][te], "call": sm["call"][te],
                                    "px": sm["px"][te], "iv": sm["iv"][te]}, slice(None), sig_new),
           "old_flat_k_rv20": _err({**sm, "S": sm["S"][te], "K": sm["K"][te], "T": sm["T"][te], "call": sm["call"][te],
                                    "px": sm["px"][te], "iv": sm["iv"][te]}, slice(None), sig_old)}
    # the contract the agent actually trades: nearest weekly expiry (≥1 DTE), |K−S| ≤ 1 step
    near = np.abs(sm["K"][te] - sm["S"][te]) <= STEP
    dte = sm["T"][te] * 365
    w = near & (dte <= 8)
    if w.sum() > 20:
        sub = {k: (v[te][w] if isinstance(v, np.ndarray) else v) for k, v in sm.items()}
        out["atm_weekly_new"] = _err(sub, slice(None), sig_new[w])
        out["atm_weekly_old"] = _err(sub, slice(None), sig_old[w])
    return out


def intraday_iv_vs_vix(sm: dict) -> dict:
    """ATM (|K−S| ≤ 25), DTE 1–8 d: 5-min Δln(IV) vs Δln(VIX) within a day, and level ratio."""
    m = (np.abs(sm["K"] - sm["S"]) <= 25) & (sm["T"] * 365 >= 1) & (sm["T"] * 365 <= 8)
    t = sm["t"][m]; iv = sm["iv"][m]; v = sm["vix"][m]; e = sm["exp"][m]
    series = {}
    for ti, ivi, vi, ei in zip(t, iv, v, e):
        series.setdefault((ti.date(), ei), {}).setdefault(ti, []).append((ivi, vi))
    dx, dy, ratio = [], [], []
    for _, s in series.items():
        ks = sorted(s)
        pts = [(k, float(np.mean([a for a, _ in s[k]])), float(np.mean([b for _, b in s[k]]))) for k in ks]
        for (t0, i0, v0), (t1, i1, v1) in zip(pts, pts[1:]):
            if (t1 - t0).seconds == 300:
                dx.append(math.log(v1 / v0)); dy.append(math.log(i1 / i0))
        ratio += [i / (v / 100) for _, i, v in pts]
    if len(dx) < 30:
        return {"n": len(dx)}
    dx = np.array(dx); dy = np.array(dy)
    beta = float(np.cov(dx, dy)[0, 1] / np.var(dx)) if np.var(dx) > 0 else None
    return {"n_5min_pairs": len(dx), "corr_dlnIV_dlnVIX": round(float(np.corrcoef(dx, dy)[0, 1]), 3),
            "beta": round(beta, 3) if beta is not None else None,
            "atm_weekly_iv_over_vix_median": round(float(np.median(ratio)), 3),
            "atm_weekly_iv_over_vix_p10_p90": [round(float(np.percentile(ratio, 10)), 3),
                                               round(float(np.percentile(ratio, 90)), 3)]}


def calibrate(rv20_of: dict, k_old: float) -> dict:
    sm = load_samples()
    if sm["n"] < 200:
        return {"ok": False, "n": sm["n"]}
    days = sorted({t.date() for t in sm["t"]})
    res = {"ok": True, "n_samples": sm["n"], "real_option_sessions": len(days),
           "first": str(days[0]), "last": str(days[-1]),
           "contracts": len({(k, c, e) for k, c, e in zip(sm["K"], sm["call"], sm["exp"])}),
           "validation": validate(sm, rv20_of, k_old),
           "intraday_iv_vs_vix": intraday_iv_vs_vix(sm),
           "coef": fit(sm)}
    MODEL_PATH.write_text(json.dumps(res, indent=1, default=str))
    return res


def load_model() -> dict:
    return json.loads(MODEL_PATH.read_text()) if MODEL_PATH.exists() else {}
