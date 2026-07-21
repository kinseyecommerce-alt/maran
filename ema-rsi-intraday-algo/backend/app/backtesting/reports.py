"""Backtest reporting: per-trade rows + CSV / JSON export (stdlib only).

Excel and PDF exporters are optional and land in a later phase (they need extra
deps); CSV + JSON cover the machine-readable and spreadsheet-import paths now.
"""

from __future__ import annotations

import csv
import io
import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.backtesting.engine import BacktestResult, Trade

_COLUMNS = [
    "symbol",
    "side",
    "entry_time",
    "entry",
    "quantity",
    "initial_stop",
    "original_R",
    "break_even_trigger",
    "partial_profit_trigger",
    "final_target",
    "ema_touched",
    "gross_pnl",
    "costs",
    "net_pnl",
    "r_result",
    "exit_reason",
    "num_exits",
]


def trade_row(t: Trade) -> dict:
    return {
        "symbol": t.symbol,
        "side": t.side.value,
        "entry_time": t.entry_time.isoformat(),
        "entry": str(t.entry),
        "quantity": t.quantity,
        "initial_stop": str(t.initial_stop),
        "original_R": str(t.original_R),
        "break_even_trigger": str(t.break_even_trigger),
        "partial_profit_trigger": str(t.partial_profit_trigger)
        if t.partial_profit_trigger is not None
        else "",
        "final_target": str(t.final_target),
        "ema_touched": t.ema_touched,
        "gross_pnl": str(t.gross_pnl),
        "costs": str(t.costs),
        "net_pnl": str(t.net_pnl),
        "r_result": f"{float(t.r_result):.3f}",
        "exit_reason": t.exit_reason.value if t.exit_reason else "",
        "num_exits": len(t.exits),
    }


def trades_to_csv(result: BacktestResult) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_COLUMNS)
    writer.writeheader()
    for t in result.trades:
        writer.writerow(trade_row(t))
    return buf.getvalue()


def report_to_json(result: BacktestResult, metrics: dict) -> str:
    return json.dumps(
        {"metrics": metrics, "trades": [trade_row(t) for t in result.trades]},
        indent=2,
        sort_keys=True,
    )
