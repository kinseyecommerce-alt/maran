"""Position reconciliation + restart recovery.

Reconciliation compares locally-tracked positions against the broker's and reports
mismatches. On any mismatch the caller enters RECONCILIATION_REQUIRED and blocks
fresh entries. Restart recovery rebuilds local positions from the broker as the
source of truth after a process restart.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.brokers.interface import BrokerAdapter


@dataclass(frozen=True)
class Mismatch:
    symbol: str
    local_qty: int
    broker_qty: int

    @property
    def detail(self) -> str:
        return f"{self.symbol}: local {self.local_qty} vs broker {self.broker_qty}"


def reconcile(local_positions: dict[str, int], broker: BrokerAdapter) -> list[Mismatch]:
    """Compare local signed quantities with the broker's. Empty list ⇒ in sync."""
    broker_qty = {p.symbol: p.quantity for p in broker.get_positions()}
    symbols = set(local_positions) | set(broker_qty)
    out: list[Mismatch] = []
    for sym in sorted(symbols):
        lq = local_positions.get(sym, 0)
        bq = broker_qty.get(sym, 0)
        if lq != bq:
            out.append(Mismatch(sym, lq, bq))
    return out


def restart_recovery(broker: BrokerAdapter) -> dict[str, int]:
    """Rebuild local positions from the broker (source of truth) after a restart."""
    return {p.symbol: p.quantity for p in broker.get_positions() if p.quantity != 0}
