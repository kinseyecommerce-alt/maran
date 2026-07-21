#!/usr/bin/env python3
"""EMA RSI Intraday Algo — command-line runner.

Load real 3-minute data and run a backtest or a paper session against your strategy.

Examples
--------
  # backtest a single CSV (symbol from a column or --symbol)
  python scripts/algo.py backtest --csv data/RELIANCE_3min.csv --symbol RELIANCE

  # backtest every CSV in a folder, custom capital + strategy config, write reports
  python scripts/algo.py backtest --dir data/ --capital 500000 \
      --config ../config/strategy.default.yaml --out out/

  # paper-trading session (simulated broker) over Kite historical JSON
  python scripts/algo.py paper --json data/INFY.json --symbol INFY --slippage-bps 5

Safety: this tool only ever runs BACKTEST or PAPER (simulated). It never connects to
a broker and never places a real order.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal
from pathlib import Path

# make `app` importable when run from scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import load_strategy_config  # noqa: E402
from app.market_data.loaders import load_candles, load_dir  # noqa: E402
from app.services.runner import (  # noqa: E402
    format_summary,
    run_backtest,
    run_paper,
    write_reports,
)
from app.strategy.config import StrategyConfig  # noqa: E402


def _load_data(args) -> dict:
    if args.dir:
        return load_dir(args.dir)
    if args.csv:
        return load_candles(args.csv, symbol=args.symbol)
    if args.json:
        return load_candles(args.json, symbol=args.symbol)
    raise SystemExit("provide one of --csv / --json / --dir")


def _config(args) -> StrategyConfig:
    cfg = load_strategy_config(args.config) if args.config else StrategyConfig()
    if args.no_shorts:
        cfg.short_enabled = False
    return cfg


def _ndays(data: dict) -> int:
    days = {c.session_date for candles in data.values() for c in candles}
    return max(1, len(days))


def cmd_backtest(args) -> int:
    data = _load_data(args)
    if not data:
        print("no candles loaded", file=sys.stderr)
        return 2
    cfg = _config(args)
    result, metrics = run_backtest(data, cfg, capital=Decimal(str(args.capital)))
    print(
        f"loaded {sum(len(c) for c in data.values())} candles across "
        f"{len(data)} symbol(s), ~{_ndays(data)} trading days\n"
    )
    print(format_summary(metrics, days=_ndays(data)))
    if args.out:
        csv_path, json_path = write_reports(result, metrics, args.out)
        print(f"\nreports written: {csv_path}  |  {json_path}")
    return 0


def cmd_paper(args) -> int:
    data = _load_data(args)
    if not data:
        print("no candles loaded", file=sys.stderr)
        return 2
    cfg = _config(args)
    res = run_paper(
        data, cfg, capital=Decimal(str(args.capital)), slippage_bps=Decimal(str(args.slippage_bps))
    )
    net = sum((t.net_pnl for t in res.trades), Decimal(0))
    print(
        f"PAPER session · {len(res.trades)} trade(s) · orders {res.orders_placed} · "
        f"reconciled_flat={res.reconciled_flat}"
    )
    print(f"net P&L ₹{float(net):,.2f}")
    for t in res.trades:
        print(
            f"  {t.symbol:<12} {t.side.value:<4} qty {t.quantity:<5} "
            f"R {float(t.r_result):+.2f}  net ₹{float(t.net_pnl):,.1f}  {t.exit_reason.value if t.exit_reason else ''}"
        )
    if res.rejections:
        print("rejections:", dict(res.rejections))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="algo", description="EMA RSI Intraday Algo runner (BACKTEST / PAPER only)"
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("backtest", cmd_backtest), ("paper", cmd_paper)):
        sp = sub.add_parser(name, help=f"run a {name}")
        src = sp.add_mutually_exclusive_group(required=True)
        src.add_argument("--csv", help="path to a 3-min OHLCV CSV")
        src.add_argument("--json", help="path to Kite historical JSON")
        src.add_argument("--dir", help="directory of *.csv (symbol = filename)")
        sp.add_argument(
            "--symbol",
            help="symbol (required for JSON / single-series CSV without a symbol column)",
        )
        sp.add_argument("--capital", default="1000000", help="starting capital (default 10,00,000)")
        sp.add_argument("--config", help="strategy YAML (defaults to built-in defaults)")
        sp.add_argument("--no-shorts", action="store_true", help="disable SELL side")
        if name == "backtest":
            sp.add_argument("--out", help="directory to write trades.csv + report.json")
        else:
            sp.add_argument("--slippage-bps", default="0", help="paper fill slippage (bps)")
        sp.set_defaults(func=fn)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
