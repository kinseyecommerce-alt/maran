"""
exit_policy.py — ONE exit state machine for every agent, used by the live
paper path (trailing_sl_engine / segment_engine) AND the unified backtester
(jag 2026-10-10: "smarter exits for all agents").

Rules (all bounded learning params, see agent_policy.POLICY_SPEC):
  • breakeven: once the trade is +be_r × R in favour, the stop moves to entry
    (+ a cost buffer) — never loosened afterwards.
  • partial profit: at +partial_r × R, close partial_frac of the position
    (only when the position has >1 unit/lot to split).
  • chandelier / ATR trail: after breakeven the stop trails the best price by
    trail_atr_mult × ATR (only tightens).
  • time stop: a trade that has not reached breakeven after time_stop_min
    minutes is closed (dead trades pay theta / costs / opportunity).
  • adverse order-book flip: when depth exists and the book imbalance turns
    against the position by ≥ book_flip_imb for book_flip_n consecutive
    observations, the trade is closed (not used where no depth is recorded).

R = |entry − initial stop|. The state machine is pure (no I/O, no clock):
callers pass the bar/tick timestamp. Intra-bar order is adverse-first: the
stop is checked against the adverse extreme BEFORE favourable moves can
tighten it (no look-ahead inside a bar).
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

DEFAULTS = {"be_r": 1.0, "partial_r": 1.5, "partial_frac": 0.5, "trail_atr_mult": 3.0,
            "time_stop_min": 60.0, "book_flip_imb": 0.30, "book_flip_n": 3}


@dataclass
class ExitState:
    side: int                    # +1 long, −1 short
    entry: float
    stop: float                  # current stop
    qty: float                   # remaining units (or lots)
    opened_ts: float             # epoch seconds
    risk: float = 0.0            # R in price units (|entry − initial stop|)
    target: Optional[float] = None
    best: float = 0.0
    be_done: bool = False
    partial_done: bool = False
    flip_count: int = 0
    min_unit: float = 1.0        # smallest tradable unit (1 share / 1 lot)
    cost_buffer: float = 0.0     # price units added to the breakeven stop (costs)
    stop_moves: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.risk:
            self.risk = abs(self.entry - self.stop)
        if not self.best:
            self.best = self.entry

    def d(self) -> dict:
        return asdict(self)


@dataclass
class ExitAction:
    kind: str                    # stop | target | partial | time_stop | book_flip | breakeven | trail
    px: float
    qty: float = 0.0
    why: str = ""


# Agent "brain" exits (should_exit_position) mix two kinds of rule: MANDATORY
# risk exits (own SL / target / session square-off / rollover / option theta
# & expiry) and DISCRETIONARY indicator exits (Supertrend flip, MACD+trend,
# RSI exhaustion, EMA breakdown, momentum fading …). The policy param
# signal_exits (agent_policy) = 0 keeps only the mandatory ones; the stop /
# breakeven / trail / time-stop machine above still manages every trade.
import re as _re
_MANDATORY_EXIT = _re.compile(r"\bSL\b|stop.?loss|\btarget\b|\bTGT\b|square|rollover|forced exit|theta flatten|time.?stop",
                              _re.IGNORECASE)


def is_mandatory_exit(reason: str) -> bool:
    """True for risk exits that must always be honoured (see note above)."""
    return bool(_MANDATORY_EXIT.search(reason or ""))


def _p(params: Optional[dict], k: str) -> float:
    if params and k in params and params[k] is not None:
        return float(params[k])
    return float(DEFAULTS[k])


def new_state(side: int, entry: float, stop: float, qty: float, ts: float, target: Optional[float] = None,
              min_unit: float = 1.0, cost_buffer: float = 0.0) -> ExitState:
    if side not in (1, -1):
        raise ValueError("side must be +1 or -1")
    if entry <= 0 or stop <= 0 or (stop - entry) * side >= 0:
        raise ValueError("initial stop must be on the losing side of entry")
    return ExitState(side=side, entry=float(entry), stop=float(stop), qty=float(qty), opened_ts=float(ts),
                     target=float(target) if target else None, min_unit=float(min_unit),
                     cost_buffer=max(0.0, float(cost_buffer)))


def _tighten(st: ExitState, new_stop: float, kind: str, ts: float, out: list) -> None:
    if (new_stop - st.stop) * st.side > 1e-12:          # only towards the trade
        # never past the current best (a stop through the price is a market exit)
        st.stop = new_stop
        st.stop_moves.append((ts, kind, round(new_stop, 6)))
        out.append(ExitAction(kind, new_stop, 0.0, f"stop → {new_stop:.4f}"))


def step(st: ExitState, params: Optional[dict], ts: float, high: float, low: float, close: float,
         atr: float = 0.0, imbalance: Optional[float] = None, open_: Optional[float] = None) -> list[ExitAction]:
    """Advance one bar (or one tick: high = low = close = ltp). Returns the
    actions taken; a terminal action (stop/target/time_stop/book_flip) sets
    st.qty = 0. Partial exits reduce st.qty."""
    out: list[ExitAction] = []
    if st.qty <= 0:
        return out
    s = st.side
    adverse = low if s > 0 else high
    favour = high if s > 0 else low
    # 1) adverse extreme first: the stop as it stood at the START of the bar
    if (adverse - st.stop) * s <= 0:
        px = st.stop
        if open_ is not None and (open_ - st.stop) * s < 0:   # gapped through the stop
            px = open_
        out.append(ExitAction("stop", px, st.qty, "breakeven stop" if st.be_done else "stop"))
        st.qty = 0
        return out
    # 2) target (if the agent set one)
    if st.target and (favour - st.target) * s >= 0:
        out.append(ExitAction("target", st.target, st.qty, "target"))
        st.qty = 0
        return out
    R = st.risk if st.risk > 0 else abs(st.entry) * 0.005
    # 3) partial profit at +partial_r R
    pr = _p(params, "partial_r")
    if not st.partial_done and pr > 0 and (favour - (st.entry + s * pr * R)) * s >= 0:
        frac = min(max(_p(params, "partial_frac"), 0.0), 0.9)
        q = st.qty * frac
        q = (q // st.min_unit) * st.min_unit
        st.partial_done = True
        if q >= st.min_unit and st.qty - q >= st.min_unit:
            st.qty -= q
            out.append(ExitAction("partial", st.entry + s * pr * R, q, f"+{pr:g}R partial"))
    # 4) best price, breakeven, chandelier trail
    if (favour - st.best) * s > 0:
        st.best = favour
    if not st.be_done and (st.best - (st.entry + s * _p(params, "be_r") * R)) * s >= 0:
        st.be_done = True
        _tighten(st, st.entry + s * st.cost_buffer, "breakeven", ts, out)
    if st.be_done and atr > 0:
        _tighten(st, st.best - s * _p(params, "trail_atr_mult") * atr, "trail", ts, out)
    # 5) adverse order-book flip (only where depth exists)
    if imbalance is not None:
        against = (0.5 - imbalance) if s > 0 else (imbalance - 0.5)   # imbalance = bid/(bid+ask)
        thr = _p(params, "book_flip_imb") / 2.0
        st.flip_count = st.flip_count + 1 if against >= thr else 0
        if st.flip_count >= int(_p(params, "book_flip_n")) and not st.partial_done:
            out.append(ExitAction("book_flip", close, st.qty, f"book imbalance {imbalance:.2f} against"))
            st.qty = 0
            return out
    # 6) time stop: not at breakeven after time_stop_min
    tsm = _p(params, "time_stop_min")
    if tsm > 0 and not st.be_done and ts - st.opened_ts >= tsm * 60.0:
        out.append(ExitAction("time_stop", close, st.qty, f"no progress in {tsm:g} min"))
        st.qty = 0
    return out
