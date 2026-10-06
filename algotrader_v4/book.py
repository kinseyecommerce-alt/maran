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

build() reads each ledger ONCE (native engine under its lock → one price tick)
and derives everything from those rows:
  realised   = Σ pnl of today's listed exit orders (PAPER; LIVE Kite segments
               fall back to the agents' own realised counters)
  unrealised = Σ pnl of the listed open positions (lot multiplier applied once)
  total      = realised + unrealised
Timestamps are normalised to IST before the "today" filter (box clock is UTC).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
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


IST = timezone(timedelta(hours=5, minutes=30))


def ist_iso(ts) -> str:
    """Any order timestamp → ISO-8601 in IST. Aware values (incl. 'Z'/UTC) are
    converted; naive values are taken as IST (Kite's convention)."""
    if ts is None or ts == "":
        return ""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), IST).isoformat(timespec="seconds")
    if isinstance(ts, datetime):
        dt = ts
    else:
        raw = str(ts).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(raw.replace(" ", "T", 1))
        except ValueError:
            return str(ts)
    dt = dt.replace(tzinfo=IST) if dt.tzinfo is None else dt.astimezone(IST)
    return dt.isoformat(timespec="seconds")


def _kite_positions() -> list[dict]:
    from kite_client import kite_client
    try:
        if settings.trading_mode == "PAPER":
            with kite_client._paper_positions_lock:
                return [dict(p) for p in kite_client._paper_positions]
        return [dict(p) for p in (kite_client.positions_cached() or {}).get("net", [])]
    except Exception:
        return []


def _kite_orders() -> list[dict]:
    """PAPER: today's full journal (not the 30-min pruned hot list)."""
    from kite_client import kite_client
    try:
        if settings.trading_mode == "PAPER":
            return [dict(o) for o in kite_client.paper_orders_today()]
        return [dict(o) for o in (kite_client.orders() or [])]
    except Exception:
        return []


def _native_snapshot() -> dict:
    from segment_engine import native_engine
    try:
        return native_engine.snapshot_state()
    except Exception:
        return {"positions": {}, "price": {}, "orders": [], "closed": []}


def _position_rows(kpos: list[dict], snap: dict, include_flat: bool = False) -> list[dict]:
    SEGMENTS, _, EXCH, _ = _segments()
    from order_guard import order_guard
    from segment_engine import native_engine
    rows: list[dict] = []
    for p in kpos:
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
    price = snap["price"]
    for key, raw in snap["positions"].items():
        spec = SEGMENTS.get(raw["segment"])
        c = native_engine.contracts.get(key)
        if not spec or not c:
            continue
        ltp = price.get(key, raw["entry"])
        rows.append({
            "tradingsymbol": raw["symbol"], "exchange": spec.kite_exchange,
            "product": "CNC" if c.kind == "EQ" else "NRML",
            "quantity": raw["qty"], "average_price": raw["entry"], "last_price": round(ltp, 4),
            "pnl": round((ltp - raw["entry"]) * raw["qty"] * c.multiplier, 2),
            "segment": raw["segment"], "strategy": raw["strategy"], "price_source": "SIMULATED",
            "simulated": True, "native": True, "lots": raw["lots"], "multiplier": c.multiplier,
            "sl": round(raw["sl"], 4), "target": round(raw["target"], 4),
            "order_id": raw.get("order_id"),
        })
    return rows


def _order_rows(korders: list[dict], snap: dict, today_only: bool = True) -> list[dict]:
    SEGMENTS, _, EXCH, _ = _segments()
    from segment_engine import native_engine
    today = _today()
    rows: list[dict] = []
    for o in korders:
        ts = ist_iso(o.get("placed_at") or o.get("order_timestamp") or o.get("placed_ts"))
        if today_only and ts and not ts.startswith(today):
            continue
        sym = o.get("tradingsymbol", "")
        src = _kite_price_source(sym)
        o.pop("placed_ts", None)
        rows.append({**o, "placed_at": ts,
                     "segment": EXCH.get((o.get("exchange") or "").upper()),
                     "strategy": _strategy_from_tag(o.get("tag") or ""),
                     "price_source": src, "simulated": src == "SIMULATED" or str(o.get("order_id", "")).startswith("PAPER"),
                     "native": False, "pnl": o.get("pnl")})
    for o in reversed(snap["orders"]):                       # ledger is newest-first
        ts = ist_iso(o.get("ts"))
        if today_only and not ts.startswith(today):
            continue
        spec = SEGMENTS[o["segment"]]
        c = native_engine.contracts.get(f"{o['symbol']}@{o['segment']}")
        rows.append({
            "order_id": o["order_id"], "tradingsymbol": o["symbol"], "exchange": spec.kite_exchange,
            "transaction_type": o["side"], "quantity": o["lots"], "order_type": "MARKET",
            "product": "CNC" if (c and c.kind == "EQ") else "NRML",
            "price": o["price"], "average_price": o["price"], "status": o.get("status", "COMPLETE"),
            "placed_at": ts, "tag": o.get("reason", ""), "segment": o["segment"],
            "strategy": o.get("strategy"), "price_source": "SIMULATED", "simulated": True, "native": True,
            "lots": o["lots"], "pnl": o.get("pnl"), "entry_price": o.get("entry_price"),
        })
    rows.sort(key=lambda r: str(r.get("placed_at") or ""))
    return rows


