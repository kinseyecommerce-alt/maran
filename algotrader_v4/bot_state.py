"""
bot_state.py — shared mutable runtime state (no circular imports).
Both main.py and master_agent_v5.py import from here.
"""

_agent_enabled: dict[str, bool] = {
    "intraday": True,
    "options":  True,
    "futures":  True,
    "swing":    True,
    "scalping": True,
}

# Pattern toggles — all enabled by default; runtime-mutable via /settings/pattern-toggles
_pattern_enabled: dict[str, dict[str, bool]] = {
    "intraday": {p: True for p in [
        "VWAP_TREND", "EMA_PULLBACK", "ORB_BREAK", "BREAKOUT", "VWAP_RECLAIM",
        "TTM_SQUEEZE", "VWAP_BAND_REVERT",
        "STOCHRSI_CROSS", "HMA_FLIP", "WILLIAMS_REVERSAL", "GAP_PLAY",
    ]},
    "scalping": {p: True for p in [
        "EMA9_CROSS", "EMA921_CROSS", "VWAP_BOUNCE", "SURGE", "ORB",
        "SUPERTREND_FLIP", "STOCHRSI_EXTREME", "WILLIAMS_SCALP", "HMA_MICRO",
    ]},
    "options": {p: True for p in [
        "EMA_CROSS", "TREND_PULL", "ORB", "VWAP_RECLAIM", "BB_SQUEEZE",
        "RSI_EXTREME", "SURGE",
        "ICHIMOKU_CLOUD", "STOCHRSI_OPTIONS", "WILLIAMS_OPTIONS",
    ]},
    "futures": {p: True for p in [
        "EMA_TREND", "ORB_FUTURES", "VWAP_PULL", "MACD_CROSS", "ATR_BREAK",
        "HMA_TREND", "STOCHRSI_FUTURES", "ICHIMOKU_FUTURES",
    ]},
    "swing": {p: True for p in [
        "EMA50_BOUNCE", "EMA50_SHORT", "MACD_SWING",
    ]},
}


def is_agent_enabled(name: str) -> bool:
    return _agent_enabled.get(name, True)


def set_agent_enabled(name: str, val: bool) -> None:
    if name in _agent_enabled:
        _agent_enabled[name] = val


def is_pattern_enabled(agent: str, pattern: str) -> bool:
    return _pattern_enabled.get(agent, {}).get(pattern, True)


def set_pattern_enabled(agent: str, pattern: str, val: bool) -> None:
    if agent in _pattern_enabled and pattern in _pattern_enabled[agent]:
        _pattern_enabled[agent][pattern] = val


def get_all_pattern_toggles() -> dict[str, dict[str, bool]]:
    return {a: dict(p) for a, p in _pattern_enabled.items()}
