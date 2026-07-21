"""Backtest metrics. Computed in Decimal; ratios as float where that is conventional."""

from __future__ import annotations

import math
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.enums import Side

if TYPE_CHECKING:
    from app.backtesting.engine import BacktestResult, Trade


def _max_drawdown(equity: list[Decimal]) -> tuple[Decimal, float]:
    peak = equity[0] if equity else Decimal(0)
    max_dd = Decimal(0)
    max_dd_pct = 0.0
    for v in equity:
        peak = max(peak, v)
        dd = peak - v
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = float(dd / peak * 100) if peak > 0 else 0.0
    return max_dd, max_dd_pct


def _consecutive(trades: list[Trade], winners: bool) -> int:
    best = cur = 0
    for t in trades:
        win = t.net_pnl > 0
        if win == winners:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def compute_metrics(result: BacktestResult) -> dict:
    trades = result.trades
    n = len(trades)
    start = result.starting_capital
    net = sum((t.net_pnl for t in trades), Decimal(0))
    gross_profit = sum((t.net_pnl for t in trades if t.net_pnl > 0), Decimal(0))
    gross_loss = sum((t.net_pnl for t in trades if t.net_pnl < 0), Decimal(0))
    wins = [t for t in trades if t.net_pnl > 0]
    losses = [t for t in trades if t.net_pnl < 0]
    breakeven = [t for t in trades if t.net_pnl == 0]
    rs = sorted(t.r_result for t in trades)
    charges = sum((t.costs for t in trades), Decimal(0))

    equity = [start] + [
        start + sum((trades[j].net_pnl for j in range(i + 1)), Decimal(0)) for i in range(n)
    ]
    max_dd, max_dd_pct = _max_drawdown(equity)

    def avg(xs: list[Decimal]) -> Decimal:
        return sum(xs, Decimal(0)) / Decimal(len(xs)) if xs else Decimal(0)

    r_values = [t.r_result for t in trades]
    expectancy_r = avg(r_values)
    win_rate = (len(wins) / n * 100) if n else 0.0

    # simple per-trade Sharpe (net P&L series), annualisation deliberately omitted
    sharpe = 0.0
    if n > 1:
        pnls = [float(t.net_pnl) for t in trades]
        mean = sum(pnls) / n
        var = sum((x - mean) ** 2 for x in pnls) / (n - 1)
        sd = math.sqrt(var)
        sharpe = (mean / sd) if sd > 0 else 0.0

    def side_block(side: Side) -> dict:
        ts = [t for t in trades if t.side is side]
        w = sum(1 for t in ts if t.net_pnl > 0)
        return {
            "trades": len(ts),
            "net": float(sum((t.net_pnl for t in ts), Decimal(0))),
            "win_rate": (w / len(ts) * 100) if ts else 0.0,
        }

    by_reason: dict[str, dict] = {}
    for t in trades:
        key = t.exit_reason.value if t.exit_reason else "UNKNOWN"
        b = by_reason.setdefault(key, {"trades": 0, "net": 0.0})
        b["trades"] += 1
        b["net"] += float(t.net_pnl)

    by_ema: dict[str, dict] = {}
    for t in trades:
        key = f"EMA{t.ema_touched}"
        b = by_ema.setdefault(key, {"trades": 0, "net": 0.0})
        b["trades"] += 1
        b["net"] += float(t.net_pnl)

    return {
        "starting_capital": float(start),
        "ending_capital": float(start + net),
        "net_profit": float(net),
        "net_profit_pct": float(net / start * 100) if start else 0.0,
        "gross_profit": float(gross_profit),
        "gross_loss": float(gross_loss),
        "total_charges": float(charges),
        "total_trades": n,
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "breakeven_trades": len(breakeven),
        "win_rate": win_rate,
        "average_winner": float(avg([t.net_pnl for t in wins])),
        "average_loser": float(avg([t.net_pnl for t in losses])),
        "average_R": float(expectancy_r),
        "median_R": float(rs[n // 2]) if n else 0.0,
        "expectancy_R": float(expectancy_r),
        "expectancy_money": float(net / Decimal(n)) if n else 0.0,
        "profit_factor": float(gross_profit / -gross_loss) if gross_loss < 0 else None,
        "max_drawdown": float(max_dd),
        "max_drawdown_pct": max_dd_pct,
        "recovery_factor": float(net / max_dd) if max_dd > 0 else None,
        "sharpe_ratio": sharpe,
        "max_consecutive_wins": _consecutive(trades, True),
        "max_consecutive_losses": _consecutive(trades, False),
        "best_trade": float(max((t.net_pnl for t in trades), default=Decimal(0))),
        "worst_trade": float(min((t.net_pnl for t in trades), default=Decimal(0))),
        "long_performance": side_block(Side.BUY),
        "short_performance": side_block(Side.SELL),
        "by_exit_reason": by_reason,
        "by_touched_ema": by_ema,
        "rejections": dict(result.rejections),
    }
