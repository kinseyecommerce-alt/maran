"""
nse_day_simulation.py — Full NSE Day Simulation with Per-Agent Performance
============================================================================
Generates a complete intraday session (375 × 1-min bars = 9:15–15:30 IST) for
10 Nifty-50 stocks using calibrated GBM with real NSE volatility parameters.

Aggregates to 5-min and 15-min timeframes. Runs all 5 agents tick-by-tick on
each timeframe, tracks every entry/exit, and reports:
  - Trade log (entry, exit, P&L, pattern, reason)
  - Per-agent summary (trades, win rate, avg P&L, max drawdown, Sharpe, R:R)
  - Cross-agent leaderboard

External data: NOT required (fully offline, calibrated to May 2026 NSE levels)

Run:  python3 nse_day_simulation.py
"""
from __future__ import annotations
import sys, os, math, random, time as _time
from datetime import datetime, date, time, timedelta
from dataclasses import dataclass, field
from collections import defaultdict
from typing import Optional
import pandas as pd
import numpy as np

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

# Fixed seed for reproducible GBM paths — change to explore different market days
random.seed(42)
np.random.seed(42)

# ── Suppress loguru noise ────────────────────────────────────────────────────
import loguru
loguru.logger.remove()

from tick_engine import LiveIndicators, IndicatorCalc, Tick, MarketSnapshot, Candle
from agents.strategy_agents import IntradayAgent, ScalpingAgent, OptionsAgent, FuturesAgent, SwingAgent
import agents.strategy_agents as _sa_mod   # for clock patch
from config import settings

# ── Console colours ───────────────────────────────────────────────────────────
G = "\033[92m"; R = "\033[91m"; C = "\033[96m"; Y = "\033[93m"
M = "\033[95m"; B = "\033[1m";  D = "\033[2m";  X = "\033[0m"

# ═══════════════════════════════════════════════════════════════════════════════
# Real NSE parameters (May 2026)
# vol = annualised σ %; atr_day = typical daily ATR ₹; beta = Nifty beta
# ═══════════════════════════════════════════════════════════════════════════════
SYMBOLS: dict[str, dict] = {
    "RELIANCE":   {"ltp": 2985.0,  "vol_ann": 0.22, "atr_day": 45.0,  "trend": +1, "beta": 0.92},
    "TCS":        {"ltp": 3840.0,  "vol_ann": 0.20, "atr_day": 55.0,  "trend": +1, "beta": 0.70},
    "HDFCBANK":   {"ltp": 1745.0,  "vol_ann": 0.21, "atr_day": 27.0,  "trend": +1, "beta": 1.05},
    "INFY":       {"ltp": 1590.0,  "vol_ann": 0.23, "atr_day": 26.0,  "trend": -1, "beta": 0.80},
    "ICICIBANK":  {"ltp": 1268.0,  "vol_ann": 0.22, "atr_day": 20.0,  "trend": +1, "beta": 1.15},
    "SBIN":       {"ltp": 798.0,   "vol_ann": 0.28, "atr_day": 16.0,  "trend": -1, "beta": 1.20},
    "BAJFINANCE": {"ltp": 8950.0,  "vol_ann": 0.32, "atr_day": 140.0, "trend": -1, "beta": 1.35},
    "TITAN":      {"ltp": 3485.0,  "vol_ann": 0.26, "atr_day": 60.0,  "trend": +1, "beta": 0.85},
    "WIPRO":      {"ltp": 265.0,   "vol_ann": 0.24, "atr_day": 5.0,   "trend": -1, "beta": 0.75},
    "AXISBANK":   {"ltp": 1145.0,  "vol_ann": 0.24, "atr_day": 22.0,  "trend": +1, "beta": 1.10},
}

MARKET_OPEN  = time(9, 15)
MARKET_CLOSE = time(15, 30)
N_BARS_1MIN  = 375   # 9:15 to 15:30 inclusive

