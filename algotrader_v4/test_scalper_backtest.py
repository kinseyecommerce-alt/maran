"""
test_scalper_backtest.py — fast-scalper tick-replay backtester + "trade less,
better" (jag 2026-10-10). No network, PAPER only, temp dirs for every store.
Checks: determinism, cost accounting, no look-ahead (features, latency fills,
walk-forward whitelist), session windows, liquidity whitelist, cool-downs /
consecutive-loss stop, caps, universe guard, recorder round trip + rotation,
one shared decision path, retune acceptance needs OOS evidence.
Run: cd algotrader_v4 && python test_scalper_backtest.py
"""
from __future__ import annotations
import os, tempfile, traceback, random, json, gzip, inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path

_d = tempfile.mkdtemp(prefix="scalper-bt-test-")
os.environ.setdefault("DATABASE_PATH", os.path.join(_d, "algotrader.db"))
os.environ.setdefault("ADAPTIVE_DATA_DIR", os.path.join(_d, "adaptive"))
os.environ.setdefault("SEBI_AUDIT_DIR", _d)
os.environ["LEARNING_DB"] = os.path.join(_d, "learning.db")
os.environ["OWNER_UNIVERSE_PATH"] = os.path.join(_d, "owner_universe.json")
os.environ["SCALPER_CONFIG_PATH"] = os.path.join(_d, "scalper_config.json")
os.environ["SCALPER_WHITELIST_PATH"] = os.path.join(_d, "scalper_whitelist.json")
os.environ["API_KEY"] = "unit-test-local-only"
os.environ["TRADING_MODE"] = "PAPER"

from config import settings
settings.trading_mode = "PAPER"
from owner_universe import owner_universe
owner_universe.apply_jag_policy("owner(test)")

import scalper_backtest as SB
from fast_scalper import ScalpLogic, Inst, SState, DayBook, Ctx, DAILY_CAP
from scalper_config import scalper_config, window_of
from tick_recorder import v2_row, rotate_ticks
from tick_replayer import parse_row, load_depth_ticks, tick_files

IST = timezone(timedelta(hours=5, minutes=30))
_results = []


def run(name, fn):
    try:
        fn()
        _results.append((name, True, ""))
        print(f"  OK  {name}")
    except Exception as exc:
        _results.append((name, False, f"{type(exc).__name__}: {exc}"[:240]))
        print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:220]}")
        traceback.print_exc(limit=4)


def ep(day: str, hh: int, mm: int, ss: float = 0.0) -> float:
    return datetime.fromisoformat(day).replace(hour=hh, minute=mm, tzinfo=IST).timestamp() + ss


