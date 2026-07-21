"""Runner + CLI integration: load real-shaped CSV → backtest/paper → report."""

import csv
import io
from decimal import Decimal

import yaml

from app.market_data.loaders import load_candles_csv
from app.services.runner import format_summary, run_backtest, run_paper, write_reports
from tests.fixtures.scenarios import build_buy_scenario, wide_session_config


def _scenario_csv() -> str:
    cfg = wide_session_config()
    cfg.trade_management.partial_exit_enabled = False
    candles = build_buy_scenario("target_3R", cfg)["RELIANCE"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "symbol", "open", "high", "low", "close", "volume"])
    for c in candles:
        w.writerow([c.timestamp.isoformat(), "RELIANCE", c.open, c.high, c.low, c.close, c.volume])
    return buf.getvalue()


def test_run_backtest_over_loaded_csv():
    data = load_candles_csv(_scenario_csv())
    cfg = wide_session_config()
    cfg.trade_management.partial_exit_enabled = False
    result, metrics = run_backtest(data, cfg, capital=Decimal("1000000"))
    assert metrics["total_trades"] == 1
    assert metrics["winning_trades"] == 1
    assert round(metrics["average_R"], 2) == 3.00
    summary = format_summary(metrics, days=2)
    assert "BACKTEST SUMMARY" in summary and "Win rate" in summary


def test_run_paper_over_loaded_csv():
    data = load_candles_csv(_scenario_csv())
    cfg = wide_session_config()
    cfg.trade_management.partial_exit_enabled = False
    res = run_paper(data, cfg, capital=Decimal("1000000"))
    assert len(res.trades) == 1
    assert res.reconciled_flat


def test_write_reports(tmp_path):
    data = load_candles_csv(_scenario_csv())
    cfg = wide_session_config()
    cfg.trade_management.partial_exit_enabled = False
    result, metrics = run_backtest(data, cfg)
    csv_path, json_path = write_reports(result, metrics, tmp_path / "out")
    assert csv_path.exists() and json_path.exists()
    assert "symbol,side,entry_time" in csv_path.read_text().splitlines()[0]


def test_cli_main_backtest(tmp_path, capsys):
    from scripts.algo import main

    csv_path = tmp_path / "RELIANCE_3min.csv"
    csv_path.write_text(_scenario_csv())
    cfg_dict = {
        "session": {
            "entry_start": "00:00",
            "entry_cutoff": "23:59",
            "forced_square_off": "23:58",
            "final_square_off": "23:59",
        },
        "trade_management": {"partial_exit_enabled": False},
    }
    cfg_path = tmp_path / "wide.yaml"
    cfg_path.write_text(yaml.safe_dump(cfg_dict))
    rc = main(["backtest", "--csv", str(csv_path), "--config", str(cfg_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "BACKTEST SUMMARY" in out
    assert "Trades          1" in out
