"""Config loading tests: typed defaults, YAML load, safety posture."""

from decimal import Decimal

from app.core.config import DEFAULT_STRATEGY_YAML, load_strategy_config
from app.core.enums import BreakEvenMode, RsiMode, TradingMode
from app.strategy.config import StrategyConfig


def test_defaults_match_spec():
    cfg = StrategyConfig()
    assert cfg.default_mode is TradingMode.SIMULATION  # safe default
    assert cfg.ema_periods.ordered() == [55, 89, 144, 233]
    assert cfg.rsi.buy_mode is RsiMode.SUPPORT_ZONE_REJECTION
    assert cfg.rsi.buy_zone_min == 38.0 and cfg.rsi.buy_zone_max == 42.0
    assert cfg.trade_management.break_even_trigger_R == Decimal("1.5")
    assert cfg.trade_management.partial_exit_R == Decimal("2.0")
    assert cfg.trade_management.final_target_R == Decimal("3.0")
    assert cfg.stop.maximum_stop_percentage == Decimal("1.0")


def test_min_history_seats_slow_ema():
    cfg = StrategyConfig()
    assert cfg.min_history >= cfg.ema_periods.slow + 5


def test_load_from_default_yaml():
    cfg = load_strategy_config(DEFAULT_STRATEGY_YAML)
    assert cfg.strategy_name == "EMA RSI Intraday"
    assert cfg.timeframe == "3m"
    assert cfg.default_mode is TradingMode.SIMULATION
    # yaml uses "entry_plus_one_tick" alias → coerced to ENTRY_PLUS_TICKS
    assert cfg.trade_management.break_even_mode is BreakEvenMode.ENTRY_PLUS_TICKS


def test_load_missing_path_uses_defaults():
    cfg = load_strategy_config("does/not/exist.yaml")
    assert cfg.strategy_name == "EMA RSI Intraday"
