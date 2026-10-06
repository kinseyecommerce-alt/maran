"""
book.py — ONE read model of every paper/live position, order and P&L across
all market segments.

Sources merged (read-only; nothing here places or changes orders):
  • kite_client book   — NSE / NFO (paper ledger in PAPER, Kite in LIVE)
  • segment_engine     — BSE_EQ / MCX / CDS paper ledgers (SIMULATED feed)

Every row carries `segment`, `strategy`, `price_source` and `simulated`, so the
Positions / Orders tabs, the header counters, Today P&L (with per-segment
breakdown) and the agent cards all read the same numbers:
  /portfolio/positions, /portfolio/orders, /portfolio/book and
  engine_status()["book"] / engine_status()["strategies"][*].pnl_*.
"""
from __future__ import annotations

import re
from typing import Optional

from config import settings

_TAG_RE = re.compile(r"^(?:Agent|TSL(?:-HIT|-T\d)?)-([a-z_]+?)(?:-(?:SL|T1))?$")


def _segments():
    from segments import SEGMENTS, SEGMENT_ORDER, EXCHANGE_SEGMENT, STRATEGY_SEGMENT
    return SEGMENTS, SEGMENT_ORDER, EXCHANGE_SEGMENT, STRATEGY_SEGMENT


def _strategy_from_tag(tag: str) -> Optional[str]:
    """Agent order tags look like 'Agent-intraday', 'Agent-mean_rever-SL',
    'TSL-HIT-swing' (≤20 chars, may be truncated)."""
    if not tag:
        return None
    m = _TAG_RE.match(tag)
    if not m:
        return None
    frag = m.group(1)
    _, _, _, strat_seg = _segments()
    for name in strat_seg:
        if name == frag or name.startswith(frag):
            return name
    return None


def _today() -> str:
    from segments import segment_manager
    return segment_manager.now().date().isoformat()


def _kite_price_source(sym: str) -> Optional[str]:
    try:
        from tick_engine import tick_engine
        src = tick_engine.price_source(sym)
    except Exception:
        src = None
    if src:
        return src
    return "SIMULATED" if settings.trading_mode == "PAPER" else None


def _kite_positions() -> list[dict]:
    from kite_client import kite_client
    try:
        if settings.trading_mode == "PAPER":
            return list(kite_client.positions().get("net", []))
        return list((kite_client.positions_cached() or {}).get("net", []))
    except Exception:
        return []


def _kite_orders() -> list[dict]:
    from kite_client import kite_client
    try:
        return list(kite_client.orders() or [])
    except Exception:
        return []


def positions(include_flat: bool = False) -> list[dict]:
    """Unified open positions. Kite-shaped fields (tradingsymbol, exchange,
    product, quantity, average_price, last_price, pnl) + segment metadata."""
    SEGMENTS, _, EXCH, _ = _segments()
    from order_guard import order_guard
    from segment_engine import native_engine
    rows: list[dict] = []
    for p in _kite_positions():
        qty = int(p.get("quantity") or 0)
        if not qty and not include_flat:
            continue
        sym = p.get("tradingsymbol", "")
        src = _kite_price_source(sym)
        owner = None
        try:
            owner = order_guard.owner_of(sym)
        except Exception:
            pass
        rows.append({**p, "quantity": qty,
                     "segment": EXCH.get((p.get("exchange") or "").upper()),
                     "strategy": owner, "price_source": src, "simulated": src == "SIMULATED",
                     "native": False, "lots": None, "multiplier": 1.0})
    for code, spec in SEGMENTS.items():
        if not spec.native:
            continue
        for key, raw in list(native_engine.positions_.items()):
            if raw["segment"] != code:
                continue
            c = native_engine.contracts[key]
            ltp = native_engine.price.get(key, raw["entry"])
            rows.append({
                "tradingsymbol": raw["symbol"], "exchange": spec.kite_exchange,
                "product": "CNC" if c.kind == "EQ" else "NRML",
                "quantity": raw["qty"], "average_price": raw["entry"], "last_price": round(ltp, 4),
                "pnl": round((ltp - raw["entry"]) * raw["qty"] * c.multiplier, 2),
                "segment": code, "strategy": raw["strategy"], "price_source": "SIMULATED",
                "simulated": True, "native": True, "lots": raw["lots"], "multiplier": c.multiplier,
                "sl": round(raw["sl"], 4), "target": round(raw["target"], 4),
                "order_id": raw["order_id"],
            })
    return rows


