"""
generate_sim_data.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Generate realistic NSE OHLCV simulation data for May 2026
using Geometric Brownian Motion with Indian market parameters.

Saves to: logs/historical_data/<SYMBOL>/<TF>.csv
Derives all time frames from a 1-minute base:
    1m · 5m · 15m · 30m · 1h · 1d

Usage:
    python3 generate_sim_data.py                          # all 20 symbols, all TFs
    python3 generate_sim_data.py --symbols RELIANCE,TCS
    python3 generate_sim_data.py --timeframes 15m,1d
    python3 generate_sim_data.py --workers 4              # parallel (default 4)
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd
import numpy as np

from loguru import logger
logger.remove()
logger.add(sys.stderr, format="<green>{time:HH:mm:ss}</green> | {message}", level="INFO")

# ── Output directory ───────────────────────────────────────────────────────────

OUTPUT_DIR   = Path("logs/historical_data")
SUMMARY_FILE = OUTPUT_DIR / "download_summary.json"
ALL_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "1d"]

# ── Market calendar ────────────────────────────────────────────────────────────
# NSE: 09:15–15:30 IST, Mon–Fri. Key Indian market holidays Jan–May 2026.
INDIA_HOLIDAYS_2026 = {
    date(2026, 1, 26),  # Republic Day
    date(2026, 3, 17),  # Holi
    date(2026, 4,  2),  # Mahavir Jayanti
    date(2026, 4,  3),  # Good Friday
    date(2026, 4, 14),  # Dr. Ambedkar Jayanti
    date(2026, 5,  1),  # Maharashtra Day / Labour Day
}

def _trading_days_range(start: date, end: date) -> list[date]:
    """Return NSE trading days (Mon–Fri, excluding Indian holidays) in [start, end]."""
    days, d = [], start
    while d <= end:
        if d.weekday() < 5 and d not in INDIA_HOLIDAYS_2026:
            days.append(d)
        d += timedelta(days=1)
    return days

def _trading_days_may2026() -> list[date]:
    """Return list of NSE trading days in May 2026."""
    return _trading_days_range(date(2026, 5, 1), date(2026, 5, 31))

# Extended daily range: Jan 2026–May 2026 gives ~100 days for EMA50 warmup
DAILY_EXTENDED_START = date(2026, 1, 5)

def _market_minutes(day: date) -> pd.DatetimeIndex:
    """Return 1-minute timestamps for a trading day (09:15–15:30 IST, 375 bars)."""
    start = pd.Timestamp(f"{day} 09:15:00", tz="Asia/Kolkata")
    end   = pd.Timestamp(f"{day} 15:29:00", tz="Asia/Kolkata")
    return pd.date_range(start, end, freq="1min")

# ── Seed prices (approximate NSE levels for May 2026) ─────────────────────────

SEED_PRICES: dict[str, float] = {
    "RELIANCE":   1280.0,
    "TCS":        3520.0,
    "HDFCBANK":   1710.0,
    "INFOSYS":    1620.0,
    "ICICIBANK":  1270.0,
    "BHARTIARTL": 1820.0,
    "SBIN":        790.0,
    "KOTAKBANK":  1890.0,
    "AXISBANK":   1150.0,
    "ETERNAL":     300.0,
    "WIPRO":       310.0,
    "HCLTECH":    1560.0,
    "MARUTI":    11800.0,
    "BAJFINANCE": 9200.0,
    "SUNPHARMA":  1720.0,
    "TITAN":      3400.0,
    "NESTLEIND":  2350.0,
    "ULTRACEMCO":11200.0,
    "TMPV":        400.0,
    "TATASTEEL":   160.0,
}

ALL_SYMBOLS = list(SEED_PRICES.keys())

# ── GBM parameters ─────────────────────────────────────────────────────────────
# Realistic NSE large-cap volatility:
#   σ_annual = 0.25  →  σ_1min = 0.25 / sqrt(252 × 375) ≈ 0.000813
#   μ_annual = 0.10  →  μ_1min = 0.10 / (252 × 375)     ≈ 0.00000106

_SIGMA_ANNUAL = 0.25
_MU_ANNUAL    = 0.10
_BARS_PER_YEAR = 252 * 375

_SIGMA_1MIN = _SIGMA_ANNUAL / math.sqrt(_BARS_PER_YEAR)
_MU_1MIN    = (_MU_ANNUAL / _BARS_PER_YEAR) - 0.5 * _SIGMA_1MIN ** 2


def _volume_for_price(price: float, rng: np.random.Generator) -> int:
    """Generate realistic intraday volume scaled to stock price tier."""
    if price > 5000:
        base = rng.integers(5_000, 50_000)
    elif price > 1000:
        base = rng.integers(20_000, 500_000)
    elif price > 200:
        base = rng.integers(100_000, 2_000_000)
    else:
        base = rng.integers(500_000, 10_000_000)
    return int(base)


def _day_vol_mult(day_idx: int, total_days: int) -> float:
    """
    Day-level volatility regime schedule across the month.
    Creates realistic vol clusters: high at start/end, low midmonth.
    Low-vol days cause ATR(14) to drop below ATR_MA(30) → iv_proxy < 40 → options signals.

    Schedule (fractions of month):
      0–25%  : high vol (1.6x) — early month volatility
      25–50% : tapering to low (1.6→0.3x)
      50–70% : low vol (0.3x) — quiet midmonth, options signals fire here
      70–85% : recovery (0.3→1.3x)
      85–100%: high vol (1.3x) — end-of-month rebalancing
    """
    t = day_idx / max(total_days - 1, 1)
    if t < 0.25:
        return 1.6
    elif t < 0.50:
        return 1.6 - (t - 0.25) / 0.25 * 1.3   # 1.6 → 0.3
    elif t < 0.70:
        return 0.3
    elif t < 0.85:
        return 0.3 + (t - 0.70) / 0.15 * 1.0   # 0.3 → 1.3
    else:
        return 1.3


def _generate_1m_bars(symbol: str, seed: float,
                      days: list[date],
                      rng: np.random.Generator) -> pd.DataFrame:
    """
    Generate 1-minute OHLCV bars for `symbol` across all `days`.
    Each 1m bar is built from 4 GBM sub-steps to give realistic OHLC spread.
    Day-level volatility regime creates ATR variation that triggers options signals.
    """
    records = []
    price = seed
    n_days = len(days)

    for day_idx, day in enumerate(days):
        timestamps = _market_minutes(day)
        day_sigma  = _SIGMA_1MIN * _day_vol_mult(day_idx, n_days)

        # Slight daily gap (overnight drift ±0.3%)
        overnight = math.exp(rng.normal(0, 0.003))
        price *= overnight

        for ts in timestamps:
            sigma = day_sigma

            # 4 sub-steps per 1m bar
            sub = [price]
            for _ in range(4):
                shock = rng.normal(_MU_1MIN / 4, sigma / 2)
                sub.append(sub[-1] * math.exp(shock))

            open_  = sub[0]
            high   = max(sub)
            low    = min(sub)
            close  = sub[-1]
            vol    = _volume_for_price(price, rng)

            records.append({
                "date":   ts,
                "open":   round(open_,  2),
                "high":   round(high,   2),
                "low":    round(low,    2),
                "close":  round(close,  2),
                "volume": vol,
            })
            price = close

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _resample_to_tf(df_1m: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate 1m bars to a higher time frame via resample."""
    df = df_1m.set_index("date")
    agg = df.resample(rule, label="left", closed="left").agg({
        "open":   "first",
        "high":   "max",
        "low":    "min",
        "close":  "last",
        "volume": "sum",
    }).dropna()
    agg = agg.reset_index()
    agg["date"] = pd.to_datetime(agg["date"])
    return agg[["date", "open", "high", "low", "close", "volume"]]


