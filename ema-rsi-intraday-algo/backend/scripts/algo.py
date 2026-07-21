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

  # tick-by-tick over the Kite WebSocket — PAPER execution (live data, simulated fills)
  python scripts/algo.py live --symbol NIFTY24JUNFUT --token 256265 --lot-size 50

  # LIVE (REAL orders) — requires BOTH the flag AND the environment switch
  ALLOW_LIVE_TRADING=true python scripts/algo.py live --symbol ... --token ... --live

Safety: `backtest` / `paper` never touch a broker. `live` streams Kite ticks and, by
default, still executes on the PAPER broker (no real orders). Real orders are placed
only when `--live` is passed AND `ALLOW_LIVE_TRADING=true` — both gates required.
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


def cmd_live(args) -> int:
    """Tick-by-tick session over Kite's WebSocket. Execution defaults to the paper
    broker (live data, simulated fills). Real orders require BOTH --live AND
    ALLOW_LIVE_TRADING=true in the environment."""
    import os

    from app.brokers.paper_broker import PaperBrokerAdapter
    from app.brokers.zerodha_broker import ZerodhaBrokerAdapter
    from app.market_data.zerodha_market_data import ZerodhaMarketDataAdapter
    from app.services.live_trader import TickDrivenSession

    api_key = args.api_key or os.environ.get("ZERODHA_API_KEY", "")
    access_token = args.access_token or os.environ.get("ZERODHA_ACCESS_TOKEN", "")
    if not (api_key and access_token):
        print("ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN required (env or flags)", file=sys.stderr)
        return 2

    symbols = [s.strip() for s in args.symbol.split(",") if s.strip()]
    tokens = [int(t) for t in args.token.split(",") if t.strip()]
    if len(symbols) != len(tokens):
        print("--symbol and --token counts must match", file=sys.stderr)
        return 2
    symbol_to_token = dict(zip(symbols, tokens, strict=True))

    cfg = _config(args)
    live_ok = args.live and os.environ.get("ALLOW_LIVE_TRADING", "").lower() == "true"
    if args.live and not live_ok:
        print("--live ignored: set ALLOW_LIVE_TRADING=true to permit real orders", file=sys.stderr)
    broker = (
        ZerodhaBrokerAdapter(api_key=api_key, access_token=access_token, allow_live=True)
        if live_ok
        else PaperBrokerAdapter()
    )
    mode = "LIVE (REAL ORDERS)" if live_ok else "PAPER (simulated fills, live data)"
    print(f"■ mode: {mode}  ·  symbols: {', '.join(symbols)}")

    adapter = ZerodhaMarketDataAdapter(
        api_key=api_key, access_token=access_token, symbol_to_token=symbol_to_token
    )
    adapter.subscribe(symbols)
    sess = TickDrivenSession(
        broker,
        cfg,
        capital=Decimal(str(args.capital)),
        tick_size=Decimal(str(args.tick_size)),
        lot_size=int(args.lot_size),
    )
    print("connecting to Kite WebSocket… (Ctrl-C to stop)")
    sess.run_stream(adapter)  # blocks on the websocket
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="algo", description="EMA RSI Intraday Algo runner (BACKTEST / PAPER / LIVE-gated)"
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

    # live: tick-by-tick over Kite WebSocket (paper execution unless explicitly gated)
    lp = sub.add_parser("live", help="tick-by-tick over Kite WebSocket (paper unless --live + env)")
    lp.add_argument("--symbol", required=True, help="comma-separated tradingsymbols")
    lp.add_argument(
        "--token", required=True, help="comma-separated instrument tokens (match --symbol)"
    )
    lp.add_argument("--api-key", help="Kite API key (or env ZERODHA_API_KEY)")
    lp.add_argument("--access-token", help="Kite access token (or env ZERODHA_ACCESS_TOKEN)")
    lp.add_argument("--lot-size", default="1", help="lot size for sizing")
    lp.add_argument("--tick-size", default="0.05", help="instrument tick size")
    lp.add_argument("--capital", default="1000000", help="starting capital")
    lp.add_argument("--config", help="strategy YAML (defaults to built-in defaults)")
    lp.add_argument("--no-shorts", action="store_true", help="disable SELL side")
    lp.add_argument(
        "--live",
        action="store_true",
        help="place REAL orders (also requires ALLOW_LIVE_TRADING=true)",
    )
    lp.set_defaults(func=cmd_live)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
