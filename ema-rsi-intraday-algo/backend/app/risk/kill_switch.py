"""Emergency kill switch. Once tripped, the system stays locked until a manual reset.

Phase 2 models the state and gating; the order-cancellation / flatten actions are
wired to the broker adapter in Phase 3.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class KillSwitch:
    engaged: bool = False
    reason: str | None = None
    engaged_at: datetime | None = None

    def engage(self, reason: str, at: datetime) -> None:
        self.engaged = True
        self.reason = reason
        self.engaged_at = at

    def reset(self) -> None:
        self.engaged = False
        self.reason = None
        self.engaged_at = None

    @property
    def blocks_new_entries(self) -> bool:
        return self.engaged
