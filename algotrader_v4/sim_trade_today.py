"""
sim_trade_today.py — Simulation trade using realistic today's NSE prices.

Builds synthetic 1-minute candle history for each symbol using:
  - Realistic NSE closing prices (last known prices for major stocks)
  - GBM-style intraday microstructure to warm up all indicators (200+ bars)
  - Real indicator computation via IndicatorCalc
  - All 5 agents evaluate each symbol tick → prints every signal that fires

Run:  python3 sim_trade_today.py
"""
from __future__ import annotations
import sys, os, random, math
from datetime import datetime, timedelta, time as dtime
import pandas as pd
import numpy as np

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ".")

from tick_engine import LiveIndicators, IndicatorCalc, Tick, MarketSnapshot, Candle, TickBuffer
from agents.strategy_agents import IntradayAgent, ScalpingAgent, OptionsAgent, FuturesAgent, SwingAgent
from config import settings

# ── Realistic NSE prices (approximate last-known closes, May 2026) ─────────
SYMBOLS: dict[str, dict] = {
    "RELIANCE":  {"ltp": 2985.0,  "vol": 8000000,  "atr_est": 22.0,  "trend": "UP"},
    "TCS":       {"ltp": 3840.0,  "vol": 2500000,  "atr_est": 28.0,  "trend": "UP"},
    "HDFCBANK":  {"ltp": 1745.0,  "vol": 12000000, "atr_est": 14.0,  "trend": "UP"},
    "INFY":      {"ltp": 1590.0,  "vol": 6000000,  "atr_est": 13.0,  "trend": "UP"},
    "ICICIBANK": {"ltp": 1268.0,  "vol": 9000000,  "atr_est": 11.0,  "trend": "UP"},
    "SBIN":      {"ltp": 798.0,   "vol": 15000000, "atr_est": 7.5,   "trend": "UP"},
    "BAJFINANCE":{"ltp": 8950.0,  "vol": 1800000,  "atr_est": 70.0,  "trend": "DOWN"},
    "TITAN":     {"ltp": 3485.0,  "vol": 1200000,  "atr_est": 30.0,  "trend": "UP"},
    "WIPRO":     {"ltp": 265.0,   "vol": 8000000,  "atr_est": 3.0,   "trend": "DOWN"},
    "AXISBANK":  {"ltp": 1145.0,  "vol": 7500000,  "atr_est": 10.0,  "trend": "UP"},
}

NIFTY_PRICE = 24650.0  # approximate NIFTY level

# ── Colour helpers ────────────────────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
RESET  = "\033[0m"

def _bar(label, val, width=40):
    filled = min(int(val / 100 * width), width)
    return f"[{'█'*filled}{'░'*(width-filled)}] {val:.0f}%"


