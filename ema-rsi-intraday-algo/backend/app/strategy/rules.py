"""Pure, direction-agnostic strategy predicates — the exact BUY/SELL rules.

Each `check_*` returns `None` when the rule passes, or a `Rejection` (with a
machine-readable code) when it fails. SELL is the exact mirror of BUY and shares
this code path — there is no duplicated strategy logic.

Everything here is a pure function of its arguments: same inputs ⇒ same result.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

from app.core.enums import (
    BreakoutMode,
    EmaSelection,
    EmaToleranceMode,
    RejectionCode,
    RsiMode,
    Side,
    StopBufferMode,
)
from app.strategy.config import StrategyConfig
from app.strategy.models import D, Rejection

Emas = tuple[Decimal, Decimal, Decimal, Decimal]  # ordered [55, 89, 144, 233]
_PERIODS = (55, 89, 144, 233)


# ── 7.2 / 8.2  EMA sequence ────────────────────────────────────────────────
def check_ema_sequence(
    emas: Emas, side: Side, min_separation_pct: Decimal = Decimal(0)
) -> Rejection | None:
    if any(v is None for v in emas):
        return Rejection(RejectionCode.EMA_VALUE_MISSING, "one or more EMA values missing")
    e = list(emas)
    ok = e[0] > e[1] > e[2] > e[3] if side is Side.BUY else e[0] < e[1] < e[2] < e[3]
    if not ok:
        want = "55>89>144>233" if side is Side.BUY else "55<89<144<233"
        return Rejection(RejectionCode.EMA_SEQUENCE, f"EMA sequence not {want}")
    if min_separation_pct > 0:
        for a, b in zip(e, e[1:], strict=False):
            sep_pct = abs(a - b) / abs(a) * Decimal(100) if a != 0 else Decimal(0)
            if sep_pct < min_separation_pct:
                return Rejection(RejectionCode.EMA_SEPARATION, "EMAs too compressed")
    return None


# ── 7.3 / 8.3  price traded beyond all EMAs before the pullback ─────────────
def check_price_above_emas(
    recent: list[tuple[Decimal, Emas]], side: Side, lookback: int, strict: bool
) -> Rejection | None:
    """`recent` = [(close, emas), ...] for the completed candles in the breakout
    phase (most recent last). BUY needs price above all EMAs; SELL below all."""
    if not recent:
        return Rejection(RejectionCode.PRICE_NOT_ABOVE_EMAS, "no breakout-phase candles")
    window = recent[-lookback:] if lookback > 0 else recent

    def beyond(close: Decimal, emas: Emas) -> bool:
        return close > max(emas) if side is Side.BUY else close < min(emas)

    if strict:
        if all(beyond(c, e) for c, e in window):
            return None
        return Rejection(
            RejectionCode.PRICE_NOT_ABOVE_EMAS, "not every breakout candle beyond all EMAs"
        )
    if any(beyond(c, e) for c, e in window):
        return None
    side_word = "above" if side is Side.BUY else "below"
    return Rejection(
        RejectionCode.PRICE_NOT_ABOVE_EMAS, f"no recent candle closed {side_word} all EMAs"
    )


# ── 7.4 / 8.4  EMA touch (retracement) ─────────────────────────────────────
def ema_tolerance(
    mode: EmaToleranceMode, value: Decimal, ema_value: Decimal, atr: Decimal, tick_size: Decimal
) -> Decimal:
    if mode is EmaToleranceMode.PERCENTAGE:
        return ema_value * value / Decimal(100)
    if mode is EmaToleranceMode.POINTS:
        return value
    if mode is EmaToleranceMode.TICKS:
        return tick_size * value
    if mode is EmaToleranceMode.ATR:
        return atr * value
    raise ValueError(f"unknown tolerance mode {mode}")  # pragma: no cover


def emas_touched(
    high: Decimal,
    low: Decimal,
    close: Decimal,
    emas: Emas,
    cfg: StrategyConfig,
    atr: Decimal,
    tick_size: Decimal,
) -> tuple[list[int], int | None]:
    """Return (all touched EMA periods, primary touched period per selection)."""
    touched: list[int] = []
    distances: dict[int, Decimal] = {}
    for period, ev in zip(_PERIODS, emas, strict=False):
        if ev is None or ev <= 0:
            continue
        tol = ema_tolerance(cfg.ema_touch.mode, D(cfg.ema_touch.value), ev, atr, tick_size)
        if (low - tol) <= ev <= (high + tol):  # candle range intersects EMA±tol
            touched.append(period)
            distances[period] = abs(close - ev)
    if not touched:
        return [], None
    if cfg.ema_touch.selection is EmaSelection.NEAREST:
        primary = min(distances, key=lambda p: distances[p])
    else:  # priority order
        primary = next((p for p in cfg.ema_touch.priority if p in touched), touched[0])
    return touched, primary


# ── 7.5 / 8.5  focus candle ────────────────────────────────────────────────
def check_focus_candle(
    is_green: bool,
    is_red: bool,
    is_doji: bool,
    body: Decimal,
    rng: Decimal,
    side: Side,
    cfg: StrategyConfig,
) -> Rejection | None:
    want_red = side is Side.BUY
    if is_doji and cfg.focus_candle.reject_doji:
        return Rejection(RejectionCode.FOCUS_DOJI, "focus candle is a doji")
    wrong = (want_red and not is_red) or (not want_red and not is_green)
    if wrong:
        want = "red" if want_red else "green"
        return Rejection(RejectionCode.FOCUS_WRONG_COLOUR, f"focus candle must be {want}")
    min_body = cfg.focus_candle.minimum_body_percentage_of_range
    if min_body > 0 and rng > 0 and (body / rng) < D(min_body) / Decimal(100):
        return Rejection(RejectionCode.FOCUS_BODY_TOO_SMALL, "focus body below minimum")
    return None


# ── 7.6 / 8.6  confirmation candle ─────────────────────────────────────────
def check_confirmation_candle(
    *,
    immediate: bool,
    is_green: bool,
    is_red: bool,
    is_doji: bool,
    close: Decimal,
    focus_high: Decimal,
    focus_low: Decimal,
    body: Decimal,
    rng: Decimal,
    volume: int,
    focus_volume: int,
    side: Side,
    cfg: StrategyConfig,
) -> Rejection | None:
    if not immediate:
        return Rejection(
            RejectionCode.CONFIRMATION_NOT_IMMEDIATE, "confirmation must directly follow focus"
        )
    if is_doji:
        return Rejection(RejectionCode.CONFIRMATION_DOJI, "confirmation candle is a doji")
    if side is Side.BUY:
        if not is_green:
            return Rejection(RejectionCode.CONFIRMATION_WRONG_COLOUR, "confirmation must be green")
        if not (close > focus_high):
            return Rejection(
                RejectionCode.CONFIRMATION_LEVEL, "confirmation close must exceed focus high"
            )
    else:
        if not is_red:
            return Rejection(RejectionCode.CONFIRMATION_WRONG_COLOUR, "confirmation must be red")
        if not (close < focus_low):
            return Rejection(
                RejectionCode.CONFIRMATION_LEVEL, "confirmation close must be below focus low"
            )
    c = cfg.confirmation_candle
    if (
        c.minimum_body_percentage_of_range > 0
        and rng > 0
        and (body / rng) < D(c.minimum_body_percentage_of_range) / Decimal(100)
    ):
        return Rejection(RejectionCode.CONFIRMATION_FILTER, "confirmation body below minimum")
    if (
        c.maximum_range_percentage > 0
        and close > 0
        and (rng / close * Decimal(100)) > D(c.maximum_range_percentage)
    ):
        return Rejection(RejectionCode.CONFIRMATION_FILTER, "confirmation range above maximum")
    if c.minimum_volume > 0 and volume < c.minimum_volume:
        return Rejection(RejectionCode.CONFIRMATION_FILTER, "confirmation volume below minimum")
    if c.volume_greater_than_focus and volume <= focus_volume:
        return Rejection(
            RejectionCode.CONFIRMATION_FILTER, "confirmation volume not greater than focus"
        )
    return None


# ── 7.1 / 8.1  previous-day breakout / breakdown ───────────────────────────
def check_breakout(
    *,
    side: Side,
    mode: BreakoutMode,
    focus_open: Decimal,
    focus_high: Decimal,
    focus_low: Decimal,
    focus_close: Decimal,
    conf_open: Decimal,
    conf_high: Decimal,
    conf_low: Decimal,
    conf_close: Decimal,
    pdh: Decimal | None,
    pdl: Decimal | None,
) -> Rejection | None:
    level = pdh if side is Side.BUY else pdl
    if level is None:
        return Rejection(
            RejectionCode.MISSING_PREVIOUS_DAY_LEVELS, "previous-day level unavailable"
        )

    def above(x: Decimal) -> bool:
        return x > level

    def below(x: Decimal) -> bool:
        return x < level

    cmp = above if side is Side.BUY else below
    m = mode
    ok: bool
    if m in (BreakoutMode.CONFIRMATION_CLOSE, BreakoutMode.CONFIRMATION_CLOSE_BELOW):
        ok = cmp(conf_close)
    elif m in (BreakoutMode.CONFIRMATION_HIGH, BreakoutMode.CONFIRMATION_LOW_BELOW):
        ok = cmp(conf_high) if side is Side.BUY else cmp(conf_low)
    elif m in (BreakoutMode.CONFIRMATION_WHOLE, BreakoutMode.CONFIRMATION_WHOLE_BELOW):
        ok = cmp(conf_low) if side is Side.BUY else cmp(conf_high)
    elif m in (BreakoutMode.FOCUS_AND_CONFIRMATION, BreakoutMode.FOCUS_AND_CONFIRMATION_BELOW):
        ok = cmp(conf_close) and (cmp(focus_close) if side is Side.BUY else cmp(focus_close))
    else:  # pragma: no cover
        raise ValueError(f"unknown breakout mode {mode}")
    if not ok:
        word = "above previous-day high" if side is Side.BUY else "below previous-day low"
        return Rejection(RejectionCode.NO_BREAKOUT, f"price not {word} for mode {mode.value}")
    return None


# ── 7.7 / 8.7  RSI ─────────────────────────────────────────────────────────
def check_rsi(
    *,
    side: Side,
    cfg: StrategyConfig,
    focus_rsi: Decimal,
    confirmation_rsi: Decimal,
    recent_rsis: list[Decimal],
) -> Rejection | None:
    r = cfg.rsi
    f, c = focus_rsi, confirmation_rsi
    if side is Side.BUY:
        mode = r.buy_mode
        # sub-predicates (reused by the combined default)
        zone_ok = (
            (D(r.buy_zone_min) <= f <= D(r.buy_zone_max))
            and c > f
            and c >= D(r.buy_confirmation_min)
        )
        dipped = (
            any(x < D(40) for x in recent_rsis[-r.recovery_lookback :])
            if recent_rsis
            else f < D(40)
        )
        recovery_ok = dipped and c >= D(40)
        if mode is RsiMode.STRICT_CROSS:
            ok = f < D(40) and c >= D(40)
        elif mode is RsiMode.SUPPORT_ZONE_REJECTION:
            ok = zone_ok
        elif mode is RsiMode.BELOW_RECOVERY:
            ok = recovery_ok
        elif mode is RsiMode.SUPPORT_ZONE_OR_RECOVERY:
            ok = zone_ok or recovery_ok  # "support at 40" OR "below 40 then recover to 40"
        elif mode is RsiMode.PIVOT_REJECTION:
            ok = abs(f - D(40)) <= D(r.buy_zone_max) - D(40) and (c - f) >= D(r.pivot_min_points)
        else:
            ok = False
    else:
        mode = r.sell_mode
        zone_ok = (
            (D(r.sell_zone_min) <= f <= D(r.sell_zone_max))
            and c < f
            and c <= D(r.sell_confirmation_max)
        )
        spiked = (
            any(x > D(60) for x in recent_rsis[-r.recovery_lookback :])
            if recent_rsis
            else f > D(60)
        )
        reversal_ok = spiked and c <= D(60)
        if mode is RsiMode.STRICT_CROSS:
            ok = f > D(60) and c <= D(60)
        elif mode is RsiMode.RESISTANCE_ZONE_REJECTION:
            ok = zone_ok
        elif mode is RsiMode.ABOVE_REJECTION:
            ok = reversal_ok
        elif mode is RsiMode.RESISTANCE_ZONE_OR_REVERSAL:
            ok = zone_ok or reversal_ok  # "resistance at 60" OR "above 60 then reverse to 60"
        elif mode is RsiMode.PIVOT_REJECTION:
            ok = abs(f - D(60)) <= D(60) - D(r.sell_zone_min) and (f - c) >= D(r.pivot_min_points)
        else:
            ok = False
    if not ok:
        return Rejection(
            RejectionCode.RSI, f"RSI condition failed (mode {mode.value}, focus {f}, conf {c})"
        )
    return None


# ── 7.8 / 8.8  entry price (next candle open + slippage) ───────────────────
def entry_price(next_open: Decimal, side: Side, slippage_bps: Decimal) -> Decimal:
    adj = next_open * slippage_bps / Decimal(10_000)
    return next_open + adj if side is Side.BUY else next_open - adj


# ── 7.9 / 8.9  initial stop ────────────────────────────────────────────────
def round_to_tick(price: Decimal, tick: Decimal, *, mode: str) -> Decimal:
    if tick <= 0:
        return price
    q = price / tick
    if mode == "down":
        q = q.to_integral_value(rounding=ROUND_DOWN)
    elif mode == "up":
        q = q.to_integral_value(rounding=ROUND_UP)
    else:  # nearest
        q = q.quantize(Decimal(1))
    return q * tick


def compute_initial_stop(
    *,
    side: Side,
    focus_high: Decimal,
    focus_low: Decimal,
    cfg: StrategyConfig,
    atr: Decimal,
    tick_size: Decimal,
) -> Decimal:
    mode = cfg.stop.buffer_mode
    val = D(cfg.stop.buffer_value)
    if mode is StopBufferMode.POINTS:
        buf = val
    elif mode is StopBufferMode.PERCENTAGE:
        base = focus_low if side is Side.BUY else focus_high
        buf = base * val / Decimal(100)
    elif mode is StopBufferMode.TICKS:
        buf = tick_size * val
    elif mode is StopBufferMode.ATR:
        buf = atr * val
    else:  # pragma: no cover
        raise ValueError(f"unknown stop buffer mode {mode}")
    if side is Side.BUY:
        raw = focus_low - buf
        return round_to_tick(raw, tick_size, mode="down")  # conservative: away from entry
    raw = focus_high + buf
    return round_to_tick(raw, tick_size, mode="up")


# ── 7.10 / 8.10  risk gate ─────────────────────────────────────────────────
def check_risk(
    *,
    side: Side,
    entry: Decimal,
    stop: Decimal,
    max_stop_percentage: Decimal,
) -> tuple[Rejection | None, Decimal]:
    """Returns (rejection|None, risk_per_unit)."""
    risk = (entry - stop) if side is Side.BUY else (stop - entry)
    if risk <= 0:
        return Rejection(RejectionCode.ZERO_OR_NEGATIVE_RISK, "risk per unit ≤ 0"), risk
    max_dist = entry * D(max_stop_percentage) / Decimal(100)
    if risk > max_dist:
        return (
            Rejection(
                RejectionCode.RISK_EXCEEDS_MAX,
                f"risk {risk} exceeds max {max_dist} ({max_stop_percentage}% of {entry})",
            ),
            risk,
        )
    return None, risk
