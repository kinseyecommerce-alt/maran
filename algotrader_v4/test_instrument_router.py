"""Offline unit tests for instrument_router."""
from datetime import date
import instrument_router as ir


def test_index_buy_routes_to_atm_ce():
    r = ir.route("NIFTY", "BUY", 24812, today=date(2026, 7, 20))
    assert r["order_action"] == "BUY"          # always buy the option
    assert r["opt_type"] == "CE"
    assert r["strike"] == 24800                 # ATM at step 50
    assert r["exchange"] == "NFO"
    assert r["premium_option"] is True
    assert r["option_symbol"].startswith("NIFTY") and r["option_symbol"].endswith("24800CE")
    assert r["lot_size"] == 75
    print("  index BUY → ATM CE:", r["option_symbol"], "lot", r["lot_size"])


def test_index_sell_routes_to_atm_pe_still_buy():
    r = ir.route("BANKNIFTY", "SELL", 52140, today=date(2026, 7, 20))
    assert r["order_action"] == "BUY"          # buy the PUT, not sell
    assert r["opt_type"] == "PE"
    assert r["strike"] == 52100                 # step 100 (>30000)
    assert r["option_symbol"].endswith("52100PE")
    assert r["premium_option"] is True
    print("  index SELL → ATM PE (buy):", r["option_symbol"])


def test_fno_stock_routes_to_future():
    r = ir.route("RELIANCE", "SELL", 1530, today=date(2026, 7, 20))
    assert r["order_action"] == "SELL"          # short the future
    assert r["exchange"] == "NFO"
    assert r["premium_option"] is False
    assert r["futures_symbol"].startswith("RELIANCE") and r["futures_symbol"].endswith("FUT")
    assert r["lot_size"] == 250
    print("  stock SELL → future:", r["futures_symbol"], "lot", r["lot_size"])


def test_mcx_routes_to_commodity_future():
    r = ir.route("CRUDEOIL", "BUY", 5600, today=date(2026, 7, 20))
    assert r["order_action"] == "BUY"
    assert r["exchange"] == "MCX"
    assert r["futures_symbol"].startswith("CRUDEOIL") and r["futures_symbol"].endswith("FUT")
    assert r["lot_size"] == 100
    print("  MCX BUY → future:", r["futures_symbol"], "lot", r["lot_size"])


def test_non_fno_trades_underlying():
    r = ir.route("SOMEMIDCAP", "BUY", 400, today=date(2026, 7, 20))
    assert r["order_action"] == "BUY"
    assert r["exchange"] == "NSE"
    assert "option_symbol" not in r and "futures_symbol" not in r
    print("  non-F&O → underlying cash")


def test_atm_strike_math():
    assert ir.atm_strike(24812, 50) == 24800
    assert ir.atm_strike(24838, 50) == 24850
    assert ir.atm_strike(52140, 100) == 52100
    print("  atm_strike math OK")


if __name__ == "__main__":
    fns = [test_atm_strike_math, test_index_buy_routes_to_atm_ce,
           test_index_sell_routes_to_atm_pe_still_buy, test_fno_stock_routes_to_future,
           test_mcx_routes_to_commodity_future, test_non_fno_trades_underlying]
    for fn in fns:
        print(f"• {fn.__name__}"); fn()
    print(f"\n{len(fns)}/{len(fns)} passed")
