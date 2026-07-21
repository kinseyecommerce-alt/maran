"""Forward-return edge test on the LIVE Kite feed (paper, read-only).

Question: do the engine's raw entries have any directional edge, BEFORE exits,
costs, and risk gates? If entries are directionally random, the mean forward
return per horizon is ≈ 0 with |t-stat| < 2.

Method (no look-ahead, intraday-only):
  * Pull live 3-min candles for every subscribed symbol (drop the forming bar).
  * Replay each symbol candle-by-candle through the SAME SignalEngine.
  * Every raw Signal it emits is an "entry" at the next bar's open — record it.
    (No risk caps / daily locks / position limits — this is pure entry quality.)
  * Forward return at horizon N bars = signed move to the close N bars later,
    measured WITHIN the same session only (clamped to the session's last bar;
    no overnight gaps). BUY: (fwd-entry)/entry, SELL: (entry-fwd)/entry.

Prints mean forward return, t-stat, and hit-rate per horizon, split BUY/SELL.
"""

from __future__ import annotations

import json
import math
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from app.core.config import DEFAULT_STRATEGY_YAML, load_strategy_config
from app.core.enums import Side
from app.market_data.http_feed import bars_to_candles, fetch_bars
from app.strategy.signal_engine import SignalEngine

HOST = "https://beaubay.info"
HORIZONS = (1, 3, 5, 10, 20)  # bars → 3, 9, 15, 30, 60 minutes


def subscribed_symbols() -> list[str]:
    d = json.load(urllib.request.urlopen(f"{HOST}/health", timeout=20))
    return list(d.get("subscribed_symbols", []))


def signals_for_symbol(cfg, symbol: str) -> list[dict]:
    """Replay one symbol, return one row per raw entry signal with forward returns."""
    bars = fetch_bars(HOST, symbol)
    candles = bars_to_candles(symbol, bars)
    if len(candles) < cfg.min_history + max(HORIZONS) + 2:
        return []
    engine = SignalEngine(cfg)
    rows: list[dict] = []
    # feed candles[:i] as completed, candles[i].open as the entry (next) open
    for i in range(cfg.min_history, len(candles) - 1):
        sig = engine.evaluate(symbol, candles[:i], forming_open=candles[i].open)
        if sig is None:
            continue
        entry = float(candles[i].open)
        if entry <= 0:
            continue
        sess = candles[i].session_date
        # last index still inside this session (intraday square-off, no gaps)
        sess_end = i
        while sess_end + 1 < len(candles) and candles[sess_end + 1].session_date == sess:
            sess_end += 1
        row = {"symbol": symbol, "side": sig.side.value, "entry": entry}
        for n in HORIZONS:
            j = min(i + n, sess_end)
            if j <= i:
                row[f"r{n}"] = None  # no room left in the session
                continue
            fwd = float(candles[j].close)
            move = (fwd - entry) / entry if sig.side == Side.BUY else (entry - fwd) / entry
            row[f"r{n}"] = move * 100.0  # percent
        rows.append(row)
    return rows


def stats(vals: list[float]) -> tuple[int, float, float, float]:
    """n, mean(%), t-stat, hit-rate(%)."""
    vals = [v for v in vals if v is not None]
    n = len(vals)
    if n == 0:
        return 0, 0.0, 0.0, 0.0
    mean = sum(vals) / n
    if n > 1:
        var = sum((v - mean) ** 2 for v in vals) / (n - 1)
        se = math.sqrt(var / n)
        t = mean / se if se > 0 else 0.0
    else:
        t = 0.0
    hit = 100.0 * sum(1 for v in vals if v > 0) / n
    return n, mean, t, hit


def main() -> None:
    cfg = load_strategy_config(DEFAULT_STRATEGY_YAML)
    syms = subscribed_symbols()
    print(f"forward-return edge test — {len(syms)} live symbols, horizons {HORIZONS} bars")
    all_rows: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as pool:
        for rows in pool.map(lambda s: _safe(cfg, s), syms):
            all_rows.extend(rows)
    n_buy = sum(1 for r in all_rows if r["side"] == "BUY")
    n_sell = len(all_rows) - n_buy
    print(f"raw entry signals: {len(all_rows)}  (BUY {n_buy} / SELL {n_sell})\n")

    def block(title: str, rows: list[dict]) -> None:
        print(f"── {title}  (n={len(rows)}) ─────────────────────────────")
        print(f"{'horizon':>8} {'n':>6} {'mean%':>9} {'t-stat':>8} {'hit%':>7}")
        for n in HORIZONS:
            cnt, mean, t, hit = stats([r[f"r{n}"] for r in rows])
            print(f"{n:>6}bar {cnt:>6} {mean:>9.4f} {t:>8.2f} {hit:>7.1f}")
        print()

    block("ALL entries", all_rows)
    block("LONG only", [r for r in all_rows if r["side"] == "BUY"])
    block("SHORT only", [r for r in all_rows if r["side"] == "SELL"])
    print("Edge exists only if mean% > 0 with t-stat > ~2. Mean≈0 / |t|<2 ⇒ no directional edge.")


def _safe(cfg, symbol: str) -> list[dict]:
    try:
        return signals_for_symbol(cfg, symbol)
    except Exception as e:  # one bad symbol never sinks the run
        print(f"  ! {symbol}: {type(e).__name__}: {e}")
        return []


if __name__ == "__main__":
    main()
