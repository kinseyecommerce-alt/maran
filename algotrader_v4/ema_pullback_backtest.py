"""
ema_pullback_backtest.py — backtest the EMA-pullback strategy on real recent
3-min data (pulled from the live server's seeded buffers, ~6 days).

Runs the exact production strategy engine (ema_pullback.EMAPullbackStrategy) over
each symbol's 3-min bars, then simulates each entry's exit on the following bars
with honest adverse-first intrabar fills (SL at focus candle, 3R target, EOD
square-off). Measures the raw signal edge on the UNDERLYING price (win%, R,
net%) — the option-premium conversion for indices is a separate sizing layer.

Usage:
  python ema_pullback_backtest.py                      # default universe
  python ema_pullback_backtest.py --symbols A,B,C
  python ema_pullback_backtest.py --host https://beaubay.info --cost 0.1
"""
from __future__ import annotations
import argparse, json, sys, urllib.request
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

from ema_pullback import EMAPullbackStrategy, EMAPullbackConfig

INDEX = ["NIFTY", "BANKNIFTY"]
MCX = ["CRUDEOIL", "COPPER", "NATURALGAS", "SILVERM", "GOLDM"]


def fetch_bars(host: str, sym: str) -> list[dict]:
    url = f"{host}/market/candles/{sym}?tf=3min"
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            return json.load(r).get("bars", [])
    except Exception:
        return []


def _to_candle(b: dict) -> dict:
    ts = datetime.fromtimestamp(b["time"])
    return {"open": b["open"], "high": b["high"], "low": b["low"],
            "close": b["close"], "volume": b.get("volume", 0), "ts": ts}


def backtest_symbol(sym: str, bars: list[dict], cfg: EMAPullbackConfig,
                    cost: float) -> list[dict]:
    """Feed the strategy bar-by-bar; simulate each entry's exit forward."""
    candles = [_to_candle(b) for b in bars]
    n = len(candles)
    if n < cfg.min_bars + 5:
        return []
    strat = EMAPullbackStrategy(cfg)
    trades: list[dict] = []
    open_trade = None                     # one position per symbol at a time

    for i in range(cfg.min_bars, n):
        c = candles[i]
        # manage an open trade on THIS bar (adverse-first), before new entries
        if open_trade is not None:
            side, ep, sl, tgt, ei = open_trade
            reason = px = None
            if side == "BUY":
                if c["low"] <= sl:   reason, px = "SL", sl
                elif c["high"] >= tgt: reason, px = "TG", tgt
            else:
                if c["high"] >= sl:  reason, px = "SL", sl
                elif c["low"] <= tgt:  reason, px = "TG", tgt
            if reason is None and c["ts"].time().hour >= 15 and c["ts"].time().minute >= 25:
                reason, px = "EOD", c["close"]
            if reason:
                g = (px - ep) / ep * 100.0
                if side == "SELL": g = -g
                trades.append({"sym": sym, "side": side, "reason": reason,
                               "net": g - cost, "R": (g) / (abs(ep - sl) / ep * 100.0)
                               if ep != sl else 0.0})
                open_trade = None

        if open_trade is not None:
            continue
        # feed closed bars 0..i-1 with bar i as the forming bar
        sig = strat.evaluate(sym, candles[:i + 1], ltp=candles[i]["open"])
        if sig is not None:
            open_trade = (sig.side, sig.entry, sig.stop_loss, sig.target, i)
    return trades


def summarize(name: str, trades: list[dict], ndays: int):
    if not trades:
        print(f"  {name:<12} 0 trades"); return
    n = len(trades); wins = sum(1 for t in trades if t["net"] > 0)
    net = sum(t["net"] for t in trades)
    avg_r = sum(t["R"] for t in trades) / n
    print(f"  {name:<12} {n:>4} tr  win {wins/n*100:>4.1f}%  net {net:>+7.1f}%  "
          f"/day {net/max(1,ndays):>+5.2f}%  /tr {net/n:>+.3f}%  avgR {avg_r:>+.2f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="https://beaubay.info")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--cost", type=float, default=0.10, help="round-trip cost %")
    a = ap.parse_args()

    cfg = EMAPullbackConfig()
    if a.symbols:
        stocks, syms = [], [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    else:
        import nifty100
        stocks = list(nifty100.NIFTY_50)
        syms = INDEX + MCX + stocks
    syms = list(dict.fromkeys(syms))
    print(f"Backtest EMA-pullback · {len(syms)} symbols · host {a.host} · cost {a.cost}%/trade\n")

    all_trades: dict[str, list] = {}
    def work(s): return s, fetch_bars(a.host, s)
    with ThreadPoolExecutor(max_workers=8) as ex:
        fetched = list(ex.map(work, syms))
    ndays = 1
    got = 0
    for s, bars in fetched:
        if not bars:
            continue
        got += 1
        ndays = max(ndays, len({datetime.fromtimestamp(b["time"]).date() for b in bars}))
        all_trades[s] = backtest_symbol(s, bars, cfg, a.cost)
    print(f"data: {got}/{len(syms)} symbols returned bars, ~{ndays} trading days\n")

    def bucket(names): return [t for s in names if s in all_trades for t in all_trades[s]]
    idx_tr = bucket(INDEX); mcx_tr = bucket(MCX)
    stk_tr = [t for s, ts in all_trades.items() if s not in INDEX and s not in MCX for t in ts]
    allt = idx_tr + mcx_tr + stk_tr

    print("BY CATEGORY:")
    summarize("Indices", idx_tr, ndays)
    summarize("Stocks", stk_tr, ndays)
    summarize("Commodities", mcx_tr, ndays)
    print("\nOVERALL:")
    summarize("ALL", allt, ndays)

    # top/bottom symbols
    per_sym = {s: sum(t["net"] for t in ts) for s, ts in all_trades.items() if ts}
    if per_sym:
        ranked = sorted(per_sym.items(), key=lambda x: x[1], reverse=True)
        print("\nTOP 8 symbols:   ", ", ".join(f"{s} {v:+.1f}%" for s, v in ranked[:8]))
        print("BOTTOM 8 symbols:", ", ".join(f"{s} {v:+.1f}%" for s, v in ranked[-8:]))


if __name__ == "__main__":
    main()
