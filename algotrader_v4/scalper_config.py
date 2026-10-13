"""
scalper_config.py — "trade less, better" settings for the fast scalper (PAPER).

jag 2026-10-10: scalp only the most liquid instruments, only in the busy
windows, only on multi-signal confluence with edge ≥ k × (costs + spread), with
per-symbol cool-downs and lower daily caps.

What lives here (owner config, persisted as JSON next to the learning store,
override path with SCALPER_CONFIG_PATH):
  • entry windows per segment (IST). Exits are never blocked by a window.
  • liquidity-whitelist rules (thresholds, preferred MCX contracts).
  • HARD ceilings on daily caps — the learning loop tunes the soft caps
    (SCALP params daily_cap / symbol_daily_cap / …) but can never exceed these.

The tunable knobs (k, confluence, cool-down, consecutive losses, caps,
whitelist size, window edge trims) are bounded learning params in
self_learning.SCALP / SCALP_OPT.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional

IST = timezone(timedelta(hours=5, minutes=30))

DEFAULTS: dict = {
    # entry windows (IST, [start, end)) — new scalps only inside these
    "windows": {
        "NSE_EQ": [["09:20", "11:00"], ["13:30", "15:00"]],
        "NSE_FO": [["09:20", "11:00"], ["13:30", "15:00"]],
        "NSE_FO_OPT": [["09:20", "11:00"], ["13:30", "15:00"]],
        # MCX: chosen from the Oct-9 recorded ticks (15:38–18:39 IST): spreads
        # flat (CRUDEOILM 2.3 bps, SILVERM 1.6–2.0 bps); traded volume dipped
        # 17:30–18:00 and peaked from 18:00 (US session). Morning window is
        # NOT yet measured (no recorded morning ticks) — re-check from Monday.
        "MCX": [["09:05", "11:00"], ["15:30", "17:30"], ["18:00", "23:00"]],
        "BSE_EQ": [],      # paused by owner anyway
        "CDS": [],         # paused by owner anyway
    },
    "whitelist": {
        "nse_eq_max": 10,                          # top-N Nifty 50 stocks (param whitelist_n ≤ this)
        "nse_eq_max_spread_ticks": 2.0,            # median spread
        "nse_eq_min_ticks_per_min": 20.0,
        "nse_fo_futures": ["NIFTY"],               # front-month futures (owner universe applies)
        "opt_atm_steps": 1,                        # NIFTY options: ATM ± this many strikes
        "opt_max_spread_ticks": 4.0,
        "mcx_preferred": ["CRUDEOILM-FUT", "SILVERM-FUT"],   # still must pass liquidity + lot-fits-cap
        "mcx_extra_max": 2,                        # others only if they pass liquidity
        "mcx_max_spread_bps": 3.0,                 # median spread, basis points of price …
        "mcx_max_spread_ticks": 1.0,               # … or a 1-tick market (NATURALGAS: 1 tick = 3.3 bps)
        "mcx_min_ticks_per_min": 20.0,
        "mcx_min_turnover_cr_per_hr": 50.0,
        "lookback_days": 3,
    },
    # hard ceilings (learning cannot exceed); the soft caps are learning params
    "hard_caps": {"NSE_EQ": 25, "NSE_FO": 12, "MCX": 20, "BSE_EQ": 0, "CDS": 0, "NSE_FO_OPT": 15},
    "symbol_hard_cap": 6,
    # backtest execution model
    "backtest": {"latency_ms": 150.0, "capital_from_segments": True},
}


def _path() -> Path:
    p = os.environ.get("SCALPER_CONFIG_PATH")
    if p:
        return Path(p)
    ldb = os.environ.get("LEARNING_DB") or str(Path(__file__).parent / "logs" / "learning.db")
    return Path(ldb).parent / "scalper_config.json"


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def _hm(s: str) -> dtime:
    h, m = (int(x) for x in str(s).split(":"))
    return dtime(h, m)


class ScalperConfig:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cfg: Optional[dict] = None

    def get(self) -> dict:
        with self._lock:
            if self._cfg is None:
                over = {}
                try:
                    if _path().exists():
                        over = json.loads(_path().read_text())
                except Exception:
                    over = {}
                self._cfg = _merge(DEFAULTS, over)
            return self._cfg

    def update(self, patch: dict, actor: str = "owner") -> dict:
        """Owner-only change (API key). Windows are validated HH:MM pairs;
        hard caps can only be LOWERED below the built-in ceilings."""
        cur = self.get()
        new = _merge(cur, patch or {})
        for seg, wins in (new.get("windows") or {}).items():
            for w in wins:
                a, b = _hm(w[0]), _hm(w[1])
                if not a < b:
                    raise ValueError(f"window {seg} {w}: start must be before end")
        for seg, v in (new.get("hard_caps") or {}).items():
            ceiling = DEFAULTS["hard_caps"].get(seg, 0)
            if int(v) < 0 or int(v) > ceiling:
                raise ValueError(f"hard cap {seg}={v} must be within 0..{ceiling}")
        if int(new.get("symbol_hard_cap", 0)) > DEFAULTS["symbol_hard_cap"]:
            raise ValueError(f"symbol_hard_cap ≤ {DEFAULTS['symbol_hard_cap']}")
        new["updated_at"] = datetime.now(IST).isoformat(timespec="seconds")
        new["updated_by"] = actor
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        save = {k: new[k] for k in new if k in ("windows", "whitelist", "hard_caps", "symbol_hard_cap",
                                                "backtest", "updated_at", "updated_by")}
        p.write_text(json.dumps(save, indent=2))
        with self._lock:
            self._cfg = new
        return new

    def reload(self) -> None:
        with self._lock:
            self._cfg = None

    # ── windows ────────────────────────────────────────────────────────────
    def windows(self, seg_key: str) -> list[tuple[dtime, dtime]]:
        w = self.get()["windows"]
        return [(_hm(a), _hm(b)) for a, b in (w.get(seg_key, w.get(seg_key.split("_OPT")[0], [])) or [])]

    def hard_cap(self, seg_key: str) -> int:
        return int(self.get()["hard_caps"].get(seg_key, 0))


def ist_dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(float(epoch), IST)


def window_of(windows: list[tuple[dtime, dtime]], epoch: float, skip_open_min: float = 0.0,
              skip_close_min: float = 0.0) -> Optional[str]:
    """Label 'HH:MM-HH:MM' of the entry window containing *epoch* (IST), after
    trimming skip_open_min from the start and skip_close_min from the end of
    each window; None when outside every window."""
    d = ist_dt(epoch)
    if d.weekday() >= 5:
        return None
    m = d.hour * 60 + d.minute + d.second / 60.0
    for a, b in windows:
        lo = a.hour * 60 + a.minute + float(skip_open_min)
        hi = b.hour * 60 + b.minute - float(skip_close_min)
        if lo <= m < hi:
            return f"{a.strftime('%H:%M')}-{b.strftime('%H:%M')}"
    return None


scalper_config = ScalperConfig()
