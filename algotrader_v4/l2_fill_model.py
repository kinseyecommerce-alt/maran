"""
l2_fill_model.py — estimate fill quality from L2 order-book depth.

A MARKET order does not fill at the LTP; it walks the book. On a thin
book a large order eats several price levels and suffers real slippage —
exactly the kind of cost that turns a backtested edge into a live loss.
tick_engine already captures top-5 `bid_depth` / `ask_depth`; this module
turns that depth into two numbers an agent can act on BEFORE sending the
order:

  fill_prob     fraction of the requested quantity the visible book can
                absorb (1.0 = book has enough resting size at the top
                levels; < 1.0 = order would exhaust visible depth)
  slippage_bps  estimated VWAP slippage of the filled portion vs LTP,
                in basis points

When depth is unavailable (PAPER / simulation / feed without L2) the model
returns a neutral (1.0, 0.0) so callers never block on missing data.

A BUY lifts the ASK side; a SELL hits the BID side.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FillEstimate:
    fill_prob:    float   # 0.0–1.0 fraction of qty the visible book can fill
    slippage_bps: float   # estimated VWAP slippage vs LTP, basis points
    avg_price:    float   # VWAP of the fillable portion
    levels_used:  int     # how many price levels the order would consume


def _normalise_depth(depth) -> list[tuple[float, int]]:
    """Accept [(price, qty), ...] or [{'price':, 'quantity':}, ...]."""
    out: list[tuple[float, int]] = []
    for lvl in depth or ():
        try:
            if isinstance(lvl, dict):
                px = float(lvl.get("price", 0) or 0)
                qty = int(lvl.get("quantity", lvl.get("qty", 0)) or 0)
            else:
                px = float(lvl[0])
                qty = int(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
        if px > 0 and qty > 0:
            out.append((px, qty))
    return out


def estimate_fill(
    side: str,
    quantity: int,
    bid_depth,
    ask_depth,
    ltp: float,
) -> FillEstimate:
    """Estimate how a MARKET `side` order for `quantity` would fill against
    the visible book. Neutral (1.0, 0.0) when depth or inputs are missing."""
    if quantity <= 0 or not ltp or ltp <= 0:
        return FillEstimate(1.0, 0.0, ltp or 0.0, 0)

    # BUY consumes offers (ask side); SELL consumes bids.
    book = _normalise_depth(ask_depth if side == "BUY" else bid_depth)
    if not book:
        return FillEstimate(1.0, 0.0, ltp, 0)   # unknown depth → do not block

    # Ask ascending (cheapest first), bid descending (highest first).
    book.sort(key=lambda x: x[0], reverse=(side != "BUY"))

    remaining = quantity
    notional = 0.0
    filled = 0
    levels = 0
    for px, avail in book:
        if remaining <= 0:
            break
        take = min(remaining, avail)
        notional += take * px
        filled += take
        remaining -= take
        levels += 1

    if filled <= 0:
        return FillEstimate(0.0, 0.0, ltp, 0)

    avg_price = notional / filled
    fill_prob = filled / quantity
    slip = abs(avg_price - ltp) / ltp * 10_000.0
    return FillEstimate(round(fill_prob, 4), round(slip, 2),
                        round(avg_price, 4), levels)


def is_book_acceptable(
    side: str,
    quantity: int,
    bid_depth,
    ask_depth,
    ltp: float,
    min_fill_prob: float,
    max_slippage_bps: float,
) -> tuple[bool, FillEstimate]:
    """Convenience gate: returns (ok, estimate). ok=True when the visible
    book can fill at least `min_fill_prob` of the order within
    `max_slippage_bps` of slippage (or when depth is unavailable)."""
    est = estimate_fill(side, quantity, bid_depth, ask_depth, ltp)
    if est.levels_used == 0:
        return True, est   # no depth data — neutral pass
    ok = est.fill_prob >= min_fill_prob and est.slippage_bps <= max_slippage_bps
    return ok, est
