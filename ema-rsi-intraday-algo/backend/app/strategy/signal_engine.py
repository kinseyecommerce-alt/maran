"""Signal engine — feeds completed candles through the rules + state machine.

One engine instance can track many symbols. For each symbol it runs the BUY and
SELL state machines (per `long_enabled`/`short_enabled`). Indicators are computed
here once, from completed candles only, using the shared indicator engine — so the
strategy behaves identically in every mode.

`evaluate()` returns the `Signal` formed on the latest completed candle (or None).
Entry price = the next candle's open (`forming_open` for the latest bar), matching
"enter at the open of the candle immediately after confirmation".
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from app.core.enums import TERMINAL_STATES, Side, SignalState
from app.indicators.engine import compute_indicator_series, previous_day_levels
from app.strategy import rules
from app.strategy.config import StrategyConfig
from app.strategy.models import (
    Candle,
    D,
    FocusCandle,
    Rejection,
    Signal,
)
from app.strategy.state_machine import SignalStateMachine

_PERIODS = (55, 89, 144, 233)


class _DirectionContext:
    def __init__(self, symbol: str, side: Side) -> None:
        self.side = side
        self.sm = SignalStateMachine(symbol, side, correlation_id=str(uuid.uuid4()))
        self.pending_focus: FocusCandle | None = None
        self.pending_focus_index: int | None = None
        self.last_index_seen: int = -1
        self.last_rejection: Rejection | None = None


class SignalEngine:
    def __init__(
        self,
        config: StrategyConfig | None = None,
        *,
        tick_size: Decimal | None = None,
        lot_size: int | None = None,
    ) -> None:
        self.config = config or StrategyConfig()
        self.tick_size = (
            D(tick_size) if tick_size is not None else D(self.config.instrument_defaults.tick_size)
        )
        self.lot_size = (
            lot_size if lot_size is not None else self.config.instrument_defaults.lot_size
        )
        self._ctx: dict[tuple[str, Side], _DirectionContext] = {}
        self._last_ts: dict[str, datetime] = {}

    # ── public API ─────────────────────────────────────────────────────────
    def reset(self, symbol: str | None = None, at: datetime | None = None) -> None:
        at = at or datetime.utcnow()
        if symbol is None:
            for ctx in self._ctx.values():
                ctx.sm.reset(at)
            self._ctx.clear()
            self._last_ts.clear()
        else:
            for side in (Side.BUY, Side.SELL):
                ctx = self._ctx.pop((symbol, side), None)
                if ctx is not None:
                    ctx.sm.reset(at)
            self._last_ts.pop(symbol, None)

    def state(self, symbol: str, side: Side) -> SignalState:
        ctx = self._ctx.get((symbol, side))
        return ctx.sm.state if ctx else SignalState.IDLE

    def last_rejection(self, symbol: str, side: Side) -> Rejection | None:
        ctx = self._ctx.get((symbol, side))
        return ctx.last_rejection if ctx else None

    def evaluate(
        self,
        symbol: str,
        candles: list[Candle],
        *,
        forming_open: Decimal | None = None,
        prev_day_high: Decimal | None = None,
        prev_day_low: Decimal | None = None,
    ) -> Signal | None:
        cfg = self.config
        completed = [c for c in candles if c.is_complete]
        if len(completed) < cfg.min_history:
            return None

        closes = [float(c.close) for c in completed]
        highs = [float(c.high) for c in completed]
        lows = [float(c.low) for c in completed]
        snaps = compute_indicator_series(
            highs,
            lows,
            closes,
            ema_periods=_PERIODS,
            rsi_period=cfg.rsi.period,
            atr_period=cfg.atr.period,
        )
        emas_dec: list[tuple[Decimal, Decimal, Decimal, Decimal]] = [
            (D(s.ema_fast), D(s.ema_medium_fast), D(s.ema_medium_slow), D(s.ema_slow))
            for s in snaps
        ]
        rsis_dec = [D(s.rsi) for s in snaps]
        atrs_dec = [D(s.atr) for s in snaps]

        pdh_pdl = self._resolve_prev_levels(completed, prev_day_high, prev_day_low)

        result: Signal | None = None
        for side in self._active_sides():
            sig = self._run_side(
                symbol, side, completed, emas_dec, rsis_dec, atrs_dec, pdh_pdl, forming_open
            )
            if sig is not None:
                result = sig  # at most one direction fires per candle in practice
        # advance the symbol clock
        self._last_ts[symbol] = completed[-1].timestamp
        return result

    # ── internals ──────────────────────────────────────────────────────────
    def _active_sides(self) -> list[Side]:
        sides: list[Side] = []
        if self.config.long_enabled:
            sides.append(Side.BUY)
        if self.config.short_enabled:
            sides.append(Side.SELL)
        return sides

    def _ctx_for(self, symbol: str, side: Side) -> _DirectionContext:
        key = (symbol, side)
        if key not in self._ctx:
            self._ctx[key] = _DirectionContext(symbol, side)
        return self._ctx[key]

    def _resolve_prev_levels(
        self, completed: list[Candle], pdh: Decimal | None, pdl: Decimal | None
    ) -> dict[date, tuple[Decimal | None, Decimal | None]]:
        if pdh is not None and pdl is not None:
            # explicit levels apply to every session date present
            return {c.session_date: (D(pdh), D(pdl)) for c in completed}
        sess = [c.session_date for c in completed]
        hi = [float(c.high) for c in completed]
        lo = [float(c.low) for c in completed]
        raw = previous_day_levels(sess, hi, lo)
        return {d: (D(hi_v), D(lo_v)) for d, (hi_v, lo_v) in raw.items()}

    def _run_side(
        self,
        symbol: str,
        side: Side,
        completed: list[Candle],
        emas: list[tuple[Decimal, Decimal, Decimal, Decimal]],
        rsis: list[Decimal],
        atrs: list[Decimal],
        pdh_pdl: dict[date, tuple[Decimal | None, Decimal | None]],
        forming_open: Decimal | None,
    ) -> Signal | None:
        cfg = self.config
        ctx = self._ctx_for(symbol, side)
        start = ctx.last_index_seen + 1
        warmup = cfg.min_history - 1
        result: Signal | None = None

        for i in range(start, len(completed)):
            ctx.last_index_seen = i
            if i < warmup:
                continue
            c = completed[i]
            rsi_i = rsis[i]
            atr_i = atrs[i]
            pdh, pdl = pdh_pdl.get(c.session_date, (None, None))
            is_last = i == len(completed) - 1
            next_open = forming_open if is_last else completed[i + 1].open

            # 1) confirmation of a pending focus from the immediately-prior candle
            if ctx.pending_focus is not None:
                immediate = ctx.pending_focus_index == i - 1
                sig = self._try_confirm(
                    ctx,
                    symbol,
                    side,
                    c,
                    rsi_i,
                    atr_i,
                    rsis[: i + 1],
                    pdh,
                    pdl,
                    immediate=immediate,
                    next_open=next_open,
                    at=c.timestamp,
                )
                # pending is consumed either way
                ctx.pending_focus = None
                ctx.pending_focus_index = None
                if sig is not None:
                    result = sig
                    continue

            # 2) focus detection on this candle (never from a terminal state)
            if ctx.sm.state not in TERMINAL_STATES:
                self._try_arm_focus(ctx, symbol, side, completed, emas, rsis, atrs, i)

        return result

    def _try_arm_focus(
        self,
        ctx: _DirectionContext,
        symbol: str,
        side: Side,
        completed: list[Candle],
        emas: list[tuple[Decimal, Decimal, Decimal, Decimal]],
        rsis: list[Decimal],
        atrs: list[Decimal],
        i: int,
    ) -> None:
        cfg = self.config
        c = completed[i]
        e = emas[i]

        # EMA sequence (trend)
        if rules.check_ema_sequence(e, side, D(cfg.ema_minimum_separation_percentage)) is not None:
            return
        # price traded beyond all EMAs before the pullback
        window = [
            (completed[j].close, emas[j])
            for j in range(max(0, i - cfg.price_above_emas.lookback), i)
        ]
        if (
            rules.check_price_above_emas(
                window, side, cfg.price_above_emas.lookback, cfg.price_above_emas.strict
            )
            is not None
        ):
            return
        # retracement: candle touches an EMA
        touched, primary = rules.emas_touched(
            c.high, c.low, c.close, e, cfg, atrs[i], self.tick_size
        )
        if primary is None:
            return
        # focus candle colour / doji / body. Arming is a SCAN, not a rejection —
        # a candle that simply isn't a focus must not overwrite the meaningful
        # setup/entry rejection surfaced at confirmation time.
        rej = rules.check_focus_candle(c.is_green, c.is_red, c.is_doji, c.body, c.range, side, cfg)
        if rej is not None:
            return

        focus = FocusCandle(
            side=side,
            symbol=symbol,
            timestamp=c.timestamp,
            open=c.open,
            high=c.high,
            low=c.low,
            close=c.close,
            volume=c.volume,
            rsi=rsis[i],
            atr=atrs[i],
            touched_ema=primary,
            touched_emas=tuple(touched),
            emas=e,
            previous_day_high=None,
            previous_day_low=None,
        )
        ctx.pending_focus = focus
        ctx.pending_focus_index = i
        ctx.sm.transition(
            SignalState.WAITING_FOR_CONFIRMATION,
            event="focus_armed",
            reason=f"focus at EMA{primary}",
            at=c.timestamp,
        )

    def _try_confirm(
        self,
        ctx: _DirectionContext,
        symbol: str,
        side: Side,
        c: Candle,
        rsi_i: Decimal,
        atr_i: Decimal,
        recent_rsis: list[Decimal],
        pdh: Decimal | None,
        pdl: Decimal | None,
        *,
        immediate: bool,
        next_open: Decimal | None,
        at: datetime,
    ) -> Signal | None:
        cfg = self.config
        focus = ctx.pending_focus
        assert focus is not None

        def reject(rej: Rejection) -> None:
            ctx.last_rejection = rej
            ctx.sm.transition(
                SignalState.WAITING_FOR_RETRACEMENT,
                event="confirmation_failed",
                reason=f"{rej.code.value}: {rej.message}",
                at=at,
            )

        # confirmation candle geometry / colour / level / filters
        rej = rules.check_confirmation_candle(
            immediate=immediate,
            is_green=c.is_green,
            is_red=c.is_red,
            is_doji=c.is_doji,
            close=c.close,
            focus_high=focus.high,
            focus_low=focus.low,
            body=c.body,
            rng=c.range,
            volume=c.volume,
            focus_volume=focus.volume,
            side=side,
            cfg=cfg,
        )
        if rej is not None:
            reject(rej)
            return None

        # previous-day breakout / breakdown (configurable mode)
        mode = cfg.breakout.buy_mode if side is Side.BUY else cfg.breakout.sell_mode
        rej = rules.check_breakout(
            side=side,
            mode=mode,
            focus_open=focus.open,
            focus_high=focus.high,
            focus_low=focus.low,
            focus_close=focus.close,
            conf_open=c.open,
            conf_high=c.high,
            conf_low=c.low,
            conf_close=c.close,
            pdh=pdh,
            pdl=pdl,
        )
        if rej is not None:
            reject(rej)
            return None

        # RSI
        rej = rules.check_rsi(
            side=side, cfg=cfg, focus_rsi=focus.rsi, confirmation_rsi=rsi_i, recent_rsis=recent_rsis
        )
        if rej is not None:
            reject(rej)
            return None

        ctx.sm.transition(
            SignalState.CONFIRMATION_FOUND,
            event="confirmed",
            reason="all setup rules passed",
            at=at,
        )

        if next_open is None:
            # confirmation valid but we cannot price the entry yet (no next open).
            ctx.sm.transition(
                SignalState.SIGNAL_EXPIRED,
                event="no_next_open",
                reason="entry candle open unavailable",
                at=at,
            )
            return None

        # entry, stop, risk (prices quantised to the instrument tick)
        entry = rules.round_to_tick(
            rules.entry_price(next_open, side, D(cfg.entry.slippage_bps)),
            self.tick_size,
            mode="nearest",
        )
        stop = rules.compute_initial_stop(
            side=side,
            focus_high=focus.high,
            focus_low=focus.low,
            cfg=cfg,
            atr=atr_i,
            tick_size=self.tick_size,
        )
        risk_rej, risk = rules.check_risk(
            side=side,
            entry=entry,
            stop=stop,
            max_stop_percentage=D(cfg.stop.maximum_stop_percentage),
        )
        if risk_rej is not None:
            ctx.last_rejection = risk_rej
            ctx.sm.transition(
                SignalState.TRADE_REJECTED,
                event="risk_rejected",
                reason=f"{risk_rej.code.value}: {risk_rej.message}",
                at=at,
            )
            return None

        original_r = risk
        tm = cfg.trade_management
        sign = Decimal(1) if side is Side.BUY else Decimal(-1)

        def level(r_mult: Decimal) -> Decimal:
            return rules.round_to_tick(
                entry + sign * r_mult * original_r, self.tick_size, mode="nearest"
            )

        be = level(D(tm.break_even_trigger_R))
        partial = level(D(tm.partial_exit_R)) if tm.partial_exit_enabled else None
        target = level(D(tm.final_target_R))

        idem = f"{cfg.strategy_name}|{symbol}|{side.value}|{focus.timestamp.isoformat()}|ENTRY"
        signal = Signal(
            side=side,
            symbol=symbol,
            confirmation_timestamp=at,
            scheduled_entry_timestamp=at,
            entry=entry,
            initial_stop=stop,
            risk_per_unit=risk,
            risk_percentage=(risk / entry * Decimal(100)) if entry else Decimal(0),
            original_R=original_r,
            break_even_trigger=be,
            partial_profit_trigger=partial,
            final_target=target,
            focus=focus,
            confirmation_rsi=rsi_i,
            rsi_mode=(cfg.rsi.buy_mode if side is Side.BUY else cfg.rsi.sell_mode),
            ema_touched=focus.touched_ema,
            idempotency_key=idem,
        )
        ctx.sm.transition(
            SignalState.ENTRY_SCHEDULED,
            event="entry_scheduled",
            reason="signal generated",
            at=at,
            signal_id=idem,
        )
        ctx.last_rejection = None
        return signal