def orders(today_only: bool = True) -> list[dict]:
    """Unified orders, oldest first (the Orders tab shows newest first)."""
    SEGMENTS, _, EXCH, _ = _segments()
    from segment_engine import native_engine
    today = _today()
    rows: list[dict] = []
    for o in _kite_orders():
        ts = o.get("placed_at") or o.get("order_timestamp") or ""
        ts = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        if today_only and ts and not ts.startswith(today):
            continue
        sym = o.get("tradingsymbol", "")
        src = _kite_price_source(sym)
        rows.append({**o, "placed_at": ts,
                     "segment": EXCH.get((o.get("exchange") or "").upper()),
                     "strategy": _strategy_from_tag(o.get("tag") or ""),
                     "price_source": src, "simulated": src == "SIMULATED" or str(o.get("order_id", "")).startswith("PAPER"),
                     "native": False})
    for o in reversed(list(native_engine.orders)):          # ledger is newest-first
        if today_only and not str(o.get("ts", "")).startswith(today):
            continue
        spec = SEGMENTS[o["segment"]]
        c = native_engine.contracts.get(f"{o['symbol']}@{o['segment']}")
        rows.append({
            "order_id": o["order_id"], "tradingsymbol": o["symbol"], "exchange": spec.kite_exchange,
            "transaction_type": o["side"], "quantity": o["lots"], "order_type": "MARKET",
            "product": "CNC" if (c and c.kind == "EQ") else "NRML",
            "price": o["price"], "average_price": o["price"], "status": o.get("status", "COMPLETE"),
            "placed_at": o["ts"], "tag": o.get("reason", ""), "segment": o["segment"],
            "strategy": o.get("strategy"), "price_source": "SIMULATED", "simulated": True, "native": True,
            "lots": o["lots"],
        })
    rows.sort(key=lambda r: str(r.get("placed_at") or ""))
    return rows


def strategy_book(pos_rows: Optional[list[dict]] = None) -> dict[str, dict]:
    """Per-strategy trades (entries today) and P&L (realised + open)."""
    from agents.strategy_agents import ALL_AGENTS
    from segment_engine import native_engine
    pos_rows = positions() if pos_rows is None else pos_rows
    unreal: dict[str, float] = {}
    nopen: dict[str, int] = {}
    for r in pos_rows:
        s = r.get("strategy")
        if s:
            unreal[s] = unreal.get(s, 0.0) + float(r.get("pnl") or 0.0)
            nopen[s] = nopen.get(s, 0) + 1
    out = {}
    for name, a in {**ALL_AGENTS, **native_engine.strategies}.items():
        realised = float(getattr(a.state, "pnl_today", 0.0) or 0.0)
        u = unreal.get(name, 0.0)
        out[name] = {"trades_today": int(getattr(a.state, "trades_today", 0) or 0),
                     "realised": round(realised, 2), "unrealised": round(u, 2),
                     "total": round(realised + u, 2), "open_positions": nopen.get(name, 0)}
    return out


def summary(pos_rows: Optional[list[dict]] = None, order_rows: Optional[list[dict]] = None) -> dict:
    """Header counters + Today P&L, aggregated over every segment."""
    SEGMENTS, ORDER, _, _ = _segments()
    from segments import segment_manager
    pos_rows = positions() if pos_rows is None else pos_rows
    order_rows = orders() if order_rows is None else order_rows
    by = {}
    for code in ORDER:
        p = segment_manager.pnl(code)
        by[code] = {"label": SEGMENTS[code].label,
                    "realised": p["realised"], "unrealised": p["unrealised"], "pnl": p["total"],
                    "positions": sum(1 for r in pos_rows if r.get("segment") == code),
                    "orders": sum(1 for r in order_rows if r.get("segment") == code),
                    "simulated": SEGMENTS[code].native or settings.trading_mode == "PAPER"}
    tot = {k: round(sum(v[k] for v in by.values()), 2) for k in ("realised", "unrealised", "pnl")}
    tot["positions"] = len(pos_rows)
    tot["orders"] = len(order_rows)
    return {"total": tot, "by_segment": by}


def snapshot() -> dict:
    pos_rows = positions()
    order_rows = orders()
    return {"positions": pos_rows, "orders": order_rows,
            "summary": summary(pos_rows, order_rows), "strategies": strategy_book(pos_rows)}
