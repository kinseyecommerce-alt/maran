"""SELL strategy binding — the exact mirror of BUY, sharing the same engine/rules.

No strategy logic is duplicated; this only fixes direction=SELL (`long_enabled=False`).
"""

from __future__ import annotations

from app.core.enums import Side
from app.strategy.config import StrategyConfig
from app.strategy.models import Candle, Signal
from app.strategy.signal_engine import SignalEngine


class SellStrategy:
    def __init__(self, config: StrategyConfig | None = None, **kw: object) -> None:
        cfg = (config or StrategyConfig()).model_copy(
            update={"long_enabled": False, "short_enabled": True}
        )
        self.engine = SignalEngine(cfg, **kw)  # type: ignore[arg-type]

    side = Side.SELL

    def evaluate(self, symbol: str, candles: list[Candle], **kw: object) -> Signal | None:
        return self.engine.evaluate(symbol, candles, **kw)  # type: ignore[arg-type]

    def reset(self, symbol: str | None = None) -> None:
        self.engine.reset(symbol)
