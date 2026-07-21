"""Application settings (env-driven) + YAML strategy/risk config loaders.

Secrets and deployment values come only from the environment. Nothing sensitive is
hard-coded. `TRADING_MODE` defaults to SIMULATION and LIVE is gated by
`ALLOW_LIVE_TRADING` (a second, independent switch).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.enums import TradingMode
from app.strategy.config import StrategyConfig


class Settings(BaseSettings):
    """Runtime settings sourced from environment variables / .env."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    app_env: str = "development"
    app_name: str = "EMA_RSI_INTRADAY_ALGO"
    app_secret_key: str = ""
    jwt_secret_key: str = ""
    database_url: str = "postgresql+asyncpg://algo:algo@localhost:5432/ema_rsi_algo"
    redis_url: str = "redis://localhost:6379/0"
    timezone: str = "Asia/Kolkata"

    # Trading mode — two independent switches, both required for LIVE.
    trading_mode: TradingMode = TradingMode.SIMULATION
    allow_live_trading: bool = False

    # Broker (never defaulted to real values). `KITE_*` aliases let this service reuse
    # the existing deployment's Kite secrets without renaming any environment variable.
    zerodha_api_key: str = Field(
        "", validation_alias=AliasChoices("zerodha_api_key", "kite_api_key")
    )
    zerodha_api_secret: str = Field(
        "", validation_alias=AliasChoices("zerodha_api_secret", "kite_api_secret")
    )
    zerodha_access_token: str = Field(
        "", validation_alias=AliasChoices("zerodha_access_token", "kite_access_token")
    )
    zerodha_redirect_url: str = Field(
        "", validation_alias=AliasChoices("zerodha_redirect_url", "kite_redirect_url")
    )
    zerodha_user_id: str = Field(
        "", validation_alias=AliasChoices("zerodha_user_id", "kite_user_id")
    )
    zerodha_password: str = Field(
        "", validation_alias=AliasChoices("zerodha_password", "kite_password")
    )
    zerodha_totp_secret: str = Field(
        "", validation_alias=AliasChoices("zerodha_totp_secret", "kite_totp_secret")
    )

    # Live service — HTTP port and the tradable symbol universe (CSV; blank ⇒ default set).
    port: int = 8080
    trading_symbols: str = ""

    # Capital / risk (mirrors risk.default.yaml; env overrides win at runtime)
    default_capital: float = 1_000_000
    default_risk_percentage: float = 0.50
    maximum_stop_percentage: float = 1.0
    max_daily_loss_percentage: float = 1.5
    max_trades_per_day: int = 5
    max_consecutive_losses: int = 3
    max_simultaneous_positions: int = 3
    max_total_open_risk_percentage: float = 1.5

    strategy_config_path: str = "config/strategy.default.yaml"
    risk_config_path: str = "config/risk.default.yaml"

    def live_allowed_by_env(self) -> bool:
        """Env-level precondition for LIVE. Full readiness gating happens in the
        risk engine (Phase 2); this is the coarse first gate."""
        return self.allow_live_trading and self.trading_mode is TradingMode.LIVE


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _read_yaml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_strategy_config(path: str | Path | None = None) -> StrategyConfig:
    """Load a StrategyConfig from YAML, falling back to typed defaults.

    Unknown/missing keys use their defaults; present keys are validated."""
    data = _read_yaml(path) if path else {}
    return StrategyConfig(**data)


def load_risk_config(path: str | Path | None = None) -> dict[str, Any]:
    """Risk config stays a plain dict in Phase 1 (typed RiskConfig lands in Phase 2)."""
    return _read_yaml(path) if path else {}


# Repo-root-relative default config locations (…/ema-rsi-intraday-algo/config/*)
_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STRATEGY_YAML = _REPO_ROOT / "config" / "strategy.default.yaml"
DEFAULT_RISK_YAML = _REPO_ROOT / "config" / "risk.default.yaml"
