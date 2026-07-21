"""Signal state machine tests: transitions recorded, terminal guard, reset."""

from datetime import datetime

import pytest

from app.core.enums import Side, SignalState
from app.core.exceptions import StateTransitionError
from app.strategy.state_machine import SignalStateMachine


def _sm():
    return SignalStateMachine("RELIANCE", Side.BUY, correlation_id="test-corr")


def test_transition_records_history():
    sm = _sm()
    at = datetime(2026, 7, 17, 9, 30)
    sm.transition(SignalState.WAITING_FOR_CONFIRMATION, event="focus", reason="armed", at=at)
    assert sm.state is SignalState.WAITING_FOR_CONFIRMATION
    assert len(sm.history) == 1
    rec = sm.history[0]
    assert rec.previous_state is SignalState.IDLE
    assert rec.new_state is SignalState.WAITING_FOR_CONFIRMATION
    assert rec.correlation_id == "test-corr"
    assert rec.symbol == "RELIANCE" and rec.direction is Side.BUY


def test_terminal_state_cannot_be_left_except_reset():
    sm = _sm()
    at = datetime(2026, 7, 17, 9, 30)
    sm.transition(SignalState.SIGNAL_EXPIRED, event="expire", reason="late", at=at)
    with pytest.raises(StateTransitionError):
        sm.transition(SignalState.POSITION_OPEN, event="x", reason="y", at=at)


def test_reset_returns_to_idle_and_logs():
    sm = _sm()
    at = datetime(2026, 7, 17, 9, 30)
    sm.transition(SignalState.TRADE_REJECTED, event="reject", reason="risk", at=at)
    sm.reset(at)
    assert sm.state is SignalState.IDLE
    assert sm.history[-1].event == "reset"


def test_terminal_can_reset_via_transition_to_idle():
    sm = _sm()
    at = datetime(2026, 7, 17, 9, 30)
    sm.transition(SignalState.POSITION_CLOSED, event="close", reason="target", at=at)
    # transitioning a terminal state back to IDLE/SESSION_READY is allowed
    sm.transition(SignalState.SESSION_READY, event="new_session", reason="reset", at=at)
    assert sm.state is SignalState.SESSION_READY
