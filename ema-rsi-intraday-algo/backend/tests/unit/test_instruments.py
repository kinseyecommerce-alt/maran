"""Instrument-token resolution from the Kite instrument master."""

from app.live.instruments import build_token_maps


class _Inst:
    def __init__(self, tradingsymbol, exchange, instrument_token):
        self.tradingsymbol = tradingsymbol
        self.exchange = exchange
        self.instrument_token = instrument_token


def test_resolves_equity_and_index_alias():
    dump = [
        {"tradingsymbol": "RELIANCE", "exchange": "NSE", "instrument_token": 738561},
        {"tradingsymbol": "NIFTY 50", "exchange": "NSE", "instrument_token": 256265},
        {"tradingsymbol": "TCS", "exchange": "BSE", "instrument_token": 111},  # wrong exchange
    ]
    s2t, t2s = build_token_maps(dump, ["RELIANCE", "NIFTY", "TCS"])
    assert s2t["RELIANCE"] == 738561
    assert s2t["NIFTY"] == 256265  # matched via the "NIFTY 50" alias
    assert "TCS" not in s2t  # BSE row ignored (NSE/NFO only)
    assert t2s[256265] == "NIFTY"


def test_accepts_objects_and_ignores_unresolvable_tokens():
    dump = [
        _Inst("INFY", "NSE", 408065),
        _Inst("BADTOK", "NSE", "not-an-int"),
    ]
    s2t, _ = build_token_maps(dump, ["INFY", "BADTOK"])
    assert s2t == {"INFY": 408065}


def test_first_match_wins_no_duplicates():
    dump = [
        {"tradingsymbol": "SBIN", "exchange": "NSE", "instrument_token": 779521},
        {"tradingsymbol": "SBIN", "exchange": "NSE", "instrument_token": 999999},
    ]
    s2t, _ = build_token_maps(dump, ["SBIN"])
    assert s2t == {"SBIN": 779521}