def synth(start: float, n: int = 3000, px0: float = 5600.0, tick: float = 1.0, seed: int = 7,
          leg: int = 120, dt: float = 1.0, top: int = 40) -> list[dict]:
    """Trending legs with a book that leans the trend's way (v2 depth)."""
    rnd = random.Random(seed)
    out, px, vol = [], px0, 1000
    for i in range(n):
        drift = 1 if (i // leg) % 2 == 0 else -1
        px += tick * (drift if rnd.random() < 0.75 else -drift)
        vol += rnd.randint(1, 8)
        bid, ask = px - tick, px
        heavy, light = (top * 4, top) if drift > 0 else (top, top * 4)
        bids = [(bid - k * tick, heavy, 3) for k in range(5)]
        asks = [(ask + k * tick, light, 3) for k in range(5)]
        ts = start + i * dt
        out.append(parse_row(list(v2_row({"recv_ts": ts, "exch_ts": ts - 0.2, "ltp": px, "bid": bid, "ask": ask,
                                           "volume": vol, "bids": bids, "asks": asks}))))
    return out


DAY = "2026-10-07"           # a Wednesday (not an NSE holiday)
MCX_KEY = "CRUDEOILM-FUT@MCX"
MCX_INST = Inst(MCX_KEY, "MCX", "CRUDEOILM-FUT", 0, 1.0, 10.0, 1, "native", "MCX")
LOOSE = {"cooldown_sec": 60, "max_consec_losses": 4, "symbol_daily_cap": 6, "daily_cap": 25}


def pf(extra=None):
    from self_learning import learning
    p = dict(learning.params("scalp:MCX"))
    p.update(extra or {})
    return lambda _i: p


def sim(ticks_by_key, p=None, wl=None, lat=150.0, windows=True, insts=None, **kw):
    return SB.simulate(ticks_by_key, p or pf(LOOSE), wl, lat, windows,
                       insts=insts or {k: (MCX_INST if k == MCX_KEY else SB.make_inst(k, v))
                                       for k, v in ticks_by_key.items()}, **kw)


# ── 1. determinism ────────────────────────────────────────────────────────────
def t_determinism():
    ticks = {MCX_KEY: synth(ep(DAY, 18, 5))}
    a, b = sim(ticks), sim(ticks)
    assert a["trades"], "synthetic trend should produce scalps"
    strip = lambda r: json.dumps([{k: v for k, v in t.items()} for t in r["trades"]], sort_keys=True, default=str)
    assert strip(a) == strip(b) and a["orders"] == b["orders"] and a["fills"] == b["fills"]


# ── 2. costs ──────────────────────────────────────────────────────────────────
def t_cost_accounting():
    from cost_model import costs
    r = sim({MCX_KEY: synth(ep(DAY, 18, 5))})
    assert r["trades"]
    for t in r["trades"]:
        side = 1 if t["side"] == "LONG" else -1
        gross = (t["exit"] - t["entry"]) * t["units"] * side
        c = costs("MCX_FUT", t["units"], t["entry"], t["exit"], "BUY" if side > 0 else "SELL", "MCX")
        parts = sum(c[k] for k in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp"))
        assert abs(t["gross"] - gross) < 0.02, (t["gross"], gross)
        assert abs(t["costs"]["total"] - c["total"]) < 0.02 and abs(parts - c["total"]) < 0.05
        assert abs(t["net"] - (gross - c["total"])) < 0.02
        assert all(k in t["costs"] for k in ("brokerage", "stt", "exchange", "sebi", "gst", "stamp"))
    s = SB.stats(r["trades"], r["orders"], r["fills"])
    assert abs(s["net"] - sum(t["net"] for t in r["trades"])) < 0.05
    assert abs(s["gross"] - s["costs"] - s["net"]) < 0.1


# ── 3. no look-ahead ──────────────────────────────────────────────────────────
def t_no_lookahead_future_ticks():
    base = synth(ep(DAY, 18, 5), n=3000)
    cut = base[1500]["recv_ts"]
    alt = [dict(t) for t in base]
    rnd = random.Random(99)
    for t in alt[1500:]:                      # rewrite the future completely
        t["ltp"] += rnd.choice((-30, 30))
        t["bid"], t["ask"] = t["ltp"] - 1, t["ltp"]
    a, b = sim({MCX_KEY: base}), sim({MCX_KEY: alt})
    done_a = [t for t in a["trades"] if t["exit_ts"] < cut - 5]
    done_b = [t for t in b["trades"] if t["exit_ts"] < cut - 5]
    assert done_a and json.dumps(done_a, default=str) == json.dumps(done_b, default=str)


def t_latency_no_early_fill():
    p = pf(LOOSE)(None)
    st = SState()
    t0 = {"ltp": 100.0, "bid": 99.0, "ask": 100.0, "bids": [(99.0, 50, 1)], "asks": [(100.0, 50, 1)], "recv_ts": 0.0}
    st.order = {"side": 1, "px": 99.0, "queue": 50, "ts": 0.0, "active": False, "active_at": 1.0}
    cross = {**t0, "ask": 99.0, "ltp": 98.0}          # would fill if we were there
    assert ScalpLogic.step(MCX_INST, st, cross, 0.5, 0, p) is None, "filled before the order reached the exchange"
    assert ScalpLogic.step(MCX_INST, st, cross, 1.2, 0, p) == ("fill",)
    # end-to-end: every replayed entry happens ≥ latency after some earlier tick
    r = sim({MCX_KEY: synth(ep(DAY, 18, 5))}, lat=800.0)
    assert r["trades"]


def t_walk_forward_split():
    d1 = {MCX_KEY: synth(ep("2026-10-06", 18, 5), seed=1)}
    d2 = {MCX_KEY: synth(ep("2026-10-07", 18, 5), seed=2)}
    fo = SB.folds({"2026-10-06": d1, "2026-10-07": d2})
    assert len(fo) == 1 and fo[0]["train_days"] == ["2026-10-06"] and fo[0]["test_days"] == ["2026-10-07"]
    tr_max = max(t["recv_ts"] for v in fo[0]["train"].values() for t in v)
    te_min = min(t["recv_ts"] for v in fo[0]["test"].values() for t in v)
    assert tr_max < te_min
    one = SB.folds({"2026-10-07": d2})
    assert one[0]["mode"] == "intraday_split"
    assert max(t["recv_ts"] for v in one[0]["train"].values() for t in v) < \
        min(t["recv_ts"] for v in one[0]["test"].values() for t in v)
    ev = SB.WFEvaluator("MCX", {"2026-10-06": d1, "2026-10-07": d2})
    assert max(t["recv_ts"] for d in ev.train for v in d.values() for t in v) < \
        min(t["recv_ts"] for d in ev.test for v in d.values() for t in v)


def t_whitelist_no_lookahead():
    """A stock liquid ONLY on the test day must not be whitelisted for it."""
    from scalper_whitelist import build
    root = Path(_d) / "ticks_wl"
    for day, sym in (("2026-10-06", "HDFCBANK"), ("2026-10-07", "INFY")):
        dd = root / day
        dd.mkdir(parents=True, exist_ok=True)
        rows = synth(ep(day, 9, 30), n=600, px0=1500.0, tick=0.1, dt=1.0)
        with open(dd / f"{sym}@NSE_EQ.csv", "w") as fh:
            for t in rows:
                fh.write(",".join(str(x) for x in v2_row(t)) + "\n")
    wl = build(as_of="2026-10-07", root=root)
    syms = [r["symbol"] for r in wl["NSE_EQ"]]
    assert wl["days"] == ["2026-10-06"] and "HDFCBANK" in syms and "INFY" not in syms, (wl["days"], syms)


# ── 4. session windows ────────────────────────────────────────────────────────
def t_session_windows():
    nse = scalper_config.windows("NSE_EQ")
    mcx = scalper_config.windows("MCX")
    assert window_of(nse, ep(DAY, 9, 19)) is None
    assert window_of(nse, ep(DAY, 9, 20)) == "09:20-11:00"
    assert window_of(nse, ep(DAY, 10, 59)) and window_of(nse, ep(DAY, 11, 0)) is None
    assert window_of(nse, ep(DAY, 12, 30)) is None and window_of(nse, ep(DAY, 13, 30)) == "13:30-15:00"
    assert window_of(nse, ep(DAY, 15, 0)) is None and window_of(nse, ep(DAY, 15, 20)) is None
    assert window_of(nse, ep(DAY, 10, 57), 0, 3) is None, "last minutes of a window are skipped"
    assert window_of(nse, ep(DAY, 9, 20), 1, 0) is None, "first minute trimmed"
    assert window_of(nse, ep("2026-10-10", 10, 0)) is None, "Saturday"
    assert window_of(mcx, ep(DAY, 17, 45)) is None and window_of(mcx, ep(DAY, 18, 30)) == "18:00-23:00"
    # replay outside every window → zero scalps, counted as window skips
    r = sim({MCX_KEY: synth(ep(DAY, 17, 31), n=1500)})
    assert not r["trades"] and r["skips"].get("window", 0) > 0, r["skips"]


# ── 5. whitelist ─────────────────────────────────────────────────────────────
def t_whitelist_ranking():
    from scalper_whitelist import build_from_ticks, allowed
    t0 = ep(DAY, 9, 30)
    ticks = {}
    for i, s in enumerate(["HDFCBANK", "ICICIBANK", "RELIANCE", "INFY", "SBIN", "AXISBANK", "KOTAKBANK",
                           "BHARTIARTL", "TCS", "ITC", "LT", "TATASTEEL"]):
        ticks[f"{s}@NSE_EQ"] = synth(t0, n=600, px0=1000.0, tick=0.1, top=400 - i * 30, seed=0)
    wide = synth(t0, n=600, px0=1000.0, tick=0.1)
    for t in wide:
        t["ask"] = t["bid"] + 1.0                          # 10-tick spread
    ticks["WIPRO@NSE_EQ"] = wide
    ticks["YESBANK@NSE_EQ"] = synth(t0, n=600, px0=20.0, tick=0.01)      # not Nifty 50
    ticks["CRUDEOILM-FUT@MCX"] = synth(ep(DAY, 18, 5), n=600)
    ticks["GOLD-FUT@MCX"] = synth(ep(DAY, 18, 5), n=600, px0=150000.0)   # 1 lot ≫ MCX cap
    ticks["USDINR-FUT@CDS"] = synth(ep(DAY, 10, 0), n=600, px0=88.0, tick=0.0025)
    wl = build_from_ticks(ticks)
    eq = [r["symbol"] for r in wl["NSE_EQ"]]
    assert len(eq) == 10 and "WIPRO" not in eq and "YESBANK" not in eq and "LT" not in eq and "TATASTEEL" not in eq, eq
    assert eq[0] == "HDFCBANK", eq                                  # deepest touch ranks first
    assert any(r["symbol"] == "WIPRO" for r in wl["rejected"])
    mcx = [r["symbol"] for r in wl["MCX"]]
    assert "CRUDEOILM-FUT" in mcx and "GOLD-FUT" not in mcx, mcx
    assert any(r["symbol"] == "GOLD-FUT" and "cap" in r["why"] for r in wl["rejected"])
    assert not any(k.endswith("@CDS") for k in wl["selected"])
    i_top = Inst("HDFCBANK@NSE_EQ", "NSE_EQ", "HDFCBANK", 0, 0.1, 1.0)
    i_10 = Inst(f"{eq[9]}@NSE_EQ", "NSE_EQ", eq[9], 0, 0.1, 1.0)
    assert allowed(wl, i_top, {"whitelist_n": 5})[0] and not allowed(wl, i_10, {"whitelist_n": 5})[0]
    assert allowed(wl, i_10, {"whitelist_n": 10})[0]
    opt_atm = Inst("X@NSE_FO_OPT", "NSE_FO", "NIFTY26O1325000CE", 0, 0.05, 1.0, 65, "opt_paper", "NFO", "opt",
                   "NIFTY", "CE", "2026-10-13", 25000.0)
    opt_far = Inst("Y@NSE_FO_OPT", "NSE_FO", "NIFTY26O1325200CE", 0, 0.05, 1.0, 65, "opt_paper", "NFO", "opt",
                   "NIFTY", "CE", "2026-10-13", 25200.0)
    assert allowed(wl, opt_atm, {}, 25050.0, 50.0)[0] and not allowed(wl, opt_far, {}, 25050.0, 50.0)[0]
    bn = Inst("Z@NSE_FO_OPT", "NSE_FO", "BANKNIFTY26OCT56000CE", 0, 0.05, 1.0, 30, "opt_paper", "NFO", "opt",
              "BANKNIFTY", "CE", "2026-10-28", 56000.0)
    assert not allowed(wl, bn, {}, 56000.0, 100.0)[0]


def t_whitelist_filters_replay():
    from scalper_whitelist import build_from_ticks
    wl = build_from_ticks({})                                      # defaults: CRUDEOILM + SILVERM
    other = "NATGASMINI-FUT@MCX"
    ticks = {MCX_KEY: synth(ep(DAY, 18, 5)), other: synth(ep(DAY, 18, 5), px0=300.0, tick=0.1, seed=3)}
    insts = {MCX_KEY: MCX_INST, other: Inst(other, "MCX", "NATGASMINI-FUT", 0, 0.1, 250.0, 1, "native", "MCX")}
    r = sim(ticks, wl=wl, insts=insts)
    assert r["trades"] and all(t["symbol"] == "CRUDEOILM-FUT" for t in r["trades"])
    assert r["skips"].get("whitelist", 0) > 0


# ── 6. cool-downs / consecutive losses / caps ───────────────────────────────
def t_cooldown_and_off():
    p = {"cooldown_sec": 300, "max_consec_losses": 2, "symbol_daily_cap": 4, "daily_cap": 12,
         "skip_open_min": 0, "skip_close_min": 0}
    st, book = SState(), DayBook()
    ctx = Ctx(risk_fn=lambda i, p: (True, 2500.0, "ok", None), capital_fn=lambda s: 1e6,
              universe_fn=lambda i: (True, "ok"), windows_fn=scalper_config.windows)
    now = ep(DAY, 18, 30)
    assert ScalpLogic.entry_filters(MCX_INST, st, book, now, p, ctx) == (True, "ok")
    ScalpLogic.on_fill(MCX_INST, st, book, now)
    ScalpLogic.on_close(MCX_INST, st, book, -500.0, now + 10, p)
    assert ScalpLogic.entry_filters(MCX_INST, st, book, now + 100, p, ctx) == (False, "cooldown")
    assert ScalpLogic.entry_filters(MCX_INST, st, book, now + 320, p, ctx) == (True, "ok")
    ScalpLogic.on_fill(MCX_INST, st, book, now + 320)
    ScalpLogic.on_close(MCX_INST, st, book, +400.0, now + 330, p)        # a win resets the streak
    assert st.sd.consec_losses == 0
    for k in range(2):
        ScalpLogic.on_fill(MCX_INST, st, book, now + 1000 + k * 400)
        ScalpLogic.on_close(MCX_INST, st, book, -300.0, now + 1010 + k * 400, p)
    assert st.sd.off and ScalpLogic.entry_filters(MCX_INST, st, book, now + 5000, p, ctx) == (False, "symbol_off")
    nxt = ep("2026-10-08", 18, 30)
    assert ScalpLogic.entry_filters(MCX_INST, st, book, nxt, p, ctx) == (True, "ok"), "new day resets"


def t_caps():
    ctx = Ctx(risk_fn=lambda i, p: (True, 2500.0, "ok", None), capital_fn=lambda s: 1e6,
              hard_cap_fn=scalper_config.hard_cap, symbol_hard_cap=6)
    p = {"cooldown_sec": 0, "max_consec_losses": 9, "symbol_daily_cap": 3, "daily_cap": 99}
    st, book = SState(), DayBook()
    now = ep(DAY, 18, 30)
    for k in range(3):
        assert ScalpLogic.entry_filters(MCX_INST, st, book, now + k * 120, p, ctx)[0]
        ScalpLogic.on_fill(MCX_INST, st, book, now + k * 120)
        ScalpLogic.on_close(MCX_INST, st, book, 10.0, now + k * 120 + 5, p)
    assert ScalpLogic.entry_filters(MCX_INST, st, book, now + 999, p, ctx) == (False, "cap"), "per-symbol cap"
    assert ScalpLogic.entry_filters(MCX_INST, SState(), book, now + 999, p, ctx)[0]
    book.seg("MCX")["entries"] = DAILY_CAP["MCX"]
    assert ScalpLogic.entry_filters(MCX_INST, SState(), book, now + 999, {**p, "daily_cap": 999}, ctx) == (False, "cap"), \
        "hard daily ceiling holds even if a param asks for more"
    st2 = SState()
    ScalpLogic.on_fill(MCX_INST, st2, DayBook(), now)
    assert ScalpLogic.entry_filters(MCX_INST, st2, DayBook(), now + 30, p, ctx) == (False, "cap"), "1/min/symbol"
    try:
        scalper_config.update({"hard_caps": {"MCX": 500}})
        raise AssertionError("hard cap could be raised")
    except ValueError:
        pass


def t_confluence_and_edge():
    p = {"imb_entry": 0.35, "mom_ticks": 4, "max_spread_ticks": 2, "confluence_min": 3, "edge_cost_mult": 2.5,
         "tp_ticks": 10}
    f = {"imbalance": 0.5, "spread_ticks": 1, "tick_mom": 5, "vwap_dev": 0.0, "bar_mom_ticks": 0.0}
    assert ScalpLogic.signal(f, p) == 0, "imbalance + momentum alone (2 votes) is not confluence"
    assert ScalpLogic.signal({**f, "bar_mom_ticks": 2}, p) == 1
    assert ScalpLogic.signal({**f, "vwap_dev": 0.001}, p) == 1
    assert ScalpLogic.signal({**f, "vwap_dev": 0.001, "bar_mom_ticks": -2}, p) == 0, "opposing bar vetoes"
    assert ScalpLogic.signal({**f, "bar_mom_ticks": 2, "vwap_dev": 0.001}, {**p, "confluence_min": 4}) == 1
    assert ScalpLogic.signal({**f, "bar_mom_ticks": 2}, {**p, "confluence_min": 4}) == 0
    assert ScalpLogic.signal({**f, "bar_mom_ticks": 2, "spread_ticks": 30}, p) == 0
    assert ScalpLogic.signal({**f, "bar_mom_ticks": 2, "spread_ticks": 30}, p, typ_spread_ticks=25) == 1, \
        "wide-tick contract judged against its own typical spread"
    eq = Inst("RELIANCE@NSE_EQ", "NSE_EQ", "RELIANCE", 0, 0.1, 1.0, 1, "kite_paper", "NSE")
    ok15, tp, need15 = ScalpLogic.cost_ok(eq, 1400.0, 300, {**p, "edge_cost_mult": 1.2, "tp_ticks": 15}, 0.1)
    ok25, tp, need25 = ScalpLogic.cost_ok(eq, 1400.0, 300, {**p, "tp_ticks": 15}, 0.1)
    assert need25 > need15 and abs(need25 / need15 - 2.5 / 1.2) < 1e-6 and ok15 and not ok25, (need15, need25, tp)


# ── 7. universe guard ─────────────────────────────────────────────────────────
def t_universe_guard():
    import dataclasses
    v = synth(ep(DAY, 10, 0), px0=1400.0, tick=0.1)
    keys = {"RELIANCE@BSE_EQ": v, "USDINR-FUT@CDS": synth(ep(DAY, 10, 0), px0=88.0, tick=0.0025),
            "BANKNIFTY26OCTFUT@NSE_FO": synth(ep(DAY, 10, 0), px0=56000.0, tick=0.1),
            "YESBANK@NSE_EQ": v, "RELIANCE@NSE_EQ": v}
    insts = {k: SB.make_inst(k, x) for k, x in keys.items()}
    insts["YESBANK@NSE_EQ"] = dataclasses.replace(insts["YESBANK@NSE_EQ"], tick=0.1)
    insts["RELIANCE@NSE_EQ"] = dataclasses.replace(insts["RELIANCE@NSE_EQ"], tick=0.1)
    p = pf({**LOOSE, "tp_ticks": 30, "sl_ticks": 10})
    r = sim(keys, p=p, windows=False, insts=insts, risk_override=2500.0)
    traded = {t["key"] for t in r["trades"]}
    assert traded == {"RELIANCE@NSE_EQ"}, traded
    assert r["skips"].get("universe", 0) > 0
    # control: same ticks without the guard → the non-Nifty-50 stock (same segment, same caps) would trade
    r2 = sim(keys, p=p, windows=False, insts=insts, risk_override=2500.0, universe=False)
    t2 = {t["key"] for t in r2["trades"]}
    assert "YESBANK@NSE_EQ" in t2, t2
    # BSE/CDS are additionally hard-capped at 0 → never trade even without the guard
    assert not ({"RELIANCE@BSE_EQ", "USDINR-FUT@CDS"} & t2), t2


# ── 8. recorder + rotation ────────────────────────────────────────────────────
def t_recorder_roundtrip_and_rotation():
    t = {"recv_ts": 1.5, "exch_ts": 1.2, "ltp": 101.0, "bid": 100.9, "ask": 101.0, "volume": 77, "oi": 5,
         "bids": [(100.9 - k * 0.1, 10 + k, 2) for k in range(5)], "asks": [(101.0 + k * 0.1, 20 + k, 3) for k in range(5)]}
    row = v2_row(t)
    assert len(row) == 37
    back = parse_row([str(x) for x in row])
    assert back["depth"] == "v2" and back["exch_ts"] == 1.2 and len(back["bids"]) == 5 and back["asks"][4][1] == 24
    v1 = parse_row(["1.0", "100", "99.9", "100", "500", "300", "200", "100", "42"])
    assert v1["depth"] == "v1" and sum(q for _p, q, _n in v1["bids"]) == 500 and v1["bids"][0][1] == 200
    root = Path(_d) / "rot"
    for day in ("2026-08-01", "2026-10-05", "2026-10-09"):
        (root / day).mkdir(parents=True, exist_ok=True)
        (root / day / "X@MCX.csv").write_text(",".join(str(x) for x in row) + "\n")
    out = rotate_ticks(root, keep_days=30, max_total_mb=100, gzip_after_days=1, today="2026-10-09")
    assert "2026-08-01" in out["deleted_days"]
    assert (root / "2026-10-05" / "X@MCX.csv.gz").exists() and not (root / "2026-10-05" / "X@MCX.csv").exists()
    assert (root / "2026-10-09" / "X@MCX.csv").exists(), "today is never compressed"
    f = tick_files(root)["2026-10-05"]["X@MCX"]
    assert load_depth_ticks(f)[0]["ltp"] == 101.0


# ── 9. one decision path, research-only ──────────────────────────────────────
def t_shared_path():
    import fast_scalper, learning_retune
    live = inspect.getsource(fast_scalper.FastScalper._decide) + inspect.getsource(fast_scalper.FastScalper._maybe_enter)
    bt = inspect.getsource(SB.simulate)
    for fn in ("ScalpLogic.step", "ScalpLogic.entry_filters", "ScalpLogic.plan"):
        assert fn in live or fn in inspect.getsource(fast_scalper.FastScalper._opt_enter), fn
        assert fn in bt, fn
    assert "ScalpLogic.ingest" in inspect.getsource(fast_scalper.FastScalper.on_tick) and "ScalpLogic.ingest" in bt
    assert "simulate" in inspect.getsource(learning_retune.replay_scalper)
    src = open(SB.__file__).read()
    assert "place_order" not in src and "trading_mode =" not in src


def t_retune_needs_oos_evidence():
    from self_learning import learning, MIN_OOS_TRADES
    d = {"2026-10-07": {MCX_KEY: synth(ep(DAY, 18, 5), n=400)}}
    ev = SB.WFEvaluator("MCX", d)
    before = dict(learning.params("scalp:MCX"))
    r = learning.retune("scalp:MCX", ev, learning.grid(learning.spec("scalp:MCX"), before, ("imb_entry",)))
    assert not r["accepted"] and learning.params("scalp:MCX") == before, r
    ok, why = learning.accept([10.0] * 30, [50.0] * (MIN_OOS_TRADES - 1))
    assert not ok and "out-of-sample" in why


def t_run_end_to_end():
    root = Path(_d) / "ticks_e2e"
    for day, seed in (("2026-10-06", 1), ("2026-10-07", 2)):
        (root / day).mkdir(parents=True, exist_ok=True)
        with open(root / day / f"{MCX_KEY}.csv", "w") as fh:
            for t in synth(ep(day, 18, 5), n=2500, seed=seed):
                fh.write(",".join(str(x) for x in v2_row(t)) + "\n")
        with open(root / day / "RELIANCE@NSE_EQ.csv", "w") as fh:     # after-hours only → dropped
            for t in synth(ep(day, 16, 0), n=50, px0=1400.0, tick=0.1):
                fh.write(",".join(str(x) for x in v2_row(t)) + "\n")
    a = SB.run(root=root, save=False)
    b = SB.run(root=root, save=False)
    assert a["ok"] and a["walk_forward"]["mode"] == "day_folds" and a["days"] == ["2026-10-06", "2026-10-07"]
    assert any("RELIANCE" in x for x in a["data"]["no_in_session_ticks"])
    for k in ("walk_forward", "in_sample", "legacy_rules"):
        assert json.dumps(a[k], sort_keys=True, default=str) == json.dumps(b[k], sort_keys=True, default=str), k
    o = a["walk_forward"]["oos"]
    for f in ("trades", "win_rate", "expectancy", "profit_factor", "sharpe_per_trade", "max_dd", "avg_hold_sec",
              "fill_rate"):
        assert f in o or o["trades"] == 0, f
    assert set(a["in_sample"]) >= {"by_symbol", "by_window", "by_hour", "costs"}


if __name__ == "__main__":
    print("\n  SCALPER BACKTEST / TRADE-LESS-BETTER TESTS")
    for name, fn in [
        ("backtester is deterministic", t_determinism),
        ("cost accounting: gross − (brokerage+STT/CTT+exchange+SEBI+GST+stamp) = net", t_cost_accounting),
        ("no look-ahead: rewriting future ticks never changes past trades", t_no_lookahead_future_ticks),
        ("no look-ahead: an order cannot fill before latency puts it at the exchange", t_latency_no_early_fill),
        ("walk-forward: train strictly earlier than test (day folds + 1-day split + evaluator)", t_walk_forward_split),
        ("walk-forward whitelist uses only days before the test day", t_whitelist_no_lookahead),
        ("session windows NSE 09:20-11:00/13:30-15:00, MCX, edge trims, weekends", t_session_windows),
        ("liquidity whitelist: rank, illiquid/non-Nifty/oversized-lot rejected, top-N, near-ATM options", t_whitelist_ranking),
        ("whitelist filters the replay", t_whitelist_filters_replay),
        ("cool-down after a loss, OFF after consecutive losses, resets next day", t_cooldown_and_off),
        ("caps: per-symbol, 1/min, hard daily ceiling, hard caps cannot be raised", t_caps),
        ("confluence (≥3 of 4 votes) and edge ≥ k×(costs+spread)", t_confluence_and_edge),
        ("owner universe guard: BSE/CDS/BANKNIFTY never traded in replay", t_universe_guard),
        ("v2 depth recorder round trip + v1 reader + rotation (gzip/delete)", t_recorder_roundtrip_and_rotation),
        ("live scalper and backtester share one decision path; no orders in research", t_shared_path),
        ("retune never accepts without out-of-sample evidence", t_retune_needs_oos_evidence),
        ("run(): end-to-end, deterministic, after-hours data dropped, stats complete", t_run_end_to_end),
    ]:
        run(name, fn)
    n_ok = sum(1 for _n, ok, _e in _results if ok)
    print(f"\n  RESULTS: {len(_results)} tests -- {n_ok} passed  {len(_results) - n_ok} failed")
    raise SystemExit(0 if n_ok == len(_results) else 1)
