"""
unified_backtest.py — ONE event-driven backtester for every agent
(jag 2026-10-10: "give all agents the same enhancement the scalper got").

Data (REAL only — never the GBM simulator)
  • Kite 1-minute history: logs/historical_data/<SYM>/1m.csv (Nifty 50 +
    NIFTY), logs/historical_data/_native/<SYM>_MCX_1m.csv (MCX front month).
  • Recorded Kite WS depth ticks (logs/ticks/<day>/<SYM@SEG>.csv.gz) where
    they exist: per-minute TRUE spread, book imbalance and the first touch
    after the order latency are used for fills / spread filter / book-flip
    exits. Otherwise a realistic spread model (instrument median from the
    recorded ticks, else a per-segment default) + slippage.

Decision code (no duplicated logic)
  • NSE built-ins: the live agent classes' evaluate_tick() / should_exit_position()
    on snapshots built with tick_engine.IndicatorCalc (the live indicator code)
    at the agent's own decision cadence (settings.decision_bar_minutes), with
    the IST clock pinned to the bar.
  • MCX native: segment_engine.NativeStrategy.signal() + NativeEngine.stop_distance /
    size_lots (1-min closes as a proxy of the live 10-s bars — stated).
  • Inventor (MCX trend-follow design): NativeEngine.trend() + invent params
    (stop_mult / target_r / min_strength), as strategy_inventor._design_native.
  • Gating: agent_policy.AgentGate (windows, whitelist, caps, cool-downs,
    consecutive-loss stop, filters, cost-edge) and bot_state's regime gate on a
    causal NIFTY regime timeline (replay_backtest.build_regime_timeline).
  • Exits: exit_policy.step (breakeven, partial, chandelier, time stop,
    book flip) + the agent's own brain exits + session square-off.

Execution: decision at bar close → order reaches the exchange after
latency_ms → fills at the first recorded touch after that (ticks) or at the
next bar's open ± half spread ± slippage (bars). Stops are SL-M: adverse-first
inside a bar, gap-through fills at the open. Limit targets / partials fill
only when the bar trades through them. Costs: cost_model (brokerage,
STT/CTT, exchange, SEBI, GST, stamp).

Overfitting control: walk-forward folds by day (expanding train, embargo,
test blocks), whitelist from TRAIN days only, minimum OOS sample, deflated
Sharpe ratio (multiple-testing penalty for the number of hypotheses tried),
bounded parameter ranges (agent_policy specs).

Runs as a SUBPROCESS of the server (the IST clock is pinned per bar inside
this process only):
    python unified_backtest.py --agents all --days 20
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import pickle
import statistics
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
IST = timezone(timedelta(hours=5, minutes=30))
DATA_DIR = HERE / "logs" / "historical_data"
NATIVE_DIR = DATA_DIR / "_native"
CACHE_DIR = HERE / "logs" / "backtests" / "ub_cache"
RESULT_PATH = HERE / "logs" / "backtests" / "unified_backtest.json"

NSE_AGENTS = ("intraday", "scalping", "swing", "momentum", "mean_reversion")
FO_AGENTS = ("futures", "options", "option_scalping")
MCX_AGENTS = ("mcx_native", "invent")
ALL_AGENTS = NSE_AGENTS + FO_AGENTS + MCX_AGENTS
LIVE_CLASS = {"intraday": "IntradayAgent", "scalping": "ScalpingAgent", "swing": "SwingAgent",
              "momentum": "MomentumAgent", "mean_reversion": "MeanReversionAgent", "futures": "FuturesAgent",
              "options": "OptionsAgent", "option_scalping": "OptionScalpingAgent"}
CAPITAL = 1_000_000.0            # per segment (segments._limits default)
RISK_PCT = 1.0                   # hard 1% per trade (risk_per_trade_hard_cap_pct)
CASH_NOTIONAL_FRAC = 0.25        # cash notional ≤ 25% of NSE_EQ capital
SLIP_BPS = {"NSE_EQ": 2.0, "NSE_FO": 1.0, "MCX": 2.0}
DEFAULT_SPREAD_BPS = {"NSE_EQ": 3.0, "NSE_FO": 1.0, "MCX": 4.0}
LATENCY_MS = 300.0
NSE_SQ = dtime(15, 15)
MCX_SQ = dtime(23, 15)
WARMUP_BARS = 260
MIN_OOS_TRADES = 20
DSR_MIN = 0.95
GEN_VERSION = "2"            # bump when gen_symbol_day / _snapshot change


# ═════════════════════════════════════════════════════════════════════════════
# data
# ═════════════════════════════════════════════════════════════════════════════
def _read_bars(path: Path) -> list[tuple]:
    """[(epoch, o, h, l, c, v)] oldest first; naive timestamps are IST."""
    import csv
    out = []
    if not path.exists():
        return out
    with open(path, newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                dt = datetime.fromisoformat(r["date"].strip())
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=IST)
                out.append((dt.timestamp(), float(r["open"]), float(r["high"]), float(r["low"]),
                            float(r["close"]), float(r.get("volume") or 0)))
            except Exception:
                continue
    out.sort(key=lambda x: x[0])
    return out


def bars_path(symbol: str, segment: str) -> Path:
    if segment == "MCX":
        return NATIVE_DIR / f"{symbol}_MCX_1m.csv"
    return DATA_DIR / symbol / "1m.csv"


def session_ok(segment: str, t: dtime) -> bool:
    if segment == "MCX":
        return dtime(9, 0) <= t < dtime(23, 30)
    return dtime(9, 15) <= t < dtime(15, 30)


def by_day(bars: list[tuple], segment: str) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = defaultdict(list)
    for b in bars:
        dt = datetime.fromtimestamp(b[0], IST)
        if dt.weekday() < 5 and session_ok(segment, dt.time()):
            out[dt.date().isoformat()].append(b)
    return dict(out)


def tick_minutes(day: str, symbol: str, segment: str, root: Optional[Path] = None) -> dict[int, dict]:
    """{minute_epoch: {spread, imb, touches, n}} from recorded ticks (in-session, two-sided)."""
    from tick_replayer import tick_files, load_depth_ticks
    files = tick_files(root, [day]).get(day, {})
    f = files.get(f"{symbol}@{segment}")
    out: dict[int, dict] = {}
    if not f:
        return out
    for t in load_depth_ticks(f):
        if t["bid"] <= 0 or t["ask"] <= 0 or t["ask"] < t["bid"]:
            continue
        dt = datetime.fromtimestamp(t["recv_ts"], IST)
        if not session_ok(segment, dt.time()):
            continue
        m = int(t["recv_ts"] // 60 * 60)
        bq = sum(q for _p, q, _n in t["bids"])
        aq = sum(q for _p, q, _n in t["asks"])
        r = out.setdefault(m, {"spread": [], "imb": [], "first": []})
        r["spread"].append(t["ask"] - t["bid"])
        if bq + aq > 0:
            r["imb"].append(bq / (bq + aq))
        r["first"].append((t["recv_ts"], t["bid"], t["ask"]))
    return {m: {"spread": statistics.median(v["spread"]),
                "imb": (sum(v["imb"]) / len(v["imb"])) if v["imb"] else None,
                "touches": v["first"][:50], "n": len(v["spread"])} for m, v in out.items()}


def lot_size(name: str = "NIFTY") -> int:
    import csv
    d = HERE / "logs" / "instruments"
    files = sorted(d.glob("NFO_*.csv")) if d.exists() else []
    if files:
        with open(files[-1], newline="") as fh:
            for r in csv.DictReader(fh):
                if r.get("name") == name and r.get("instrument_type") == "FUT":
                    try:
                        return int(float(r["lot_size"]))
                    except Exception:
                        break
    return 65


_NOT_IN_PHASE1 = ("should_exit_position",)    # exit-only methods: never run when signals are generated


def _signal_source(src: str) -> str:
    """Source with exit-only methods removed (AST), so an exit-rule fix does
    not invalidate cached ENTRY signals that it cannot affect. Phase 1 only
    calls evaluate_tick(); should_exit_position runs in phase 2 (Sim._manage)
    on every run, uncached."""
    import ast
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return src
    lines = src.splitlines(keepends=True)
    cut: list[tuple[int, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for f in node.body:
                if isinstance(f, ast.FunctionDef) and f.name in _NOT_IN_PHASE1:
                    start = min([f.lineno] + [d.lineno for d in f.decorator_list]) - 1
                    cut.append((start, f.end_lineno))
    for a, b in sorted(cut, reverse=True):
        del lines[a:b]
    return "".join(lines)


def code_version() -> str:
    h = hashlib.sha1()
    h.update(GEN_VERSION.encode())
    for f in ("agents/strategy_agents.py", "tick_engine.py"):      # the code that produces cached signals
        p = HERE / f
        if p.exists():
            h.update(_signal_source(p.read_text()).encode() if f.endswith("strategy_agents.py") else p.read_bytes())
    return h.hexdigest()[:12]


# ═════════════════════════════════════════════════════════════════════════════
# phase 1 — run the LIVE decision code over real bars (cached)
# ═════════════════════════════════════════════════════════════════════════════
_CLOCK = {"now": datetime.now(IST)}


def _fake_now() -> datetime:
    return _CLOCK["now"]


def pin_clock() -> None:
    """Point every module-level now_ist used by the agents at the replay clock.
    Only ever called inside the backtest subprocess / tests."""
    import ist_clock
    ist_clock.now_ist = _fake_now
    for mod in ("agents.strategy_agents", "agents.base_agent", "segment_engine", "strategy_inventor"):
        m = sys.modules.get(mod)
        if m is not None and hasattr(m, "now_ist"):
            m.now_ist = _fake_now


def _snapshot(symbol: str, window: list[tuple], session_start: int):
    """MarketSnapshot as the tick engine builds it from completed 1-min bars."""
    import pandas as pd
    from tick_engine import IndicatorCalc, Tick, MarketSnapshot, Candle
    ts, o, h, l, c, v = window[-1]
    sess = window[session_start:] if session_start < len(window) else window[-1:]
    day_open = sess[0][1]
    df = pd.DataFrame(window, columns=["epoch", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    now = datetime.fromtimestamp(ts + 60, IST)
    tick = Tick(symbol=symbol, ltp=c, bid=c, ask=c, volume=int(v), change=c - day_open,
                change_pct=(c - day_open) / day_open * 100 if day_open else 0.0,
                high=max(b[2] for b in sess), low=min(b[3] for b in sess), open=day_open, timestamp=now)
    ind = IndicatorCalc.compute(symbol, tick, df[["date", "open", "high", "low", "close", "volume"]])
    c1 = [Candle(b[1], b[2], b[3], b[4], int(b[5]), datetime.fromtimestamp(b[0], IST)) for b in sess[-60:]]
    c5: list = []
    for b in sess:
        bt = datetime.fromtimestamp(b[0], IST)
        b5 = bt.replace(minute=bt.minute - bt.minute % 5, second=0)
        if c5 and c5[-1].ts == b5:
            k = c5[-1]
            k.high, k.low, k.close, k.volume = max(k.high, b[2]), min(k.low, b[3]), b[4], k.volume + int(b[5])
        else:
            c5.append(Candle(b[1], b[2], b[3], b[4], int(b[5]), b5))
    return MarketSnapshot(symbol=symbol, tick=tick, indicators=ind, candles_1min=c1, candles_5min=c5[-60:],
                          bar_seconds=60), ind


def _decision_tf(agent_obj) -> int:
    try:
        return int(agent_obj._decision_tf_min())
    except Exception:
        return 1


_ENTRY = {"BUY", "SELL", "LONG", "SHORT", "CE", "PE"}


def gen_symbol_day(symbol: str, segment: str, agents: list[str], day: str, prior: list[tuple],
                   bars: list[tuple]) -> dict:
    """Signals of the LIVE agent code for one symbol-day (prior = warm-up bars)."""
    import bot_state as _bs
    import agents.strategy_agents as sa
    pin_clock()
    objs = {a: getattr(sa, LIVE_CLASS[a])() for a in agents if a in LIVE_CLASS}
    for o in objs.values():
        try:
            o._approved = {symbol}
        except Exception:
            pass
    tf = {a: _decision_tf(o) for a, o in objs.items()}
    # options agents decide inside evaluate_tick but hand the trade to the
    # options engine on a thread; capture that hand-off as the agent's signal
    handoffs: list = []
    for a, o in objs.items():
        if a in ("options", "option_scalping"):
            o._engine_handoff = (lambda engine, und, opt_type, pattern, score, is_sell, _a=a:
                                 handoffs.append((_a, opt_type, pattern, score, is_sell)))
    sig: dict[str, list] = {a: [] for a in objs}
    inds: list = []
    window = list(prior[-(WARMUP_BARS - 1):]) if prior else []
    try:
        _bs.set_current_trade_date(date.fromisoformat(day))
    except Exception:
        pass
    n_prior = len(window)
    for i, b in enumerate(bars):
        window.append(b)
        if len(window) > WARMUP_BARS:
            window.pop(0)
            n_prior = max(0, n_prior - 1)
        bt = datetime.fromtimestamp(b[0], IST)
        _CLOCK["now"] = bt + timedelta(minutes=1)          # decision at bar CLOSE
        snap, ind = _snapshot(symbol, window, n_prior)
        inds.append(ind)
        for a, o in objs.items():
            if tf[a] > 1 and (bt.minute + 1) % tf[a] != 0:
                continue
            try:
                action, signal = o.evaluate_tick(snap)
            except Exception:
                continue
            if a in ("options", "option_scalping"):
                import threading as _thr
                for th in list(_thr.enumerate()):
                    if th.name.startswith("opt-handoff-"):
                        th.join(timeout=2.0)
                while handoffs:
                    _a, opt_type, pattern, score, is_sell = handoffs.pop(0)
                    if not is_sell and str(opt_type).upper() in ("CE", "PE"):
                        sig[_a].append((i, str(opt_type).upper(), {"pattern": pattern, "score": score}))
                continue
            if action and str(action).upper() in _ENTRY and signal:
                keep = {k: v for k, v in signal.items() if isinstance(v, (int, float, str, bool)) or v is None}
                sig[a].append((i, str(action).upper(), keep))
    return {"sig": sig, "inds": inds, "tf": tf}


def _cache_key(symbol: str, segment: str, day: str, agents: list[str], bars: list[tuple]) -> Path:
    h = hashlib.sha1(json.dumps([symbol, segment, day, sorted(agents), len(bars), bars[-1] if bars else None,
                                 code_version()]).encode()).hexdigest()[:16]
    return CACHE_DIR / f"{symbol}_{day}_{h}.pkl"


def _gen_worker(args) -> tuple:
    symbol, segment, agents, days = args
    import loguru
    loguru.logger.remove()
    allb = _read_bars(bars_path(symbol, segment))
    per = by_day(allb, segment)
    ds = sorted(per)
    out = {}
    for d in days:
        if d not in per:
            continue
        j = ds.index(d)
        prior: list = []
        for pd_ in reversed(ds[:j]):
            prior = per[pd_] + prior
            if len(prior) >= WARMUP_BARS:
                break
        ck = _cache_key(symbol, segment, d, agents, per[d])
        if ck.exists():
            try:
                out[d] = pickle.loads(ck.read_bytes())
                continue
            except Exception:
                pass
        r = gen_symbol_day(symbol, segment, agents, d, prior, per[d])
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        ck.write_bytes(pickle.dumps(r))
        out[d] = r
    return symbol, out


# ═════════════════════════════════════════════════════════════════════════════
# phase 2 — execution simulation (cheap; re-run per hypothesis)
# ═════════════════════════════════════════════════════════════════════════════
@dataclass
class Trade:
    agent: str
    symbol: str
    segment: str
    side: int
    entry_ts: float
    entry: float
    qty: float
    exit_ts: float = 0.0
    exit: float = 0.0
    gross: float = 0.0
    costs: float = 0.0
    net: float = 0.0
    reason: str = ""
    regime: str = "UNKNOWN"
    pattern: str = ""
    data: str = "kite_1m"
    legs: list = field(default_factory=list)     # partial exits [(ts, px, qty, why)]
    r_risk: float = 0.0

    def d(self) -> dict:
        return {"agent": self.agent, "symbol": self.symbol, "segment": self.segment,
                "side": "BUY" if self.side > 0 else "SELL",
                "entry_ts": datetime.fromtimestamp(self.entry_ts, IST).isoformat(timespec="minutes"),
                "exit_ts": datetime.fromtimestamp(self.exit_ts, IST).isoformat(timespec="minutes") if self.exit_ts else None,
                "entry": round(self.entry, 4), "exit": round(self.exit, 4), "qty": self.qty,
                "gross": round(self.gross, 2), "costs": round(self.costs, 2), "net": round(self.net, 2),
                "reason": self.reason, "regime": self.regime, "pattern": self.pattern, "data": self.data,
                "hold_min": round((self.exit_ts - self.entry_ts) / 60.0, 1) if self.exit_ts else None,
                "partials": len(self.legs)}


class Market:
    """Bars + recorded-tick microstructure for one instrument."""

    def __init__(self, symbol: str, segment: str, day_bars: dict[str, list[tuple]], tick_days: list[str],
                 tick_root: Optional[Path] = None) -> None:
        self.symbol, self.segment = symbol, segment
        self.bars = day_bars
        self.tm: dict[str, dict] = {}
        for d in tick_days:
            if d in day_bars:
                try:
                    m = tick_minutes(d, symbol, segment, tick_root)
                    if m:
                        self.tm[d] = m
                except Exception:
                    pass
        px = next(iter(day_bars.values()))[-1][4] if day_bars else 100.0
        sp = [v["spread"] for m in self.tm.values() for v in m.values()]
        self.typ_spread = float(statistics.median(sp)) if len(sp) >= 30 else px * DEFAULT_SPREAD_BPS.get(segment, 3) / 1e4
        self.spread_src = "recorded ticks" if len(sp) >= 30 else "model"

    def micro(self, day: str, epoch: float) -> Optional[dict]:
        return (self.tm.get(day) or {}).get(int(epoch // 60 * 60))

    def data_label(self, day: str) -> str:
        return "ticks+kite_1m" if day in self.tm else "kite_1m"

    def spread_at(self, day: str, epoch: float) -> float:
        mm = self.micro(day, epoch)
        return float(mm["spread"]) if mm and mm.get("spread") else self.typ_spread

    def fill(self, day: str, i: int, side: int, latency_ms: float, slip_bps: float) -> tuple[float, str]:
        """Market order decided at the close of bar i → fill after latency."""
        bars = self.bars[day]
        decide = bars[i][0] + 60.0
        arrive = decide + latency_ms / 1000.0
        mm = self.micro(day, arrive)
        if mm:
            for ts, bid, ask in mm["touches"]:
                if ts >= arrive:
                    px = ask if side > 0 else bid
                    return px * (1 + side * slip_bps / 1e4 * 0.5), "touch"
        o = bars[i + 1][1] if i + 1 < len(bars) else bars[i][4]
        half = self.spread_at(day, decide) / 2.0
        return o + side * (half + o * slip_bps / 1e4), "bar"


def realised_vol_ratio(closes: list[float], hist_rv: list[float]) -> Optional[float]:
    if len(closes) < 31 or len(hist_rv) < 20:
        return None
    rets = [math.log(b / a) for a, b in zip(closes[-31:-1], closes[-30:]) if a > 0 and b > 0]
    if len(rets) < 10:
        return None
    med = statistics.median(hist_rv)
    return statistics.pstdev(rets) / med if med > 0 else None


def _cost(segment: str, product: str, qty: float, entry: float, exit_: float, side: int, symbol: str = "") -> float:
    from cost_model import costs, kind_for
    kind = "OPT" if product == "OPT" else ("EQ_DELIVERY" if product == "CNC" else kind_for(segment, product, symbol))
    exch = {"MCX": "MCX", "NSE_FO": "NFO"}.get(segment, "NSE")
    return float(costs(kind, qty, entry, exit_, "BUY" if side > 0 else "SELL", exch)["total"])


def _next_tuesday(d: date) -> date:
    return d + timedelta(days=(1 - d.weekday()) % 7)


def option_premium(spot: float, strike: float, typ: str, epoch: float, iv: float) -> float:
    """Black-Scholes mark (options_engine.bs) to the NIFTY weekly (Tuesday) expiry."""
    from options_engine import bs
    dt = datetime.fromtimestamp(epoch, IST)
    exp_dt = datetime.combine(_next_tuesday(dt.date()), dtime(15, 30), tzinfo=IST)
    if exp_dt <= dt:
        exp_dt += timedelta(days=7)
    T = max((exp_dt - dt).total_seconds() / (365.0 * 86400), 1.0 / (365 * 24 * 4))
    try:
        return max(0.05, float(bs(spot, strike, T, max(iv, 0.05), typ)["price"]))
    except Exception:
        return max(0.05, (spot - strike) if typ == "CE" else (strike - spot))


def _atr(bars: list[tuple], n: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs = [max(b[2] - b[3], abs(b[2] - a[4]), abs(b[3] - a[4])) for a, b in zip(bars[-n - 1:-1], bars[-n:])]
    return sum(trs) / len(trs) if trs else 0.0


def _skip_key(why: str) -> str:
    w = (why or "").lower()
    for k, name in (("stopped for the day", "agent_off"), ("daily cap", "daily_cap"), ("trades today", "symbol_cap"),
                    ("straight losses", "symbol_off"), ("cool-down", "cooldown"), ("whitelist", "whitelist"),
                    ("window", "window"), ("blackout", "event_blackout"), ("vix", "vix"), ("volatility", "vix"),
                    ("spread", "spread"), ("allocation", "allocation")):
        if k in w:
            return name
    return "filter_other"


class Sim:
    """Executes one agent's signals over the given days (chronological, one
    position per instrument, the shared AgentGate state across days)."""

    def __init__(self, agent: str, markets: dict[str, Market], signals: dict[str, dict[str, dict]],
                 params: dict, *, days: list[str], latency_ms: float = LATENCY_MS, whitelist: Optional[dict] = None,
                 regime_tl: Optional[dict] = None, use_regime: bool = True, use_brain_exits: bool = True,
                 agent_params: Optional[dict] = None, nifty_rv: Optional[dict] = None) -> None:
        from agent_policy import AgentGate, clamp_params
        self.agent = agent
        self.m = markets
        self.sig = signals
        self.p = clamp_params(agent, params)
        self.ap = agent_params or {}
        self.days = days
        self.latency = latency_ms
        self.wl = whitelist
        self.regime_tl = regime_tl or {}
        self.use_regime = use_regime
        self.use_brain = use_brain_exits
        self.nifty_rv = nifty_rv or {}
        self.gate = AgentGate(params_fn=lambda _a: self.p, whitelist_fn=lambda: self.wl or {}, weight_fn=None)
        self.trades: list[Trade] = []
        self.skips: dict[str, int] = defaultdict(int)
        self.signals_seen = 0
        self._brain_obj = None

    def _segment(self) -> str:
        return "MCX" if self.agent in MCX_AGENTS else ("NSE_FO" if self.agent in FO_AGENTS else "NSE_EQ")

    def _regime(self, epoch: float) -> str:
        keys = self.regime_tl.get("keys") or []
        if not keys:
            return "UNKNOWN"
        import bisect
        j = bisect.bisect_right(keys, epoch) - 1
        return self.regime_tl["vals"][j] if j >= 0 else "UNKNOWN"

    def _brain(self):
        if self._brain_obj is None and self.agent in LIVE_CLASS:
            import agents.strategy_agents as sa
            pin_clock()
            self._brain_obj = getattr(sa, LIVE_CLASS[self.agent])()
        return self._brain_obj

    def run(self) -> list[Trade]:
        from exit_policy import new_state, step
        import bot_state as _bs
        seg = self._segment()
        ev: dict[str, dict[int, list]] = defaultdict(lambda: defaultdict(list))
        for sym, per in self.sig.items():
            for d in self.days:
                g = per.get(d)
                if g:
                    for i, action, s in g["sig"].get(self.agent, []):
                        ev[d][i].append((sym, action, s))
        open_pos: dict[str, dict] = {}
        for d in self.days:
            syms = [s for s in self.m if d in self.m[s].bars]
            if not syms:
                continue
            nbar = max(len(self.m[s].bars[d]) for s in syms)
            for i in range(nbar):
                for key in list(open_pos):
                    pos = open_pos[key]
                    bars = self.m[pos["symbol"]].bars.get(d) or []
                    if i >= len(bars) or (pos["day"] == d and i < pos["start_i"]):
                        continue
                    self._manage(pos, d, i, bars, self.m[pos["symbol"]], step)
                    if pos["st"].qty <= 0:
                        self._book(pos)
                        del open_pos[key]
                for sym, action, s in ev[d].get(i, []):
                    self.signals_seen += 1
                    mk = self.m.get(sym)
                    if mk is None or d not in mk.bars or i + 1 >= len(mk.bars[d]):
                        continue
                    if sym in open_pos:
                        self.skips["already_in_position"] += 1
                        continue
                    self._try_entry(d, i, sym, action, s, mk, open_pos, new_state, _bs, seg)
            for key in list(open_pos):
                pos = open_pos[key]
                if self.agent == "swing" and pos["days_held"] < 5:
                    pos["days_held"] += 1
                    continue
                bars = self.m[pos["symbol"]].bars.get(d) or []
                if bars:
                    self._exit_all(pos, bars[-1][0] + 60, bars[-1][4], "eod_squareoff")
                    self._book(pos)
                    del open_pos[key]
        for key, pos in list(open_pos.items()):
            last = self.m[pos["symbol"]].bars[pos["day"]][-1]
            self._exit_all(pos, last[0] + 60, last[4], "end_of_data")
            self._book(pos)
        return self.trades

    def _try_entry(self, d, i, sym, action, s, mk: Market, open_pos, new_state, _bs, seg) -> None:
        bars = mk.bars[d]
        b = bars[i]
        decide = b[0] + 60.0
        ts = datetime.fromtimestamp(decide, IST)
        side = 1 if action in ("BUY", "LONG", "CE") else -1
        regime = self._regime(decide)
        if self.use_regime and self.agent in LIVE_CLASS:
            _bs.set_current_regime(regime)
            try:
                _bs.set_current_trade_date(ts.date())
            except Exception:
                pass
            if not _bs.is_agent_allowed_in_regime(self.agent):
                self.skips["regime"] += 1
                return
        if self.agent in MCX_AGENTS:
            rv = realised_vol_ratio([x[4] for x in bars[: i + 1]], [])
        else:
            rv = realised_vol_ratio(self.nifty_rv.get("closes", {}).get(d, [])[: i + 1],
                                    self.nifty_rv.get("hist", {}).get(d, []))
        mm = mk.micro(d, decide)
        dec = self.gate.pre_check(self.agent, sym, ts, seg, spread=(mm or {}).get("spread"),
                                  typical_spread=mk.typ_spread if mm else None, rv_ratio=rv, params=self.p,
                                  whitelist=self.wl)
        if not dec.ok:
            self.skips[_skip_key(dec.why)] += 1
            return
        ltp = b[4]
        atr = _atr(bars[: i + 1])
        # live resolution order (base_agent._place_orders / _register_position):
        # absolute price → agent pct (FuturesAgent carries only stop_loss_pct /
        # target_pct) → default. Before 2026-10-10 the pct fields were ignored
        # here, so futures ran without its own target and with a generic stop.
        stop = float(s.get("stop_loss") or 0)
        if (not stop or stop <= 0) and float(s.get("stop_loss_pct") or 0) > 0:
            stop = ltp * (1 - side * float(s["stop_loss_pct"]) / 100.0)
        if not stop or (stop - ltp) * side >= 0 or abs(stop - ltp) > 0.2 * ltp:
            stop = ltp - side * max(1.5 * atr, ltp * 0.003)
        target = float(s.get("target") or 0)
        if (not target or target <= 0) and float(s.get("target_pct") or 0) > 0:
            target = ltp * (1 + side * float(s["target_pct"]) / 100.0)
        if target and (target - ltp) * side <= 0:
            target = 0.0
        dist = abs(ltp - stop)
        product, unit = "MIS", 1.0
        risk_budget = CAPITAL * RISK_PCT / 100.0 * dec.size_mult
        opt = None
        if self.agent == "swing":
            product = "CNC"
        if self.agent == "futures":
            product, unit = "NRML", float(lot_size("NIFTY"))
        if self.agent in ("options", "option_scalping"):
            opt = self._option_plan(d, i, side, ltp, risk_budget)
            if opt is None:
                self.skips["option_size"] += 1
                return
        if self.agent in MCX_AGENTS:
            unit = float(s.get("multiplier") or 1.0)
        if opt:
            qty = opt["qty"]
            exp_move = opt["prem"] * float(self.ap.get("tgt_pct", 40.0)) / 100.0
            spread = max(0.05, opt["prem"] * 0.002)
            cost_rt = _cost("NSE_FO", "OPT", qty, opt["prem"], opt["prem"] + exp_move, 1)
        else:
            if dist <= 0:
                return
            if self.agent in MCX_AGENTS:
                lots = int(s.get("lots") or 0)
                lots = int(lots * min(1.0, dec.size_mult)) if dec.size_mult < 1 else lots
                qty = max(0, lots) * unit
            elif unit > 1:
                lots = int(risk_budget // (dist * unit))
                if lots < 1 and dist * unit <= CAPITAL * RISK_PCT / 100.0 and dec.size_mult >= 0.5:
                    lots = 1
                qty = lots * unit
            else:
                qty = math.floor(risk_budget / dist)
                qty = min(qty, math.floor(CAPITAL * CASH_NOTIONAL_FRAC / max(ltp, 1e-9)))
            if qty <= 0:
                self.skips["size_below_one"] += 1
                return
            exp_move = abs(target - ltp) if target else float(self.p["partial_r"]) * dist
            spread = mk.spread_at(d, decide)
            cost_rt = _cost(seg, product, qty, ltp, ltp + side * exp_move, side, sym)
        ok, _why = self.gate.edge_ok(self.agent, qty, exp_move, cost_rt, spread, params=self.p)
        if not ok:
            self.skips["cost_edge"] += 1
            return
        if opt:
            und_px, how = mk.fill(d, i, 1 if opt["typ"] == "CE" else -1, self.latency, SLIP_BPS["NSE_FO"])
            entry = option_premium(und_px, opt["K"], opt["typ"], decide, opt["iv"]) + spread / 2
            st = new_state(1, entry, entry * (1 - float(self.ap.get("sl_pct", 25.0)) / 100.0), qty, decide,
                           target=entry * (1 + float(self.ap.get("tgt_pct", 40.0)) / 100.0),
                           min_unit=float(lot_size("NIFTY")))
            pside = 1
        else:
            entry, how = mk.fill(d, i, side, self.latency, SLIP_BPS.get(seg, 2.0))
            tgt = (entry + side * abs(target - ltp)) if target else None
            st = new_state(side, entry, entry - side * dist, qty, decide, target=tgt,
                           min_unit=unit if unit > 1 else 1.0, cost_buffer=cost_rt / max(qty, 1e-9) / 2.0)
            pside = side
        self.gate.on_entry(self.agent, sym, ts)
        tr = Trade(self.agent, sym if not opt else f"NIFTY{opt['K']:.0f}{opt['typ']}", seg, pside, decide, entry, qty,
                   regime=regime, pattern=str(s.get("pattern") or s.get("idea") or ""),
                   data=mk.data_label(d) + ("+touch" if how == "touch" else ""), r_risk=abs(entry - st.stop) * qty)
        open_pos[sym] = {"symbol": sym, "day": d, "start_i": i + 1, "st": st, "trade": tr, "side": side, "opt": opt,
                         "product": "OPT" if opt else product, "days_held": 0, "seg": seg}

    def _option_plan(self, d, i, side, spot, risk_budget) -> Optional[dict]:
        typ = "CE" if side > 0 else "PE"
        K = round(spot / 50.0) * 50.0
        iv = self.nifty_rv.get("iv", {}).get(d, 0.13)
        b = self.m["NIFTY"].bars[d][i]
        prem = option_premium(spot, K, typ, b[0] + 60, iv)
        lot = lot_size("NIFTY")
        sl = float(self.ap.get("sl_pct", 25.0)) / 100.0
        lots = int(risk_budget // (prem * sl * lot))
        if self.agent == "option_scalping":
            lots = min(lots, 2)                         # half size until 30 real trades (live rule)
        if lots < 1:
            return None
        return {"typ": typ, "K": K, "iv": iv, "prem": prem, "qty": lots * lot}

    def _manage(self, pos, d, i, bars, mk: Market, step) -> None:
        b = bars[i]
        st = pos["st"]
        ts_close = b[0] + 60.0
        t = datetime.fromtimestamp(b[0], IST).time()
        tr: Trade = pos["trade"]
        if pos["opt"]:
            o = pos["opt"]
            ce = o["typ"] == "CE"
            p_hi = option_premium(b[2] if ce else b[3], o["K"], o["typ"], b[0] + 30, o["iv"])
            p_lo = option_premium(b[3] if ce else b[2], o["K"], o["typ"], b[0] + 30, o["iv"])
            p_cl = option_premium(b[4], o["K"], o["typ"], ts_close, o["iv"])
            p_op = option_premium(b[1], o["K"], o["typ"], b[0], o["iv"])
            acts = step(st, {**self.p, "time_stop_min": float(self.ap.get("max_hold_min", self.p["time_stop_min"]))},
                        ts_close, p_hi, p_lo, p_cl, atr=0.0, imbalance=None, open_=p_op)
            spread_adj = max(0.05, p_cl * 0.002) / 2
        else:
            mm = mk.micro(d, b[0])
            imb = (mm or {}).get("imb")
            acts = step(st, self.p, ts_close, b[2], b[3], b[4], atr=_atr(bars[: i + 1]),
                        imbalance=imb if self.agent in ("scalping", "momentum") else None, open_=b[1])
            spread_adj = mk.spread_at(d, b[0]) / 2
        for a in acts:
            if a.kind in ("breakeven", "trail"):
                continue
            px = a.px
            if a.kind in ("stop", "time_stop", "book_flip"):
                px = a.px - st.side * (spread_adj + abs(a.px) * SLIP_BPS.get(pos["seg"], 2.0) / 1e4)
            if a.kind == "partial":
                tr.legs.append((ts_close, px, a.qty, a.why))
                continue
            tr.exit_ts, tr.exit, tr.reason = ts_close, px, a.kind
        if st.qty <= 0:
            return
        if self.use_brain and not pos["opt"] and self.agent in LIVE_CLASS:
            g = self.sig.get(pos["symbol"], {}).get(d) or {}
            tfm = (g.get("tf") or {}).get(self.agent, 1)
            inds = g.get("inds") or []
            ind = inds[i] if i < len(inds) else None
            if ind is not None and (tfm <= 1 or (datetime.fromtimestamp(b[0], IST).minute + 1) % tfm == 0):
                o = self._brain()
                _CLOCK["now"] = datetime.fromtimestamp(ts_close, IST)
                p = {"average_price": tr.entry, "quantity": int(st.qty) * pos["side"], "last_price": b[4],
                     "tradingsymbol": pos["symbol"], "pnl": (b[4] - tr.entry) * st.qty * pos["side"],
                     "product": pos["product"]}
                try:
                    should, why = o.should_exit_position(p, ind)
                except Exception:
                    should, why = False, ""
                if should and not int(self.p.get("signal_exits", 1)):
                    from exit_policy import is_mandatory_exit
                    if not is_mandatory_exit(str(why)):
                        should = False            # discretionary indicator exit disabled by policy
                if should and ts_close - tr.entry_ts >= 120:
                    px, _how = mk.fill(d, i, -pos["side"], self.latency, SLIP_BPS.get(pos["seg"], 2.0))
                    self._exit_all(pos, ts_close, px, f"brain:{str(why)[:40]}")
                    return
        sq = MCX_SQ if pos["seg"] == "MCX" else NSE_SQ
        if self.agent != "swing" and t >= sq:
            if pos["opt"]:
                px = option_premium(b[4], pos["opt"]["K"], pos["opt"]["typ"], ts_close, pos["opt"]["iv"]) - spread_adj
            else:
                px = b[4] - st.side * spread_adj
            self._exit_all(pos, ts_close, px, "squareoff")

    def _exit_all(self, pos, ts, px, reason) -> None:
        tr: Trade = pos["trade"]
        if pos["opt"] and reason in ("eod_squareoff", "end_of_data"):
            b_ = self.m["NIFTY"].bars[pos["day"]][-1]
            px = option_premium(b_[4], pos["opt"]["K"], pos["opt"]["typ"], ts, pos["opt"]["iv"])
        tr.exit_ts, tr.exit, tr.reason = ts, px, reason
        pos["st"].qty = 0

    def _book(self, pos) -> None:
        tr: Trade = pos["trade"]
        side = tr.side
        rest = tr.qty - sum(q for _t, _p, q, _w in tr.legs)
        gross = sum((p - tr.entry) * q * side for _t, p, q, _w in tr.legs) + (tr.exit - tr.entry) * rest * side
        costs = sum(_cost(pos["seg"], pos["product"], q, tr.entry, p, side, tr.symbol) for _t, p, q, _w in tr.legs)
        if rest > 0:
            costs += _cost(pos["seg"], pos["product"], rest, tr.entry, tr.exit, side, tr.symbol)
        tr.gross, tr.costs = gross, costs
        tr.net = gross - costs
        self.trades.append(tr)
        self.gate.on_close(self.agent, pos["symbol"], tr.net, datetime.fromtimestamp(tr.exit_ts or tr.entry_ts, IST),
                           params=self.p)


# ═════════════════════════════════════════════════════════════════════════════
# MCX: native strategy + inventor trend design on real MCX 1-min bars
# ═════════════════════════════════════════════════════════════════════════════
def _native_stub(contract, closes: list[float]):
    """A minimal NativeEngine-shaped object so the LIVE trend / stop_distance /
    size_lots methods run unchanged on replayed bars."""
    import functools
    import types
    import segment_engine as se
    key = f"{contract.symbol}@MCX"
    stub = types.SimpleNamespace(contracts={key: contract}, price={key: closes[-1]}, bars={key: closes},
                                 src={key: "KITE"}, positions_={})
    stub.notional_open = lambda _seg: 0.0
    stub.max_lots_by_notional = functools.partial(se.NativeEngine.max_lots_by_notional, stub)
    stub.trend = functools.partial(se.NativeEngine.trend, stub)
    stub.stop_distance = functools.partial(se.NativeEngine.stop_distance, stub)
    stub.size_lots = functools.partial(se.NativeEngine.size_lots, stub)
    return stub, key


def gen_mcx(symbol: str, agents: list[str], day: str, prior: list[tuple], bars: list[tuple],
            invent_params: Optional[dict] = None) -> dict:
    import segment_engine as se
    se.BAR_SEC = 60                                   # replay bars are 1-minute
    c = next((x for x in se.UNIVERSE["MCX"] if x.symbol == symbol), None)
    sig: dict[str, list] = {a: [] for a in agents}
    if c is None:
        return {"sig": sig, "inds": [], "tf": {}}
    strat = se.NativeStrategy("mcx_trend") if "mcx_native" in agents else None
    ip = invent_params or {}
    closes = [b[4] for b in prior[-120:]]
    for i, b in enumerate(bars):
        closes.append(b[4])
        if len(closes) > 120:
            closes.pop(0)
        stub, key = _native_stub(c, list(closes))
        for a in agents:
            side = None
            dist = 0.0
            extra: dict = {}
            if a == "mcx_native" and strat is not None:
                side = strat.signal(list(closes))
                if side:
                    p = strat.params()
                    dist = stub.stop_distance(key, float(p.get("sl_range_frac", se.SL_RANGE_FRAC)))
                    extra["target_r"] = float(p.get("target_r", se.TARGET_R))
            elif a == "invent" and i % 5 == 4:          # inventor designs every ~5 min
                t = stub.trend(key)
                if t and t["strength"] >= float(ip.get("min_strength", 0.03)):
                    side = t["side"]
                    dist = stub.stop_distance(key) * float(ip.get("stop_mult", 1.0))
                    extra["target_r"] = float(ip.get("target_r", 1.6))
                    extra["idea"] = f"{symbol.replace('-FUT', '').lower()}_trend_{'long' if side == 'BUY' else 'short'}"
            if not side or dist <= 0:
                continue
            try:
                lots, _m, _why = stub.size_lots(key, dist)
            except Exception:
                lots = 0
            if lots < 1:
                continue
            px = b[4]
            sd = 1 if side == "BUY" else -1
            sig[a].append((i, side, {"stop_loss": px - sd * dist, "target": px + sd * dist * extra["target_r"],
                                     "lots": lots, "multiplier": c.multiplier, "idea": extra.get("idea", "")}))
    return {"sig": sig, "inds": [], "tf": {}}


def _mcx_worker(args) -> tuple:
    symbol, agents, days, ip = args
    import loguru
    loguru.logger.remove()
    per = by_day(_read_bars(bars_path(symbol, "MCX")), "MCX")
    ds = sorted(per)
    out = {}
    for d in days:
        if d not in per:
            continue
        j = ds.index(d)
        prior = per[ds[j - 1]] if j > 0 else []
        out[d] = gen_mcx(symbol, agents, d, prior, per[d], ip)
    return symbol, out


# ═════════════════════════════════════════════════════════════════════════════
# statistics + overfitting control
# ═════════════════════════════════════════════════════════════════════════════
def stats(trades: list[Trade]) -> dict:
    n = len(trades)
    if not n:
        return {"trades": 0, "net": 0.0, "gross": 0.0, "costs": 0.0, "win_rate": None, "expectancy": None,
                "profit_factor": None, "max_dd": 0.0, "sharpe_daily": None, "days": 0}
    net = [t.net for t in trades]
    wins = [x for x in net if x > 0]
    losses = [x for x in net if x <= 0]
    eq = peak = dd = 0.0
    for x in net:
        eq += x
        peak = max(peak, eq)
        dd = min(dd, eq - peak)
    daily: dict[str, float] = defaultdict(float)
    for t in trades:
        daily[datetime.fromtimestamp(t.entry_ts, IST).date().isoformat()] += t.net
    dv = list(daily.values())
    sh = (statistics.mean(dv) / statistics.pstdev(dv) * math.sqrt(252)) if len(dv) >= 3 and statistics.pstdev(dv) > 0 else None
    return {"trades": n, "net": round(sum(net), 2), "gross": round(sum(t.gross for t in trades), 2),
            "costs": round(sum(t.costs for t in trades), 2), "win_rate": round(len(wins) / n * 100, 1),
            "expectancy": round(sum(net) / n, 2),
            "profit_factor": round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) else None,
            "max_dd": round(dd, 2), "sharpe_daily": round(sh, 2) if sh is not None else None, "days": len(dv)}


def breakdown(trades: list[Trade], key: Callable[[Trade], str]) -> dict:
    g: dict[str, list] = defaultdict(list)
    for t in trades:
        g[key(t)].append(t)
    return {k: {"n": len(v), "net": round(sum(x.net for x in v), 2),
                "win_rate": round(sum(1 for x in v if x.net > 0) / len(v) * 100, 1)} for k, v in sorted(g.items())}


def folds(days: list[str], k: int = 4, embargo: int = 1, min_train: int = 5) -> list[tuple[list, list]]:
    """Walk-forward: expanding train window, `embargo` days purged between
    train and test (overnight carry / indicator memory), k test blocks."""
    days = sorted(days)
    if len(days) < min_train + embargo + 1:
        return []
    usable = days[min_train + embargo:]
    k = max(1, min(k, len(usable)))
    size = math.ceil(len(usable) / k)
    out = []
    for j in range(k):
        test = usable[j * size:(j + 1) * size]
        if not test:
            continue
        first = days.index(test[0])
        train = days[:max(0, first - embargo)]
        if len(train) >= min_train:
            out.append((train, test))
    return out


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_ppf(p: float) -> float:
    lo, hi = -10.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if _norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def deflated_sharpe(returns: list[float], n_trials: int = 1, sr_var: Optional[float] = None) -> Optional[float]:
    """Probability that the true per-trade Sharpe > the max Sharpe expected
    from n_trials pure-noise strategies (Bailey & López de Prado 2014)."""
    n = len(returns)
    if n < 5:
        return None
    m = statistics.mean(returns)
    sd = statistics.pstdev(returns)
    if sd <= 0:
        return None
    sr = m / sd
    sk = sum((r - m) ** 3 for r in returns) / n / sd ** 3
    ku = sum((r - m) ** 4 for r in returns) / n / sd ** 4
    N = max(1, int(n_trials))
    if N > 1:
        v = sr_var if sr_var is not None else 1.0 / n
        g = 0.5772156649
        sr0 = math.sqrt(v) * ((1 - g) * _norm_ppf(1 - 1.0 / N) + g * _norm_ppf(1 - 1.0 / (N * math.e)))
    else:
        sr0 = 0.0
    den = math.sqrt(max(1e-12, 1 - sk * sr + (ku - 1) / 4 * sr * sr))
    return _norm_cdf((sr - sr0) * math.sqrt(n - 1) / den)


def accept(cur: dict, cand: dict, cand_returns: list[float], n_trials: int) -> tuple[bool, str]:
    """A candidate replaces the current policy only with enough OOS trades,
    positive OOS net, ≥10% better than the current OOS net (or current < 0
    and candidate > 0), and a deflated Sharpe ≥ DSR_MIN after the trial count."""
    if cand["trades"] < MIN_OOS_TRADES:
        return False, f"only {cand['trades']} OOS trades (< {MIN_OOS_TRADES})"
    if cand["net"] <= 0:
        return False, f"OOS net ₹{cand['net']:,.0f} ≤ 0 after costs"
    base = cur.get("net", 0.0) or 0.0
    if base > 0 and cand["net"] < base * 1.10:
        return False, f"OOS net ₹{cand['net']:,.0f} not ≥10% better than current ₹{base:,.0f}"
    dsr = deflated_sharpe(cand_returns, n_trials)
    if dsr is None or dsr < DSR_MIN:
        return False, (f"deflated Sharpe {dsr if dsr is None else round(dsr, 3)} < {DSR_MIN} "
                       f"after {n_trials} trials — could be luck")
    return True, f"OOS ₹{cand['net']:,.0f} on {cand['trades']} trades, DSR {dsr:.3f} after {n_trials} trials"


# ═════════════════════════════════════════════════════════════════════════════
# dataset + evaluation
# ═════════════════════════════════════════════════════════════════════════════
def nse_symbols() -> list[str]:
    try:
        from owner_universe import owner_universe, _nifty50
        syms = sorted(_nifty50())
        syms = [s for s in syms if owner_universe.allows(s, "NSE_EQ")[0]]
    except Exception:
        syms = []
    if not syms:
        syms = sorted(p.name for p in DATA_DIR.iterdir() if p.is_dir() and not p.name.startswith("_")
                      and p.name not in ("NIFTY", "BANKNIFTY"))
    return [s for s in syms if bars_path(s, "NSE_EQ").exists()]


def mcx_symbols() -> list[str]:
    try:
        from owner_universe import owner_universe
        if not owner_universe.segment_enabled("MCX"):
            return []
    except Exception:
        pass
    import segment_engine as se
    return [c.symbol for c in se.UNIVERSE["MCX"] if bars_path(c.symbol, "MCX").exists()]


def build_whitelist(markets: dict[str, Market], train_days: list[str], agents: list[str]) -> dict:
    """Rank instruments by TRAIN-day median turnover per bps of spread (no test data)."""
    score: dict[str, float] = {}
    for s, mk in markets.items():
        tv = []
        for d in train_days:
            bs_ = mk.bars.get(d)
            if bs_:
                tv.append(sum(b[4] * b[5] for b in bs_))
        if not tv:
            continue
        px = mk.bars[train_days[-1]][-1][4] if train_days[-1] in mk.bars else next(iter(mk.bars.values()))[-1][4]
        sp_bps = max(0.1, mk.typ_spread / px * 1e4) if px else 10.0
        score[s] = statistics.median(tv) / sp_bps
    ranked = {}
    for a in agents:
        seg_syms = [s for s in score if (markets[s].segment == ("MCX" if a in MCX_AGENTS else "NSE_EQ"))]
        if a in FO_AGENTS:
            continue
        ranked[a] = sorted(seg_syms, key=lambda s: -score[s])
    return {"ranked": ranked}


def _nifty_ctx(nifty: dict[str, list[tuple]]) -> tuple[dict, dict]:
    """Causal NIFTY regime timeline + realised-vol context (VIX proxy, option IV)."""
    import pandas as pd
    from replay_backtest import build_regime_timeline
    keys, vals = [], []
    closes: dict[str, list] = {}
    hist: dict[str, list] = {}
    iv: dict[str, float] = {}
    ds = sorted(nifty)
    day_rv: list[float] = []
    day_close: list[float] = []
    for d in ds:
        bars = nifty[d]
        df = pd.DataFrame(bars, columns=["epoch", "open", "high", "low", "close", "volume"])
        df.index = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
        tl = build_regime_timeline(df)
        for ts, r in tl.items():
            keys.append(ts.timestamp() + 60)
            vals.append(r)
        closes[d] = [b[4] for b in bars]
        hist[d] = list(day_rv[-20:])
        rets = [math.log(b[4] / a[4]) for a, b in zip(bars[:-1], bars[1:]) if a[4] > 0]
        if len(day_close) >= 6:
            dr = [math.log(b / a) for a, b in zip(day_close[-21:-1], day_close[-20:])]
            iv[d] = max(0.08, statistics.pstdev(dr) * math.sqrt(252) * 1.1) if len(dr) >= 5 else 0.13
        else:
            iv[d] = 0.13
        if len(rets) > 30:
            day_rv.append(statistics.pstdev(rets))
        day_close.append(bars[-1][4])
    return {"keys": keys, "vals": vals}, {"closes": closes, "hist": hist, "iv": iv}


class Dataset:
    def __init__(self, agents: list[str], n_days: int = 20, symbols: Optional[list[str]] = None,
                 workers: int = 6, log: Callable[[str], None] = print) -> None:
        from owner_universe import owner_universe
        self.notes: list[str] = []
        self.agents = list(agents)
        nse_a = [a for a in agents if a in NSE_AGENTS]
        fo_a = [a for a in agents if a in FO_AGENTS]
        mcx_a = [a for a in agents if a in MCX_AGENTS]
        nifty_all = by_day(_read_bars(bars_path("NIFTY", "NSE_EQ")), "NSE_EQ")
        self.days = sorted(nifty_all)[-n_days:]
        if mcx_a:
            mdays = sorted({d for s in mcx_symbols()[:1] for d in by_day(_read_bars(bars_path(s, "MCX")), "MCX")})
            self.mcx_days = mdays[-n_days:]
        else:
            self.mcx_days = []
        nse_syms = [s for s in (symbols or nse_symbols()) if s in nse_symbols()] if nse_a else []
        mcx_syms = [s for s in (symbols or mcx_symbols()) if s in mcx_symbols()] if mcx_a else []
        if fo_a and not owner_universe.fo_underlying_allowed("NIFTY"):
            self.notes.append("NIFTY F&O paused by owner — futures/options agents skipped")
            fo_a = []
        from tick_replayer import tick_files
        tick_days = sorted(tick_files().keys())
        self.markets: dict[str, Market] = {}
        for s in nse_syms + (["NIFTY"] if (fo_a or nse_a) else []):
            per = by_day(_read_bars(bars_path(s, "NSE_EQ")), "NSE_EQ")
            self.markets[s] = Market(s, "NSE_EQ" if s != "NIFTY" else "NSE_FO",
                                     {d: per[d] for d in self.days if d in per}, tick_days)
        for s in mcx_syms:
            per = by_day(_read_bars(bars_path(s, "MCX")), "MCX")
            self.markets[s] = Market(s, "MCX", {d: per[d] for d in self.mcx_days if d in per}, tick_days)
        self.regime_tl, self.nifty_ctx = _nifty_ctx({d: nifty_all[d] for d in sorted(nifty_all)[-(n_days + 25):]})
        self.signals: dict[str, dict[str, dict]] = {}
        jobs = [(s, "NSE_EQ", nse_a, self.days) for s in nse_syms]
        if fo_a:
            jobs.append(("NIFTY", "NSE_EQ", fo_a, self.days))
        t0 = time.time()
        if jobs:
            log(f"generating live-agent signals: {len(jobs)} instruments × {len(self.days)} days …")
            self._pool(_gen_worker, jobs, workers)
        if mcx_a and mcx_syms:
            from self_learning import learning
            ip = learning.params("invent") if hasattr(learning, "params") else {}
            self._pool(_mcx_worker, [(s, mcx_a, self.mcx_days, ip) for s in mcx_syms], workers)
        log(f"signals ready in {time.time() - t0:.0f}s")
        if tick_days:
            self.notes.append(f"recorded depth ticks used for spread/imbalance/fills on {', '.join(tick_days)}; "
                              "all other days use Kite 1-min bars + spread model")
        self.notes.append("India VIX history not stored — NIFTY realised-vol ratio used as the VIX-spike proxy")
        if fo_a:
            self.notes.append("futures/options decide on the NIFTY index 1-min series (no stored futures/option "
                              "history); option premia marked with Black-Scholes, IV = 20-day RV × 1.1")

    def _pool(self, fn, jobs, workers) -> None:
        if workers <= 1 or len(jobs) == 1:
            for j in jobs:
                s, out = fn(j)
                self.signals.setdefault(s, {}).update(out)
            return
        import multiprocessing as mp
        with mp.get_context("fork").Pool(min(workers, len(jobs))) as pool:
            for s, out in pool.imap_unordered(fn, jobs):
                self.signals.setdefault(s, {}).update(out)

    def days_for(self, agent: str) -> list[str]:
        return self.mcx_days if agent in MCX_AGENTS else self.days

    def markets_for(self, agent: str) -> dict[str, Market]:
        if agent in MCX_AGENTS:
            return {s: m for s, m in self.markets.items() if m.segment == "MCX"}
        if agent in FO_AGENTS:
            return {s: m for s, m in self.markets.items() if s == "NIFTY"}
        return {s: m for s, m in self.markets.items() if m.segment == "NSE_EQ"}


def agent_params(agent: str) -> dict:
    """Live learned params of the agent's own strategy family (options → OPT_BUY)."""
    try:
        from self_learning import learning
        if agent in ("options", "option_scalping"):
            return learning.params("opt_buy:AGENT")
        if agent == "invent":
            return learning.params("invent")
    except Exception:
        pass
    return {}


