"""
download_historical.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Download 1 month of OHLCV data for all NSE Top-100 symbols
across all trading timeframes via TrueData REST.

Output:  logs/historical_data/<SYMBOL>/<TIMEFRAME>.csv
         logs/historical_data/download_summary.json

Usage:
    cd algotrader_v4
    python3 download_historical.py                     # all symbols, all TFs
    python3 download_historical.py --symbols RELIANCE,TCS
    python3 download_historical.py --timeframes 1d,1h,15m
    python3 download_historical.py --resume            # skip already-downloaded files
    python3 download_historical.py --workers 5         # parallel downloads (default 8)

Timeframes downloaded (by default):
    1m · 5m · 15m · 30m · 1h · 1d
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from loguru import logger

logger.remove()
logger.add(sys.stderr,
           format="<green>{time:HH:mm:ss}</green> | {message}",
           level="INFO")

# ── Constants ──────────────────────────────────────────────────────────────────

ALL_TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h", "1d"]

# Lookback days needed to cover 1 full calendar month (~22 trading days)
# Use 35 calendar days to guarantee 30 trading-day coverage across weekends/holidays
LOOKBACK_DAYS = 35

OUTPUT_DIR = Path("logs/historical_data")
SUMMARY_FILE = OUTPUT_DIR / "download_summary.json"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _out_path(symbol: str, tf: str) -> Path:
    return OUTPUT_DIR / symbol / f"{tf}.csv"


def _load_summary() -> dict:
    if SUMMARY_FILE.exists():
        return json.loads(SUMMARY_FILE.read_text())
    return {"started": datetime.now().isoformat(), "results": {}}


def _save_summary(summary: dict) -> None:
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2, default=str))


def _download_one(symbol: str, tf: str, resume: bool) -> dict:
    """Download one symbol × timeframe combination. Returns a result dict."""
    out = _out_path(symbol, tf)

    if resume and out.exists() and out.stat().st_size > 100:
        return {"symbol": symbol, "tf": tf, "status": "skipped", "rows": 0}

    from truedata_client import truedata_historical

    t0 = time.monotonic()
    df = truedata_historical.historical(symbol, "NSE", tf, lookback_days=LOOKBACK_DAYS)
    elapsed = round(time.monotonic() - t0, 2)

    if df.empty:
        return {"symbol": symbol, "tf": tf, "status": "empty", "rows": 0,
                "elapsed_s": elapsed}

    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)

    return {"symbol": symbol, "tf": tf, "status": "ok",
            "rows": len(df), "elapsed_s": elapsed,
            "from": str(df["date"].iloc[0]) if "date" in df.columns else "",
            "to":   str(df["date"].iloc[-1]) if "date" in df.columns else ""}


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Download 1-month historical data from TrueData")
    parser.add_argument("--symbols",    default="",  help="Comma-separated symbols (default: all NSE Top 100)")
    parser.add_argument("--timeframes", default=",".join(ALL_TIMEFRAMES), help="Comma-separated timeframes")
    parser.add_argument("--resume",     action="store_true", help="Skip already-downloaded files")
    parser.add_argument("--workers",    type=int, default=8, help="Parallel download threads (default 8)")
    args = parser.parse_args()

    # Resolve symbol list
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    else:
        from symbol_scanner import NSE_TOP_100
        symbols = NSE_TOP_100

    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]

    tasks = [(sym, tf) for sym in symbols for tf in timeframes]
    total = len(tasks)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    summary = _load_summary()
    summary["started"]    = datetime.now().isoformat()
    summary["symbols"]    = symbols
    summary["timeframes"] = timeframes
    summary["total"]      = total

    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("TrueData Historical Downloader")
    logger.info("  Symbols    : {} ({} total)", len(symbols), "all NSE 100" if not args.symbols else "custom")
    logger.info("  Timeframes : {}", ", ".join(timeframes))
    logger.info("  Lookback   : {} calendar days (~1 month)", LOOKBACK_DAYS)
    logger.info("  Total tasks: {}", total)
    logger.info("  Output dir : {}", OUTPUT_DIR)
    logger.info("  Resume     : {}", args.resume)
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    # Check TrueData connectivity
    from truedata_client import _get_td
    td = _get_td()
    if td is None:
        logger.error("TrueData not connected — check TRUEDATA_USERNAME / TRUEDATA_PASSWORD in .env")
        sys.exit(1)
    logger.info("TrueData connected. Starting downloads...")

    done = 0
    ok = skipped = empty = failed = 0
    t_start = time.monotonic()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(_download_one, sym, tf, args.resume): (sym, tf)
            for sym, tf in tasks
        }
        for fut in futures:
            sym, tf = futures[fut]
            try:
                result = fut.result()
            except Exception as exc:
                result = {"symbol": sym, "tf": tf, "status": "error", "error": str(exc)}

            done += 1
            st = result["status"]
            if st == "ok":       ok      += 1
            elif st == "skipped": skipped += 1
            elif st == "empty":  empty   += 1
            else:                 failed  += 1

            key = f"{sym}_{tf}"
            summary["results"][key] = result

            if st == "ok":
                logger.info("[{:>4}/{}] ✓  {:>12} {:>4}  {:>5} rows  ({:.1f}s)",
                            done, total, sym, tf,
                            result.get("rows", 0), result.get("elapsed_s", 0))
            elif st == "skipped":
                logger.info("[{:>4}/{}] –  {:>12} {:>4}  already exists",
                            done, total, sym, tf)
            elif st == "empty":
                logger.warning("[{:>4}/{}] ⚠  {:>12} {:>4}  no data returned",
                               done, total, sym, tf)
            else:
                logger.error("[{:>4}/{}] ✗  {:>12} {:>4}  {}",
                             done, total, sym, tf, result.get("error", "unknown error"))

            # Save running summary every 50 tasks
            if done % 50 == 0:
                _save_summary(summary)

    elapsed_total = round(time.monotonic() - t_start, 1)

    summary["finished"]       = datetime.now().isoformat()
    summary["elapsed_s"]      = elapsed_total
    summary["count_ok"]       = ok
    summary["count_skipped"]  = skipped
    summary["count_empty"]    = empty
    summary["count_failed"]   = failed
    _save_summary(summary)

    logger.info("")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("Download complete in {:.0f}s", elapsed_total)
    logger.info("  ✓ Downloaded : {}", ok)
    logger.info("  – Skipped    : {}", skipped)
    logger.info("  ⚠ Empty      : {}", empty)
    logger.info("  ✗ Failed     : {}", failed)
    logger.info("  Output       : {}", OUTPUT_DIR.resolve())
    logger.info("  Summary      : {}", SUMMARY_FILE.resolve())
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
