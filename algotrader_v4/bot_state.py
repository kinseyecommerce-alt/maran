"""
bot_state.py — shared mutable runtime state (no circular imports).
Both main.py and master_agent_v5.py import from here.
"""

_agent_enabled: dict[str, bool] = {
    "intraday":        True,
    "options":         True,
    "futures":         True,
    "swing":           True,
    "scalping":        True,
    "momentum":        True,
    "mean_reversion":  True,
    "pairs":           True,
    "option_scalping": True,
}

# Pattern toggles — all enabled by default; runtime-mutable via /settings/pattern-toggles
_pattern_enabled: dict[str, dict[str, bool]] = {
    "intraday": {p: True for p in [
        "VWAP_TREND", "EMA_PULLBACK", "ORB_BREAK", "BREAKOUT", "VWAP_RECLAIM",
        "TTM_SQUEEZE", "VWAP_BAND_REVERT",
        "STOCHRSI_CROSS", "HMA_FLIP", "WILLIAMS_REVERSAL", "GAP_PLAY",
        "PREV_DAY_LEVEL", "MOMENTUM_SURGE", "GAP_FADE", "TREND_DAY_RIDE",
        "BAND_WALK_PULLBACK", "KELTNER_RIDE", "VWAP_EXT_RIDE", "HIGH_TIGHT_FLAG",
    ]},
    "scalping": {p: True for p in [
        "EMA9X", "EMA921X", "VWAP_BOUNCE", "SURGE", "ORB",
        "SUPERTREND_FLIP", "STOCHRSI_EXTREME", "WILLIAMS_SCALP", "HMA_MICRO",
        "VWAP_SCALP", "EMA9_MOMENTUM", "SQUEEZE_RELEASE", "MICROTREND",
        "MACD_MICRO", "DEPTH_PULSE",
        "BAND_WALK_3X", "SUPERTREND_PULLBACK", "MOMENTUM_STACK", "RANGE_BREAK_RETEST",
    ]},
    "options": {p: True for p in [
        "EMA_CROSS", "TREND_PULL", "ORB", "VWAP_RECLAIM", "BB_SQUEEZE",
        "RSI_EXTREME", "SURGE",
        "ICHIMOKU_CLOUD", "STOCHRSI_OPTIONS", "WILLIAMS_OPTIONS",
        "EXPIRY_SCALP", "BB_WALK_OPT",
        "MORNING_THRUST_OPT", "STOCHRSI_TREND_OPT", "INDEX_TREND_RIDE_OPT",
        "POWER_HOUR_OPT",
    ]},
    "futures": {p: True for p in [
        "EMA_TREND", "ORB_FUTURES", "VWAP_PULL", "MACD_CROSS", "ATR_BREAK",
        "HMA_TREND", "STOCHRSI_FUTURES", "ICHIMOKU_FUTURES",
        "VOL_SURGE", "MULTI_TF_ALIGN", "EMA200_BOUNCE", "BB_WALK_FUT",
        "OPEN_DRIVE_FUT", "VWAP_MAGNET_FADE", "SQUEEZE_WALK_FUT", "ADX_TREND_RIDE",
    ]},
    "swing": {p: True for p in [
        "EMA50_BOUNCE", "EMA50_SHORT", "MACD_SWING",
        "EMA21_PULLBACK_SWING", "MACD_ZERO_TURN_SWING",
    ]},
    "momentum": {p: True for p in [
        "RELATIVE_STRENGTH", "RS_BREAKOUT",
    ]},
    "mean_reversion": {p: True for p in [
        "EOD_REVERSION", "LATE_DAY_VWAP_REVERT",
    ]},
}

# settings.disabled_patterns kill-list ("agent:PATTERN,agent:PATTERN") parsed
# lazily and cached against the raw string, since settings mutate at runtime.
_disabled_src: str | None = None
_disabled_set: set[str] = set()


def _settings_disabled() -> set[str]:
    global _disabled_src, _disabled_set
    try:
        from config import settings
        raw = getattr(settings, "disabled_patterns", "") or ""
    except Exception:
        raw = ""
    if raw != _disabled_src:
        _disabled_src = raw
        _disabled_set = {p.strip().upper() for p in raw.split(",") if p.strip()}
    return _disabled_set


def is_agent_enabled(name: str) -> bool:
    return _agent_enabled.get(name, True)


