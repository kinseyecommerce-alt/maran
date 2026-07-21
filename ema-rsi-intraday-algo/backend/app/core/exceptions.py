"""Typed domain exceptions. Errors are raised explicitly and never swallowed."""

from __future__ import annotations


class AlgoError(Exception):
    """Base class for all application errors."""


class ConfigError(AlgoError):
    """Invalid or missing configuration."""


class InsufficientHistoryError(AlgoError):
    """Not enough completed candles to compute indicators / evaluate the setup."""


class PreviousDayLevelsUnavailableError(AlgoError):
    """Previous valid trading session high/low could not be resolved."""


class LookAheadError(AlgoError):
    """A forming/future candle was used where only completed candles are allowed."""


class StateTransitionError(AlgoError):
    """An illegal signal-state transition was attempted."""


class RiskRejectionError(AlgoError):
    """A trade was rejected by a risk gate. Carries a machine-readable reason."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


class LiveModeNotPermittedError(AlgoError):
    """LIVE mode requested but a safety gate failed. Default posture: refuse."""
