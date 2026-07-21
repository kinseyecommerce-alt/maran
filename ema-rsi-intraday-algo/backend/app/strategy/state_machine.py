"""Deterministic per-(symbol, direction) signal state machine.

Every transition is validated and recorded. Terminal states cannot be left without
an explicit `reset()`. The recorded transition log is the audit trail and the basis
for restart reconstruction (Phase 3)."""

from __future__ import annotations

from datetime import datetime

from app.core.enums import TERMINAL_STATES, Side, SignalState
from app.core.exceptions import StateTransitionError
from app.strategy.models import StateTransition


class SignalStateMachine:
    """Holds the current state for one symbol+direction and its transition history."""

    def __init__(self, symbol: str, direction: Side, correlation_id: str) -> None:
        self.symbol = symbol
        self.direction = direction
        self.correlation_id = correlation_id
        self.state: SignalState = SignalState.IDLE
        self.history: list[StateTransition] = []

    def transition(
        self,
        new_state: SignalState,
        *,
        event: str,
        reason: str,
        at: datetime,
        signal_id: str | None = None,
        trade_id: str | None = None,
        metadata: dict | None = None,
    ) -> StateTransition:
        if self.state in TERMINAL_STATES and new_state not in (
            SignalState.IDLE,
            SignalState.SESSION_READY,
        ):
            raise StateTransitionError(
                f"{self.symbol}/{self.direction}: cannot leave terminal {self.state} → {new_state}"
            )
        rec = StateTransition(
            symbol=self.symbol,
            direction=self.direction,
            previous_state=self.state,
            new_state=new_state,
            event=event,
            reason=reason,
            timestamp=at,
            correlation_id=self.correlation_id,
            signal_id=signal_id,
            trade_id=trade_id,
            metadata=metadata or {},
        )
        self.state = new_state
        self.history.append(rec)
        return rec

    def reset(self, at: datetime, reason: str = "session_reset") -> None:
        """Return to IDLE (daily session reset / after a terminal outcome)."""
        prev = self.state
        self.state = SignalState.IDLE
        self.history.append(
            StateTransition(
                symbol=self.symbol,
                direction=self.direction,
                previous_state=prev,
                new_state=SignalState.IDLE,
                event="reset",
                reason=reason,
                timestamp=at,
                correlation_id=self.correlation_id,
            )
        )