def simulate(ds: Dataset, agent: str, params: dict, days: list[str], whitelist: Optional[dict]) -> Sim:
    sim = Sim(agent, ds.markets_for(agent), ds.signals, params, days=days, whitelist=whitelist,
              regime_tl=ds.regime_tl, nifty_rv=ds.nifty_ctx, agent_params=agent_params(agent))
    sim.run()
    return sim


def evaluate_agent(ds: Dataset, agent: str, params: dict, k: int = 4) -> dict:
    """Walk-forward OOS: whitelist from each fold's TRAIN days, trades counted on TEST days only."""
    days = ds.days_for(agent)
    fs = folds(days, k=k)
    oos: list[Trade] = []
    skips: dict[str, int] = defaultdict(int)
    seen = 0
    for train, test in fs:
        wl = build_whitelist(ds.markets_for(agent), train, [agent])
        sim = simulate(ds, agent, params, test, wl)
        oos.extend(sim.trades)
        seen += sim.signals_seen
        for kk, v in sim.skips.items():
            skips[kk] += v
    st = stats(oos)
    st["signals"] = seen
    st["skips"] = dict(skips)
    st["folds"] = len(fs)
    return {"stats": st, "trades": oos}


def verdict(st: dict) -> str:
    if st["trades"] < MIN_OOS_TRADES:
        return f"INSUFFICIENT SAMPLE ({st['trades']} OOS trades < {MIN_OOS_TRADES}) — no conclusion"
    if st["net"] > 0 and (st.get("profit_factor") or 0) >= 1.2:
        return "EDGE ON THIS SAMPLE (after costs) — paper only, keep collecting evidence"
    if st["net"] > 0:
        return "MARGINAL (net > 0 but profit factor < 1.2)"
    return "NO EDGE after costs on this sample"


