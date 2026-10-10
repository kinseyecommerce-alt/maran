"""
owner_universe.py — jag's persistent OWNER trading-universe setting.

Decides, for every NEW entry (paper or live), whether the instrument is inside
the universe the owner allowed. Exits / square-offs of existing positions are
never blocked. Automation (master agent, learning loop, strategy inventor)
can read this but never change it: only POST /owner/universe does.

Persisted as JSON next to the learning store (so tests that isolate
LEARNING_DB are isolated here too). Override with OWNER_UNIVERSE_PATH.

No file  → unrestricted (legacy behaviour). jag 2026-10-10 policy:
  NSE_EQ  : the Nifty 50 constituents only (cash)
  NSE_FO  : NIFTY index futures + NIFTY index options only
  MCX     : enabled (all liquid contracts, existing caps)
  BSE_EQ, CDS : PAUSED (owner)
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from loguru import logger

ALL_SEGMENTS = ("NSE_EQ", "NSE_FO", "BSE_EQ", "MCX", "CDS")
IST = timezone(timedelta(hours=5, minutes=30))

JAG_POLICY: dict[str, Any] = {
    "restricted": True,
    "segments": {"NSE_EQ": True, "NSE_FO": True, "BSE_EQ": False, "MCX": True, "CDS": False},
    "nse_eq_universe": "NIFTY50",                 # or "ALL"
    "nse_fo_underlyings": ["NIFTY"],              # index futures + index options on these only
    "reason": "jag 2026-10-10: trade only Nifty 50 stocks + NIFTY index futures/options + MCX; "
              "pause BSE, CDS, other indices and all stock F&O",
}

# ── FOCUS mode (jag 2026-10-10: "recreate the agents, focus only one — intraday
# NIFTY options — and test"). Owner-only, persistent, reversible: while a focus is
# set, ONLY the focus agent may open new entries, ONLY in the focus instruments;
# every other agent / strategy / segment / the inventor / native MCX shows
# "PAUSED (focus)". Exits and square-offs are never blocked. Code is kept.
FOCUS_MODES: dict[str, dict] = {
    "nifty_intraday_options": {
        "agents": ("nifty_options_intraday",),
        "segments": ("NSE_FO",),
        "underlyings": ("NIFTY",),
        "instruments": "NIFTY index options only (weekly + nearest monthly), intraday, flat by 15:15 IST",
        "capital": 1_000_000,
    },
}
FOCUS_LABEL = "PAUSED (focus)"
_OPT_RE = re.compile(r"(CE|PE)$")

_EXCH_SEG = {"NSE": "NSE_EQ", "NFO": "NSE_FO", "BSE": "BSE_EQ", "BFO": "BSE_EQ", "MCX": "MCX", "CDS": "CDS"}
_UNDERLYING_RE = re.compile(r"^([A-Z&\-]+?)(?=\d|-FUT$|$)")


def _path() -> Path:
    p = os.environ.get("OWNER_UNIVERSE_PATH")
    if p:
        return Path(p)
    ldb = os.environ.get("LEARNING_DB") or str(Path(__file__).parent / "logs" / "learning.db")
    return Path(ldb).parent / "owner_universe.json"


def _nifty50() -> set[str]:
    try:
        from nifty100 import NIFTY_50
        return {s.upper() for s in NIFTY_50}
    except Exception:
        return set()


def underlying_of(symbol: str) -> str:
    """'NFO:NIFTY26OCTFUT' → NIFTY, 'NIFTY2610622500CE' → NIFTY,
    'BANKNIFTY26OCT55000PE' → BANKNIFTY, 'NIFTYNXT5026OCTFUT' → NIFTYNXT,
    'RELIANCE26OCTFUT' → RELIANCE, 'NIFTY 50'/'NIFTY' → NIFTY, 'GOLDM-FUT' → GOLDM."""
    s = (symbol or "").upper().strip()
    if ":" in s:
        s = s.split(":", 1)[1]
    s = s.replace("NIFTY 50", "NIFTY").replace(" ", "")
    m = _UNDERLYING_RE.match(s)
    return m.group(1) if m else s


def segment_of(symbol: str, exchange: str = "", segment: str = "") -> str:
    if segment:
        return segment.upper()
    ex = (exchange or "").upper()
    if not ex and ":" in (symbol or ""):
        ex = symbol.split(":", 1)[0].upper()
    if ex in _EXCH_SEG:
        return _EXCH_SEG[ex]
    if ex in ALL_SEGMENTS:
        return ex
    return ""


class OwnerUniverse:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: Optional[dict] = None
        self._mtime: float = -1.0
        self._loaded_path: Optional[Path] = None

    # ── persistence ─────────────────────────────────────────────────────────
    def policy(self) -> dict:
        p = _path()
        with self._lock:
            try:
                mt = p.stat().st_mtime
            except FileNotFoundError:
                self._cache, self._mtime, self._loaded_path = None, -1.0, p
                return {"restricted": False, "segments": {s: True for s in ALL_SEGMENTS},
                        "nse_eq_universe": "ALL", "nse_fo_underlyings": [], "reason": "no owner setting",
                        "focus": None}
            if self._cache is None or mt != self._mtime or p != self._loaded_path:
                try:
                    self._cache = json.loads(p.read_text())
                except Exception as exc:          # unreadable → fail CLOSED to jag's policy
                    logger.error("[owner-universe] unreadable {} ({}) — enforcing jag policy", p, exc)
                    self._cache = dict(JAG_POLICY)
                self._mtime, self._loaded_path = mt, p
            return dict(self._cache)

    def set_policy(self, segments: Optional[dict] = None, nse_eq_universe: Optional[str] = None,
                   nse_fo_underlyings: Optional[list] = None, reason: str = "", actor: str = "owner",
                   restricted: bool = True) -> dict:
        cur = self.policy()
        segs = {s: bool((cur.get("segments") or {}).get(s, True)) for s in ALL_SEGMENTS}
        for k, v in (segments or {}).items():
            k = str(k).upper()
            if k not in ALL_SEGMENTS:
                raise ValueError(f"unknown segment {k}")
            segs[k] = bool(v)
        eq = (nse_eq_universe or cur.get("nse_eq_universe") or "ALL").upper()
        if eq not in ("ALL", "NIFTY50"):
            raise ValueError("nse_eq_universe must be ALL or NIFTY50")
        fo = [str(u).upper() for u in (nse_fo_underlyings if nse_fo_underlyings is not None
                                       else cur.get("nse_fo_underlyings") or [])]
        hist = list(cur.get("history") or [])[-49:]
        now = datetime.now(IST).isoformat(timespec="seconds")
        new = {"restricted": bool(restricted), "segments": segs, "nse_eq_universe": eq,
               "nse_fo_underlyings": fo, "reason": reason or cur.get("reason", ""),
               "updated_at": now, "updated_by": actor, "focus": cur.get("focus"),
               "focus_reason": cur.get("focus_reason"), "focus_set_at": cur.get("focus_set_at")}
        hist.append({"ts": now, "by": actor, "reason": reason,
                     "segments": segs, "nse_eq_universe": eq, "nse_fo_underlyings": fo})
        new["history"] = hist
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(new, indent=2))
        os.replace(tmp, p)
        with self._lock:
            self._cache = None
        logger.warning("[owner-universe] set by {}: segments={} eq={} fo={} — {}", actor, segs, eq, fo, reason)
        return self.status()

    def set_focus(self, mode: Optional[str], actor: str = "owner", reason: str = "") -> dict:
        """Owner-only. mode=None/'' clears the focus (everything returns to the
        universe rules above). Automation never calls this."""
        mode = (mode or "").strip().lower() or None
        if mode and mode not in FOCUS_MODES:
            raise ValueError(f"unknown focus {mode}; known: {sorted(FOCUS_MODES)}")
        p = _path()
        cur = self.policy() if p.exists() else dict(JAG_POLICY)
        now = datetime.now(IST).isoformat(timespec="seconds")
        new = {k: v for k, v in cur.items()}
        new.update({"focus": mode, "focus_reason": reason or (f"owner focus {mode}" if mode else "focus cleared"),
                    "focus_set_at": now, "updated_at": now, "updated_by": actor})
        hist = list(cur.get("history") or [])[-49:]
        hist.append({"ts": now, "by": actor, "reason": new["focus_reason"], "focus": mode})
        new["history"] = hist
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(new, indent=2))
        os.replace(tmp, p)
        with self._lock:
            self._cache = None
        logger.warning("[owner-universe] FOCUS set by {}: {} — {}", actor, mode, new["focus_reason"])
        return self.status()

    def focus(self) -> Optional[str]:
        f = self.policy().get("focus")
        return f if f in FOCUS_MODES else None

    def focus_spec(self) -> Optional[dict]:
        f = self.focus()
        return FOCUS_MODES.get(f) if f else None

    def agent_allowed(self, name: str) -> tuple[bool, str]:
        """May agent/strategy/engine `name` open NEW entries? (focus mode only)."""
        spec = self.focus_spec()
        if not spec:
            return True, "ok"
        if (name or "") in spec["agents"]:
            return True, "focus agent"
        return False, f"{FOCUS_LABEL}: only {', '.join(spec['agents'])} trades ({self.focus()})"

    def apply_jag_policy(self, actor: str = "owner") -> dict:
        return self.set_policy(JAG_POLICY["segments"], JAG_POLICY["nse_eq_universe"],
                               JAG_POLICY["nse_fo_underlyings"], JAG_POLICY["reason"], actor)

    # ── checks ──────────────────────────────────────────────────────────────
    def segment_enabled(self, segment: str) -> bool:
        spec = self.focus_spec()
        if spec and (segment or "").upper() not in spec["segments"]:
            return False
        pol = self.policy()
        if not pol.get("restricted"):
            return True
        return bool((pol.get("segments") or {}).get((segment or "").upper(), True))

    def allows(self, symbol: str, segment: str = "", exchange: str = "") -> tuple[bool, str]:
        """(allowed, why) for a NEW entry on `symbol`. Exits never call this."""
        spec = self.focus_spec()
        if spec:
            seg = segment_of(symbol, exchange, segment)
            sym = (symbol or "").upper().split(":", 1)[-1]
            if seg not in spec["segments"] or not _OPT_RE.search(sym) or underlying_of(sym) not in spec["underlyings"]:
                return False, f"{FOCUS_LABEL}: {sym or symbol} — {spec['instruments']}"
        pol = self.policy()
        if not pol.get("restricted"):
            return True, "ok"
        seg = segment_of(symbol, exchange, segment)
        if seg and not (pol.get("segments") or {}).get(seg, True):
            return False, f"PAUSED (owner): segment {seg}"
        sym = (symbol or "").upper()
        if ":" in sym:
            sym = sym.split(":", 1)[1]
        if seg == "NSE_EQ" and (pol.get("nse_eq_universe") or "ALL") == "NIFTY50":
            if sym.replace("-EQ", "") not in _nifty50():
                return False, f"PAUSED (owner): {sym} not a Nifty 50 stock"
        if seg == "NSE_FO":
            und = underlying_of(sym)
            allowed = [u.upper() for u in (pol.get("nse_fo_underlyings") or [])]
            if allowed and und not in allowed:
                return False, f"PAUSED (owner): NSE_FO underlying {und} not in {allowed}"
        return True, "ok"

    def fo_underlying_allowed(self, underlying: str) -> bool:
        spec = self.focus_spec()
        if spec:
            return underlying_of(underlying) in spec["underlyings"]
        pol = self.policy()
        if not pol.get("restricted"):
            return True
        if not self.segment_enabled("NSE_FO"):
            return False
        allowed = [u.upper() for u in (pol.get("nse_fo_underlyings") or [])]
        return not allowed or underlying_of(underlying) in allowed

    def filter_symbols(self, symbols, segment: str = "", exchange: str = "") -> list:
        return [s for s in symbols if self.allows(s, segment, exchange)[0]]

    def agent_symbol_allowed(self, strategy: str, symbol: str) -> bool:
        """Watchlist/approval-time filter for an agent's book. NSE_EQ agents:
        cash symbol; NSE_FO agents (futures/options/option_scalping): the
        UNDERLYING (book symbols there are underlyings like 'NIFTY')."""
        try:
            from segments import STRATEGY_SEGMENT
            seg = STRATEGY_SEGMENT.get(strategy, "")
        except Exception:
            seg = ""
        if not self.policy().get("restricted"):
            return True
        if seg == "NSE_FO":
            return self.fo_underlying_allowed(symbol)
        if seg:
            return self.allows(symbol, segment=seg)[0]
        return True

    def filter_agent_items(self, strategy: str, items: list) -> list:
        out = []
        for it in items or []:
            sym = it.get("symbol") if isinstance(it, dict) else it
            if self.agent_symbol_allowed(strategy, sym):
                out.append(it)
        return out

    def segment_scope(self, segment: str) -> str:
        spec = self.focus_spec()
        if spec:
            return spec["instruments"] if segment in spec["segments"] else FOCUS_LABEL
        pol = self.policy()
        if not pol.get("restricted"):
            return "all"
        if not self.segment_enabled(segment):
            return "PAUSED (owner)"
        if segment == "NSE_EQ" and pol.get("nse_eq_universe") == "NIFTY50":
            return "Nifty 50 stocks only"
        if segment == "NSE_FO" and pol.get("nse_fo_underlyings"):
            return " + ".join(pol["nse_fo_underlyings"]) + " index futures & options only"
        return "all"

    def status(self) -> dict:
        pol = self.policy()
        fspec = self.focus_spec()
        segs = {s: ("enabled" if self.segment_enabled(s) else (FOCUS_LABEL if fspec else "PAUSED (owner)"))
                for s in ALL_SEGMENTS}
        return {"restricted": bool(pol.get("restricted")), "segments": segs,
                "nse_eq_universe": pol.get("nse_eq_universe", "ALL"),
                "nse_fo_underlyings": pol.get("nse_fo_underlyings", []),
                "nifty50_count": len(_nifty50()), "reason": pol.get("reason", ""),
                "updated_at": pol.get("updated_at"), "updated_by": pol.get("updated_by"),
                "history": (pol.get("history") or [])[-10:], "path": str(_path()),
                "focus": self.focus(), "focus_spec": fspec, "focus_reason": pol.get("focus_reason"),
                "focus_set_at": pol.get("focus_set_at"),
                "note": "Owner setting — automation never changes it. Exits of existing positions always allowed."}


owner_universe = OwnerUniverse()
