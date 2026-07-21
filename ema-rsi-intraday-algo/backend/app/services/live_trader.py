"""Tick-driven trading session — the live loop.

Consumes a raw tick stream (KiteTicker in LIVE, the replay adapter in tests), builds
session-aligned 3-minute candles tick-by-tick, and on each completed candle runs the
SAME strategy engine, sizing, risk gate, OMS and exit managers as the backtester /
paper session. Nothing about the strategy or risk logic changes — only the data
source (ticks) and the entry-price source (the first tick of the next interval, i.e.
the real next-candle open).

Execution goes through whatever `BrokerAdapter` is injected:
  * `PaperBrokerAdapter` (default) → simulated fills, no real orders.
  * `ZerodhaBrokerAdapter(allow_live=True)` → real Kite orders (LIVE, gated).

For production LIVE the protective stop should also be placed as a resting SL-M order
so the exchange enforces it between candle closes (see `protective_stop_request`);
the default tested path lets the exit manager decide exits on each candle close.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from app.backtesting.cost_model import CostModel
from app.brokers.interface import BrokerAdapter, OrderRequest
from app.core.enums import ExitReason, OrderStatus, OrderType, Side
from app.indicators.engine import atr as atr_series
from app.market_data.candle_builder import ThreeMinuteCandleBuilder
from app.market_data.interfaces import MarketDataAdapter, Tick
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
class LiveTrade:
    symbol: str
    side: Side
    entry: Decimal
    quantity: int
    net_pnl: Decimal
    r_result: Decimal
    exit_reason: ExitReason | None


@dataclass
class LiveSessionResult:
    trades: list[LiveTrade] = field(default_factory=list)
    orders_placed: int = 0
    candles_built: int = 0
    rejections: dict[str, int] = field(default_factory=dict)
    mismatches: list[Mismatch] = field(default_factory=list)

    @property
    def reconciled_flat(self) -> bool:
        return not self.mismatches


def _t(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


class _SymbolState:
    def __init__(self, symbol: str) -> None:
        self.builder = ThreeMinuteCandleBuilder(symbol)
        self.completed: list[Candle] = []
        self.pos: Position | None = None
        self.trade_risk: Decimal = Decimal(0)
        self.exits_mirrored: int = 0


class TickDrivenSession:
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
        self.engine = SignalEngine(self.cfg, tick_size=self.tick_size, lot_size=self.lot_size)
        self.result = LiveSessionResult()
        self._local: dict[str, int] = {}
        self._sym: dict[str, _SymbolState] = {}
        self._state: DailyRiskState | None = None
        self._date: date | None = None
        self._entry_start = _t(self.cfg.session.entry_start)
        self._entry_cutoff = _t(self.cfg.session.entry_cutoff)
        self._square_off = _t(self.cfg.session.forced_square_off)

    # ── public ──
    def run_stream(self, adapter: MarketDataAdapter) -> LiveSessionResult:
        """Drive the session from an adapter's tick stream to completion (replay), or
        until disconnect (live)."""
        adapter.stream_ticks(self.on_tick)
        self._square_off_all()
        self.result.mismatches = reconcile(self._local, self.broker)
        return self.result

    def on_tick(self, tick: Tick) -> None:
        st = self._sym.setdefault(tick.symbol, _SymbolState(tick.symbol))
        closed = st.builder.add_tick(tick)
        if closed is None:
            return
        self.result.candles_built += 1
        st.completed.append(closed)
        self._roll_session(tick.timestamp)
        # `tick` is the first tick of the new interval → its price ≈ next-candle open.
        self._on_candle_closed(st, tick.symbol, next_open=tick.last_price, at=tick.timestamp)

    # ── internals ──
    def _roll_session(self, ts: datetime) -> None:
        if self._date != ts.date():
            self._date = ts.date()
            self._state = DailyRiskState(self._date, self.capital)

    def _atr_last(self, completed: list[Candle]) -> Decimal:
        a = atr_series(
            [float(c.high) for c in completed],
            [float(c.low) for c in completed],
            [float(c.close) for c in completed],
            self.cfg.atr.period,
        )
        return D(a[-1]) if a else Decimal(0)

    def _on_candle_closed(
        self, st: _SymbolState, symbol: str, *, next_open: Decimal, at: datetime
    ) -> None:
        candle = st.completed[-1]
        # 1) manage an open position on the just-closed candle
        if st.pos is not None:
            prev = st.completed[-2] if len(st.completed) >= 2 else None
            exit_manager.process_candle(
                st.pos,
                self.cfg,
                candle,
                prev_candle=prev,
                atr=self._atr_last(st.completed),
                # Square-off keys off the just-closed candle's OWN interval time (as the
                # backtester does), not the triggering tick's time — otherwise the live
                # loop would force-exit one candle early. `at` (next interval's first tick)
                # still gates entries below, matching the backtester's entry-window check.
                is_square_off=candle.timestamp.time() >= self._square_off,
                policy=self.cfg.intrabar_policy,
            )
            self._mirror_exits(st)
            if st.pos.closed:
                self._finalize(st, symbol)
                st.pos = None
                st.exits_mirrored = 0

        # 2) look for an entry (flat + inside the entry window)
        if st.pos is not None:
            return
        if not (self._entry_start <= at.time() <= self._entry_cutoff):
            return
        sig = self.engine.evaluate(symbol, st.completed, forming_open=next_open)
        if sig is None:
            return

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
            self._reject(f"sizing:{sizing.reason}")
            return
        trade_risk = sizing.risk_per_unit * Decimal(sizing.quantity)
        assert self._state is not None
        decision = pre_trade_checks(
            self._state, self.limits, symbol=symbol, new_trade_risk=trade_risk
        )
        if not decision.allowed:
            self._reject(f"risk:{decision.reason}")
            return

        entry_order = self.oms.place(
            OrderRequest(
                symbol=symbol,
                transaction=sig.side,
                order_type=OrderType.ENTRY_MARKET,
                quantity=sizing.quantity,
                tag=f"entry:{symbol}",
                idempotency_key=sig.idempotency_key,
            ),
            reference_price=sig.entry,
            at=at,
        )
        self.result.orders_placed += 1
        if entry_order.status is not OrderStatus.FILLED:
            self._reject(f"broker:{entry_order.rejection_reason}")
            return

        fill = entry_order.average_price
        signed = sizing.quantity if sig.side is Side.BUY else -sizing.quantity
        self._local[symbol] = self._local.get(symbol, 0) + signed
        sign = Decimal(1) if sig.side is Side.BUY else Decimal(-1)
        risk = (fill - sig.initial_stop) if sig.side is Side.BUY else (sig.initial_stop - fill)
        if risk <= 0:
            return
        tm = self.cfg.trade_management
        st.pos = Position(
            symbol=symbol,
            side=sig.side,
            entry=fill,
            quantity=sizing.quantity,
            initial_stop=sig.initial_stop,
            original_R=risk,
            break_even_trigger=fill + sign * D(tm.break_even_trigger_R) * risk,
            partial_profit_trigger=(fill + sign * D(tm.partial_exit_R) * risk)
            if tm.partial_exit_enabled
            else None,
            final_target=fill + sign * D(tm.final_target_R) * risk,
            entry_time=at,
            tick_size=self.tick_size,
            lot_size=self.lot_size,
        )
        st.pos.ema_touched = sig.ema_touched  # type: ignore[attr-defined]
        st.trade_risk = trade_risk
        st.exits_mirrored = 0
        self._state.register_open(symbol, trade_risk)

    def protective_stop_request(self, pos: Position) -> OrderRequest:
        """The resting SL-M order a LIVE deployment should place so the exchange
        enforces the stop tick-by-tick between candle closes."""
        return OrderRequest(
            symbol=pos.symbol,
            transaction=pos.side.opposite,
            order_type=OrderType.PROTECTIVE_STOP,
            quantity=pos.remaining_qty,
            trigger_price=pos.current_stop,
            tag=f"stop:{pos.symbol}",
            idempotency_key=f"{pos.symbol}|PROTECTIVE_STOP|{pos.entry_time.isoformat()}",
        )

    def _mirror_exits(self, st: _SymbolState) -> None:
        pos = st.pos
        assert pos is not None
        for ev in pos.exits[st.exits_mirrored :]:
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
            self.result.orders_placed += 1
            signed = ev.quantity if exit_side is Side.BUY else -ev.quantity
            self._local[pos.symbol] = self._local.get(pos.symbol, 0) + signed
        st.exits_mirrored = len(pos.exits)

    def _square_off_all(self) -> None:
        for symbol, st in self._sym.items():
            if st.pos is not None and not st.pos.closed and st.completed:
                last = st.completed[-1]
                st.pos.record_exit(
                    st.pos.remaining_qty, last.close, ExitReason.FORCED_SQUARE_OFF, last.timestamp
                )
                self._mirror_exits(st)
                self._finalize(st, symbol)

    def _finalize(self, st: _SymbolState, symbol: str) -> None:
        pos = st.pos
        assert pos is not None and self._state is not None
        gross = pos.gross_pnl()
        entry_is_buy = pos.side is Side.BUY
        costs = self.cost_model.leg(pos.entry, pos.quantity, is_buy=entry_is_buy).total
        for e in pos.exits:
            costs += self.cost_model.leg(e.price, e.quantity, is_buy=not entry_is_buy).total
        net = gross - costs
        self._state.register_close(symbol, st.trade_risk, net)
        self.result.trades.append(
            LiveTrade(
                symbol, pos.side, pos.entry, pos.quantity, net, pos.realized_R(), pos.close_reason
            )
        )

    def _reject(self, reason: str) -> None:
        self.result.rejections[reason] = self.result.rejections.get(reason, 0) + 1
