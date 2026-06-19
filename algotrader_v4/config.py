import re
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator, model_validator
from typing import Literal


class Settings(BaseSettings):
    # Zerodha
    kite_api_key: str = ""
    kite_api_secret: str = ""
    kite_access_token: str = ""
    # Kite auto-login (Playwright) — set these to enable morning auto-refresh
    kite_user_id: str = ""
    kite_password: str = ""
    kite_totp_secret: str = ""      # TOTP seed from Zerodha 2FA setup
    kite_redirect_url: str = ""     # e.g. https://yourdomain.com/auth/kite/callback

    # Anthropic
    anthropic_api_key: str = ""

    # TrueData
    truedata_username: str = ""
    truedata_password: str = ""
    use_truedata_websocket:   bool = False  # primary live tick source (replaces Kite WS)
    use_truedata_historical:  bool = False  # OHLCV history for backtesting / warm-up
    use_truedata_options:     bool = False  # options chain: IV rank, PCR, max pain

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Email / alert notifications
    alert_email: str = ""                # recipient for alerts; empty = email disabled
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""                  # sender email / SMTP login
    smtp_pass: str = ""                  # app password or SMTP password
    alert_on_loss_limit: bool = True     # send alert when daily loss limit is hit
    alert_on_kill_switch: bool = True    # send alert on kill switch trigger
    alert_on_startup: bool = True        # send startup notification at market open

    # n8n webhook integration
    n8n_webhook_url:    str = ""  # e.g. https://your-n8n.com/webhook/algotrader
    n8n_webhook_secret: str = ""  # optional HMAC-SHA256 signing secret

    # Security — API access
    api_key: str = ""                   # X-API-Key header for mutating routes
    kill_switch_reset_secret: str = ""  # Separate secret required to reset kill switch

    # Security — App login (JWT)
    admin_username: str = "admin"
    admin_password: str = ""            # plain fallback for dev only
    admin_password_hash: str = ""       # bcrypt hash — takes precedence
    jwt_secret_key: str = ""
    jwt_expire_hours: int = 8

    # Trading
    trading_mode: Literal["PAPER", "LIVE"] = "PAPER"

    # Risk — money-critical fields are validated (must be sane positive values)
    max_daily_loss: float = Field(default=5000.0, gt=0)
    max_position_size: float = Field(default=50000.0, gt=0)
    max_open_positions: int = 5
    stop_loss_pct: float = Field(default=1.5, gt=0, le=50)
    target_pct: float = 3.0
    squareoff_time: str = "15:10"
    max_indicator_age_sec: float = 5.0  # reject entries on indicators older than this

    # Position reconciler — broker is truth; poll + correct internal drift
    use_position_reconciler: bool = True
    reconcile_interval_sec: float = Field(default=10.0, gt=0)

    # Pattern decay monitor — auto-mute a pattern whose live edge goes negative
    use_pattern_monitor: bool = True
    pattern_window: int = Field(default=20, gt=1)        # rolling trades per pattern
    pattern_min_trades: int = Field(default=12, gt=1)    # min sample before muting
    pattern_disable_sharpe: float = 0.0                  # mute when rolling Sharpe < this AND mean < 0

    # News / corporate-action gate
    use_news_gate: bool = False             # block trades on negative news (opt-in)
    news_block_hours: int = 4              # how long to hold a news block
    news_poll_interval_sec: int = 300      # how often to refresh NSE announcements

    # L2 fill-quality gate — size/skip on thin order books (LIVE only; needs depth)
    use_l2_fill_gate: bool = False                       # opt-in: needs L2 depth feed
    l2_min_fill_prob: float = Field(default=0.5, ge=0, le=1)   # min fraction of qty the book must absorb
    l2_max_slippage_bps: float = Field(default=25.0, gt=0)     # max acceptable VWAP slippage
    l2_shrink_to_book: bool = True                        # shrink qty to fillable size instead of skipping

    # Disclosed-quantity (iceberg-lite) — hide large equity orders from the book
    use_disclosed_qty: bool = False                      # opt-in
    disclosed_qty_value_threshold: float = Field(default=500_000.0, gt=0)  # only for orders above this notional
    disclosed_qty_pct: float = Field(default=0.20, gt=0, le=1)             # disclose this fraction (NSE floor 10%)

    # Latency guard — pause new entries briefly after a slow order placement
    use_latency_guard: bool = False                      # opt-in
    max_order_latency_ms: float = Field(default=1500.0, gt=0)
    latency_cooldown_sec: float = Field(default=30.0, gt=0)

    # Cross-session pattern memory — carry prior rolling stats across restarts
    pattern_history_carry_pct: float = Field(default=0.5, gt=0, le=1)  # fraction of prior window to seed

    # Cross-agent signal bus — real-time cross-strategy conviction sharing
    use_signal_bus: bool = True
    signal_bus_window_sec: float = Field(default=60.0, gt=0)   # event TTL
    signal_bus_min_score: int = Field(default=3, ge=0)          # min signal score to count toward boost
    signal_bus_max_boost: float = Field(default=0.25, ge=0, le=1)  # max size boost per agreed agent

    # Adaptive agent capital allocation — shift buckets toward best Sharpe agents
    use_adaptive_capital: bool = False                           # opt-in (needs ≥N live trades)
    adaptive_capital_days: int = Field(default=5, ge=1)          # rolling look-back days
    adaptive_capital_min_trades: int = Field(default=10, ge=1)   # minimum trades before shifting
    adaptive_capital_step_pct: float = Field(default=5.0, gt=0)  # max pct shift per rebalance
    adaptive_capital_floor_ratio: float = Field(default=0.10, gt=0, le=0.5)  # floor = ratio × baseline

    # Backtest gate thresholds
    bt_min_win_rate: float = 55.0
    bt_min_sharpe: float = 1.0
    bt_max_drawdown_pct: float = 15.0
    bt_min_trades: int = 20
    bt_lookback_days: int = 730
    bt_min_calmar: float = 0.5            # minimum Calmar ratio to pass backtest gate

    # Overtrade prevention — calibrated to Kite's 2,000 orders/day hard limit.
    # Each trade = 2 orders (entry + SL-M); 500 orders reserved for TSL modifications.
    # Effective budget: 1,500 new orders → ~750 trades total across all agents.
    max_trades_scalping:       int = 250   # highest frequency — ~1 trade/min
    max_trades_intraday:       int = 150
    max_trades_momentum:       int = 100
    max_trades_mean_reversion: int = 100
    max_trades_pairs:          int = 100
    max_trades_options:        int = 75
    max_trades_futures:        int = 75
    max_trades_swing:          int = 25    # position trades, low turnover
    cooldown_after_loss_sec:   int = 300
    post_exit_cooldown_sec:    int = 5      # minimum cooldown after ANY exit (incl. winners)

    # Per-agent stop-loss % (intraday/scalping/futures/swing = price %; options = premium %)
    sl_pct_intraday:       float = 1.5
    sl_pct_scalping:       float = 0.3
    sl_pct_options:        float = 25.0
    sl_pct_futures:        float = 1.0
    sl_pct_swing:          float = 3.0
    sl_pct_mean_reversion: float = 1.2
    sl_pct_momentum:       float = 1.5
    sl_pct_pairs:          float = 0.8

    # Per-agent target %
    tgt_pct_intraday:       float = 3.0
    tgt_pct_scalping:       float = 0.70
    tgt_pct_options:        float = 65.0
    tgt_pct_futures:        float = 2.0
    tgt_pct_swing:          float = 8.0
    tgt_pct_mean_reversion: float = 2.0
    tgt_pct_momentum:       float = 3.0
    tgt_pct_pairs:          float = 1.5

    # Per-agent minimum pattern score to fire
    min_score_intraday:       int = 4
    min_score_scalping:       int = 3
    min_score_options:        int = 6
    min_score_futures:        int = 4
    min_score_swing:          int = 1
    min_score_mean_reversion: int = 3
    min_score_momentum:       int = 4
    min_score_pairs:          int = 4

    # Per-agent entry cooldown (seconds between trades on same symbol)
    cooldown_intraday:       int = 180
    cooldown_scalping:       int = 90
    cooldown_options:        int = 120
    cooldown_futures:        int = 180
    cooldown_mean_reversion: int = 180
    cooldown_momentum:       int = 180
    cooldown_pairs:          int = 120

    # Pre-learned system (set after running historical_learner.py)
    skip_startup_backtest: bool = False   # use pre-learned approved_symbols.json
    use_nifty100_watchlist: bool = False  # auto-use full Nifty 100 as watchlist

    # Intelligence layer
    use_claude_trade_gate: bool = True    # per-trade Claude assessment via Opus
    claude_gate_model: str = "claude-sonnet-4-6"  # Sonnet: ~10× faster than Opus, sufficient accuracy for intraday gates
    claude_gate_threshold: int = 30       # min confidence to enter — Opus must be very confident a trade is BAD to block it
    master_review_model: str = "claude-opus-4-8"   # regime review model
    signal_engine_model: str = "claude-opus-4-8"   # signal generation model
    use_extended_thinking: bool = False            # extended thinking off — Sonnet doesn't need it; re-enable for Opus deep-review
    gate_thinking_budget: int = 2000               # thinking tokens per trade assessment (only used when use_extended_thinking=True)
    gate_api_timeout: float = 5.0                  # seconds — 5s fits Sonnet well; raise to 12s if switching back to Opus+thinking
    gate_bypass_min_score: int = 7                 # signals scoring ≥ this skip Claude gate entirely (auto-approve, ~65% of trades)
    use_multi_timeframe: bool = True      # require 5m/15m alignment with entry direction
    mtf_min_alignment: int = 2            # how many of 3 TFs must agree (1, 2, or 3)
    use_kelly_sizing: bool = True         # apply Claude gate's size_factor to qty

    # Auto-start (set to enable fully-lights-out operation)
    # Comma-separated strategy names e.g. "intraday,scalping"
    auto_start_strategies: str = ""
    # Comma-separated symbols e.g. "RELIANCE,TCS" — empty = use symbol scanner
    auto_start_watchlist: str = ""

    # Upstox
    upstox_api_key: str = ""
    upstox_api_secret: str = ""
    upstox_access_token: str = ""
    upstox_redirect_url: str = ""

    # Kotak Neo
    kotak_consumer_key:    str = ""   # from Kotak developer portal
    kotak_consumer_secret: str = ""   # from Kotak developer portal
    kotak_access_token:    str = ""   # set after OTP login (refreshed daily)
    kotak_sid:             str = ""   # session ID returned with access token
    kotak_mobile_number:   str = ""   # registered mobile e.g. "+919876543210"
    kotak_password:        str = ""   # Kotak login password (for OTP trigger)

    # Active broker selection
    active_broker: Literal["zerodha", "upstox", "kotak"] = "zerodha"

    # Phase 1: Transaction cost / slippage model
    bt_slippage_bps_intraday: int = 10
    bt_slippage_bps_scalping: int = 5
    bt_slippage_bps_swing: int = 3
    bt_slippage_bps_options: int = 15
    bt_apply_tx_costs: bool = True
    use_transaction_costs: bool = True      # deduct Zerodha fees from P&L in PAPER + LIVE
    apply_slippage: bool = True             # apply ATR-proportional slippage in PAPER mode
    slippage_bps_override: int = 0          # 0 = use volume-tier table; >0 = fixed bps for all
    use_kelly_capital_sizing: bool = False  # size by half-Kelly fraction of total capital

    # Phase 2: Extended backtest
    bt_wf_folds: int = 12
    bt_wf_anchored: bool = True
    bt_min_oos_trades: int = 15

    # Phase 2: Monte Carlo
    bt_require_mc_pass: bool = False
    bt_mc_permutations: int = 1000

    # Phase 2/3: Position sizing
    use_atr_sizing: bool = True
    risk_per_trade_pct: float = Field(default=0.5, gt=0, le=50)
    use_conviction_sizing: bool = True  # score-proportional size: low=0.5×, mid=0.75×, high=1.0×

    # Phase 3: Intelligence
    max_positions_per_sector: int = 2
    min_rolling_sharpe: float = 0.5
    use_limit_orders: bool = True
    limit_order_timeout_sec: int = 8
    max_portfolio_beta: float = 1.3   # block BUY if portfolio beta would exceed this
    use_ml_filter: bool = False       # GBM win-probability gate (requires trained model)
    ml_filter_min_prob: float = 0.45  # minimum predicted win probability to enter
    use_ml_signals: bool = False      # sklearn signal scorer gate
    ml_signal_min_confidence: float = 0.5  # minimum ML confidence to pass

    # Portfolio VaR / CVaR gate
    use_portfolio_var: bool = False          # block new entries that breach CVaR limit
    portfolio_var_limit_pct: float = 2.0    # max allowed portfolio CVaR as % of total_capital

    # TWAP order splitting (large-lot market impact reduction)
    use_twap: bool = False            # split large orders into equal slices
    twap_slices: int = 4              # number of child orders per TWAP execution
    twap_interval_sec: int = 15       # seconds between each slice
    twap_min_qty: int = 100           # only TWAP if qty >= this (don't slice small retail lots)
    # Aliases accepted by twap_executor (mirrors twap_min_qty / twap_interval_sec * twap_slices)
    twap_threshold_qty: int = 0       # alias for twap_min_qty (0 = use twap_min_qty)
    twap_duration_sec: int = 0        # alias for twap_interval_sec * twap_slices (0 = compute)

    # Real-time tick feed
    use_kite_websocket: bool = True   # use KiteConnect WebSocket for ticks in LIVE mode

    # Daily capital allocation by trading type
    total_capital:          float = Field(default=500000.0, gt=0)  # total account capital (₹)
    intraday_capital_pct:   float = Field(default=40.0, ge=0, le=100)  # % for equity intraday MIS (intraday + scalping)
    swing_capital_pct:      float = Field(default=25.0, ge=0, le=100)  # % for equity delivery CNC (swing)
    options_capital_pct:    float = Field(default=25.0, ge=0, le=100)  # % for options premium NRML (fno)
    futures_capital_pct:    float = Field(default=10.0, ge=0, le=100)  # % for futures margin NRML (reserved)

    # Max concurrent positions per agent (capital divided per-symbol to avoid overrun)
    max_intraday_positions: int = 5
    max_scalping_positions: int = 5
    max_swing_positions:    int = 3

    # Phase 4: Persistence backends (empty = use SQLite in-memory fallback)
    database_url: str = ""   # e.g. postgresql+asyncpg://user:pass@host/db
    redis_url:    str = ""   # e.g. redis://localhost:6379/0

    # kite_accounts.json encryption — Fernet key (base64-url, 32 bytes).
    # Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # When set, API key/secret are encrypted at rest. When empty, fallback to plaintext + chmod 600.
    kite_accounts_key: str = ""

    # Multi-broker mirroring — place orders on Zerodha AND a secondary broker simultaneously
    enable_multi_broker: bool = False          # master switch
    secondary_brokers: str = ""               # comma-separated: "upstox", "kotak"

    # Black Swan detection + opportunity response (veteran trader mode)
    black_swan_vix_zscore:     float = 3.0    # VIX z-score threshold (beyond HIGH_VOLATILE's 2.0)
    black_swan_price_drop_pct: float = 3.0    # 1-min NIFTY drop % to trigger flash crash detect
    black_swan_exit_time:      str   = "14:30" # hard cut-off for all black swan positions
    black_swan_iv_rank_min:    float = 75.0   # min IV rank required to sell condors (IV crush trade)
    black_swan_volume_mult:    float = 3.0    # volume multiple required for bounce confirmation

    # Runtime tuning — named constants (avoids magic numbers scattered across modules)
    db_write_queue_size:      int   = 2000   # state_store write backlog before drops
    db_keep_days:             int   = 90    # days of trade/position history to retain (Sunday cleanup)
    adaptive_refresh_sec:     int   = 300    # how often base_agent re-reads adaptive params
    ws_max_connections:       int   = 50     # max simultaneous WebSocket clients
    order_max_retries:        int   = 3      # kite_client retry attempts on transient error
    tick_interval_ms:         int   = 250    # PAPER mode poll interval (ms); 250 = 4 ticks/s

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    allowed_origins: str = "http://localhost:3000,http://localhost:5173"

    # SEBI IP whitelist — comma-separated IPs pre-loaded at startup.
    # Empty = whitelist disabled (all IPs allowed on order/kill-switch routes).
    # In production set to your static outbound IP e.g. "1.2.3.4"
    sebi_whitelisted_ips: str = ""

    @field_validator("squareoff_time")
    @classmethod
    def validate_squareoff_time(cls, v: str) -> str:
        if not re.match(r"^\d{2}:\d{2}$", v):
            raise ValueError("squareoff_time must be HH:MM format")
        h, m = int(v[:2]), int(v[3:])
        if not (9 <= h <= 15 and 0 <= m <= 59):
            raise ValueError("squareoff_time must be between 09:00 and 15:59")
        return v

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret(cls, v: str) -> str:
        if len(v) < 32:
            raise ValueError("jwt_secret_key must be at least 32 characters for HS256 security")
        return v

    @model_validator(mode="after")
    def validate_capital_allocation(self) -> "Settings":
        total_pct = (self.intraday_capital_pct + self.swing_capital_pct
                     + self.options_capital_pct + self.futures_capital_pct)
        if total_pct > 100:
            raise ValueError(
                f"Capital allocation percentages must sum to <= 100, got {total_pct:.1f} "
                f"(intraday={self.intraday_capital_pct}, swing={self.swing_capital_pct}, "
                f"options={self.options_capital_pct}, futures={self.futures_capital_pct})"
            )
        return self

    class Config:
        env_file = __import__("os").environ.get("APP_ENV_FILE", ".env")
        env_file_encoding = "utf-8"
        case_sensitive = False

    @property
    def origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",")]


settings = Settings()
