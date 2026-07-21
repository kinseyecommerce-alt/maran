"""Resolve our trading symbols to Kite instrument tokens from the instrument master.

Pure and testable: `build_token_maps` takes the instrument dump (any iterable of objects
or dicts exposing tradingsymbol / exchange / instrument_token) and the wanted symbols, and
returns `(symbol_to_token, token_to_symbol)`. Index underlyings are matched via their NSE
index-segment trading symbols (see `INDEX_ALIASES`).
"""

from __future__ import annotations

from collections.abc import Iterable

from app.live.universe import INDEX_ALIASES


def _field(inst: object, name: str, alt: str) -> str:
    if isinstance(inst, dict):
        return str(inst.get(name, inst.get(alt, "")))
    return str(getattr(inst, alt, getattr(inst, name, "")))


def build_token_maps(
    instruments: Iterable[object], wanted: Iterable[str]
) -> tuple[dict[str, int], dict[int, str]]:
    # kite tradingsymbol (as it appears in the dump) → our canonical symbol
    want: dict[str, str] = {}
    for sym in wanted:
        s = sym.upper()
        want[INDEX_ALIASES.get(s, s).upper()] = s

    symbol_to_token: dict[str, int] = {}
    for inst in instruments:
        exch = _field(inst, "exchange", "exchange").upper()
        if exch not in ("NSE", "NFO"):
            continue
        tsym = _field(inst, "tradingsymbol", "symbol").upper()
        our = want.get(tsym)
        if our is None or our in symbol_to_token:
            continue
        token_raw = _field(inst, "instrument_token", "instrument_token")
        try:
            symbol_to_token[our] = int(token_raw)
        except (TypeError, ValueError):
            continue

    token_to_symbol = {tok: sym for sym, tok in symbol_to_token.items()}
    return symbol_to_token, token_to_symbol