def build_candles(sym: str, meta: dict, n_bars: int = 250, seed: int = 42) -> pd.DataFrame:
    """
    Generate n_bars 1-minute OHLCV candles that replicate a realistic intraday
    session for the given symbol. Uses GBM with mean-reversion toward VWAP.
    """
    rng  = random.Random(seed + hash(sym) % 1000)
    base = meta["ltp"]
    vol_base = meta["vol"] / 375      # per-minute volume
    atr  = meta["atr_est"]
    trend = 1.0 if meta["trend"] == "UP" else -1.0

    sigma = atr / base * math.sqrt(1/375)  # per-bar σ

    rows = []
    price = base * (1 - rng.uniform(0.002, 0.012) * trend)  # open below/above close
    ts = datetime.now().replace(hour=9, minute=15, second=0, microsecond=0)

    # Force known patterns into specific regions of the history
    for i in range(n_bars):
        # GBM drift + trend push + random
        ret  = sigma * rng.gauss(0, 1) + trend * sigma * 0.15
        # Extra momentum burst in middle of day (to trigger patterns)
        if 90 < i < 120:
            ret += trend * sigma * 0.4
        # RSI-reset zone toward end (pullback)
        if 180 < i < 200:
            ret -= trend * sigma * 0.3

        o = round(price, 2)
        c = round(price * (1 + ret), 2)
        h = round(max(o, c) * (1 + abs(rng.gauss(0, sigma * 0.5))), 2)
        l = round(min(o, c) * (1 - abs(rng.gauss(0, sigma * 0.5))), 2)
        v = int(vol_base * rng.lognormvariate(0, 0.4))

        rows.append({"datetime": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
        price = c
        ts += timedelta(minutes=1)

    df = pd.DataFrame(rows).set_index("datetime")
    return df


def df_to_indicators(sym: str, df: pd.DataFrame, ltp: float) -> LiveIndicators:
    """Run the real IndicatorCalc on a full history DataFrame."""
    row  = df.iloc[-1]
    tick = Tick(symbol=sym, ltp=ltp, bid=ltp-0.05, ask=ltp+0.05,
                volume=int(row.volume), change=ltp-row.open,
                change_pct=(ltp-row.open)/row.open*100,
                high=float(df["high"].max()), low=float(df["low"].min()),
                open=float(df["open"].iloc[0]), timestamp=datetime.now())
    return IndicatorCalc.compute(sym, tick, df)


def make_snapshot(sym: str, ind: LiveIndicators, df: pd.DataFrame, ltp: float) -> MarketSnapshot:
    """Build a MarketSnapshot from DataFrame rows + indicators."""
    candles = []
    for row in df.tail(60).itertuples():
        candles.append(Candle(
            open=row.open, high=row.high, low=row.low,
            close=row.close, volume=row.volume,
            ts=row.Index.to_pydatetime() if hasattr(row.Index, 'to_pydatetime') else row.Index,
        ))
    row  = df.iloc[-1]
    tick = Tick(symbol=sym, ltp=ltp, bid=ltp-0.05, ask=ltp+0.05,
                volume=int(row.volume), change=ltp-row.open,
                change_pct=(ltp-row.open)/row.open*100,
                high=float(df["high"].max()), low=float(df["low"].min()),
                open=float(df["open"].iloc[0]), timestamp=datetime.now())
    return MarketSnapshot(
        symbol=sym, tick=tick, indicators=ind,
        candles_1min=candles, candles_5min=candles[::5],
    )


def run_agents(snap: MarketSnapshot, ind: LiveIndicators):
    """Evaluate all 5 agents on a snapshot; return list of (agent_name, action, signal)."""
    agents = [
        ("Intraday",  IntradayAgent()),
        ("Scalping",  ScalpingAgent()),
        ("Options",   OptionsAgent()),
        ("Futures",   FuturesAgent()),
        ("Swing",     SwingAgent()),
    ]
    results = []
    for name, agent in agents:
        try:
            action, signal = agent.evaluate_tick(snap)
            if action not in ("HOLD", None, ""):
                results.append((name, action, signal))
        except Exception as e:
            results.append((name, "ERROR", {"error": str(e)[:80]}))
    return results


def print_header():
    print(f"\n{BOLD}{'═'*70}{RESET}")
    print(f"{BOLD}  AlgoTrader Pro — Simulation Trade  |  Today's NSE Data{RESET}")
    print(f"  Date: {datetime.now().strftime('%A %d %b %Y')}  |  Mode: PAPER")
    print(f"{BOLD}{'═'*70}{RESET}\n")


def print_indicators(sym: str, ind: LiveIndicators, ltp: float, meta: dict):
    print(f"\n  {CYAN}{BOLD}▶ {sym}{RESET}  ltp={BOLD}₹{ltp:,.1f}{RESET}  "
          f"trend={ind.trend}  vol_ratio={ind.volume_ratio:.2f}x  "
          f"atr={ind.atr_14:.2f}")
    print(f"    EMA  9={ind.ema9:.1f}  21={ind.ema21:.1f}  "
          f"50={ind.ema50:.1f}  200={ind.ema200:.1f}")
    print(f"    RSI={ind.rsi_14:.1f}  MACD_hist={ind.macd_hist:.3f}  "
          f"VWAP={ind.vwap:.1f}  Williams%R={ind.williams_r:.1f}")
    print(f"    StochRSI K={ind.stoch_rsi_k:.1f}/D={ind.stoch_rsi_d:.1f}  "
          f"Supertrend={ind.supertrend_dir}  HMA={ind.hma_dir}  "
          f"Squeeze={'ON' if ind.squeeze_on else 'off'}")
    if ind.ichimoku_cloud_dir != "NEUTRAL":
        print(f"    Ichimoku cloud={BOLD}{ind.ichimoku_cloud_dir}{RESET}  "
              f"senkou_a={ind.ichimoku_senkou_a:.1f}  b={ind.ichimoku_senkou_b:.1f}")


def print_signal(agent: str, action: str, signal: dict):
    colour = GREEN if action in ("BUY", "CE") else RED if action in ("SELL", "PE") else YELLOW
    pattern = signal.get("pattern", signal.get("trigger", "?")[:50])
    score   = signal.get("score", "–")
    sl_pct  = signal.get("stop_loss_pct", "–")
    tgt_pct = signal.get("target_pct", "–")
    sf      = signal.get("_gate_size_factor", 1.0)
    sym     = signal.get("symbol", signal.get("option_symbol", signal.get("futures_symbol", "?")))

    print(f"\n    {colour}{BOLD}▲ {agent} → {action}{RESET}  "
          f"{DIM}pattern={pattern}  score={score}  sf={sf}{RESET}")
    print(f"      SL%={sl_pct}  TGT%={tgt_pct}  size_factor={sf}")
    if signal.get("option_type"):
        print(f"      {signal['option_type']} strike={signal.get('strike')}  "
              f"IVr={signal.get('iv_rank')}%  lot={signal.get('lot_size')}")


def main():
    print_header()

    # Patch time check — force market hours so agents don't gate-out
    import ist_clock, datetime as _dt_module
    _real_now = ist_clock.now_ist

    class _FakeIST:
        def __call__(self):
            n = _real_now()
            return n.replace(hour=10, minute=30, second=0, tzinfo=n.tzinfo)
        def __getattr__(self, name):
            return getattr(_real_now, name)

    ist_clock.now_ist = _FakeIST()

    all_signals = []
    symbol_list = list(SYMBOLS.items())

    print(f"  Building {len(symbol_list)} symbol histories ({250} bars each) …\n")

    for sym, meta in symbol_list:
        ltp = meta["ltp"]
        df  = build_candles(sym, meta, n_bars=250)

        # Compute all indicators
        ind = df_to_indicators(sym, df, ltp)

        # Build snapshot
        snap = make_snapshot(sym, ind, df, ltp)

        print_indicators(sym, ind, ltp, meta)

        # Run all agents
        signals = run_agents(snap, ind)
        if signals:
            for agent_name, action, signal in signals:
                if "error" in str(signal):
                    print(f"    {YELLOW}⚠ {agent_name}: {signal}{RESET}")
                else:
                    print_signal(agent_name, action, signal)
                    all_signals.append({
                        "symbol": sym, "agent": agent_name,
                        "action": action, "pattern": signal.get("pattern", ""),
                        "score": signal.get("score", "–"),
                        "sl_pct": signal.get("stop_loss_pct"),
                        "tgt_pct": signal.get("target_pct"),
                        "ltp": ltp,
                    })
        else:
            print(f"    {DIM}  → all agents: HOLD{RESET}")

    # ── Summary ──────────────────────────────────────────────────────────────
    print(f"\n{BOLD}{'═'*70}{RESET}")
    print(f"{BOLD}  SIMULATION SUMMARY{RESET}")
    print(f"{'═'*70}")
    print(f"  Symbols evaluated : {len(symbol_list)}")
    print(f"  Signals generated : {BOLD}{len(all_signals)}{RESET}")
    print()

    if all_signals:
        buys  = [s for s in all_signals if s["action"] in ("BUY","CE","LONG")]
        sells = [s for s in all_signals if s["action"] in ("SELL","PE","SHORT")]
        print(f"  {GREEN}BUY / CE / LONG : {len(buys)}{RESET}")
        for s in buys:
            print(f"    {s['symbol']:12s} {s['agent']:10s} {s['pattern']:25s}  "
                  f"score={s['score']}  SL={s['sl_pct']}%  TGT={s['tgt_pct']}%")
        print()
        print(f"  {RED}SELL / PE / SHORT: {len(sells)}{RESET}")
        for s in sells:
            print(f"    {s['symbol']:12s} {s['agent']:10s} {s['pattern']:25s}  "
                  f"score={s['score']}  SL={s['sl_pct']}%  TGT={s['tgt_pct']}%")
        print()

        # Risk summary
        avg_sl  = sum(float(s["sl_pct"]) for s in all_signals if s["sl_pct"] not in (None, "–")) / max(len(all_signals),1)
        avg_tgt = sum(float(s["tgt_pct"]) for s in all_signals if s["tgt_pct"] not in (None, "–")) / max(len(all_signals),1)
        print(f"  Avg SL  : {avg_sl:.2f}%")
        print(f"  Avg TGT : {avg_tgt:.2f}%")
        if avg_sl > 0:
            print(f"  R:R     : 1 : {avg_tgt/avg_sl:.1f}")
    else:
        print(f"  {YELLOW}No actionable signals — all agents held (indicators may need more history or "
              f"trend alignment missing){RESET}")

    print(f"\n{DIM}  Note: Swing/Options agents require live options intelligence data;"
          f" signals shown use PAPER-mode defaults.{RESET}")
    print(f"{BOLD}{'═'*70}{RESET}\n")


if __name__ == "__main__":
    main()
