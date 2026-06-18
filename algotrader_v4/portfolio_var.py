"""portfolio_var.py"""
from __future__ import annotations
import numpy as np
from scipy import stats as _stats
from loguru import logger
from config import settings
from kite_client import kite_client
from market_data import yf_client as _yf


class PortfolioVaR:
    def _fetch_returns(self, symbols: list[str]) -> dict[str, np.ndarray]:
        result: dict[str, np.ndarray] = {}
        for sym in symbols:
            try:
                df = _yf.historical(sym, exchange="NSE", interval="1d", period="60d")
                if df.empty:
                    continue
                col = "close" if "close" in df.columns else "Close"
                if col not in df.columns:
                    continue
                closes = df[col].dropna().values.astype(float)
                if len(closes) < 3:   # need ≥2 returns for meaningful std (ddof=1)
                    continue
                if (closes <= 0).any():
                    continue
                rets = np.diff(closes) / closes[:-1]
                result[sym] = rets
            except Exception:
                pass
        return result

    def compute(self, positions: list[dict]) -> dict:
        _zero = {"var_95": 0.0, "var_99": 0.0, "cvar_99": 0.0, "total_notional": 0.0, "n_positions": 0}
        active = [p for p in positions if p.get("quantity", 0) != 0]
        if len(active) < 2:
            total = sum(abs(p["quantity"]) * p.get("last_price", 0.0) for p in active)
            return {**_zero, "total_notional": total, "n_positions": len(active)}

        symbols = [p["tradingsymbol"] for p in active]
        returns_map = self._fetch_returns(symbols)

        valid = [(p, returns_map[p["tradingsymbol"]]) for p in active if p["tradingsymbol"] in returns_map]
        if len(valid) < 2:
            total = sum(abs(p["quantity"]) * p.get("last_price", 0.0) for p in active)
            return {**_zero, "total_notional": total, "n_positions": len(active)}

        notionals = np.array([abs(p["quantity"]) * p.get("last_price", 0.0) for p, _ in valid])
        total_notional = float(notionals.sum())
        if total_notional == 0.0:
            return {**_zero, "n_positions": len(active)}

        weights = notionals / total_notional

        min_len = min(len(r) for _, r in valid)
        ret_matrix = np.vstack([r[-min_len:] for _, r in valid])  # shape: (n, min_len)
        portfolio_rets = weights @ ret_matrix  # shape: (min_len,)

        if not np.isfinite(portfolio_rets).all():
            logger.warning("[PortfolioVaR] portfolio returns contain NaN/Inf — returning zero VaR")
            return {**_zero, "total_notional": round(total_notional, 2), "n_positions": len(active)}

        mu = float(portfolio_rets.mean())
        sigma = float(portfolio_rets.std(ddof=1)) if len(portfolio_rets) > 1 else 0.0

        z95 = float(_stats.norm.ppf(0.05))
        z99 = float(_stats.norm.ppf(0.01))
        var_95 = -(mu + z95 * sigma) * total_notional
        var_99 = -(mu + z99 * sigma) * total_notional

        # CVaR: use sorted tail (bottom ceil(1%) observations) — np.quantile equality
        # comparison can include too many values in flat-market conditions
        sorted_rets = np.sort(portfolio_rets)
        cutoff = max(1, int(np.ceil(0.01 * len(sorted_rets))))
        tail = sorted_rets[:cutoff]
        cvar_99 = (-float(tail.mean()) * total_notional) if len(tail) > 0 else var_99

        return {
            "var_95": round(var_95, 2),
            "var_99": round(var_99, 2),
            "cvar_99": round(cvar_99, 2),
            "total_notional": round(total_notional, 2),
            "n_positions": len(active),
        }

    def check_pre_trade(self, new_symbol: str, new_notional: float) -> tuple[bool, str]:
        use_var: bool = getattr(settings, "use_portfolio_var", False)
        if not use_var:
            return True, ""

        try:
            raw = kite_client.positions_cached()
            positions = list(raw.get("net", []))
        except Exception:
            positions = []

        existing = [p for p in positions if p.get("quantity", 0) != 0]
        if len(existing) < 1:
            return True, ""

        synthetic = {
            "tradingsymbol": new_symbol,
            "quantity": 1,
            "last_price": new_notional,
        }
        # Exclude any existing position for new_symbol to avoid double-counting its notional
        existing_excl = [p for p in positions if p.get("tradingsymbol") != new_symbol]
        combined = existing_excl + [synthetic]

        result = self.compute(combined)
        if result["n_positions"] < 2:
            return True, ""

        limit_pct: float = getattr(settings, "portfolio_var_limit_pct", 2.0)
        total_capital: float = getattr(settings, "total_capital", 1_000_000.0)
        limit_inr = (limit_pct / 100.0) * total_capital

        if result["cvar_99"] > limit_inr:
            return False, (
                f"Portfolio CVaR (99%) ₹{result['cvar_99']:,.0f} would exceed "
                f"{limit_pct}% capital limit ₹{limit_inr:,.0f}"
            )
        return True, ""

    def status(self) -> dict:
        try:
            raw = kite_client.positions_cached()
            positions = list(raw.get("net", []))
        except Exception:
            positions = []

        result = self.compute(positions)
        limit_pct: float = getattr(settings, "portfolio_var_limit_pct", 2.0)
        total_capital: float = getattr(settings, "total_capital", 1_000_000.0)
        return {
            **result,
            "var_limit_pct": limit_pct,
            "capital": total_capital,
            "var_limit_inr": round((limit_pct / 100.0) * total_capital, 2),
            "use_portfolio_var": getattr(settings, "use_portfolio_var", False),
        }


portfolio_var = PortfolioVaR()
