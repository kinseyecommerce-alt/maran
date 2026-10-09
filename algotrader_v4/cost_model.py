"""
cost_model.py — realistic round-trip trading costs for every segment
(Zerodha-style retail schedule, approximate, 2025-26 rates).

    costs(kind, qty_units, entry, exit, side="BUY") -> dict breakdown

kind:
  EQ_INTRADAY  NSE/BSE cash, MIS            STT 0.025% sell, stamp 0.003% buy
  EQ_DELIVERY  NSE/BSE cash, CNC            STT 0.1% both legs, stamp 0.015% buy, ₹0 brokerage
  FUT          NSE index/stock futures      STT 0.02% sell, stamp 0.002% buy
  OPT          NSE options (premium)        STT 0.1% sell premium, stamp 0.003% buy, flat ₹20
  MCX_FUT      commodity futures            CTT 0.01% sell (non-agri), stamp 0.002% buy
  CDS_FUT      currency futures             no STT, stamp 0.0001% buy

`qty_units` is the quantity in price units (shares, or lots × multiplier), so
turnover = qty_units × price. `side` is the ENTRY side (BUY entry → the exit
leg is the sell; SELL entry → the entry leg is the sell).

Components: brokerage (min(₹20, 0.03%) per executed order; ₹0 delivery; flat ₹20
options), STT/CTT, exchange transaction charges, SEBI fee (₹10/crore), GST 18%
on (brokerage + exchange + SEBI), stamp duty (buy leg).
Slippage is NOT in here — the journal records it separately from the fills.
"""
from __future__ import annotations

# exchange transaction charge (fraction of turnover)
_EXCH = {
    "EQ_INTRADAY": {"NSE": 0.0000297, "BSE": 0.0000375},
    "EQ_DELIVERY": {"NSE": 0.0000297, "BSE": 0.0000375},
    "FUT": {"NSE": 0.0000173},
    "OPT": {"NSE": 0.0003503},
    "MCX_FUT": {"MCX": 0.0000210},
    "CDS_FUT": {"NSE": 0.0000035, "CDS": 0.0000035},
}
_STT_SELL = {"EQ_INTRADAY": 0.00025, "EQ_DELIVERY": 0.001, "FUT": 0.0002, "OPT": 0.001,
             "MCX_FUT": 0.0001, "CDS_FUT": 0.0}
_STT_BUY = {"EQ_DELIVERY": 0.001}
_STAMP_BUY = {"EQ_INTRADAY": 0.00003, "EQ_DELIVERY": 0.00015, "FUT": 0.00002, "OPT": 0.00003,
              "MCX_FUT": 0.00002, "CDS_FUT": 0.000001}
SEBI = 0.000001          # ₹10 per crore
GST = 0.18
KINDS = tuple(_STT_SELL)


def kind_for(segment: str, product: str = "", symbol: str = "") -> str:
    seg = (segment or "").upper()
    if seg == "MCX":
        return "MCX_FUT"
    if seg == "CDS":
        return "CDS_FUT"
    sym = (symbol or "").upper()
    if seg == "NSE_FO" or sym.endswith("FUT"):
        if sym.endswith("CE") or sym.endswith("PE"):
            return "OPT"
        return "FUT"
    if (product or "").upper() == "CNC":
        return "EQ_DELIVERY"
    return "EQ_INTRADAY"


def _brokerage(kind: str, value: float) -> float:
    if kind == "EQ_DELIVERY":
        return 0.0
    if kind == "OPT":
        return 20.0
    return min(20.0, value * 0.0003)


def costs(kind: str, qty_units: float, entry: float, exit: float, side: str = "BUY",
          exchange: str = "") -> dict:
    if kind not in KINDS:
        raise ValueError(f"unknown cost kind {kind}")
    q = abs(float(qty_units))
    entry_val, exit_val = q * float(entry), q * float(exit)
    buy_val, sell_val = (entry_val, exit_val) if side.upper() == "BUY" else (exit_val, entry_val)
    turnover = entry_val + exit_val
    exch_tbl = _EXCH[kind]
    ex_rate = exch_tbl.get((exchange or "").upper()) or next(iter(exch_tbl.values()))
    brokerage = _brokerage(kind, entry_val) + _brokerage(kind, exit_val)
    stt = sell_val * _STT_SELL[kind] + buy_val * _STT_BUY.get(kind, 0.0)
    exch = turnover * ex_rate
    sebi = turnover * SEBI
    gst = (brokerage + exch + sebi) * GST
    stamp = buy_val * _STAMP_BUY[kind]
    total = brokerage + stt + exch + sebi + gst + stamp
    return {"kind": kind, "brokerage": round(brokerage, 2), "stt": round(stt, 2),
            "exchange": round(exch, 2), "sebi": round(sebi, 4), "gst": round(gst, 2),
            "stamp": round(stamp, 2), "total": round(total, 2), "turnover": round(turnover, 2)}


def total(kind: str, qty_units: float, entry: float, exit: float, side: str = "BUY",
          exchange: str = "") -> float:
    return costs(kind, qty_units, entry, exit, side, exchange)["total"]
