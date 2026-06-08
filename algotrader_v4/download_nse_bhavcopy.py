#!/usr/bin/env python3
"""
download_nse_bhavcopy.py
─────────────────────────────────────────────────────
Downloads NSE F&O daily bhavcopy (options + futures EOD data).
Parses it and saves per-symbol IV snapshots to:
  logs/bhavcopy/YYYY-MM-DD.csv           (raw, full market)
  logs/iv_history/{SYMBOL}.csv           (settle_pr/OI per expiry per date)

Usage:
  python download_nse_bhavcopy.py                  # yesterday
  python download_nse_bhavcopy.py --date 2026-05-30
  python download_nse_bhavcopy.py --backfill 30    # last 30 trading days
"""
from __future__ import annotations

import argparse
import csv
import gzip
import http.cookiejar
import io
import os
import sys
import urllib.request
import zipfile
from datetime import date, timedelta
from typing import Optional

# ── Constants ──────────────────────────────────────────────────────────────────

_MONTH_ABBR = {
    1: "JAN", 2: "FEB", 3: "MAR", 4: "APR",
    5: "MAY", 6: "JUN", 7: "JUL", 8: "AUG",
    9: "SEP", 10: "OCT", 11: "NOV", 12: "DEC",
}

_NSE_BASE = "https://nseindia.com/content/historical/DERIVATIVES"

_NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# ── URL builder ─────────────────────────────────────────────────────────────────

def _bhavcopy_url(dt: date) -> str:
    """Return the NSE bhavcopy zip URL for the given date."""
    dd  = f"{dt.day:02d}"
    mon = _MONTH_ABBR[dt.month]
    yyyy = str(dt.year)
    filename = f"fo{dd}{mon}{yyyy}bhav.csv.zip"
    return f"{_NSE_BASE}/{yyyy}/{mon}/{filename}"

# ── NSE Session opener ───────────────────────────────────────────────────────────

def _build_opener() -> urllib.request.OpenerDirector:
    """
    Build a urllib opener with cookie support and NSE headers.
    Seeds the cookie jar by visiting the NSE homepage first.
    """
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(jar),
        urllib.request.HTTPSHandler(),
    )
    opener.addheaders = list(_NSE_HEADERS.items())
    try:
        opener.open("https://www.nseindia.com/", timeout=8)
    except Exception:
        pass  # best-effort; continue even if sandbox blocks the homepage
    return opener

# ── Download & parse ─────────────────────────────────────────────────────────────

def _download_zip(opener: urllib.request.OpenerDirector, url: str) -> Optional[bytes]:
    """Return raw zip bytes, or None on failure."""
    try:
        resp = opener.open(url, timeout=15)
        raw = resp.read()
        # Decompress if server returned gzip on top of zip
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass
        return raw
    except Exception as exc:
        print(f"  [WARN] Download failed: {exc}")
        return None


def _parse_bhavcopy(zip_bytes: bytes) -> list[dict]:
    """
    Extract and parse the CSV from the bhavcopy zip.
    Returns a list of row dicts for OPTION_TYP in ('CE', 'PE').
    """
    rows: list[dict] = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            csv_name = next(
                (n for n in zf.namelist() if n.lower().endswith(".csv")),
                None,
            )
            if csv_name is None:
                print("  [WARN] No CSV found inside zip.")
                return rows
            with zf.open(csv_name) as csvfile:
                reader = csv.DictReader(
                    io.TextIOWrapper(csvfile, encoding="utf-8", errors="replace")
                )
                for row in reader:
                    # Strip whitespace from keys and values
                    clean = {k.strip(): v.strip() for k, v in row.items() if k}
                    opt_type = clean.get("OPTION_TYP", "").upper()
                    if opt_type in ("CE", "PE"):
                        rows.append(clean)
    except zipfile.BadZipFile as exc:
        print(f"  [WARN] Bad zip file: {exc}")
    return rows

# ── File writers ─────────────────────────────────────────────────────────────────

_RAW_COLUMNS = [
    "INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP",
    "OPEN", "HIGH", "LOW", "CLOSE", "SETTLE_PR", "CONTRACTS",
    "VAL_INLAKH", "OPEN_INT", "CHG_IN_OI", "TIMESTAMP",
]

_IV_COLUMNS = ["date", "expiry", "strike", "opt_type", "settle_pr", "open_int", "chg_in_oi"]


