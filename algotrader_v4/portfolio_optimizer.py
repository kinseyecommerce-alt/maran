"""
portfolio_optimizer.py
Mean-variance portfolio optimizer for AlgoTrader Pro v4.
Runs every 15 minutes via master_agent_v5 scheduler.

Given pending signals from all 8 agents, allocates capital optimally:
  - Maximizes Sharpe ratio subject to constraints
  - Respects per-symbol max, sector weights, portfolio beta cap
  - Returns {symbol: capital_allocation} dict for _try_enter()

Constraints:
  - Sum of allocations <= total_intraday_capital
  - Per-symbol allocation <= settings.max_position_size
  - Portfolio beta <= settings.max_portfolio_beta
  - No single sector > 35% of total capital
  - Max settings.max_open_positions active at once
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from loguru import logger

from config import settings

# ── Data paths ────────────────────────────────────────────────────────────────
_BASE = Path(__file__).parent
_SECTOR_MAP_PATH  = _BASE / "sector_map.json"
_BETA_MAP_PATH    = _BASE / "data" / "stock_betas.json"

_DEFAULT_BETA   = 1.0
_DEFAULT_SECTOR = "OTHER"
_SECTOR_CAP     = 0.35   # max 35% of capital in any single sector


def _load_json(path: Path) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception as exc:
        logger.warning("[portfolio_optimizer] Could not load {}: {}", path.name, exc)
        return {}


class PortfolioOptimizer:
    """
    Mean-variance (Sharpe-maximising) portfolio optimizer.

    Usage
    -----
    Call ``optimize(signals, capital)`` every 15 minutes to get a
    {symbol: rupee_allocation} dict.  Agents can call ``should_trade()``
    inside ``_try_enter()`` to check whether their signal is competitive.
    """

    def __init__(self) -> None:
        self._sector_map: dict[str, str] = _load_json(_SECTOR_MAP_PATH)
        self._beta_map:   dict[str, float] = _load_json(_BETA_MAP_PATH)
        # Latest optimization result — shared across agents.
        self._latest_allocations: dict[str, float] = {}
        # Sharpe score per symbol from last optimization run.
        self._latest_sharpes: dict[str, float] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def optimize(
        self,
        signals: list[dict],   # [{"symbol", "score", "atr_pct", "agent"}]
        capital: float,        # total capital to allocate
    ) -> dict[str, float]:
        """
        Return {symbol: rupee_allocation}.

        Symbols absent from the result should not be traded this cycle.
        Falls back to equal-weight if scipy is unavailable or N < 2.
        """
        if not signals:
            self._latest_allocations = {}
            self._latest_sharpes = {}
            return {}

        if len(signals) == 1:
            sym = signals[0]["symbol"]
            alloc = min(capital, settings.max_position_size)
            self._latest_allocations = {sym: alloc}
            self._latest_sharpes = {sym: self._sharpe_score(signals[0])}
            return dict(self._latest_allocations)

        # Deduplicate by symbol — keep highest score if two agents flag same symbol
        seen: dict[str, dict] = {}
        for sig in signals:
            sym = sig["symbol"]
            if sym not in seen or sig.get("score", 0) > seen[sym].get("score", 0):
                seen[sym] = sig
        signals = list(seen.values())

        # Cap at max_open_positions
        max_pos = settings.max_open_positions
        if len(signals) > max_pos:
            signals = sorted(signals, key=lambda s: s.get("score", 0), reverse=True)[:max_pos]

        try:
            result = self._mv_optimize(signals, capital)
        except Exception as exc:
            logger.warning("[portfolio_optimizer] scipy optimization failed ({}), using equal-weight", exc)
            result = self._equal_weight(signals, capital)

        self._latest_allocations = result
        self._latest_sharpes = {
            sig["symbol"]: self._sharpe_score(sig)
            for sig in signals
        }
        logger.info(
            "[portfolio_optimizer] Allocated {:.0f} across {} signals: {}",
            sum(result.values()), len(result),
            {s: f"₹{v:,.0f}" for s, v in result.items()},
        )
        return dict(result)

    def should_trade(
        self,
        symbol: str,
        signal: dict,
        pending_allocations: dict[str, float],
    ) -> tuple[bool, float]:
        """
        Soft gate called from base_agent._try_enter().

        Returns (allowed, suggested_capital).

        Rules:
        - If no optimization has run yet (startup): always allow, return 0.0.
        - If this symbol has a positive allocation: allow with that capital.
        - If a competing signal in the same sector has >20% better Sharpe: block.
        - Otherwise: allow (optimizer hasn't run against this exact signal set).
        """
        if not self._latest_allocations:
            # No optimization has run — startup grace, let normal risk checks decide.
            return True, 0.0

        # If symbol appears in the latest allocation, it was approved.
        if symbol in self._latest_allocations:
            return True, self._latest_allocations[symbol]

        # Symbol not in latest run — check whether a same-sector rival beat it.
        my_sector = self._sector_map.get(symbol, _DEFAULT_SECTOR)
        my_sharpe = self._sharpe_score(signal)

        for rival_sym, rival_alloc in self._latest_allocations.items():
            if rival_alloc <= 0:
                continue
            rival_sector = self._sector_map.get(rival_sym, _DEFAULT_SECTOR)
            if rival_sector != my_sector:
                continue
            rival_sharpe = self._latest_sharpes.get(rival_sym, 0.0)
            if rival_sharpe > 0 and my_sharpe > 0:
                if rival_sharpe > my_sharpe * 1.20:
                    logger.debug(
                        "[portfolio_optimizer] {} SKIP — sector {} rival {} Sharpe {:.3f} > {:.3f} (>20%)",
                        symbol, my_sector, rival_sym, rival_sharpe, my_sharpe,
                    )
                    return False, 0.0

        # No dominant rival found — allow with zero suggested capital (agent uses own sizing).
        return True, 0.0

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _sharpe_score(self, sig: dict) -> float:
        """
        Proxy Sharpe for a single signal.
        expected_return = score / 10 ; volatility = atr_pct (as fraction)
        Returns 0 if inputs are invalid.
        """
        score   = sig.get("score", 0)
        atr_pct = sig.get("atr_pct", 0.0)
        if atr_pct <= 0 or score <= 0:
            return 0.0
        expected_return = score / 10.0
        volatility      = atr_pct / 100.0   # convert percent → fraction
        return expected_return / volatility

    def _equal_weight(
        self,
        signals: list[dict],
        capital: float,
    ) -> dict[str, float]:
        """Simple equal-weight fallback."""
        if not signals:
            return {}
        per_sym = min(capital / len(signals), settings.max_position_size)
        return {sig["symbol"]: round(per_sym, 2) for sig in signals}

    def _mv_optimize(
        self,
        signals: list[dict],
        capital: float,
    ) -> dict[str, float]:
        """
        Constrained mean-variance optimization via scipy SLSQP.

        Objective: minimize negative Sharpe (diagonal covariance approximation).
        Falls back to equal-weight on any scipy error.
        """
        import numpy as np
        from scipy.optimize import minimize  # noqa: F401 — may not be installed

        n = len(signals)
        symbols  = [s["symbol"] for s in signals]
        returns  = np.array([s.get("score", 0) / 10.0 for s in signals])
        vols     = np.array([max(s.get("atr_pct", 1.0) / 100.0, 1e-6) for s in signals])
        betas    = np.array([self._beta_map.get(sym, _DEFAULT_BETA) for sym in symbols])
        sectors  = [self._sector_map.get(sym, _DEFAULT_SECTOR) for sym in symbols]

        max_per_sym  = min(settings.max_position_size, capital)
        beta_cap     = settings.max_portfolio_beta
        sector_cap   = _SECTOR_CAP * capital

        def neg_sharpe(w: np.ndarray) -> float:
            port_return = float(np.dot(w, returns))
            port_var    = float(np.dot(w ** 2, vols ** 2))   # diagonal covariance
            if port_var <= 0 or port_return <= 0:
                return 0.0
            return -port_return / (port_var ** 0.5)

        # ── Constraints ────────────────────────────────────────────────────────
        constraints: list[dict] = [
            # Total allocation ≤ capital
            {
                "type": "ineq",
                "fun": lambda w: capital - w.sum(),
            },
            # Portfolio weighted-average beta ≤ beta_cap
            {
                "type": "ineq",
                "fun": lambda w, b=betas, bc=beta_cap: (
                    bc - float(np.dot(w, b)) / max(float(w.sum()), 1.0)
                ),
            },
        ]

        # Per-sector: sum of weights in each sector ≤ sector_cap
        unique_sectors = set(sectors)
        for sec in unique_sectors:
            idx = [i for i, s in enumerate(sectors) if s == sec]
            if len(idx) > 1:
                def _sec_constraint(w, ix=idx, cap=sector_cap):
                    return cap - float(w[ix].sum())
                constraints.append({"type": "ineq", "fun": _sec_constraint})

        bounds = [(0.0, max_per_sym) for _ in range(n)]

        # Warm-start with equal-weight
        w0 = np.full(n, min(capital / n, max_per_sym))

        result = minimize(
            neg_sharpe,
            w0,
            method="SLSQP",
            bounds=bounds,
            constraints=constraints,
            options={"maxiter": 500, "ftol": 1e-9},
        )

        if not result.success:
            logger.debug(
                "[portfolio_optimizer] SLSQP did not converge ({}), using equal-weight",
                result.message,
            )
            return self._equal_weight(signals, capital)

        allocations = {
            sym: round(float(w), 2)
            for sym, w in zip(symbols, result.x)
            if w > 1.0   # drop effectively-zero allocations
        }
        return allocations


# ── Module-level singleton ────────────────────────────────────────────────────
portfolio_optimizer = PortfolioOptimizer()
