"""Deterministic post-entry scenarios for the backtester.

Each builder takes the valid BUY/SELL setup from `synthetic.py`, runs the real
signal engine to learn the entry/stop/target levels, then appends an entry candle
plus management candles engineered to produce a specific outcome (3R, stop, BE-then-
stop, partial+trail, gap, stop&target-same-candle). The backtester re-derives the
entry from the entry candle's open, so these stay self-consistent.

Synthetic timestamps run past the real session clock (240-candle EMA warm-up in two
synthetic days), so scenario configs widen the session window — the strategy/exit
logic is what's under test, not the wall clock.
"""

from __future__ import annotations

from datetime import timedelta

from app.strategy.config import StrategyConfig
from app.strategy.models import Candle, candle_from_ohlc
from app.strategy.signal_engine import SignalEngine
from tests.fixtures.synthetic import build_buy_setup, build_sell_setup


def wide_session_config(**overrides) -> StrategyConfig:
    cfg = StrategyConfig(**overrides) if overrides else StrategyConfig()
    cfg.session.entry_start = "00:00"
    cfg.session.entry_cutoff = "23:59"
    cfg.session.forced_square_off = "23:58"
    cfg.session.final_square_off = "23:59"
    return cfg


def _append(candles: list[Candle], sym, ts, o, h, low, c) -> None:
    candles.append(candle_from_ohlc(sym, ts, o, h, low, c, volume=1000, session_date=ts.date()))


def _base(cfg: StrategyConfig, direction: str, sym: str = "RELIANCE"):
    setup = build_buy_setup(cfg, sym) if direction == "BUY" else build_sell_setup(cfg, sym)
    eng = SignalEngine(cfg)
    sig = eng.evaluate(sym, setup.candles, forming_open=setup.forming_open)
    assert sig is not None, "base setup must produce a signal"
    return setup, sig


def build_buy_scenario(
    kind: str, cfg: StrategyConfig, sym: str = "RELIANCE"
) -> dict[str, list[Candle]]:
    setup, sig = _base(cfg, "BUY", sym)
    candles = list(setup.candles)
    ts = candles[-1].timestamp + timedelta(minutes=3)
    entry = float(sig.entry)
    stop = float(sig.initial_stop)
    target = float(sig.final_target)
    be = float(sig.break_even_trigger)
    partial = float(sig.partial_profit_trigger) if sig.partial_profit_trigger else target
    entry_open = float(setup.forming_open)

    # entry candle — tight around entry, never touches stop/target
    _append(candles, sym, ts, entry_open, entry_open + 0.2, entry_open - 0.2, entry_open + 0.1)
    ts += timedelta(minutes=3)

    if kind == "target_3R":
        _append(candles, sym, ts, entry + 0.1, target + 2.0, entry - 0.1, target)  # runs to 3R
    elif kind == "initial_stop":
        _append(
            candles, sym, ts, entry - 0.1, entry + 0.2, stop - 1.0, stop - 0.5
        )  # breaks the stop
    elif kind == "be_then_stop":
        _append(candles, sym, ts, entry, be + 0.5, entry - 0.1, be + 0.2)  # reach 1.5R → BE armed
        ts += timedelta(minutes=3)
        _append(
            candles, sym, ts, be, be + 0.1, entry - 2.0, entry - 1.0
        )  # fall back to entry → BE stop
    elif kind == "partial_then_trail":
        _append(
            candles, sym, ts, entry, partial + 0.2, entry - 0.1, partial + 0.1
        )  # reach 2R → partial + trail arm
        ts += timedelta(minutes=3)
        _append(
            candles, sym, ts, partial, partial + 0.3, partial - 0.2, partial + 0.1
        )  # sets trailing ref
        ts += timedelta(minutes=3)
        _append(
            candles, sym, ts, partial, partial + 0.2, partial - 5.0, partial - 4.0
        )  # dips into trailed stop
    elif kind == "gap_through_stop":
        _append(
            candles, sym, ts, stop - 3.0, stop - 2.5, stop - 4.0, stop - 3.5
        )  # opens below stop → gap
    elif kind == "stop_and_target_same_candle":
        _append(
            candles, sym, ts, entry, target + 1.0, stop - 1.0, entry
        )  # both touched → stop wins
    else:  # pragma: no cover
        raise ValueError(kind)
    return {sym: candles}


def build_sell_scenario(
    kind: str, cfg: StrategyConfig, sym: str = "RELIANCE"
) -> dict[str, list[Candle]]:
    setup, sig = _base(cfg, "SELL", sym)
    candles = list(setup.candles)
    ts = candles[-1].timestamp + timedelta(minutes=3)
    entry = float(sig.entry)
    stop = float(sig.initial_stop)
    target = float(sig.final_target)
    entry_open = float(setup.forming_open)

    _append(candles, sym, ts, entry_open, entry_open + 0.2, entry_open - 0.2, entry_open - 0.1)
    ts += timedelta(minutes=3)

    if kind == "target_3R":
        _append(candles, sym, ts, entry - 0.1, entry + 0.1, target - 2.0, target)  # runs down to 3R
    elif kind == "initial_stop":
        _append(
            candles, sym, ts, entry + 0.1, stop + 1.0, entry - 0.2, stop + 0.5
        )  # breaks stop (up)
    else:  # pragma: no cover
        raise ValueError(kind)
    return {sym: candles}