_TF_RULE: dict[str, str | None] = {
    "1m":  None,        # base — no resample needed
    "5m":  "5min",
    "15m": "15min",
    "30m": "30min",
    "1h":  "60min",
    # "1d" handled separately via extended daily GBM
}


def _generate_1d_bars(symbol: str, seed_price: float,
                      days: list[date],
                      rng: np.random.Generator) -> pd.DataFrame:
    """Generate one OHLC bar per trading day (GBM close-to-close, intra-day spread)."""
    records = []
    price = seed_price
    sigma_daily = _SIGMA_ANNUAL / math.sqrt(252)
    mu_daily    = (_MU_ANNUAL / 252) - 0.5 * sigma_daily ** 2

    for day in days:
        overnight = math.exp(rng.normal(0, 0.003))
        price *= overnight
        close = price * math.exp(rng.normal(mu_daily, sigma_daily))
        high  = max(price, close) * math.exp(abs(rng.normal(0, sigma_daily * 0.4)))
        low   = min(price, close) * math.exp(-abs(rng.normal(0, sigma_daily * 0.4)))
        vol   = _volume_for_price(price, rng) * 375

        ts = pd.Timestamp(f"{day} 00:00:00", tz="Asia/Kolkata")
        records.append({
            "date":   ts,
            "open":   round(price,  2),
            "high":   round(high,   2),
            "low":    round(low,    2),
            "close":  round(close,  2),
            "volume": vol,
        })
        price = close

    df = pd.DataFrame(records)
    df["date"] = pd.to_datetime(df["date"])
    return df