def set_agent_enabled(name: str, val: bool) -> None:
    if name in _agent_enabled:
        _agent_enabled[name] = val
        # Persist — a deliberately disabled agent must NOT come back enabled
        # after a deploy restart (observed live 2026-07-13: scalping, paused
        # mid-day for risk, silently resumed when the evening deploy rebooted).
        try:
            import json as _json
            from state_store import set_kv as _set_kv
            _set_kv("agent_enabled_map", _json.dumps(_agent_enabled))
        except Exception:
            pass


def load_persisted_enables() -> None:
    """Reload persisted agent enables on boot — call BEFORE auto-start."""
    try:
        import json as _json
        from state_store import get_kv as _get_kv
        raw = _get_kv("agent_enabled_map", "")
        if raw:
            for k, v in _json.loads(raw).items():
                if k in _agent_enabled:
                    _agent_enabled[k] = bool(v)
    except Exception:
        pass


# ── Regime entry gate ────────────────────────────────────────────────────────
# master_agent publishes the confirmed regime here; base_agent consults the
# settings-driven block matrix before every new entry. Same lazy-parse/cache
# pattern as the kill-list so runtime settings mutations apply immediately.
_current_regime: str = "UNKNOWN"
_regime_block_src: str | None = None
_regime_block_map: dict[str, set[str]] = {}


def set_current_regime(regime: str) -> None:
    global _current_regime
    _current_regime = (regime or "UNKNOWN").upper()


def get_current_regime() -> str:
    return _current_regime


def _regime_blocks() -> dict[str, set[str]]:
    global _regime_block_src, _regime_block_map
    try:
        from config import settings
        raw = getattr(settings, "regime_blocked_agents", "") or ""
        if not getattr(settings, "regime_agent_gating", True):
            raw = ""
    except Exception:
        raw = ""
    if raw != _regime_block_src:
        _regime_block_src = raw
        m: dict[str, set[str]] = {}
        for part in raw.split(";"):
            if ":" not in part:
                continue
            reg, agents = part.split(":", 1)
            m[reg.strip().upper()] = {a.strip().lower() for a in agents.split(",") if a.strip()}
        _regime_block_map = m
    return _regime_block_map


_current_trade_date = None   # date; replay stamps historical days, live uses IST clock


def set_current_trade_date(d) -> None:
    global _current_trade_date
    _current_trade_date = d


def _today():
    if _current_trade_date is not None:
        return _current_trade_date
    from ist_clock import now_ist
    return now_ist().date()


def _expiry_day_blocks() -> set[str]:
    """Extra agent bench on index weekly-expiry days, independent of regime.
    2026-07-14 (Tuesday expiry): the whipsaw flipped the regime label all day
    and every flip to a BULL label re-enabled options at the worst moments —
    15 premium-buying entries bled ₹8.7k into gamma pinning. Regime hysteresis
    cannot save an agent whose habitat is simply absent on expiry days."""
    try:
        from config import settings
        raw = getattr(settings, "expiry_day_blocked_agents", "") or ""
        if not raw:
            return set()
        wds: set[int] = set()
        for part in (getattr(settings, "index_expiry_weekdays", "") or "").split(","):
            if ":" in part:
                try:
                    wds.add(int(part.split(":", 1)[1]))
                except ValueError:
                    pass
        if _today().weekday() in wds:
            return {a.strip().lower() for a in raw.split(",") if a.strip()}
    except Exception:
        pass
    return set()


def is_agent_allowed_in_regime(agent: str) -> bool:
    """Entry gate: False when the current confirmed regime is one where the
    62-day replay evidence says this agent loses, or when today is an index
    expiry day and the agent is on the expiry-day bench. Open positions
    are unaffected."""
    a = agent.lower()
    if a in _expiry_day_blocks():
        return False
    return a not in _regime_blocks().get(_current_regime, set())


def is_pattern_enabled(agent: str, pattern: str) -> bool:
    if f"{agent.upper()}:{pattern.upper()}" in _settings_disabled():
        return False
    return _pattern_enabled.get(agent, {}).get(pattern, True)


def set_pattern_enabled(agent: str, pattern: str, val: bool) -> None:
    if agent in _pattern_enabled and pattern in _pattern_enabled[agent]:
        _pattern_enabled[agent][pattern] = val


def get_all_pattern_toggles() -> dict[str, dict[str, bool]]:
    return {a: dict(p) for a, p in _pattern_enabled.items()}