def _is_exit(o: dict) -> bool:
    return o.get("pnl") is not None


def summary(pos_rows: Optional[list[dict]] = None, order_rows: Optional[list[dict]] = None) -> dict:
    """Header counters + Today P&L, aggregated over every segment, derived
    only from the rows passed in (one snapshot)."""
    if pos_rows is None or order_rows is None:
        b = build()
        pos_rows = b["positions"] if pos_rows is None else pos_rows
        order_rows = b["orders"] if order_rows is None else order_rows
    SEGMENTS, ORDER, _, _ = _segments()
    live_kite = settings.trading_mode != "PAPER"
    by = {}
    for code in ORDER:
        spec = SEGMENTS[code]
        seg_orders = [o for o in order_rows if o.get("segment") == code]
        seg_pos = [r for r in pos_rows if r.get("segment") == code]
        exits = [o for o in seg_orders if _is_exit(o)]
        if live_kite and not spec.native:
            realised = _agents_realised(spec.strategies)
            rsrc = "agents"
        else:
            realised = sum(float(o["pnl"]) for o in exits)
            rsrc = "orders"
        unreal = sum(float(r.get("pnl") or 0.0) for r in seg_pos)
        by[code] = {"label": spec.label,
                    "realised": round(realised, 2), "unrealised": round(unreal, 2),
                    "pnl": round(realised + unreal, 2),
                    "positions": len(seg_pos), "orders": len(seg_orders), "closed": len(exits),
                    "realised_source": rsrc,
                    "simulated": spec.native or settings.trading_mode == "PAPER"}
    tot = {k: round(sum(v[k] for v in by.values()), 2) for k in ("realised", "unrealised")}
    tot["pnl"] = round(tot["realised"] + tot["unrealised"], 2)
    tot["positions"] = len(pos_rows)
    tot["orders"] = len(order_rows)
    tot["closed"] = sum(v["closed"] for v in by.values())
    last = order_rows[-1]["order_id"] if order_rows else ""
    return {"total": tot, "by_segment": by,
            "rev": f"{len(order_rows)}:{last}:{len(pos_rows)}",
            "ts": datetime.now(IST).isoformat(timespec="seconds")}


def _agents_realised(names) -> float:
    try:
        from agents.strategy_agents import ALL_AGENTS
        return sum(float(ALL_AGENTS[n].state.pnl_today or 0.0) for n in names if n in ALL_AGENTS)
    except Exception:
        return 0.0


def strategy_book(pos_rows: Optional[list[dict]] = None, order_rows: Optional[list[dict]] = None) -> dict[str, dict]:
    """Per-strategy trades (entries today), realised (Σ its listed exit orders)
    and open P&L (Σ its open positions) — same rows as summary()."""
    if pos_rows is None or order_rows is None:
        b = build()
        pos_rows = b["positions"] if pos_rows is None else pos_rows
        order_rows = b["orders"] if order_rows is None else order_rows
    from agents.strategy_agents import ALL_AGENTS
    from segment_engine import native_engine
    live_kite = settings.trading_mode != "PAPER"
    unreal: dict[str, float] = {}
    nopen: dict[str, int] = {}
    for r in pos_rows:
        s = r.get("strategy")
        if s:
            unreal[s] = unreal.get(s, 0.0) + float(r.get("pnl") or 0.0)
            nopen[s] = nopen.get(s, 0) + 1
    realised: dict[str, float] = {}
    entries: dict[str, int] = {}
    for o in order_rows:
        s = o.get("strategy")
        if not s:
            continue
        if _is_exit(o):
            realised[s] = realised.get(s, 0.0) + float(o["pnl"])
        elif o.get("native"):
            entries[s] = entries.get(s, 0) + 1
    out = {}
    for name, a in {**ALL_AGENTS, **native_engine.strategies}.items():
        native = name in native_engine.strategies
        if native:
            trades = entries.get(name, 0)
        else:
            trades = int(getattr(a.state, "trades_today", 0) or 0)
        r = (float(getattr(a.state, "pnl_today", 0.0) or 0.0) if (live_kite and not native)
             else realised.get(name, 0.0))
        u = unreal.get(name, 0.0)
        out[name] = {"trades_today": trades,
                     "realised": round(r, 2), "unrealised": round(u, 2),
                     "total": round(r + u, 2), "open_positions": nopen.get(name, 0)}
    return out


def build(today_only: bool = True) -> dict:
    """ONE snapshot: positions, orders, summary and per-strategy P&L."""
    snap = _native_snapshot()
    pos_rows = _position_rows(_kite_positions(), snap)
    order_rows = _order_rows(_kite_orders(), snap, today_only)
    return {"positions": pos_rows, "orders": order_rows,
            "summary": summary(pos_rows, order_rows),
            "strategies": strategy_book(pos_rows, order_rows)}


def positions(include_flat: bool = False) -> list[dict]:
    """Unified open positions. Kite-shaped fields (tradingsymbol, exchange,
    product, quantity, average_price, last_price, pnl) + segment metadata."""
    return _position_rows(_kite_positions(), _native_snapshot(), include_flat)


def orders(today_only: bool = True) -> list[dict]:
    """Unified orders, oldest first (the Orders tab shows newest first)."""
    return _order_rows(_kite_orders(), _native_snapshot(), today_only)


def snapshot() -> dict:
    return build()
