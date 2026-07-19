"""
cadence_shadow.py — answer "which bar cadence works best?" from REAL forward
market data, not an idealised backtest.

The live IntradayAgent only ever decides on 1-minute candles. A historical
replay can score other cadences, but it over-states the per-trade edge
uniformly (idealised fills, no latency, pattern overfit) — so it cannot tell
you whether 5-min or 10-min would actually beat 1-min live.

This recorder settles it by shadow-evaluating the SAME Intraday pattern book on
1/5/10-minute candle views of the SAME live tick stream, opening and closing
shadow positions against real forward 1-minute prices (NO real orders). Because
all three cadences run through identical machinery on identical real data, any
idealisation is common-mode and cancels: the *relative* ranking between
cadences is trustworthy even though the absolute numbers are not.

Safe by construction:
  • read-only — consumes snapshots, never places orders, never touches agents;
  • self-contained — its own candle aggregation and mini position tracker;
  • fail-closed — every public entry point is wrapped so a bug here can never
    propagate into the live trading path;
  • off by default — only runs when settings.enable_cadence_shadow is true.

Wiring (production): add one guarded subscriber task that calls
`recorder.on_snapshot(snap)` for each live 1-min snapshot. Offline, call
`score_cadence_shadow()` to read logs/cadence_shadow.jsonl into a per-cadence
edge report.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path
from typing import Optional

import pandas as pd

from loguru import logger

import nse_day_simulation as sim
from config import settings

SHADOW_LOG = Path("logs/cadence_shadow.jsonl")
_SESSION_CLOSE = time(15, 25)     # square off shadow positions at EOD like live
_NO_NEW_ENTRY = time(14, 50)      # mirror IntradayAgent's late-day entry cutoff


@dataclass
class _ShadowPos:
    sym: str
    cadence: int
    action: str          # BUY | SELL
    pattern: str
    entry_ts: datetime
    entry_px: float
    sl_px: float
    tgt_px: float


@dataclass
class _CadenceState:
    """Per (symbol, cadence): the forming higher-cadence bar + closed history."""
    bars: list = field(default_factory=list)          # closed OHLCV dicts
    cur: Optional[dict] = None                         # forming bar
    cur_slot: Optional[int] = None                     # minute-slot of forming bar
    open_pos: Optional[_ShadowPos] = None


class CadenceShadowRecorder:
    def __init__(self, agent_factory, cadences=(1, 5, 10),
                 out_path: Path = SHADOW_LOG, cost_pct: float | None = None):
        # agent_factory() → a fresh IntradayAgent (for its pattern book + scoring)
        self._agent = agent_factory()
        self._cadences = tuple(cadences)
        self._out = Path(out_path)
        self._out.parent.mkdir(parents=True, exist_ok=True)
        # equity MIS all-in round-trip cost; default to the backtest's figure
        self._cost = settings.__dict__.get("cost_pct_intraday", 0.15) \
            if cost_pct is None else cost_pct
        self._state: dict[tuple[str, int], _CadenceState] = {}
        self._cur_date = None

    # ── public, fail-closed entry point ────────────────────────────────────
    def on_snapshot(self, snap) -> None:
        """Feed one live 1-minute MarketSnapshot. Never raises."""
        try:
            self._on_snapshot(snap)
        except Exception as e:                        # pragma: no cover - safety
            logger.debug("[cadence_shadow] ignored error: {}", e)

    # ── internals ──────────────────────────────────────────────────────────
    def _on_snapshot(self, snap) -> None:
        sym = snap.symbol
        c1 = snap.candles_1min[-1] if snap.candles_1min else None
        if c1 is None:
            return
        ts = c1.ts if isinstance(c1.ts, datetime) else snap.tick.timestamp
        ltp = float(snap.tick.ltp)
        # Day-boundary reset: never fold yesterday's forming bar into today's
        # open (same minute-slot value recurs each day), and drop any position
        # that somehow survived the EOD square-off.
        d = ts.date()
        if self._cur_date is not None and d != self._cur_date:
            for stt in self._state.values():
                stt.cur = None
                stt.cur_slot = None
                stt.open_pos = None
        self._cur_date = d
        minute = ts.hour * 60 + ts.minute
        for cad in self._cadences:
            st = self._state.setdefault((sym, cad), _CadenceState())
            # 1) mark-to-market any open shadow position on this 1-min print
            self._check_exit(sym, cad, st, ts, ltp)
            # 2) fold the 1-min candle into the cadence's forming bar
            slot = minute // cad
            if st.cur_slot is None:
                st.cur_slot = slot
                st.cur = self._new_bar(c1)
            elif slot != st.cur_slot:
                # forming bar closed → commit it, then evaluate on bar close
                st.bars.append(st.cur)
                st.bars[:] = st.bars[-120:]           # cap memory
                st.cur_slot = slot
                st.cur = self._new_bar(c1)
                self._evaluate(sym, cad, st, ts)
            else:
                self._fold(st.cur, c1)

    @staticmethod
    def _new_bar(c) -> dict:
        return {"date": c.ts, "open": float(c.open), "high": float(c.high),
                "low": float(c.low), "close": float(c.close),
                "volume": float(c.volume)}

    @staticmethod
    def _fold(bar: dict, c) -> None:
        bar["high"] = max(bar["high"], float(c.high))
        bar["low"] = min(bar["low"], float(c.low))
        bar["close"] = float(c.close)
        bar["volume"] += float(c.volume)

    def _evaluate(self, sym: str, cad: int, st: _CadenceState, ts: datetime) -> None:
        if st.open_pos is not None:
            return                                    # one shadow position at a time
        if ts.time() >= _NO_NEW_ENTRY or len(st.bars) < 25:
            return
        df = pd.DataFrame(st.bars)
        ltp = float(df["close"].iloc[-1])
        ind = sim.compute_indicators_at(sym, df, len(df) - 1, ltp)
        shadow = sim.make_snapshot(sym, ind, df, len(df) - 1, ltp,
                                   bar_seconds=cad * 60)
        t = ts.time().replace(tzinfo=None)
        best_score, best_action, best_pattern = -1, "", ""
        fns = self._agent._buy_pattern_fns() + self._agent._sell_pattern_fns()
        for fn in fns:
            try:
                action, base, pname = fn(sym, shadow, ind, ltp, t)
            except Exception:
                continue
            if not action:
                continue
            if base > best_score:
                best_score, best_action, best_pattern = base, action, pname
        if best_score < settings.min_score_intraday or not best_action:
            return
        act = "BUY" if best_action in ("BUY", "CE", "LONG") else "SELL"
        sl_pct = float(settings.sl_pct_intraday) / 100.0
        tgt_pct = float(settings.tgt_pct_intraday) / 100.0
        if act == "BUY":
            sl_px, tgt_px = ltp * (1 - sl_pct), ltp * (1 + tgt_pct)
        else:
            sl_px, tgt_px = ltp * (1 + sl_pct), ltp * (1 - tgt_pct)
        st.open_pos = _ShadowPos(sym, cad, act, best_pattern, ts, ltp, sl_px, tgt_px)

    def _check_exit(self, sym: str, cad: int, st: _CadenceState,
                    ts: datetime, ltp: float) -> None:
        pos = st.open_pos
        if pos is None:
            return
        reason = None
        is_long = pos.action == "BUY"
        # adverse-first: stop before target within the same print
        if is_long and ltp <= pos.sl_px:
            reason, px = "SL_HIT", pos.sl_px
        elif (not is_long) and ltp >= pos.sl_px:
            reason, px = "SL_HIT", pos.sl_px
        elif is_long and ltp >= pos.tgt_px:
            reason, px = "TARGET", pos.tgt_px
        elif (not is_long) and ltp <= pos.tgt_px:
            reason, px = "TARGET", pos.tgt_px
        elif ts.time() >= _SESSION_CLOSE:
            reason, px = "EOD", ltp
        if reason is None:
            return
        gross = (px - pos.entry_px) / pos.entry_px * 100.0
        if not is_long:
            gross = -gross
        net = gross - self._cost
        self._append({
            "cadence": pos.cadence, "symbol": sym, "action": pos.action,
            "pattern": pos.pattern, "entry_ts": pos.entry_ts.isoformat(),
            "entry_px": round(pos.entry_px, 2), "exit_ts": ts.isoformat(),
            "exit_px": round(px, 2), "exit_reason": reason,
            "gross_pct": round(gross, 4), "net_pct": round(net, 4),
        })
        st.open_pos = None

    def _append(self, row: dict) -> None:
        with self._out.open("a") as f:
            f.write(json.dumps(row) + "\n")


def score_cadence_shadow(path: Path = SHADOW_LOG) -> dict:
    """Aggregate the shadow log into a per-cadence real-forward-data edge report.

    Returns {cadence: {trades, win_rate, mean_net, median_net, sum_net}}.
    """
    p = Path(path)
    if not p.exists():
        return {}
    rows = [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]
    out: dict = {}
    by_cad: dict = {}
    for r in rows:
        by_cad.setdefault(r["cadence"], []).append(r)
    for cad, rs in sorted(by_cad.items()):
        nets = sorted(x["net_pct"] for x in rs)
        n = len(nets)
        wins = sum(1 for x in nets if x > 0)
        median = nets[n // 2] if n % 2 else (nets[n // 2 - 1] + nets[n // 2]) / 2
        out[cad] = {
            "trades": n,
            "win_rate": round(wins / n * 100, 1) if n else 0.0,
            "mean_net": round(sum(nets) / n, 4) if n else 0.0,
            "median_net": round(median, 4) if n else 0.0,
            "sum_net": round(sum(nets), 2),
        }
    return out
