"""Data-loader tests: CSV, Kite JSON, timestamp parsing, error handling."""

import json
from datetime import datetime
from decimal import Decimal

import pytest

from app.market_data.loaders import (
    load_candles_csv,
    load_candles_kite_json,
    parse_timestamp,
)

CSV = """timestamp,symbol,open,high,low,close,volume
2026-07-17 09:15:00,RELIANCE,100.0,101.0,99.5,100.5,1000
2026-07-17 09:18:00,RELIANCE,100.5,102.0,100.0,101.8,1200
2026-07-17 09:21:00,RELIANCE,101.8,101.9,100.1,100.4,900
"""


def test_parse_timestamp_forms():
    assert parse_timestamp("2026-07-17T09:15:00") == datetime(2026, 7, 17, 9, 15)
    assert parse_timestamp("2026-07-17 09:15:00") == datetime(2026, 7, 17, 9, 15)
    assert parse_timestamp("2026-07-17T09:15:00+05:30") == datetime(
        2026, 7, 17, 9, 15
    )  # tz dropped
    # epoch
    assert isinstance(parse_timestamp(1783000000), datetime)


def test_load_csv_basic():
    data = load_candles_csv(CSV)
    assert set(data) == {"RELIANCE"}
    c = data["RELIANCE"]
    assert len(c) == 3
    assert c[0].open == Decimal("100.0") and c[0].close == Decimal("100.5")
    assert c[0].volume == 1000
    assert c[0].session_date == datetime(2026, 7, 17).date()
    # sorted chronologically
    assert c[0].timestamp < c[1].timestamp < c[2].timestamp


def test_load_csv_without_symbol_column_uses_arg():
    csv_no_sym = "datetime,open,high,low,close,volume\n2026-07-17 09:15:00,10,11,9,10.5,5\n"
    data = load_candles_csv(csv_no_sym, symbol="TCS")
    assert set(data) == {"TCS"}


def test_load_csv_missing_columns_raises():
    with pytest.raises(ValueError):
        load_candles_csv("timestamp,open\n2026-07-17 09:15:00,100\n")


def test_load_kite_json():
    payload = json.dumps(
        {
            "data": {
                "candles": [
                    ["2026-07-17T09:15:00+0530", 100, 101, 99, 100.5, 1000],
                    ["2026-07-17T09:18:00+0530", 100.5, 102, 100, 101.5, 1100],
                ]
            }
        }
    )
    data = load_candles_kite_json(payload, symbol="INFY")
    assert len(data["INFY"]) == 2
    assert data["INFY"][0].high == Decimal("101")


def test_load_kite_json_bare_list():
    payload = json.dumps([["2026-07-17T09:15:00", 100, 101, 99, 100.5, 1000]])
    data = load_candles_kite_json(payload, symbol="SBIN")
    assert len(data["SBIN"]) == 1
