"""Tests for backtest_1y / iv_vix_model (offline, no Kite)."""
from datetime import date, datetime, timedelta

import backtest_1y as B
import iv_vix_model as IVM
import nifty_options_research as R

PASS = []


def run(name, fn):
    fn()
    PASS.append(name)
    print("  OK ", name)


def t_folds_embargo_and_order():
    f = B.folds12(274)
    assert len(f) == 12
    seen = set()
    for tr, te in f:
        assert max(tr) < min(te) - B.EMBARGO, "embargo violated"
        assert not (set(te) & seen), "test folds overlap"
        seen |= set(te)
    assert min(seen) == B.FIRST_TRAIN and max(seen) == 273


def t_lot_by_expiry():
    assert B.lot_for(date(2025, 12, 23)) == 75 and B.lot_for(date(2025, 12, 30)) == 75
    assert B.lot_for(date(2026, 1, 6)) == 65


def t_vix_no_lookahead_at_open():
    t0 = datetime(2026, 1, 5, 9, 15)
    bars = [R.Bar(t0 + timedelta(minutes=k), 24000, 24010, 23990, 24000) for k in range(10)]
    coef = [0.0] * 7
    vix = [10.0] * 10
    vix[5] = 40.0                               # spike in bar 5's close
    c = B.VixCtx(t0.date(), bars, 0.1, date(2026, 1, 6), date(2026, 1, 6), False, 24000, vix=vix, coef=coef, lot=65)
    # entry at bar 5's OPEN must not see bar 5's VIX close
    p_open = c.prem(24000, 24000, "CE", 5, date(2026, 1, 6), at_open=True)
    p_flat = c.prem(24000, 24000, "CE", 4, date(2026, 1, 6), at_open=False)
    assert abs(p_open - p_flat) / p_flat < 0.02, (p_open, p_flat)
    assert c.prem(24000, 24000, "CE", 5, date(2026, 1, 6)) > 2 * p_open


def t_iv_model_roundtrip():
    import numpy as np
    S = np.array([24000.0]); K = np.array([24100.0]); T = np.array([3 / 365]); call = np.array([True])
    px = IVM.bs_vec(S, K, T, np.array([0.12]), call)
    iv = IVM.iv_vec(px, S, K, T, call)
    assert abs(iv[0] - 0.12) < 1e-3
    assert abs(IVM.predict_iv1([0.0] * 7, 24000, 24000, 0.01, 15.0) - 0.15) < 1e-9


for n, f in list(globals().items()):
    if n.startswith("t_"):
        run(n, f)
print(f"\n{len(PASS)}/{len(PASS)} passed")