# ── Patch IST clock to return trading hours ──────────────────────────────────
# Must patch BOTH the ist_clock module AND the direct import in strategy_agents
import ist_clock as _ist

_sim_bar_time: datetime = datetime.now().replace(hour=10, minute=30)

def _fake_now_ist() -> datetime:
    return _sim_bar_time

_ist.now_ist  = _fake_now_ist     # patch ist_clock module
_sa_mod.now_ist = _fake_now_ist   # patch direct import in strategy_agents


# ═══════════════════════════════════════════════════════════════════════════════
# Data generation
# ═══════════════════════════════════════════════════════════════════════════════
def _intraday_vol_shape(bar_idx: int, n: int = N_BARS_1MIN) -> float:
    """U-shaped intraday volatility: high open, calmer midday, elevated close."""
    x = bar_idx / n
    # Opening 30 min boost, lunch dip, closing 45 min boost
    opening = math.exp(-20 * x) * 1.8
    closing  = math.exp(-20 * (1.0 - x)) * 1.5
    base = 0.6
    return base + opening + closing

def _intraday_volume_shape(bar_idx: int, n: int = N_BARS_1MIN) -> float:
    """Volume: high at open/close, low midday."""
    x = bar_idx / n
    return 0.4 + 1.8 * math.exp(-15 * x) + 1.2 * math.exp(-15 * (1 - x))

PRE_HISTORY = 250   # extra bars before market open for EMA200 warm-up

def build_1min_session(sym: str, meta: dict, seed: int = 0) -> pd.DataFrame:
    """Build a full session: 250 pre-history bars + 375-bar intraday session."""
    rng = random.Random(seed ^ hash(sym))
    np.random.seed((seed + hash(sym)) % 2**31)

    base      = meta["ltp"]
    ann_vol   = meta["vol_ann"]
    trend     = meta["trend"]      # +1 bullish, -1 bearish

    # Per-bar σ as a FRACTION (not Rs): ann_vol / sqrt(trading_bars_per_year)
    # e.g. 22% annual / sqrt(94500 bars/year) = 0.0716% per 1-min bar
    bar_sigma = ann_vol / math.sqrt(252 * N_BARS_1MIN)

    session_date = date.today()

    # ── 1. Pre-history (250 bars before 9:15 — not actual market bars,
    #       purely for indicator warm-up). Marked with is_session=False.
    pre_rows = []
    price = base
    ts_pre = datetime.combine(session_date, MARKET_OPEN) - timedelta(minutes=PRE_HISTORY)
    avg_vol = meta.get("vol", 5_000_000) / 375
    for i in range(PRE_HISTORY):
        drift = trend * bar_sigma * 0.05 + rng.gauss(0, bar_sigma)
        o = round(price, 2)
        c = round(price * (1 + drift), 2)
        h = round(max(o, c) * (1 + abs(rng.gauss(0, bar_sigma * 0.3))), 2)
        l = round(min(o, c) * (1 - abs(rng.gauss(0, bar_sigma * 0.3))), 2)
        v = max(1000, int(avg_vol * rng.lognormvariate(0, 0.3)))
        pre_rows.append({"datetime": ts_pre, "open": o, "high": h, "low": l,
                         "close": c, "volume": v, "is_session": False})
        price = c
        ts_pre += timedelta(minutes=1)

    # ── 2. Intraday session (375 bars: 9:15–15:30)
    gap_pct  = rng.uniform(0.003, 0.010) * trend * rng.uniform(0.5, 1.0)
    open_price = price * (1 + gap_pct)
    is_trending = rng.random() < 0.55

    session_rows = []
    price   = open_price
    day_open = open_price
    ts = datetime.combine(session_date, MARKET_OPEN)

    for i in range(N_BARS_1MIN):
        vol_mult = _intraday_vol_shape(i) * bar_sigma
        vol_frac = _intraday_volume_shape(i)

        if is_trending:
            drift = trend * bar_sigma * 0.25
        else:
            mean_rev = (base - price) / base * 0.5
            drift = mean_rev * bar_sigma

        if 15 <= i <= 20 and is_trending:
            drift *= 3.5
        if 100 <= i <= 115 and not is_trending:
            drift = -drift * 2.0

        ret = drift + vol_mult * rng.gauss(0, 1)
        o   = round(price, 2)
        c   = round(price * (1 + ret), 2)
        h   = round(max(o, c) * (1 + abs(rng.gauss(0, vol_mult * 0.4))), 2)
        l   = round(min(o, c) * (1 - abs(rng.gauss(0, vol_mult * 0.4))), 2)
        v   = max(1000, int(meta.get("vol", 5_000_000) / 375 * vol_frac *
                            rng.lognormvariate(0, 0.35)))
        session_rows.append({"datetime": ts, "open": o, "high": h, "low": l,
                             "close": c, "volume": v, "is_session": True})
        price = c
        ts += timedelta(minutes=1)

    all_rows = pre_rows + session_rows
    df = pd.DataFrame(all_rows).set_index("datetime")
    df["day_open"]   = day_open
    df["prev_close"] = base
    return df