def _generate_symbol(symbol: str, seed_price: float,
                     days: list[date],
                     timeframes: list[str]) -> dict[str, int]:
    """Generate all requested time frames for one symbol. Returns row counts."""
    rng = np.random.default_rng(seed=abs(hash(symbol)) % (2**32))

    # 1m base (May 2026 intraday bars) — used for 1m/5m/15m/30m/1h
    df_1m = _generate_1m_bars(symbol, seed_price, days, rng)

    sym_dir = OUTPUT_DIR / symbol
    sym_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, int] = {}

    for tf in timeframes:
        if tf == "1d":
            # Extended daily history Jan–May 2026 for EMA50 warmup in swing backtest
            extended_days = _trading_days_range(DAILY_EXTENDED_START, days[-1])
            rng_d = np.random.default_rng(seed=abs(hash(symbol + "_daily")) % (2**32))
            df_tf = _generate_1d_bars(symbol, seed_price, extended_days, rng_d)
        else:
            rule = _TF_RULE.get(tf)
            if rule is None:
                df_tf = df_1m.copy()
            else:
                df_tf = _resample_to_tf(df_1m, rule)

        out = sym_dir / f"{tf}.csv"
        df_tf.to_csv(out, index=False)
        results[tf] = len(df_tf)

    return results


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate May 2026 NSE simulation data")
    parser.add_argument("--symbols",    default=",".join(ALL_SYMBOLS),
                        help="Comma-separated symbols (default: all 20)")
    parser.add_argument("--timeframes", default=",".join(ALL_TIMEFRAMES),
                        help="Comma-separated timeframes (default: all 6)")
    parser.add_argument("--workers",    type=int, default=4,
                        help="Parallel worker threads (default: 4)")
    args = parser.parse_args()

    symbols    = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    timeframes = [t.strip().lower() for t in args.timeframes.split(",") if t.strip()]
    unknown_tfs = set(timeframes) - set(ALL_TIMEFRAMES)
    if unknown_tfs:
        logger.error("Unknown timeframes: {}", unknown_tfs)
        sys.exit(1)

    days = _trading_days_may2026()
    logger.info("May 2026 trading days: {} ({}–{})", len(days), days[0], days[-1])
    logger.info("Symbols:    {} — {}", len(symbols), symbols)
    logger.info("Timeframes: {}", timeframes)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary: dict = {"generated_at": datetime.now().isoformat(), "results": {}}
    total_bars = 0
    t_start = time.time()

    def _worker(sym: str) -> tuple[str, dict]:
        seed = SEED_PRICES.get(sym, 1000.0)
        try:
            counts = _generate_symbol(sym, seed, days, timeframes)
            bars = sum(counts.values())
            logger.info("✅  {:15s}  seed=₹{:>8.2f}  bars={}", sym, seed, bars)
            return sym, {"status": "ok", "seed_price": seed, "bars_per_tf": counts}
        except Exception as exc:
            logger.error("❌  {}  error: {}", sym, exc)
            return sym, {"status": "error", "error": str(exc)}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(_worker, s): s for s in symbols}
        for fut in as_completed(futs):
            sym, res = fut.result()
            summary["results"][sym] = res
            if res["status"] == "ok":
                total_bars += sum(res["bars_per_tf"].values())

    elapsed = time.time() - t_start
    summary["total_bars"] = total_bars
    summary["elapsed_s"]  = round(elapsed, 2)
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2, default=str))

    ok_count  = sum(1 for r in summary["results"].values() if r["status"] == "ok")
    err_count = len(summary["results"]) - ok_count
    logger.info("")
    logger.info("━" * 56)
    logger.info("Done in {:.1f}s — {} symbols OK, {} errors, {:,} total bars",
                elapsed, ok_count, err_count, total_bars)
    logger.info("Output: {}/", OUTPUT_DIR)

    if err_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
