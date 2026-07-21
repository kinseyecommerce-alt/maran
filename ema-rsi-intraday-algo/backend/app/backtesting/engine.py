"""Event-driven backtester.

Reuses the Phase-1 `SignalEngine` for entries and the Phase-2 managers for exits —
there is a single strategy/exit implementation shared with (future) paper/live. The
backtest is deterministic and honours: next-candle-open entry, the entry window and
forced square-off, conservative intrabar fills, transaction costs, position sizing,
and daily/portfolio risk locks. One open position per symbol at a time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from app.backtesting.cost_model import CostModel
from app.core.enums import ExitReason, Side
from app.indicators.engine import atr as atr_series
from app.position_management import exit_manager
from app.position_management.position_manager import ExitEvent, Position
from app.risk.daily_limits import DailyRiskState, RiskLimits
from app.risk.position_sizing import SizingInputs, calculate_quantity
from app.risk.risk_engine import pre_trade_checks
from app.strategy.config import StrategyConfig
from app.strategy.models import Candle, D
from app.strategy.signal_engine import SignalEngine


@dataclass
class Trade:
    symbol: str
    side: Side
    entry_time: datetime
    entry: Decimal
    quantity: int
    initial_stop: Decimal
    original_R: Decimal
    break_even_trigger: Decimal
    partial_profit_trigger: Decimal | None
    final_target: Decimal
    exits: list[ExitEvent]
    gross_pnl: Decimal
    costs: Decimal
    net_pnl: Decimal
    r_result: Decimal
    exit_reason: ExitReason | None
    ema_touched: int


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, Decimal]] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    starting_capital: Decimal = Decimal(0)

    @property
    def ending_capital(self) -> Decimal:
        return self.starting_capital + sum((t.net_pnl for t in self.trades), Decimal(0))


def _parse_time(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


class Backtester:
    def __init__(
        self,
        config: StrategyConfig | None = None,
        *,
        starting_capital: Decimal = Decimal("1000000"),
        limits: RiskLimits | None = None,
        cost_model: CostModel | None = None,
        tick_size: Decimal | None = None,
        lot_size: int | None = None,
        margin_per_lot: Decimal | None = None,
    ) -> None:
        self.cfg = config or StrategyConfig()
        self.starting_capital = D(starting_capital)
        self.limits = limits or RiskLimits()
        self.cost_model = cost_model or CostModel()
        self.tick_size = (
            D(tick_size) if tick_size is not None else D(self.cfg.instrument_defaults.tick_size)
        )
        self.lot_size = lot_size if lot_size is not None else self.cfg.instrument_defaults.lot_size
        self.margin_per_lot = D(margin_per_lot) if margin_per_lot is not None else None
        self._entry_start = _parse_time(self.cfg.session.entry_start)
        self._entry_cutoff = _parse_time(self.cfg.session.entry_cutoff)
        self._square_off = _parse_time(self.cfg.session.forced_square_off)

    # ── public ──
    def run(self, candles_by_symbol: dict[str, list[Candle]]) -> BacktestResult:
        result = BacktestResult(starting_capital=self.starting_capital)

        # global, timestamp-ordered event stream (portfolio-realistic risk/positions)
        events: list[tuple[datetime, str, int]] = []
        atr_by_symbol: dict[str, list[Decimal]] = {}
        for sym, candles in candles_by_symbol.items():
            for i in range(len(candles)):
                events.append((candles[i].timestamp, sym, i))
            a = atr_series(
                [float(c.high) for c in candles],
                [float(c.low) for c in candles],
                [float(c.close) for c in candles],
                self.cfg.atr.period,
            )
            atr_by_symbol[sym] = [D(x) for x in a]
        events.sort(key=lambda e: (e[0], e[1]))

        engine = SignalEngine(self.cfg, tick_size=self.tick_size, lot_size=self.lot_size)
        open_pos: dict[str, Position] = {}
        entry_index: dict[str, int] = {}
        pending_risk: dict[str, Decimal] = {}
        state: DailyRiskState | None = None
        cur_date: date | None = None

        for ts, sym, i in events:
            candles = candles_by_symbol[sym]
            if cur_date != ts.date():
                cur_date = ts.date()
                state = DailyRiskState(cur_date, self.starting_capital)

            # 1) manage an open position for this symbol on candle i
            pos = open_pos.get(sym)
            if pos is not None and i >= entry_index[sym]:
                prev = candles[i - 1] if i > 0 else None
                is_sqoff = ts.time() >= self._square_off
                exit_manager.process_candle(
                    pos,
                    self.cfg,
                    candles[i],
                    prev_candle=prev,
                    atr=atr_by_symbol[sym][i],
                    is_square_off=is_sqoff,
                    policy=self.cfg.intrabar_policy,
                )
                if pos.closed:
                    self._close_trade(result, state, sym, pos, pending_risk[sym])
                    del open_pos[sym]
                    del entry_index[sym]
                    del pending_risk[sym]

            # 2) look for a new entry (only if flat and within the entry window)
            if sym in open_pos or i + 1 >= len(candles):
                continue
            entry_candle = candles[i + 1]
            if not (self._entry_start <= entry_candle.timestamp.time() <= self._entry_cutoff):
                continue

            sig = engine.evaluate(sym, candles[: i + 1], forming_open=entry_candle.open)
            if sig is None:
                continue

            sizing = calculate_quantity(
                SizingInputs(
                    available_capital=self.starting_capital,
                    risk_percentage_per_trade=self.limits.risk_per_trade_percentage,
                    entry_price=sig.entry,
                    initial_stop=sig.initial_stop,
                    side=sig.side,
                    lot_size=self.lot_size,
                    tick_size=self.tick_size,
                    margin_per_lot=self.margin_per_lot,
                    max_margin_utilisation_pct=self.limits.max_margin_utilisation_percentage,
                    fixed_lot_mode=self.limits.fixed_lot_mode,
                    fixed_lots=self.limits.fixed_lots,
                )
            )
            if sizing.rejected:
                self._reject(result, f"sizing:{sizing.reason}")
                continue

            trade_risk = sizing.risk_per_unit * Decimal(sizing.quantity)
            decision = pre_trade_checks(state, self.limits, symbol=sym, new_trade_risk=trade_risk)
            if not decision.allowed:
                self._reject(result, f"risk:{decision.reason}")
                continue

            pos = Position(
                symbol=sym,
                side=sig.side,
                entry=sig.entry,
                quantity=sizing.quantity,
                initial_stop=sig.initial_stop,
                original_R=sig.original_R,
                break_even_trigger=sig.break_even_trigger,
                partial_profit_trigger=sig.partial_profit_trigger,
                final_target=sig.final_target,
                entry_time=entry_candle.timestamp,
                tick_size=self.tick_size,
                lot_size=self.lot_size,
            )
            pos.ema_touched = sig.ema_touched  # type: ignore[attr-defined]
            open_pos[sym] = pos
            entry_index[sym] = i + 1
            pending_risk[sym] = trade_risk
            state.register_open(sym, trade_risk)

        # close any positions still open at data end (mark to last close)
        for sym, pos in list(open_pos.items()):
            last = candles_by_symbol[sym][-1]
            if not pos.closed:
                pos.record_exit(
                    pos.remaining_qty, last.close, ExitReason.FORCED_SQUARE_OFF, last.timestamp
                )
            assert state is not None
            self._close_trade(result, state, sym, pos, pending_risk[sym])
        return result

    # ── internals ──
    def _close_trade(
        self,
        result: BacktestResult,
        state: DailyRiskState,
        sym: str,
        pos: Position,
        trade_risk: Decimal,
    ) -> None:
        gross = pos.gross_pnl()
        entry_is_buy = pos.side is Side.BUY
        costs = self.cost_model.leg(pos.entry, pos.quantity, is_buy=entry_is_buy).total
        for e in pos.exits:
            costs += self.cost_model.leg(e.price, e.quantity, is_buy=not entry_is_buy).total
        net = gross - costs
        state.register_close(sym, trade_risk, net)
        result.trades.append(
            Trade(
                symbol=sym,
                side=pos.side,
                entry_time=pos.entry_time,
                entry=pos.entry,
                quantity=pos.quantity,
                initial_stop=pos.initial_stop,
                original_R=pos.original_R,
                break_even_trigger=pos.break_even_trigger,
                partial_profit_trigger=pos.partial_profit_trigger,
                final_target=pos.final_target,
                exits=list(pos.exits),
                gross_pnl=gross,
                costs=costs,
                net_pnl=net,
                r_result=pos.realized_R(),
                exit_reason=pos.close_reason,
                ema_touched=getattr(pos, "ema_touched", 0),
            )
        )
        result.equity_curve.append((pos.exits[-1].timestamp, result.ending_capital))

    @staticmethod
    def _reject(result: BacktestResult, reason: str) -> None:
        result.rejections[reason] = result.rejections.get(reason, 0) + 1