def run(agents: list[str], n_days: int = 20, symbols: Optional[list[str]] = None, workers: int = 6,
        out: Optional[Path] = RESULT_PATH, hypotheses: Optional[list[dict]] = None,
        log: Callable[[str], None] = print) -> dict:
    """Baseline (current live policy params) per agent + optional hypotheses
    [{"id", "agent", "params"}] judged by accept() with a per-agent trial count."""
    import loguru
    loguru.logger.remove()
    loguru.logger.add(sys.stderr, level="WARNING")
    from agent_policy import live_params, clamp_params
    from owner_universe import owner_universe
    t0 = time.time()
    agents = [a for a in agents if a in ALL_AGENTS]
    if not owner_universe.segment_enabled("MCX"):
        agents = [a for a in agents if a not in MCX_AGENTS]
    ds = Dataset(agents, n_days, symbols, workers, log)
    res: dict = {"generated": datetime.now(IST).isoformat(timespec="seconds"), "code_version": code_version(),
                 "days": ds.days, "mcx_days": ds.mcx_days, "notes": ds.notes, "agents": {}, "hypotheses": [],
                 "costs_model": "cost_model (brokerage, STT/CTT, exchange, SEBI, GST, stamp)",
                 "execution": f"decision at bar close, {LATENCY_MS:.0f} ms latency, next-bar open ± half spread ± "
                              f"slippage {SLIP_BPS} bps; first recorded touch where ticks exist; SL-M adverse-first"}
    base: dict[str, dict] = {}
    for a in agents:
        p = live_params(a)
        ev = evaluate_agent(ds, a, p)
        st = ev["stats"]
        tr = ev["trades"]
        st["dsr"] = (round(deflated_sharpe([t.net for t in tr], 1), 3)
                     if deflated_sharpe([t.net for t in tr], 1) is not None else None)
        st["sharpe_per_trade"] = (round(statistics.mean([t.net for t in tr]) / statistics.pstdev([t.net for t in tr]), 3)
                                  if len(tr) > 2 and statistics.pstdev([t.net for t in tr]) > 0 else None)
        base[a] = st
        res["agents"][a] = {"params": p, "stats": st, "verdict": verdict(st),
                            "by_symbol": breakdown(tr, lambda t: t.symbol),
                            "by_hour": breakdown(tr, lambda t: datetime.fromtimestamp(t.entry_ts, IST).strftime("%H")),
                            "by_exit": breakdown(tr, lambda t: t.reason.split(":")[0]),
                            "by_regime": breakdown(tr, lambda t: t.regime),
                            "by_data": breakdown(tr, lambda t: t.data),
                            "sample_trades": [t.d() for t in tr[-15:]],
                            "equity": [[datetime.fromtimestamp(t.exit_ts or t.entry_ts, IST).isoformat(timespec="minutes"),
                                        round(t.net, 2)] for t in sorted(tr, key=lambda x: x.exit_ts or x.entry_ts)]}
        log(f"{a:16s} OOS {st['trades']:4d} trades  net ₹{st['net']:>10,.0f}  costs ₹{st['costs']:>9,.0f}  "
            f"win {st['win_rate']}%  → {verdict(st)}")
    # live liquidity whitelist: ranked on ALL replayed days (the live gate uses
    # it going forward; the walk-forward above ranked on train days only)
    wl: dict = {"ranked": {}}
    for a in agents:
        if a in FO_AGENTS:
            continue
        wl["ranked"].update(build_whitelist(ds.markets_for(a), ds.days_for(a), [a])["ranked"])
    res["whitelist"] = wl
    trials: dict[str, int] = defaultdict(int)
    for h in hypotheses or []:
        trials[h.get("agent", "")] += 1
    for h in hypotheses or []:
        a = h.get("agent")
        if a not in agents:
            h2 = {**h, "result": "skipped", "why": f"{a} not evaluated (paused/retired or unknown)"}
            res["hypotheses"].append(h2)
            continue
        p = clamp_params(a, {**live_params(a), **(h.get("params") or {})})
        ev = evaluate_agent(ds, a, p)
        ok, why = accept(base[a], ev["stats"], [t.net for t in ev["trades"]], trials[a] + 1)
        res["hypotheses"].append({**h, "params": p, "stats": ev["stats"], "baseline": base[a],
                                  "result": "pass" if ok else "fail", "why": why,
                                  "n_trials": trials[a] + 1})
        log(f"  hypothesis {h.get('id')} ({a}): {'PASS' if ok else 'fail'} — {why}")
    res["elapsed_s"] = round(time.time() - t0, 1)
    if out:
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, indent=1, default=str))
    return res


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Unified real-data backtest of every AbiTrade agent (PAPER research)")
    ap.add_argument("--agents", default="all")
    ap.add_argument("--days", type=int, default=20)
    ap.add_argument("--symbols", default="")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--out", default=str(RESULT_PATH))
    ap.add_argument("--hypotheses", default="", help="JSON file: [{id, agent, params}]")
    a = ap.parse_args(argv)
    os.chdir(HERE)
    sys.path.insert(0, str(HERE))
    agents = list(ALL_AGENTS) if a.agents == "all" else [x.strip() for x in a.agents.split(",") if x.strip()]
    syms = [x.strip() for x in a.symbols.split(",") if x.strip()] or None
    hyp = json.loads(Path(a.hypotheses).read_text()) if a.hypotheses else None
    run(agents, a.days, syms, a.workers, Path(a.out) if a.out else None, hyp)
    return 0


if __name__ == "__main__":
    sys.exit(main())
