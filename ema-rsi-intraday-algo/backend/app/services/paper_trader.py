"""Paper-trading session — the live-shaped execution path.

Runs the SAME strategy engine, sizing, risk gate and exit managers as the backtester,
but routes every fill through the OrderManager → BrokerAdapter (PaperBroker by
default). This is the code path that Phase 5 points at the real Zerodha adapter: only
the broker changes, not the strategy or the risk logic.

Fills come from the broker (never assumed), positions are reconciled against the
broker at the end, and entries are blocked by the same daily/portfolio risk locks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from app.backtesting.cost_model import CostModel
from app.brokers.interface import BrokerAdapter, OrderRequest
from app.core.enums import ExitReason, OrderStatus, OrderType, Side
from app.indicators.engine import atr as atr_series
from app.order_management.order_manager import OrderManager
from app.order_management.reconciliation import Mismatch, reconcile
from app.position_management import exit_manager
from app.position_management.position_manager import Position
from app.risk.daily_limits import DailyRiskState, RiskLimits
from app.risk.position_sizing import SizingInputs, calculate_quantity
from app.risk.risk_engine import pre_trade_checks
from app.strategy.config import StrategyConfig
from app.strategy.models import Candle, D
from app.strategy.signal_engine import SignalEngine


@dataclass
class PaperTrade:
    symbol: str
    side: Side
    entry: Decimal
    quantity: int
    net_pnl: Decimal
    r_result: Decimal
    exit_reason: ExitReason | None


@dataclass
class PaperSessionResult:
    trades: list[PaperTrade] = field(default_factory=list)
    mismatches: list[Mismatch] = field(default_factory=list)
    orders_placed: int = 0
    rejections: dict[str, int] = field(default_factory=dict)

    @property
    def reconciled_flat(self) -> bool:
        return not self.mismatches


def _t(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


class PaperTradingSession:
    def __init__(
        self,
        broker: BrokerAdapter,
        config: StrategyConfig | None = None,
        *,
        starting_capital: Decimal = Decimal("1000000"),
        limits: RiskLimits | None = None,
        cost_model: CostModel | None = None,
        tick_size: Decimal | None = None,
        lot_size: int | None = None,
    ) -> None:
        self.broker = broker
        self.cfg = config or StrategyConfig()
        self.capital = D(starting_capital)
        self.limits = limits or RiskLimits()
        self.cost_model = cost_model or CostModel()
        self.tick_size = (
            D(tick_size) if tick_size is not None else D(self.cfg.instrument_defaults.tick_size)
        )
        self.lot_size = lot_size if lot_size is not None else self.cfg.instrument_defaults.lot_size
        self.oms = OrderManager(broker)
        self._entry_start = _t(self.cfg.session.entry_start)
        self._entry_cutoff = _t(self.cfg.session.entry_cutoff)
        self._square_off = _t(self.cfg.session.forced_square_off)

    def run(self, candles_by_symbol: dict[str, list[Candle]]) -> PaperSessionResult:
        result = PaperSessionResult()
        local_positions: dict[str, int] = {}

        events: list[tuple[datetime, str, int]] = []
        atrs: dict[str, list[Decimal]] = {}
        for sym, candles in candles_by_symbol.items():
            for i in range(len(candles)):
                events.append((candles[i].timestamp, sym, i))
            a = atr_series(
                [float(c.high) for c in candles],
                [float(c.low) for c in candles],
                [float(c.close) for c in candles],
                self.cfg.atr.period,
            )
            atrs[sym] = [D(x) for x in a]
        events.sort(key=lambda e: (e[0], e[1]))

        engine = SignalEngine(self.cfg, tick_size=self.tick_size, lot_size=self.lot_size)
        pos_by_sym: dict[str, Position] = {}
        entry_idx: dict[str, int] = {}
        risk_by_sym: dict[str, Decimal] = {}
        exits_mirrored: dict[str, int] = {}
        state: DailyRiskState | None = None
        cur_date: date | None = None

        for ts, sym, i in events:
            candles = candles_by_symbol[sym]
            if cur_date != ts.date():
                cur_date = ts.date()
                state = DailyRiskState(cur_date, self.capital)

            pos = pos_by_sym.get(sym)
            if pos is not None and i >= entry_idx[sym]:
                prev = candles[i - 1] if i > 0 else None
                exit_manager.process_candle(
                    pos,
                    self.cfg,
                    candles[i],
                    prev_candle=prev,
                    atr=atrs[sym][i],
                    is_square_off=ts.time() >= self._square_off,
                    policy=self.cfg.intrabar_policy,
                )
                # mirror any newly-recorded exits to the broker via the OMS
                self._mirror_exits(result, local_positions, pos, exits_mirrored, ts)
                if pos.closed:
                    self._finalize(result, state, sym, pos, risk_by_sym[sym])
                    for d in (pos_by_sym, entry_idx, risk_by_sym, exits_mirrored):
                        d.pop(sym, None)

            if sym in pos_by_sym or i + 1 >= len(candles):
                continue
            entry_candle = candles[i + 1]
            if not (self._entry_start <= entry_candle.timestamp.time() <= self._entry_cutoff):
                continue

            sig = engine.evaluate(sym, candles[: i + 1], forming_open=entry_candle.open)
            if sig is None:
                continue

            sizing = calculate_quantity(
                SizingInputs(
                    available_capital=self.capital,
                    risk_percentage_per_trade=self.limits.risk_per_trade_percentage,
                    entry_price=sig.entry,
                    initial_stop=sig.initial_stop,
                    side=sig.side,
                    lot_size=self.lot_size,
                    tick_size=self.tick_size,
                    fixed_lot_mode=self.limits.fixed_lot_mode,
                    fixed_lots=self.limits.fixed_lots,
                )
            )
            if sizing.rejected:
                result.rejections[f"sizing:{sizing.reason}"] = (
                    result.rejections.get(f"sizing:{sizing.reason}", 0) + 1
                )
                continue
            trade_risk = sizing.risk_per_unit * Decimal(sizing.quantity)
            decision = pre_trade_checks(state, self.limits, symbol=sym, new_trade_risk=trade_risk)
            if not decision.allowed:
                result.rejections[f"risk:{decision.reason}"] = (
                    result.rejections.get(f"risk:{decision.reason}", 0) + 1
                )
                continue

            # place the entry order through the OMS → broker; fill price comes back
            entry_order = self.oms.place(
                OrderRequest(
                    symbol=sym,
                    transaction=sig.side,
                    order_type=OrderType.ENTRY_MARKET,
                    quantity=sizing.quantity,
                    tag=f"entry:{sym}",
                    idempotency_key=sig.idempotency_key,
                ),
                # entry reference = the strategy's computed (tick-rounded) entry price
                reference_price=sig.entry,
                at=entry_candle.timestamp,
            )
            result.orders_placed += 1
            if entry_order.status is not OrderStatus.FILLED:
                result.rejections[f"broker:{entry_order.rejection_reason}"] = (
                    result.rejections.get(f"broker:{entry_order.rejection_reason}", 0) + 1
                )
                continue

            fill = entry_order.average_price
            signed = sizing.quantity if sig.side is Side.BUY else -sizing.quantity
            local_positions[sym] = local_positions.get(sym, 0) + signed
            # rebuild trade-management levels from the ACTUAL fill (original_R frozen here)
            sign = Decimal(1) if sig.side is Side.BUY else Decimal(-1)
            risk = (fill - sig.initial_stop) if sig.side is Side.BUY else (sig.initial_stop - fill)
            if risk <= 0:
                continue
            pos = Position(
                symbol=sym,
                side=sig.side,
                entry=fill,
                quantity=sizing.quantity,
                initial_stop=sig.initial_stop,
                original_R=risk,
                break_even_trigger=fill
                + sign * D(self.cfg.trade_management.break_even_trigger_R) * risk,
                partial_profit_trigger=(
                    fill + sign * D(self.cfg.trade_management.partial_exit_R) * risk
                )
                if self.cfg.trade_management.partial_exit_enabled
                else None,
                final_target=fill + sign * D(self.cfg.trade_management.final_target_R) * risk,
                entry_time=entry_candle.timestamp,
                tick_size=self.tick_size,
                lot_size=self.lot_size,
            )
            pos.ema_touched = sig.ema_touched  # type: ignore[attr-defined]
            pos_by_sym[sym] = pos
            entry_idx[sym] = i + 1
            risk_by_sym[sym] = trade_risk
            exits_mirrored[sym] = 0
            state.register_open(sym, trade_risk)

        # square off anything still open at data end, then reconcile against the broker
        for sym, pos in list(pos_by_sym.items()):
            last = candles_by_symbol[sym][-1]
            if not pos.closed:
                pos.record_exit(
                    pos.remaining_qty, last.close, ExitReason.FORCED_SQUARE_OFF, last.timestamp
                )
            self._mirror_exits(result, local_positions, pos, exits_mirrored, last.timestamp)
            assert state is not None
            self._finalize(result, state, sym, pos, risk_by_sym[sym])

        result.mismatches = reconcile(local_positions, self.broker)
        return result

    def _mirror_exits(
        self,
        result: PaperSessionResult,
        local_positions: dict[str, int],
        pos: Position,
        exits_mirrored: dict[str, int],
        ts: datetime,
    ) -> None:
        already = exits_mirrored.get(pos.symbol, 0)
        for ev in pos.exits[already:]:
            exit_side = pos.side.opposite
            self.oms.place(
                OrderRequest(
                    symbol=pos.symbol,
                    transaction=exit_side,
                    order_type=OrderType.MANUAL_EXIT,
                    quantity=ev.quantity,
                    tag=f"exit:{ev.reason.value}",
                    idempotency_key=f"{pos.symbol}|{ev.reason.value}|{ev.timestamp.isoformat()}|{ev.quantity}",
                ),
                reference_price=ev.price,
                at=ev.timestamp,
            )
            result.orders_placed += 1
            signed = ev.quantity if exit_side is Side.BUY else -ev.quantity
            local_positions[pos.symbol] = local_positions.get(pos.symbol, 0) + signed
        exits_mirrored[pos.symbol] = len(pos.exits)

    def _finalize(
        self,
        result: PaperSessionResult,
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
            PaperTrade(
                sym, pos.side, pos.entry, pos.quantity, net, pos.realized_R(), pos.close_reason
            )
        )
