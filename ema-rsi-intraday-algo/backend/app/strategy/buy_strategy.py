"""BUY strategy binding.

The BUY rules are the direction=BUY specialisation of the shared engine/rules — no
strategy logic is duplicated here. This module exists so callers (and the tests)
have an explicit, discoverable BUY entry point, and to force `short_enabled=False`.
"""

from __future__ import annotations

from app.core.enums import Side
from app.strategy.config import StrategyConfig
from app.strategy.models import Candle, Signal
from app.strategy.signal_engine import SignalEngine


class BuyStrategy:
    def __init__(self, config: StrategyConfig | None = None, **kw: object) -> None:
        cfg = (config or StrategyConfig()).model_copy(
            update={"long_enabled": True, "short_enabled": False}
        )
        self.engine = SignalEngine(cfg, **kw)  # type: ignore[arg-type]

    side = Side.BUY

    def evaluate(self, symbol: str, candles: list[Candle], **kw: object) -> Signal | None:
        return self.engine.evaluate(symbol, candles, **kw)  # type: ignore[arg-type]

    def reset(self, symbol: str | None = None) -> None:
        self.engine.reset(symbol)
