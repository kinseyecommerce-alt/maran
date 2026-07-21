"""Reusable run-and-report helpers shared by the CLI (and, later, the API).

Keeps the CLI thin: load candles → `run_backtest` / `run_paper` → `format_summary`
+ `write_reports`.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.backtesting.cost_model import CostModel
from app.backtesting.engine import Backtester, BacktestResult
from app.backtesting.metrics import compute_metrics
from app.backtesting.reports import report_to_json, trades_to_csv
from app.brokers.paper_broker import PaperBrokerAdapter
from app.risk.daily_limits import RiskLimits
from app.services.paper_trader import PaperSessionResult, PaperTradingSession
from app.strategy.config import StrategyConfig
from app.strategy.models import Candle


def run_backtest(
    candles_by_symbol: dict[str, list[Candle]],
    cfg: StrategyConfig,
    *,
    capital: Decimal = Decimal("1000000"),
    limits: RiskLimits | None = None,
    cost_model: CostModel | None = None,
) -> tuple[BacktestResult, dict]:
    bt = Backtester(cfg, starting_capital=capital, limits=limits, cost_model=cost_model)
    result = bt.run(candles_by_symbol)
    return result, compute_metrics(result)


def run_paper(
    candles_by_symbol: dict[str, list[Candle]],
    cfg: StrategyConfig,
    *,
    capital: Decimal = Decimal("1000000"),
    limits: RiskLimits | None = None,
    slippage_bps: Decimal = Decimal("0"),
) -> PaperSessionResult:
    broker = PaperBrokerAdapter(slippage_bps=slippage_bps)
    return PaperTradingSession(broker, cfg, starting_capital=capital, limits=limits).run(
        candles_by_symbol
    )


def _fmt(v: object) -> str:
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def format_summary(metrics: dict, *, days: int | None = None) -> str:
    m = metrics
    lines = [
        "═" * 56,
        f"  BACKTEST SUMMARY{'  ·  ' + str(days) + ' days' if days else ''}",
        "═" * 56,
        f"  Trades          {m['total_trades']}   "
        f"(W {m['winning_trades']} / L {m['losing_trades']} / BE {m['breakeven_trades']})",
        f"  Win rate        {m['win_rate']:.1f}%",
        f"  Net P&L         ₹{_fmt(m['net_profit'])}   ({m['net_profit_pct']:.2f}%)",
        f"  Gross / charges  ₹{_fmt(m['gross_profit'] + m['gross_loss'])} / ₹{_fmt(m['total_charges'])}",
        f"  Avg R / expct.  {m['average_R']:.2f}R  /  {m['expectancy_R']:.2f}R per trade",
        f"  Profit factor   {_fmt(m['profit_factor'])}",
        f"  Max drawdown    ₹{_fmt(m['max_drawdown'])}  ({m['max_drawdown_pct']:.2f}%)",
        f"  Best / worst    ₹{_fmt(m['best_trade'])} / ₹{_fmt(m['worst_trade'])}",
        f"  Long / short    {m['long_performance']['trades']} tr ₹{_fmt(m['long_performance']['net'])}"
        f"  |  {m['short_performance']['trades']} tr ₹{_fmt(m['short_performance']['net'])}",
    ]
    if m.get("by_exit_reason"):
        lines.append(
            "  Exit reasons    "
            + ", ".join(f"{k} {v['trades']}" for k, v in sorted(m["by_exit_reason"].items()))
        )
    if m.get("rejections"):
        lines.append(
            "  Rejections      " + ", ".join(f"{k}×{v}" for k, v in sorted(m["rejections"].items()))
        )
    lines.append("═" * 56)
    return "\n".join(lines)


def write_reports(result: BacktestResult, metrics: dict, out_dir: str | Path) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "trades.csv"
    json_path = out / "report.json"
    csv_path.write_text(trades_to_csv(result), encoding="utf-8")
    json_path.write_text(report_to_json(result, metrics), encoding="utf-8")
    return csv_path, json_path
