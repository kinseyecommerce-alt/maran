"""Candle loaders for real 3-minute data.

Supported inputs:
  * CSV   — flexible headers: timestamp/date/datetime, open, high, low, close,
            volume, optional symbol. Timestamps: ISO-8601, "YYYY-MM-DD HH:MM:SS",
            or epoch seconds.
  * Kite historical JSON — {"data": {"candles": [[ts, o, h, l, c, vol], …]}} or a
            bare list of such rows.
  * Generic bars JSON — [{"time"|"timestamp", "open", …}, …].

All loaders return `dict[symbol → list[Candle]]`, chronologically sorted, with
`session_date` derived from each timestamp. Indicators are computed downstream by the
strategy engine — loaders only carry OHLCV.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

from app.strategy.models import Candle, candle_from_ohlc

_TS_KEYS = ("timestamp", "datetime", "date", "time")
_ALIASES = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "c"),
    "volume": ("volume", "vol", "v"),
    "symbol": ("symbol", "sym", "ticker", "tradingsymbol"),
}


def parse_timestamp(raw: str | int | float) -> datetime:
    """Parse an ISO / space-separated / epoch timestamp into a naive datetime.

    Timezone offsets are honoured then dropped (we normalise to wall-clock IST as
    supplied by the exchange feed); downstream logic is tz-agnostic on naive values."""
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(float(raw))
    s = str(raw).strip()
    if s.isdigit():
        return datetime.fromtimestamp(int(s))
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


def _pick(row: dict, aliases: tuple[str, ...]) -> str | None:
    lower = {k.lower(): k for k in row}
    for a in aliases:
        if a in lower:
            return lower[a]
    return None


def load_candles_csv(
    text_or_path: str | Path, *, symbol: str | None = None, data_source: str = "csv"
) -> dict[str, list[Candle]]:
    text = _read(text_or_path)
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        return {}
    ts_key = next(
        (
            _pick({k: 1 for k in reader.fieldnames}, (t,))
            for t in _TS_KEYS
            if _pick({k: 1 for k in reader.fieldnames}, (t,))
        ),
        None,
    )
    cols = {name: _pick({k: 1 for k in reader.fieldnames}, al) for name, al in _ALIASES.items()}
    if ts_key is None or not all(cols[c] for c in ("open", "high", "low", "close")):
        raise ValueError("CSV missing required timestamp/OHLC columns")

    out: dict[str, list[Candle]] = {}
    for row in reader:
        ts = parse_timestamp(row[ts_key])
        sym = (row[cols["symbol"]] if cols["symbol"] else None) or symbol or "SYMBOL"
        vol = int(float(row[cols["volume"]])) if cols["volume"] and row[cols["volume"]] else 0
        candle = candle_from_ohlc(
            sym,
            ts,
            row[cols["open"]],
            row[cols["high"]],
            row[cols["low"]],
            row[cols["close"]],
            volume=vol,
            session_date=ts.date(),
            data_source=data_source,
        )
        out.setdefault(sym, []).append(candle)
    for sym in out:
        out[sym].sort(key=lambda c: c.timestamp)
    return out


def load_candles_kite_json(
    text_or_path: str | Path, *, symbol: str, data_source: str = "kite"
) -> dict[str, list[Candle]]:
    data = json.loads(_read(text_or_path))
    rows = data.get("data", {}).get("candles") if isinstance(data, dict) else data
    if rows is None:
        raise ValueError("no candles found in Kite JSON")
    candles = []
    for r in rows:
        ts = parse_timestamp(r[0])
        candles.append(
            candle_from_ohlc(
                symbol,
                ts,
                r[1],
                r[2],
                r[3],
                r[4],
                volume=int(r[5]) if len(r) > 5 else 0,
                session_date=ts.date(),
                data_source=data_source,
            )
        )
    candles.sort(key=lambda c: c.timestamp)
    return {symbol: candles}


def load_candles(path: str | Path, *, symbol: str | None = None) -> dict[str, list[Candle]]:
    """Dispatch by extension. `symbol` required for single-series JSON."""
    p = Path(path)
    if p.suffix.lower() == ".csv":
        return load_candles_csv(p, symbol=symbol)
    if p.suffix.lower() == ".json":
        if symbol is None:
            raise ValueError("--symbol is required for JSON input")
        return load_candles_kite_json(p, symbol=symbol)
    raise ValueError(f"unsupported file type: {p.suffix}")


def load_dir(directory: str | Path) -> dict[str, list[Candle]]:
    """Load every *.csv in a directory; symbol = column or filename stem."""
    out: dict[str, list[Candle]] = {}
    for f in sorted(Path(directory).glob("*.csv")):
        for sym, candles in load_candles_csv(f, symbol=f.stem.upper()).items():
            out.setdefault(sym, []).extend(candles)
    for sym in out:
        out[sym].sort(key=lambda c: c.timestamp)
    return out


def _read(text_or_path: str | Path) -> str:
    p = Path(text_or_path) if not isinstance(text_or_path, Path) else text_or_path
    try:
        if p.exists():
            return p.read_text(encoding="utf-8")
    except OSError:
        pass
    return str(text_or_path)