def _save_raw(rows: list[dict], dt: date, base_dir: str) -> str:
    """Save full option rows to logs/bhavcopy/YYYY-MM-DD.csv. Returns path."""
    out_dir = os.path.join(base_dir, "logs", "bhavcopy")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"{dt.isoformat()}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_RAW_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


def _save_iv_history(rows: list[dict], dt: date, base_dir: str) -> int:
    """
    Append per-symbol summary rows to logs/iv_history/{SYMBOL}.csv.
    Returns number of symbols written.
    """
    out_dir = os.path.join(base_dir, "logs", "iv_history")
    os.makedirs(out_dir, exist_ok=True)

    # Group by symbol
    by_symbol: dict[str, list[dict]] = {}
    for row in rows:
        sym = row.get("SYMBOL", "UNKNOWN").upper()
        by_symbol.setdefault(sym, []).append(row)

    date_str = dt.isoformat()
    for sym, sym_rows in by_symbol.items():
        path = os.path.join(out_dir, f"{sym}.csv")
        file_exists = os.path.isfile(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_IV_COLUMNS)
            if not file_exists:
                writer.writeheader()
            for r in sym_rows:
                writer.writerow({
                    "date":       date_str,
                    "expiry":     r.get("EXPIRY_DT", ""),
                    "strike":     r.get("STRIKE_PR", ""),
                    "opt_type":   r.get("OPTION_TYP", ""),
                    "settle_pr":  r.get("SETTLE_PR", ""),
                    "open_int":   r.get("OPEN_INT", ""),
                    "chg_in_oi":  r.get("CHG_IN_OI", ""),
                })
    return len(by_symbol)

# ── Business-day helper ──────────────────────────────────────────────────────────

def _last_n_business_days(n: int) -> list[date]:
    """Return the last n weekdays (Mon–Fri) before today, newest last."""
    result: list[date] = []
    d = date.today() - timedelta(days=1)
    while len(result) < n:
        if d.weekday() < 5:  # Mon=0 … Fri=4
            result.append(d)
        d -= timedelta(days=1)
    result.reverse()
    return result

# ── Main downloader ──────────────────────────────────────────────────────────────

def download_bhavcopy(dt: date, base_dir: str) -> bool:
    """
    Download, parse, and save bhavcopy for a single date.
    Returns True on success, False on failure (network or data issue).
    """
    url = _bhavcopy_url(dt)
    print(f"  Fetching {dt.isoformat()} → {url}")

    opener = _build_opener()
    zip_bytes = _download_zip(opener, url)
    if zip_bytes is None:
        print(f"  [SKIP] No data downloaded for {dt.isoformat()}.")
        return False

    rows = _parse_bhavcopy(zip_bytes)
    if not rows:
        print(f"  [SKIP] No option rows parsed for {dt.isoformat()}.")
        return False

    raw_path = _save_raw(rows, dt, base_dir)
    n_symbols = _save_iv_history(rows, dt, base_dir)
    print(
        f"  [OK]   {dt.isoformat()} — {len(rows)} option rows, "
        f"{n_symbols} symbols → {raw_path}"
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download NSE F&O bhavcopy and build IV history.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="Download bhavcopy for a specific date (default: yesterday).",
    )
    group.add_argument(
        "--backfill",
        metavar="N",
        type=int,
        help="Download bhavcopy for the last N business days.",
    )
    args = parser.parse_args()

    # Resolve the target directory to the script's own directory
    base_dir = os.path.dirname(os.path.abspath(__file__))

    if args.backfill:
        dates = _last_n_business_days(args.backfill)
        print(f"Backfilling {len(dates)} trading days …")
        ok = sum(download_bhavcopy(d, base_dir) for d in dates)
        print(f"Done: {ok}/{len(dates)} days succeeded.")
    else:
        if args.date:
            try:
                target = date.fromisoformat(args.date)
            except ValueError:
                print(f"[ERROR] Invalid date format: {args.date!r} — use YYYY-MM-DD.")
                sys.exit(0)
        else:
            target = date.today() - timedelta(days=1)
            # If yesterday is a weekend, roll back to Friday
            while target.weekday() >= 5:
                target -= timedelta(days=1)

        print(f"Downloading NSE F&O bhavcopy for {target.isoformat()} …")
        download_bhavcopy(target, base_dir)


if __name__ == "__main__":
    main()