def resample_ohlcv(df: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Aggregate 1-min bars to a higher timeframe."""
    r = df.resample(freq).agg({
        "open":       "first",
        "high":       "max",
        "low":        "min",
        "close":      "last",
        "volume":     "sum",
        "day_open":   "first",
        "prev_close": "first",
        "is_session": "last",
    }).dropna(subset=["close"])
    return r


# ═══════════════════════════════════════════════════════════════════════════════
# Indicator computation on a rolling window
# ═══════════════════════════════════════════════════════════════════════════════
def compute_indicators_at(sym: str, df: pd.DataFrame, bar_idx: int,
                          ltp: float) -> LiveIndicators:
    """Compute indicators using bars[0:bar_idx+1] (includes pre-history)."""
    window = df.iloc[: bar_idx + 1].copy()
    if len(window) < 5:
        return LiveIndicators(symbol=sym, ltp=ltp)
    window.index.name = "datetime"
    window = window.rename(columns=str.lower)
    # Drop simulation-only columns that IndicatorCalc doesn't know about
    for drop_col in ("is_session", "day_open", "prev_close"):
        window = window.drop(columns=[drop_col], errors="ignore")
    row = window.iloc[-1]
    tick = Tick(
        symbol=sym, ltp=ltp, bid=ltp - 0.05, ask=ltp + 0.05,
        volume=int(row.get("volume", 1000)),
        change=ltp - float(window["open"].iloc[0]),
        change_pct=(ltp - float(window["open"].iloc[0])) / float(window["open"].iloc[0]) * 100,
        high=float(window["high"].max()),
        low=float(window["low"].min()),
        open=float(window["open"].iloc[0]),
        timestamp=_sim_bar_time,
    )
    try:
        return IndicatorCalc.compute(sym, tick, window.reset_index())
    except Exception:
        return LiveIndicators(symbol=sym, ltp=ltp)


def make_snapshot(sym: str, ind: LiveIndicators, df: pd.DataFrame,
                  bar_idx: int, ltp: float) -> MarketSnapshot:
    # Use only real session bars for the candle history shown to agents
    session_bars = df[df.get("is_session", pd.Series([True]*len(df), index=df.index))]
    candle_src = session_bars.iloc[max(0, len(session_bars) - 60):]
    candles = []
    for row in candle_src.itertuples():
        ts2 = row.Index.to_pydatetime() if hasattr(row.Index, "to_pydatetime") else row.Index
        candles.append(Candle(open=row.open, high=row.high, low=row.low,
                               close=row.close, volume=row.volume, ts=ts2))
    row = df.iloc[bar_idx]
    # Use day_open for the session's open price (not pre-history open)
    if "day_open" in df.columns:
        day_open = float(df["day_open"].iloc[bar_idx])
    if not day_open or day_open <= 0:
        # Fallback: first real session close price
        day_open = ltp
    tick = Tick(
        symbol=sym, ltp=ltp, bid=ltp - 0.05, ask=ltp + 0.05,
        volume=int(row.volume),
        change=ltp - day_open,
        change_pct=(ltp - day_open) / day_open * 100 if day_open else 0.0,
        high=float(session_bars["high"].max()) if len(session_bars) else ltp + 5,
        low=float(session_bars["low"].min()) if len(session_bars) else ltp - 5,
        open=day_open,
        timestamp=_sim_bar_time,
    )
    return MarketSnapshot(
        symbol=sym, tick=tick, indicators=ind,
        candles_1min=candles, candles_5min=candles[::5],
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Trade tracker
# ═══════════════════════════════════════════════════════════════════════════════
@dataclass
class TradeRecord:
    sym:       str
    agent:     str
    tf:        str        # "1m" | "5m" | "15m"
    action:    str        # "BUY" | "SELL" | "CE" | "PE"
    pattern:   str
    entry_ts:  datetime
    entry_px:  float
    sl_px:     float
    tgt_px:    float
    sl_pct:    float
    tgt_pct:   float
    qty:       int = 1
    exit_ts:   Optional[datetime] = None
    exit_px:   Optional[float]    = None
    exit_reason: str = ""
    pnl_pct:   float = 0.0
    pnl_rs:    float = 0.0

    @property
    def closed(self): return self.exit_px is not None

    def close(self, px: float, ts: datetime, reason: str):
        self.exit_px  = px
        self.exit_ts  = ts
        self.exit_reason = reason
        if self.action in ("BUY", "CE", "LONG"):
            self.pnl_pct = (px - self.entry_px) / self.entry_px * 100
        else:
            self.pnl_pct = (self.entry_px - px) / self.entry_px * 100
        self.pnl_rs = self.pnl_pct / 100 * self.entry_px * self.qty

    @property
    def won(self): return self.pnl_pct > 0


class AgentTracker:
    def __init__(self, name: str, tf: str):
        self.name   = name
        self.tf     = tf
        self.trades: list[TradeRecord] = []
        self._open: dict[str, TradeRecord] = {}   # sym → open trade

    def on_signal(self, sym: str, action: str, signal: dict, ts: datetime, ltp: float):
        if sym in self._open:
            return   # already in position
        if action in ("HOLD", None, ""):
            return
        # Options signal uses premium-based sl/tgt (30%/65% of premium) which can't be
        # applied to the underlying stock price in simulation — use the stock-price proxy
        if self.name == "Options":
            sl_pct  = float(signal.get("underlying_sl_pct",  2.0))
            tgt_pct = float(signal.get("underlying_tgt_pct", 4.0))
        else:
            sl_pct  = float(signal.get("stop_loss_pct", settings.sl_pct_intraday))
            tgt_pct = float(signal.get("target_pct",    settings.tgt_pct_intraday))
        entry_px = ltp
        # Normalize position size: ₹50,000 per trade (mirrors risk_manager capital
        # allocation per symbol), capped at 1 share minimum.
        # This prevents high-priced stocks (BAJFINANCE ₹8950) from having 5-6×
        # the rupee impact vs low-priced stocks (SBIN ₹798) at qty=1.
        TRADE_CAPITAL = 50_000
        qty = max(1, int(TRADE_CAPITAL / entry_px))
        is_long  = action in ("BUY", "CE", "LONG")
        if is_long:
            sl_px  = entry_px * (1 - sl_pct / 100)
            tgt_px = entry_px * (1 + tgt_pct / 100)
        else:
            sl_px  = entry_px * (1 + sl_pct / 100)
            tgt_px = entry_px * (1 - tgt_pct / 100)

        tr = TradeRecord(
            sym=sym, agent=self.name, tf=self.tf,
            action=action, pattern=signal.get("pattern", "?"),
            entry_ts=ts, entry_px=entry_px,
            sl_px=sl_px, tgt_px=tgt_px,
            sl_pct=sl_pct, tgt_pct=tgt_pct, qty=qty,
        )
        self._open[sym] = tr
        self.trades.append(tr)

    def on_price(self, sym: str, ltp: float, ts: datetime):
        tr = self._open.get(sym)
        if not tr:
            return
        is_long = tr.action in ("BUY", "CE", "LONG")
        if is_long:
            if ltp <= tr.sl_px:
                tr.close(ltp, ts, "SL_HIT")
                del self._open[sym]
            elif ltp >= tr.tgt_px:
                tr.close(ltp, ts, "TARGET")
                del self._open[sym]
        else:
            if ltp >= tr.sl_px:
                tr.close(ltp, ts, "SL_HIT")
                del self._open[sym]
            elif ltp <= tr.tgt_px:
                tr.close(ltp, ts, "TARGET")
                del self._open[sym]

    def squareoff_all(self, prices: dict[str, float], ts: datetime):
        for sym, tr in list(self._open.items()):
            tr.close(prices.get(sym, tr.entry_px), ts, "SQUAREOFF")
        self._open.clear()

    def metrics(self) -> dict:
        closed  = [t for t in self.trades if t.closed]
        if not closed:
            return {"trades": 0, "win_rate": 0, "avg_pnl_pct": 0,
                    "total_pnl_rs": 0, "max_drawdown_pct": 0,
                    "sharpe": 0, "avg_rr": 0, "wins": 0, "losses": 0}
        wins    = [t for t in closed if t.won]
        losses  = [t for t in closed if not t.won]
        pnls    = [t.pnl_pct for t in closed]
        win_rate = len(wins) / len(closed) * 100

        # Equity curve for drawdown / Sharpe
        eq = [0.0]
        for p in pnls:
            eq.append(eq[-1] + p)
        peak = eq[0]
        max_dd = 0.0
        for v in eq:
            if v > peak: peak = v
            dd = peak - v
            if dd > max_dd: max_dd = dd

        avg_pnl = float(np.mean(pnls)) if pnls else 0
        std_pnl = float(np.std(pnls))  if len(pnls) > 1 else 0
        sharpe  = (avg_pnl / std_pnl * math.sqrt(len(pnls))) if std_pnl > 0 else 0

        avg_win  = float(np.mean([t.pnl_pct for t in wins]))    if wins    else 0
        avg_loss = float(np.mean([t.pnl_pct for t in losses]))  if losses  else 0
        rr = abs(avg_win / avg_loss) if avg_loss != 0 else 0

        return {
            "trades":          len(closed),
            "wins":            len(wins),
            "losses":          len(losses),
            "win_rate":        round(win_rate, 1),
            "avg_pnl_pct":     round(avg_pnl, 3),
            "total_pnl_rs":    round(sum(t.pnl_rs for t in closed), 2),
            "max_drawdown_pct":round(max_dd, 2),
            "sharpe":          round(sharpe, 2),
            "avg_rr":          round(rr, 2),
            "avg_win_pct":     round(avg_win, 3),
            "avg_loss_pct":    round(avg_loss, 3),
        }


# ═══════════════════════════════════════════════════════════════════════════════
# Main simulation loop
# ═══════════════════════════════════════════════════════════════════════════════
TIMEFRAMES = [("1m", "1min"), ("5m", "5min"), ("15m", "15min")]

AGENT_CLASSES = [
    ("Intraday",  IntradayAgent),
    ("Scalping",  ScalpingAgent),
    ("Options",   OptionsAgent),
    ("Futures",   FuturesAgent),
    ("Swing",     SwingAgent),
]


def run_simulation(seed: int = 2026) -> dict[str, dict[str, AgentTracker]]:
    """Returns {tf: {agent_name: AgentTracker}}"""
    global _sim_bar_time

    print(f"\n{B}{'═'*72}{X}")
    print(f"{B}  NSE Day Simulation — {date.today().strftime('%A %d %b %Y')}{X}")
    print(f"  Symbols: {len(SYMBOLS)}  |  Session: 09:15 – 15:30 IST  |  Timeframes: 1m / 5m / 15m")
    print(f"{B}{'═'*72}{X}\n")

    # Build session data for all symbols
    print(f"  {D}Building intraday candle history …{X}", end="", flush=True)
    sessions: dict[str, dict[str, pd.DataFrame]] = {}
    for sym, meta in SYMBOLS.items():
        df_1m = build_1min_session(sym, meta, seed=seed)
        sessions[sym] = {
            "1m":   df_1m,
            "5m":   resample_ohlcv(df_1m, "5min"),
            "15m":  resample_ohlcv(df_1m, "15min"),
        }
    print(f"  {G}done{X}\n")

    # Trackers: {tf: {agent_name: AgentTracker}}
    trackers: dict[str, dict[str, AgentTracker]] = {}
    for tf_label, _ in TIMEFRAMES:
        trackers[tf_label] = {name: AgentTracker(name, tf_label)
                               for name, _ in AGENT_CLASSES}

    all_signals_log: list[dict] = []

    # Simulate each timeframe independently
    for tf_label, tf_freq in TIMEFRAMES:
        print(f"  {C}{B}── {tf_label.upper()} TIMEFRAME ──────────────────────────────{X}")

        # Build agents fresh per timeframe (clean state)
        agents = [(name, cls()) for name, cls in AGENT_CLASSES]

        for sym in SYMBOLS:
            df = sessions[sym][tf_label]
            n_bars = len(df)
            tracker_set = trackers[tf_label]

            for bar_idx in range(n_bars):
                row = df.iloc[bar_idx]
                ts  = row.name if isinstance(row.name, datetime) else datetime.combine(date.today(), MARKET_OPEN)
                ltp = float(row.close)

                # Patch IST clock — must use global
                global _sim_bar_time
                _sim_bar_time = ts if isinstance(ts, datetime) else datetime.combine(date.today(), ts)
                _sa_mod.now_ist = _fake_now_ist   # re-assert each bar (agents may cache)

                # Update open positions with this price first (exit before entry)
                for agent_name, _ in AGENT_CLASSES:
                    tracker_set[agent_name].on_price(sym, ltp, ts)

                # Only trade real session bars (is_session=True), not pre-history
                is_sess = bool(row.get("is_session", True))
                if not is_sess:
                    continue

                # Skip last bar (squareoff time)
                if bar_idx >= n_bars - 1:
                    continue

                # Only trade within market hours 9:30–14:50
                bar_time = ts.time() if isinstance(ts, datetime) else ts
                if isinstance(bar_time, time):
                    if bar_time < time(9, 30) or bar_time > time(14, 50):
                        continue

                # Compute indicators on full window (pre-history + session so far)
                ind  = compute_indicators_at(sym, df, bar_idx, ltp)
                snap = make_snapshot(sym, ind, df, bar_idx, ltp)

                # Run each agent
                for agent_name, agent in agents:
                    try:
                        action, signal = agent.evaluate_tick(snap)
                        if action and action not in ("HOLD", None, ""):
                            tracker_set[agent_name].on_signal(sym, action, signal or {}, ts, ltp)
                            all_signals_log.append({
                                "tf": tf_label, "sym": sym, "agent": agent_name,
                                "action": action, "ts": ts, "ltp": ltp,
                                "pattern": (signal or {}).get("pattern", "?"),
                                "score": (signal or {}).get("score", "–"),
                            })
                    except Exception:
                        pass

            # Squareoff all open positions at 15:25
            final_px = {sym: float(sessions[sym][tf_label]["close"].iloc[-1])
                        for sym in SYMBOLS}
            sq_ts = datetime.combine(date.today(), time(15, 25))
            for agent_name, _ in AGENT_CLASSES:
                tracker_set[agent_name].squareoff_all(final_px, sq_ts)

        # Print per-TF signal count
        total_sigs = sum(len(t.trades) for t in trackers[tf_label].values())
        print(f"    Signals fired this TF: {total_sigs}")
        for agent_name, _ in AGENT_CLASSES:
            n = len(trackers[tf_label][agent_name].trades)
            print(f"      {agent_name:10s}: {n} trades")
        print()

    return trackers, all_signals_log


# ═══════════════════════════════════════════════════════════════════════════════
# Reporting
# ═══════════════════════════════════════════════════════════════════════════════
def pct_bar(pct: float, width: int = 20) -> str:
    filled = max(0, min(width, int(abs(pct) / 100 * width * 5)))
    colour = G if pct >= 0 else R
    return f"{colour}{'█'*filled}{'░'*(width-filled)}{X}"

def print_report(trackers: dict, all_signals_log: list):
    print(f"\n{B}{'═'*72}{X}")
    print(f"{B}  SIMULATION RESULTS — PER-AGENT PERFORMANCE{X}")
    print(f"{'═'*72}{X}\n")

    # Aggregate across timeframes
    agg: dict[str, dict] = defaultdict(lambda: {
        "trades": 0, "wins": 0, "losses": 0,
        "total_pnl_rs": 0.0, "pnl_pcts": [],
    })

    leaderboard: list[tuple] = []   # (agent, tf, metrics)

    for tf_label, tf_freq in TIMEFRAMES:
        print(f"  {C}{B}── {tf_label.upper()} TIMEFRAME ─────────────────────────────────────{X}")
        print(f"  {'Agent':10s} {'Trades':>6} {'Wins':>5} {'WinRate':>8} {'AvgPnL%':>8}"
              f" {'TotalP&L':>10} {'MaxDD%':>7} {'Sharpe':>7} {'R:R':>5}")
        print(f"  {D}{'─'*70}{X}")

        for agent_name, _ in AGENT_CLASSES:
            m = trackers[tf_label][agent_name].metrics()
            trades    = m["trades"]
            wr        = m["win_rate"]
            avg_pnl   = m["avg_pnl_pct"]
            total_pnl = m["total_pnl_rs"]
            dd        = m["max_drawdown_pct"]
            sharpe    = m["sharpe"]
            rr        = m["avg_rr"]

            wr_col  = G if wr >= 55 else (Y if wr >= 45 else R)
            pnl_col = G if avg_pnl >= 0 else R

            if trades > 0:
                print(f"  {B}{agent_name:10s}{X}"
                      f" {trades:>6}"
                      f" {m['wins']:>5}"
                      f"  {wr_col}{wr:>7.1f}%{X}"
                      f"  {pnl_col}{avg_pnl:>+7.3f}%{X}"
                      f" {pnl_col}₹{total_pnl:>+9.1f}{X}"
                      f"  {dd:>6.2f}%"
                      f"  {sharpe:>6.2f}"
                      f"  {rr:>4.1f}")

                leaderboard.append((agent_name, tf_label, m))
                agg[agent_name]["trades"]     += trades
                agg[agent_name]["wins"]       += m["wins"]
                agg[agent_name]["losses"]     += m["losses"]
                agg[agent_name]["total_pnl_rs"] += total_pnl
            else:
                print(f"  {D}{agent_name:10s}{'no trades':>60}{X}")
        print()

    # Overall leaderboard
    print(f"\n  {B}{'═'*72}{X}")
    print(f"  {B}  OVERALL LEADERBOARD (by total P&L across all timeframes){X}")
    print(f"  {'═'*72}{X}")

    lb_sorted = sorted(leaderboard, key=lambda x: x[2]["total_pnl_rs"], reverse=True)
    rank = 1
    for agent_name, tf, m in lb_sorted:
        if m["trades"] == 0:
            continue
        medal = ["🥇", "🥈", "🥉"][rank-1] if rank <= 3 else f" {rank}."
        wr_col = G if m["win_rate"] >= 55 else (Y if m["win_rate"] >= 45 else R)
        pnl_col = G if m["total_pnl_rs"] >= 0 else R
        print(f"  {medal}  {B}{agent_name:10s}{X}({tf:3s})  "
              f"trades={m['trades']:3d}  "
              f"{wr_col}win={m['win_rate']:.0f}%{X}  "
              f"{pnl_col}P&L=₹{m['total_pnl_rs']:+.1f}{X}  "
              f"Sharpe={m['sharpe']:+.2f}  "
              f"DD={m['max_drawdown_pct']:.1f}%")
        rank += 1

    # Aggregate summary
    print(f"\n  {B}{'═'*72}{X}")
    print(f"  {B}  CUMULATIVE (all agents, all timeframes){X}")
    print(f"  {'═'*72}{X}")
    for agent_name, _ in AGENT_CLASSES:
        a = agg[agent_name]
        if a["trades"] == 0:
            continue
        wr  = a["wins"] / a["trades"] * 100
        col = G if a["total_pnl_rs"] >= 0 else R
        print(f"  {B}{agent_name:10s}{X}  "
              f"total={a['trades']:3d} trades  "
              f"{G if wr>=55 else R}win={wr:.0f}%{X}  "
              f"{col}P&L=₹{a['total_pnl_rs']:+.0f}{X}")

    # Top patterns
    print(f"\n  {B}{'═'*72}{X}")
    print(f"  {B}  TOP 10 PATTERNS BY FREQUENCY{X}")
    print(f"  {'═'*72}{X}")
    from collections import Counter
    pat_counts = Counter(
        f"{s['agent']}:{s['pattern']}" for s in all_signals_log if s["pattern"] != "?"
    )
    for pat, cnt in pat_counts.most_common(10):
        agent, pattern = pat.split(":", 1)
        print(f"  {agent:10s}  {pattern:30s}  fired {cnt:3d}×")

    # Sample trade log (top 15 by P&L)
    all_trades: list[TradeRecord] = []
    for tf_data in trackers.values():
        for tracker in tf_data.values():
            all_trades.extend(t for t in tracker.trades if t.closed)

    all_trades.sort(key=lambda t: abs(t.pnl_pct), reverse=True)
    print(f"\n  {B}{'═'*72}{X}")
    print(f"  {B}  TOP 15 INDIVIDUAL TRADES (by |P&L%|){X}")
    print(f"  {'═'*72}{X}")
    print(f"  {'Sym':10s} {'Agent':9s} {'TF':3s} {'Act':5s} {'Pattern':22s}"
          f" {'Entry':>8} {'Exit':>8} {'P&L%':>7} {'Reason':12s}")
    print(f"  {D}{'─'*80}{X}")
    for t in all_trades[:15]:
        col = G if t.pnl_pct >= 0 else R
        print(f"  {t.sym:10s} {t.agent:9s} {t.tf:3s} {t.action:5s} "
              f"{t.pattern:22s} ₹{t.entry_px:>7.1f} ₹{t.exit_px:>7.1f}"
              f" {col}{t.pnl_pct:>+6.2f}%{X} {t.exit_reason}")

    print(f"\n{B}{'═'*72}{X}")
    print(f"{B}  SIMULATION COMPLETE{X}  |  "
          f"Total signals: {len(all_signals_log)}  |  "
          f"Total trades closed: {len([t for t in all_trades if t.closed])}")
    print(f"{B}{'═'*72}{X}\n")


if __name__ == "__main__":
    t0 = _time.time()
    trackers, signals_log = run_simulation(seed=2026)
    print_report(trackers, signals_log)
    print(f"  {D}Elapsed: {_time.time()-t0:.1f}s{X}\n")
