import re
import secrets
from pydantic_settings import BaseSettings
from pydantic import Field, field_validator, model_validator, AliasChoices
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

    # Anthropic — reads ANTHROPIC_API_KEY or ANTHROPIC_API_KEY1 (whichever is set)
    anthropic_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("anthropic_api_key", "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY1"),
    )

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
    jwt_secret_key: str = ""  # auto-generated at boot if empty
    # 24h, not 8: an 8h token issued at Friday's login died over the weekend and
    # the Monday dashboard sat frozen on 401s (2026-07-13) — the page shell loads
    # without auth so it looks alive while every data call is silently rejected.
    jwt_expire_hours: int = 24

    # Trading
    trading_mode: Literal["PAPER", "LIVE"] = "PAPER"

    # Risk — money-critical fields are validated (must be sane positive values)
    # Scaled to the ₹10L-per-agent capital model below: max_position_size caps
    # a single equity order at one agent's whole pool (no trade should ever
    # need more), max_daily_loss is 2% of the 8-agent ₹80L book.
    max_daily_loss: float = Field(default=160000.0, gt=0)
    max_position_size: float = Field(default=1_000_000.0, gt=0)
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
    max_trades_scalping:       int = 60    # was 250 — backtests: churn is cost-negative
    max_trades_intraday:       int = 150
    max_trades_momentum:       int = 100
    max_trades_mean_reversion: int = 100
    max_trades_pairs:          int = 100
    max_trades_options:        int = 75
    max_trades_futures:        int = 75
    max_trades_swing:          int = 25    # position trades, low turnover
    cooldown_after_loss_sec:   int = 300
    post_exit_cooldown_sec:    int = 5      # minimum cooldown after ANY exit (incl. winners)
    # Per-SYMBOL cooldown after a LOSING exit — the anti-whipsaw gate. A symbol
    # that just stopped you out is usually chopping; blocking re-entry on it for
    # a while stops death-by-a-thousand-cuts (e.g. 20-Jul live: HINDPETRO 0W/3L,
    # INTELLECT 1W/3L — repeated stops on the same name under the old 30s gate).
    post_loss_symbol_cooldown_sec: int = 900   # 15 min

    # LIVE-mode symbol promotion: when true, the 10-min intraday movers scan can
    # add a fresh top-scoring mover to an agent's LIVE book even if it is not in
    # the overnight learner-approved list — i.e. live trades the day's trenders,
    # not only pre-vetted names. The mover still must clear the scanner's quality
    # filters (liquidity/ATR/RSI/ADX/trend), the per-agent book cap, and every
    # live safety layer (risk sizing, order guard, SEBI, SL/target). Set False to
    # restore the conservative behaviour (LIVE trades only pre-approved names).
    # No effect in PAPER (which already approves movers immediately).
    live_chase_movers: bool = True

    # Cadence-shadow recorder: read-only, no orders. When true, shadow-evaluates
    # the Intraday pattern book on 1/5/10-min views of the live tick stream and
    # logs realised forward P&L per cadence to logs/cadence_shadow.jsonl, so
    # "which bar cadence works best" can be answered from real data. Off by
    # default; see cadence_shadow.py.
    enable_cadence_shadow: bool = False

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
    tgt_pct_scalping:       float = 0.90   # 3:1 RR at 0.3 SL — 24% win rate needs >=3.2:1 to clear costs
    tgt_pct_options:        float = 65.0
    tgt_pct_futures:        float = 2.0
    tgt_pct_swing:          float = 8.0
    tgt_pct_mean_reversion: float = 2.0
    tgt_pct_momentum:       float = 3.0
    tgt_pct_pairs:          float = 1.5

    # Per-agent minimum pattern score to fire
    min_score_intraday:       int = 5      # was 4 — recent-window audit: fewer, higher-conviction entries
    min_score_scalping:       int = 5      # was 3 — higher-conviction entries only
    min_score_options:        int = 6
    min_score_futures:        int = 4
    # Weekly expiry weekday per index underlying, "SYM:weekday" CSV
    # (Mon=0…Sun=6). NSE/SEBI have moved expiry days repeatedly (2024-25
    # circulars) — when they change again, update this setting instead of
    # code. Empty entries fall back to the legacy defaults
    # (Thu=3; Wed=2 for BANKNIFTY/MIDCPNIFTY).
    # NSE index derivatives expire TUESDAY since 2025-09-01 (SEBI single-weekly
    # standardization: NSE=Tue, BSE=Thu). BANKNIFTY/FINNIFTY/MIDCPNIFTY weeklies
    # are discontinued — monthly only, last Tuesday. Stale Thursday defaults
    # built non-existent contract symbols and mistimed every expiry-day gate.
    index_expiry_weekdays:    str = "NIFTY:1,BANKNIFTY:1,FINNIFTY:1,MIDCPNIFTY:1,SENSEX:3,BANKEX:3"
    # NSE monthly expiry weekday (stock options + index monthlies): last Tuesday.
    nse_monthly_expiry_weekday: int = 1
    min_score_swing:          int = 1
    min_score_mean_reversion: int = 3
    min_score_momentum:       int = 4
    min_score_pairs:          int = 4

    # Per-agent entry cooldown (seconds between trades on same symbol)
    cooldown_intraday:       int = 180
    cooldown_scalping:       int = 180     # was 90 — fewer re-entries on the same symbol
    cooldown_options:        int = 120
    cooldown_futures:        int = 180
    # Swing had NO re-entry cooldown at all (only a 60s scan throttle that
    # re-arms regardless of trade outcome) — a "hold for days" strategy could
    # re-enter the same symbol a minute after its own exit (2026-07-08 live:
    # churned to the 25-trade daily cap TWICE in one session). Now that Swing
    # scores patterns off real DAILY bars (see SwingAgent._daily_indicators),
    # the underlying thesis is constant all day — a 1h cooldown let a stopped-
    # out position re-enter up to ~6x in one session against the same daily
    # signal. One full trading day is the right floor for a genuine swing hold.
    cooldown_swing:          int = 86400
    # Minimum trading days of daily-bar history before SwingAgent will compute
    # daily indicators at all (n>=200 is required for a real EMA200 to exist
    # rather than sit at its 0.0 default and short-circuit every pattern's
    # ltp > ind.ema200 check as vacuously true). 220 is the textbook floor for
    # live trading, where Kite's daily historical API can supply years of
    # data. Lower only for backtesting against a shorter fixture dataset.
    swing_daily_history_days: int = 220
    cooldown_mean_reversion: int = 180
    cooldown_momentum:       int = 180
    cooldown_pairs:          int = 120

    # Pre-learned system (set after running historical_learner.py)
    skip_startup_backtest: bool = False   # use pre-learned approved_symbols.json
    use_nifty100_watchlist: bool = False  # auto-use full Nifty 100 as watchlist

    # Intelligence layer — Claude Opus real-time market timing gate
    # Per-trade Claude assessment — OPT-IN. The system is designed to trade
    # fully rule-based: min-score floors, cost gate, kill-list, regime matrix,
    # expiry/calendar benches, caps/cooldowns, ATR+Kelly sizing, TSL/SL-M
    # protection. Flip on (with API credits) to trial AI vetoes; measure its
    # per-trade value in /gate/log before paying for it permanently.
    use_claude_trade_gate: bool = False
    claude_gate_model: str = "claude-opus-4-8"   # Opus: deepest reasoning for trade timing
    # Regime review fires every 60s ALL DAY — on Opus that burned an entire
    # credit purchase overnight (2026-07-13/14, ~1,200 calls with the market
    # closed). Sonnet is fully adequate for a summarize-and-direct job; Opus
    # stays where depth pays: the per-trade gate above.
    claude_gate_threshold: int = 25       # min confidence to enter — low bar keeps good trades flowing
    master_review_model: str = "claude-sonnet-5"   # regime review: Sonnet (cost fix, see above)
    master_review_when_closed: bool = False        # opt-in: off-hours review for offline GBM testing
    # Per-minute Claude commentary on top of the rule-based regime plans —
    # OPT-IN. The enforced matrix made every decision while this call was dead
    # (2026-07-13/14); Claude spends credits only where value is measurable:
    # the per-trade gate.
    use_master_claude_review: bool = False
    signal_engine_model: str = "claude-sonnet-5"   # signal generation: Sonnet (cost fix)
    use_extended_thinking: bool = True             # extended thinking ON — Opus deep-reasons before every trade
    gate_thinking_budget: int = 5000               # thinking tokens per trade assessment (Opus with extended thinking)
    gate_api_timeout: float = 15.0                 # seconds — 15s accommodates Opus + extended thinking latency
    gate_bypass_min_score: int = 20                 # raised to 20 — effectively disabled; all signals route through Opus gate
    scalping_skip_gate: bool = False                # False — scalping now gates through Opus like all other agents
    gate_max_concurrent: int = 4                    # max simultaneous Claude Opus gate calls; overflow degrades to cache/allow
    use_multi_timeframe: bool = True      # require 5m/15m alignment with entry direction
    mtf_min_alignment: int = 1            # how many of 3 TFs must agree (1, 2, or 3)
    use_kelly_sizing: bool = True         # apply Claude gate's size_factor to qty

    # Auto-start (set to enable fully-lights-out operation)
    # Comma-separated strategy names e.g. "intraday,scalping"
    auto_start_strategies: str = ""
    # Comma-separated symbols e.g. "RELIANCE,TCS" — empty = use symbol scanner
    auto_start_watchlist: str = ""
    # Keep every enabled agent trading regardless of the master regime plan —
    # the regime review can still resize (size_factor) but never pauses agents.
    # For per-agent performance evaluation where all strategies must stay live.
    force_all_agents: bool = False
    # Download N months of multi-timeframe Kite history into the CSV cache at
    # startup (containers are ephemeral). 0 = disabled.
    auto_download_history_months: int = 0
    # Symbol universe for history downloads: "watchlist" (tick-engine symbols)
    # or "nifty100" (full Nifty 100 — needed for historical_learner backtests).
    history_universe: str = "watchlist"

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

    # DigitalOcean (admin-only OAuth — view this app's own App Platform
    # deployment status; not a trading broker)
    digitalocean_client_id:     str = ""   # from Register a new OAuth Application
    digitalocean_client_secret: str = ""   # shown once at registration — save it
    digitalocean_redirect_url:  str = ""   # must match the app's Callback URL exactly
    digitalocean_access_token:  str = ""   # set after OAuth callback exchange
    digitalocean_app_id:        str = ""   # optional — narrows status to one App Platform app

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

    # Cost-floor entry gate: expected profit at target must be at least this
    # multiple of the round-trip transaction cost, else the entry is skipped.
    # Backtests showed scalping's small-target churn is cost-negative (PF~0.3);
    # this blocks trades whose edge can't clear their own costs. 0 = disabled.
    min_edge_cost_ratio: float = 2.0
    # Per-agent override (min_edge_cost_ratio_<agent_name>); 0 = use the global.
    # Scalping gets a stricter floor — its edge is thinnest relative to costs.
    min_edge_cost_ratio_scalping: float = 3.0

    # Decision cadence — THE churn fix (live 2026-07-10: tick-cadence
    # decisions produced 200 round trips in 32 minutes; gross −₹5.4k but
    # ₹17.4k transaction costs → net −₹22.8k; 175/208 exits were forming-bar
    # indicator fires, 21 closed at the exact entry price). The replay that
    # validated every strategy (+53%/2d) decides once per CLOSED 1-min bar —
    # these flags restore that exact cadence live. Hard stops, trailing
    # stops, targets and SL-M triggers remain tick-level. Env-overridable
    # (AGENT_DECISIONS_ON_BAR_CLOSE=false) for instant rollback, no deploy.
    agent_decisions_on_bar_close: bool = True   # entries evaluate on bar roll
    indicator_exit_on_bar_close:  bool = True   # discretionary exits too
    # Decision timeframe per agent in minutes ("agent:min,agent:min"; absent
    # agents decide on 1m bars). 1-YEAR replay (245 days, 2026-07-10):
    # intraday +954% @5m vs +613% @15m; futures +3,281% @5m vs +1,804% @15m —
    # both with >78% win rates. Scalping's patterns exist only at 1m.
    # Indicators stay 1m-computed; only DECISION timing changes.
    decision_bar_minutes: str = "intraday:5,futures:5"
    # India calendar risk-down. Thursday (weekly index expiry) is measurably
    # the worst weekday for every trend agent over the year (expiry gamma
    # chop): intraday +162% vs ~+180% other days, futures +57% vs ~+84%.
    expiry_size_down_weekdays: str = "1"                       # Mon=0 CSV; Tue = NSE expiry
    expiry_day_size_factor: float = Field(default=0.6, gt=0, le=1.0)
    # Operator-maintained event dates (Budget, RBI MPC…): momentum's worst
    # day of the year was Budget day 2026-02-01.
    event_risk_dates: str = ""                                 # "YYYY-MM-DD,…"
    event_day_size_factor: float = Field(default=0.5, gt=0, le=1.0)

    # OptionScalpingAgent (ships DARK — not in AUTO_START_STRATEGIES until
    # replay + paper validation). Built around EXPIRY_SCALP, the only options
    # pattern net-positive over the 1-year replay. Small budget by design:
    # the unlocked arm (−6,420%) is what unbudgeted option scalping becomes.
    max_trades_option_scalping: int = 8
    cooldown_option_scalping:   int = 300
    min_score_option_scalping:  int = 6
    option_scalp_max_hold_min:  int = Field(default=25, ge=5, le=120)

    # Book width per agent. Full-year breadth test (tf15, gated): the
    # 30-symbol book beat the 12-symbol core on every live agent (intraday
    # +1,757% vs +613%, futures +1,999% vs +1,804%, options +64% vs −17%)
    # at ~21 trades/day. The old uncapped live book (~500 subscriptions) was
    # never tested and amplified the 2026-07-10 churn. 0 = uncapped.
    max_symbols_per_agent: int = Field(default=30, ge=0, le=500)
    # Manual/dashboard orders get this much extra open-position headroom so a
    # human override is never boxed out by agents holding every slot.
    manual_extra_slots: int = Field(default=1, ge=0, le=5)
    # No indicator exit inside the first N seconds of a trade (SL/TSL exempt).
    # 120s = 2 full bars: a trade must survive its first two closes before a
    # momentum-fade/RSI exit is allowed to kill it.
    min_hold_before_indicator_exit_sec: int = 120

    # Pattern kill-list: "agent:PATTERN,agent:PATTERN" muted at the source
    # (bot_state.is_pattern_enabled checks this before the runtime toggles).
    # Seeded from the 62-day real-agent replay over recorded 1m candles —
    # every entry here lost money gross, before costs:
    #   intraday VWAP_TREND -54.96%, EMA_PULLBACK -11.03%, VWAP_RECLAIM -5.64%
    #   scalping EMA9X -7.13%, STOCHRSI_EXTREME -8.20%, MACD_MICRO -5.37%
    # Kill-list v2: every pattern that was gross-negative over the 62-day
    # replay (a gross-negative pattern is ALWAYS net-negative after costs,
    # independent of trade count). Enforced at the _try_enter chokepoint so
    # no per-site gate is needed. Evidence: logs/replay_backtest_result_
    # ungated_q{1-4}.json by_pattern sums. Survivors per agent:
    #   scalping BB_BAND_WALK +156.8, SUPERTREND_FLIP +25.4, VWAP_BOUNCE +0.5
    #   intraday BB_SQUEEZE_WALK +106.1, STOCHRSI_CROSS +22.6,
    #            DUAL_EMA_RETEST +11.1, HMA_FLIP +1.4, VWAP_BAND_REVERT +0.5
    #   options  TREND_PULL +16.8, BB_SQUEEZE +7.9, EXPIRY_SCALP +2.5,
    #            ICHIMOKU_CLOUD +2.2, STOCHRSI_OPTIONS +2.2, WILLIAMS_OPTIONS
    #            +1.3, VOL_BREAKOUT +0.9
    #   futures  EMA200_BOUNCE +4.8, STOCHRSI_FUTURES +1.4,
    #            TRIPLE_EMA_PULLBACK +0.6, MACD_CROSS +0.2
    disabled_patterns: str = (
        # v17 (HONEST-fills 3mo, tf5 core-12, ungated, 2026-07-16 — first-ever
        # real evaluation of PREV_DAY_HIGH/LOW, which read a nonexistent
        # LiveIndicators field and never fired a single trade before this
        # session's daily-bar rewrite): PREV_DAY_LOW is Swing's worst pattern
        # once it can actually fire, -47.6% over 12 trades (~-4.0%/trade);
        # its BUY-side sibling PREV_DAY_HIGH is also negative, -7.6% @7tr.
        # Combined -55.2% @19tr clears the project's -2%/>=20tr kill bar.
        # Fixing dead code doesn't obligate shipping what it reveals.
        "swing:PREV_DAY_HIGH,swing:PREV_DAY_LOW,"
        # v16 (HONEST-fills year, tf1 core-12, 2026-07-15): STOCHRSI_OPTIONS
        # was kept alive at v4 as a "survivor" under the OLD close-only
        # simulator (net-positive in both measurement rounds) — exactly the
        # class of result the honest wick-aware simulator was built to
        # correct. Under honest fills it's Options' worst pattern: -156.5%
        # over 49 trades (~-3.2%/trade), a mean-reversion bounce signal that
        # can't clear theta + spread before the bounce fizzles.
        "options:STOCHRSI_OPTIONS,"
        # v15 (HONEST-fills year, tf15, all 122 symbols, 2026-07-14): ORB_BREAK
        # negative in ALL 12 months (−45% to −106% each), 2,493 trades, −972%
        # total — a structural bleeder invisible in the thin core-12 sample
        # (it barely fires on the most liquid names) but decisive at breadth.
        "intraday:ORB_BREAK,"
        # v14 (HONEST-fills year, tf5 core-12, 2026-07-14): first kill under
        # the wick-aware simulator — VWAP_EXT_RIDE −56% over 84 trades while
        # BB_SQUEEZE_WALK (+707%) and KELTNER_RIDE (+597%) carry the agent.
        "intraday:VWAP_EXT_RIDE,"
        # v1 (62d replay, pre-kill baseline)
        "intraday:VWAP_TREND,intraday:EMA_PULLBACK,intraday:VWAP_RECLAIM,"
        "scalping:EMA9X,scalping:STOCHRSI_EXTREME,scalping:MACD_MICRO,"
        # v2 scalping tail (-60.6% gross combined, most of the cost churn)
        "scalping:HMA_MICRO,scalping:EMA9_MOMENTUM,scalping:SQUEEZE_RELEASE,"
        "scalping:WILLIAMS_SCALP,scalping:VWAP_SCALP,scalping:EMA921X,"
        "scalping:MICROTREND,scalping:SURGE,"
        # v2 intraday tail (-25.0% gross combined)
        "intraday:ADX_BREAKOUT,intraday:WILLIAMS_REVERSAL,intraday:TTM_SQUEEZE,"
        "intraday:MOMENTUM_SURGE,intraday:SUPERTREND_ALIGN,"
        "intraday:PREV_DAY_LEVEL,intraday:BREAKOUT,"
        # v2 options tail (-6.6% gross combined)
        "options:EMA_CROSS,options:RSI_MOMENTUM,options:VWAP_RECLAIM,"
        # v2 futures tail (-1.9% gross combined)
        "futures:EMA_TREND,futures:ICHIMOKU_FUTURES,futures:HMA_TREND,"
        # v3: gross-positive but NET-negative after per-trade costs (edge <
        # cost; final validation run with trade counts, kill rule: net <= -2%
        # over >= 20 trades)
        "scalping:VWAP_BOUNCE,intraday:DUAL_EMA_RETEST,options:TREND_PULL,"
        "options:ICHIMOKU_CLOUD,futures:TRIPLE_EMA_PULLBACK,intraday:HMA_FLIP,"
        # v4 (post-v3 measurement): options patterns that flip net-negative
        # when their trade count scales (per-trade edge < cost at volume;
        # BB_SQUEEZE +0.118%/tr at 74tr but +0.040%/tr at 362tr). Survivors
        # EXPIRY_SCALP and STOCHRSI_OPTIONS were net-positive in BOTH runs.
        "options:BB_SQUEEZE,options:VOL_BREAKOUT,options:WILLIAMS_OPTIONS,"
        # v5 (min-score grid, 62d × NIFTY/BANKNIFTY): net-negative in every
        # measurement round. Futures survives as an EMA200_BOUNCE-only
        # specialist (+1.62% net @94tr ungated; best futures pattern in all
        # four rounds).
        "futures:STOCHRSI_FUTURES,futures:MACD_CROSS,"
        # v6 verdict (62d validation of the 22 new patterns): failures killed.
        # Winners kept: KELTNER_RIDE +266.9% net @299tr (new intraday star),
        # STOCHRSI_TREND_OPT +8.2% net @169tr (options' best pattern).
        "intraday:HIGH_TIGHT_FLAG,options:BB_WALK_OPT,options:MORNING_THRUST_OPT,"
        "scalping:MOMENTUM_STACK,scalping:SUPERTREND_PULLBACK,"
        # v7 (stock-futures validation): futures becomes a band-walk
        # specialist — BB_WALK_FUT +74.4% net @365tr, SQUEEZE_WALK_FUT +30.2%
        # @139tr on stock futures. Everything churning against them killed,
        # including EMA200_BOUNCE (+1.6% on 2 indices, -33.9% @984tr once
        # stocks let it overtrade).
        "futures:EMA200_BOUNCE,futures:MULTI_TF_ALIGN,futures:VOL_SURGE,"
        "futures:OPEN_DRIVE_FUT,futures:VWAP_PULL,futures:MOMENTUM_CATCH,"
        "futures:VWAP_BAND_BREAK,futures:RANGE_COMPRESSION_BREAK,"
        # v8 (final-config run): INDEX_TREND_RIDE_OPT went negative at volume
        # (-1.83 @18tr in v6 -> -5.75 @41tr) — same edge-dies-at-scale
        # signature as the v4 kills.
        "options:INDEX_TREND_RIDE_OPT,"
        # v9 (range-day experiment, 62d, RANGING-only, first per-pattern
        # attribution for these agents): kills at net <= -1% / >= 15tr.
        # Survivors: momentum VOL_SURGE_TREND +16.5%, SQUEEZE_RELEASE +11.7%,
        # MACD_ZERO_CROSS +8.0%; mean_reversion BB_MID_REVERT +10.4%,
        # PRICE_ZSCORE +1.5% — the system's first proven range-day earners.
        "mean_reversion:WILLIAMS_EXTREME,mean_reversion:MACD_DIVERGENCE,"
        "mean_reversion:BB_UPPER_REJECT,mean_reversion:BB_WIDTH_SQUEEZE,"
        "momentum:BREAKOUT_RETEST,momentum:HL_BREAKOUT,momentum:LL_BREAKDOWN,"
        "momentum:HIGHER_HIGH_CONFIRM,"
        # v10 (HONEST RULER — 62d, premium/margin-scaled P&L + realistic 0.15%
        # equity / 0.30% options costs, calibrated to live-booked costs). This
        # ruler overturns earlier verdicts made on the under-costed sim:
        #   - STOCHRSI_TREND_OPT: v6 KEPT it at "+8.2% @169tr" — the honest run
        #     shows -2068.8% over 1037 trades (-2.0%/tr). The single biggest
        #     drain in the whole system; explains most live options losses.
        #   - RSI_EXTREME (meanrev) -36.5% @208tr, RSI_TRIPLE_EXTREME -3.7%,
        #     BB_MID_REVERT flat-negative @376tr (v9 "survivor" fails honest
        #     costs), RANGE_BREAK_RETEST -6.7%, futures ATR_BREAK -2.3%.
        # Survivors confirmed across 3 independent agents = the band-walk /
        # squeeze family (BB_WALK_FUT, KELTNER_RIDE, BB_SQUEEZE_WALK,
        # BB_BAND_WALK) — the real edge the lean rebuild is built around.
        "options:STOCHRSI_TREND_OPT,mean_reversion:RSI_EXTREME,"
        "mean_reversion:RSI_TRIPLE_EXTREME,mean_reversion:BB_MID_REVERT,"
        "scalping:RANGE_BREAK_RETEST,futures:ATR_BREAK,"
        # v11 (honest ruler, per-pattern prune of the two still-red agents).
        # Rule: cut net-negative at >=15 trades. mean_reversion has NO pattern
        # that clears real costs at sample size — every remaining live pattern
        # bleeds (STOCHRSI_CROSS -44%@415tr the worst), so it is effectively
        # benched. Momentum keeps its 3 real winners (VOL_SURGE_TREND +16.3%,
        # MACD_ZERO_CROSS +19.4%, VWAP_BREAKOUT +0.4%) and drops the bleeders,
        # led by its SUPERTREND_FLIP entry (-43.6% @627tr — the whipsaw).
        "mean_reversion:STOCHRSI_CROSS,mean_reversion:BB_LOWER_BOUNCE,"
        "mean_reversion:PRICE_ZSCORE,mean_reversion:VWAP_EXTREME,"
        "momentum:SUPERTREND_FLIP,momentum:EMA_ALIGNMENT,momentum:SQUEEZE_RELEASE,"
        # v13 (1-YEAR replay 2026-07-10, 245 days): POWER_HOUR_OPT negative at
        # every cadence (−40.2% @5m, −43.7% @15m); MACD_ZERO_CROSS −19.4%/yr
        # @15m (its +5.1 on Jul 8-9 was small-sample noise).
        "options:POWER_HOUR_OPT,momentum:MACD_ZERO_CROSS,"
        # v12 (regime-matrix re-measure of the pruned momentum book, 62d × 12
        # symbols, honest costs): VWAP_BREAKOUT gross +2.2% @79tr − ~11.9% costs
        # = net -9.7% → kill (rule: net <= -2% at >= 15tr). Momentum's real
        # earners are VOL_SURGE_TREND (net ~+56% @538tr) and MACD_ZERO_CROSS
        # (net ~+33% @176tr) — the book behind the bear/ranging/high-vol unbench.
        "momentum:VWAP_BREAKOUT"
    )

    # Scalping approved-book seed: the proxy learner cannot model the real
    # scalping agent (0/100 approvals despite scalping being the top gross
    # earner in the replay). These are the symbols where the REAL agent was
    # net-positive after costs over 62 replayed days (+38.4% combined).
    # Used only when the learner leaves scalping's book empty.
    scalping_book_seed: str = "WIPRO,INFY,HDFCBANK,SBIN,TITAN,AXISBANK"

    # Stock futures for FuturesAgent (beyond its index set). Liquid F&O names
    # with lot sizes known to kite_client._FON_LOT_SIZES; stocks give futures
    # the selection edge indices cannot (its index-only replay showed the
    # 2-chart universe was the binding constraint). Validated per-symbol by
    # replay before any name earns permanence.
    futures_stock_symbols: str = (
        "RELIANCE,HDFCBANK,ICICIBANK,SBIN,AXISBANK,TATASTEEL,TITAN,INFY,TCS,WIPRO"
    )

    # Options approved book (REPLACES the learner book when non-empty): the
    # proxy learner cannot model premium economics, so the replay's per-symbol
    # evidence governs. Final-config run (62d): TITAN +15.9%, AXISBANK +12.9%,
    # HDFCBANK +4.0%, RELIANCE +3.0%, ICICIBANK +0.4% net — the other five
    # watchlist names bled -35% combined and are excluded from live trading.
    # Expanded by the 20-symbol options test (62d, final config): six more
    # names qualified at net > +1% / >=15 trades — ADANIENT +19.1%,
    # BAJFINANCE +9.5%, KOTAKBANK +6.9%, DLF +6.2%, EICHERMOT +4.6%,
    # BHARTIARTL +4.0% (+50.3% combined). 14 candidates failed and stay out.
    # NOTE before LIVE: verify lot sizes for ADANIENT/DLF/EICHERMOT/
    # BHARTIARTL against the instruments dump (not yet in _FON_LOT_SIZES;
    # PAPER is unaffected).
    options_book_seed: str = (
        "TITAN,AXISBANK,HDFCBANK,RELIANCE,ICICIBANK,"
        "ADANIENT,BAJFINANCE,KOTAKBANK,DLF,EICHERMOT,BHARTIARTL"
    )

    # Regime entry gate: block NEW entries for agents in regimes where the
    # 62-day replay proved they lose. Format "REGIME:agent,agent;REGIME:...".
    # Evidence (gross P&L by NIFTY day-type, 62 real days × 12 symbols):
    #   RANGE (25d):      momentum -61.5%, options -46.7%, intraday -20.4%,
    #                     futures -8.4% | mean_reversion +9.8%, pairs +14.0%
    #   TREND_DOWN (9d):  options -38.6%, mean_reversion -15.3%, intraday -7.5%,
    #                     pairs -4.5%, futures -2.8% | scalping +6.3%, momentum +4.2%
    #   VOLATILE (16d):   mean_reversion -41.0%, pairs -7.7% | intraday +33.0%,
    #                     scalping +73.7%, options +47.2%, momentum +21.3%
    #   TREND_UP (12d):   mean_reversion -6.8%, pairs -1.8% | options +65.4%
    # Open positions are unaffected — exits/TSL keep managing them.
    # Matrix v2 — refined by the gated-vs-baseline 62-day comparison:
    #  * intraday UNBLOCKED everywhere (its range/bear-day losses came from
    #    patterns the kill-list already removed; gating it cost -7.5 pts net,
    #    kill-list-only intraday = +76.0% net over 62d)
    #  * momentum & mean_reversion blocked in ALL regimes incl. UNKNOWN
    #    (net -85 to -109% in every configuration tested — gate cannot save
    #    them; re-enable only on fresh evidence)
    #  * pairs RANGING-only (gate turned it net-positive: -15.5 → +0.1)
    regime_agent_gating: bool = True
    # futures was benched after post-v3 (-2.02% net), then un-benched as an
    # EMA200_BOUNCE-only specialist: the min-score grid showed that single
    # pattern is net-positive (+1.62% @94tr) measured UNGATED, while its
    # other patterns lose in every round (killed in disabled_patterns v5).
    # Like intraday, it runs ungated except BLACK_SWAN — the measurement
    # conditions that produced the positive number.
    regime_blocked_agents: str = (
        # Options benched pre-10:15: bought options bleed theta, and firing a
        # "trend" contract before the day's regime is even confirmed is the
        # single biggest options loss source (8/12 losers on 2026-07-06 all
        # entered in this window). No options entries until the trend is known.
        # Swing BENCHED everywhere (2026-07-08 live): churned to its 25-trade
        # daily cap TWICE in one session (-Rs 5.5k), both times within ~45 min
        # of a restart AND after the 15-min warm-up guard — its
        # SUPERTREND_BOUNCE/EMA50_BOUNCE "weekly" patterns fire at intraday
        # frequency. Benched until the pattern-frequency bug is diagnosed and
        # replay-validated.
        # option_scalping (dark until validated): benched in UNKNOWN/RANGING —
        # its two patterns are momentum-burst shaped; the unlocked-arm proof
        # says option scalps without regime discipline are the graveyard.
        "UNKNOWN:options,option_scalping,momentum,mean_reversion,pairs,swing;"
        "BULL_TREND:momentum,mean_reversion,pairs,swing;"
        "BULL_VOLATILE:momentum,mean_reversion,pairs,swing;"
        # Momentum UNBENCHED in bear/ranging/high-vol (matrix v3, post-v11-prune
        # 62d ungated re-measure of the 3-winner book, honest costs): net ≈
        # +27 BEAR_TREND, +31 RANGING, +22 HIGH_VOLATILE — vs ~flat BULL_TREND,
        # so it stays blocked there and in UNKNOWN. The earlier global bench was
        # measured on the PRE-prune book (SUPERTREND_FLIP -43.6% et al, killed
        # v11); the pruned book earns. The 2026-07-07 live bleed (-₹1,672) was
        # also pre-prune momentum. mean_reversion stays benched everywhere: the
        # v9 range-candidates FAILED the honest re-test (BB_MID_REVERT -42%
        # GROSS @775tr, PRICE_ZSCORE net -17% @140tr, RANGING-day gross -13).
        # Momentum RE-BENCHED everywhere (matrix v4, 1-YEAR replay 2026-07-10):
        # net −85.6% @15m and −103.5% @5m over 245 days — gross positive
        # (+153 @5m) but its 1,700-trade frequency is structurally
        # cost-negative. The 62d bear/ranging unbench was a shorter-window
        # read; the year overrules it. Re-evaluate only with a validated
        # frequency cut (trade budget or higher-conviction entries).
        "BEAR_TREND:options,momentum,mean_reversion,pairs,swing;"
        "BEAR_VOLATILE:options,momentum,mean_reversion,pairs,swing;"
        # RANGING: options + mean_reversion + pairs stay benched (options -46.7%
        # on range days; pairs -38% net over 62d with no winning pattern).
        # option_scalping unbenched in RANGING (2026-07-12): its
        # RANGE_FADE_SCALP pattern exists precisely for this tape. The agent
        # is DARK; the year replay's per-pattern attribution decides whether
        # the fade earns its place before anything activates.
        "RANGING:options,momentum,mean_reversion,pairs,swing;"
        "HIGH_VOLATILE:momentum,mean_reversion,pairs,swing;"
        "BLACK_SWAN:swing,intraday,futures,momentum,pairs,mean_reversion"
    )

    # Index weekly-expiry-day bench, applied on TOP of the regime matrix on
    # days whose weekday appears in index_expiry_weekdays. 2026-07-14 live:
    # regime flips re-enabled options mid-whipsaw (allowed in BULL labels) and
    # 15 premium-buying entries lost ₹8.7k into the expiry pin. Bought premium
    # on 0-DTE whipsaw is structurally the worst trade in the book.
    expiry_day_blocked_agents: str = "options"

    # Supertrend-flip exit: adverse-move gate (×ATR) for NON-trending tape
    # (ADX≥20 keeps its own branch). 0.3 = current behavior; the 2-day live
    # audit showed it collapses realized win:loss to 1:1 against a 3:1 target
    # design on chop days. Change ONLY with year-replay validation.
    st_flip_adverse_atr: float = 0.3

    # Phase 2/3: Position sizing
    use_atr_sizing: bool = True
    # Risk budget per trade as % of the agent's pool. 0.5% risked only ~₹1k on a
    # ₹2L slice → ~₹66k notional, then compounded down by Kelly/conviction/gate,
    # so the ₹10L pools sat barely used. 2.0% ~4× the risk budget; ATR sizing
    # still caps each position at the pool slice, so it can't overshoot capital.
    risk_per_trade_pct: float = Field(default=2.0, gt=0, le=50)
    use_conviction_sizing: bool = True  # score-proportional size (floor loosened: low=0.75×, mid=1.0×, high=1.25×)
    # Conviction concentration: a signal that reaches sizing with a FULL gate
    # size-factor (top score bucket AND gate-confident) earns a doubled capital
    # slice — the "manual trader" concentration on the highest-probability
    # setups (replay win rates at top scores run 60-96%). Scales the proven
    # edge linearly; caps at max_position_size and 2x the per-agent slice.
    conviction_2x_enabled: bool = True
    conviction_2x_mult: float = Field(default=2.0, ge=1.0, le=3.0)
    # MIS intraday leverage: Zerodha margins equity intraday at min 20%
    # (SEBI VAR+ELM floor) = up to 5x buying power on the MIS list. Sizing
    # previously capped every equity position at the raw CASH slice, so the
    # ATR risk formula's intended position (rupee risk / stop distance) was
    # chopped to ~1/5th by affordability, not by risk. This raises only the
    # AFFORDABILITY cap — risk_per_trade_pct stays anchored to the cash
    # slice, so rupee risk per trade is unchanged.
    mis_leverage: float = Field(default=5.0, ge=1.0, le=5.0)
    # Aggression sleeve (user-requested "10% day" venue, honest version):
    # a high-delta options trend-ride that fires ONLY on strong trend stacks
    # in bull/volatile regimes, max N trades/day, premium hard-stop. It can
    # print +50-100% premium days (= big book days) occasionally; most days
    # it does nothing. OFF until the qualifying week's base engine passes
    # AND the sleeve's own replay validation is positive.
    aggression_sleeve_enabled: bool = False
    sleeve_max_trades_day: int = Field(default=2, ge=0, le=5)
    sleeve_target_delta: float = Field(default=0.60, gt=0, le=0.9)
    sleeve_premium_sl_pct: float = Field(default=40.0, gt=0, le=100)
    sleeve_premium_tgt_pct: float = Field(default=90.0, gt=0)
    # Dead-tape gate (2026-07-09 evidence): on an ultra-calm RANGING day
    # (ATR 0.04-0.09%) the fast agents ground 27/27 exits into SL_HIT — zero
    # targets all day, costs 37% of the bleed. A veteran sits that tape out.
    # Blocks NEW scalping/momentum entries when the vol band is CALM AND the
    # confirmed regime is RANGING/UNKNOWN. Trend days keep calm-band trading.
    # Default OFF in code — activated via DEAD_TAPE_GATE=true env once the
    # 62-day replay validation passes (ship code and switch-on separately).
    dead_tape_gate: bool = False

    # Day-drift veto for scalping — default OFF: the 2026-07-13 session
    # (29 counter-drift SELL scalps −₹5.4k in RANGING chop) motivated it, but
    # the year tf1 A/B replay refuted it decisively: veto ON cut only 10% of
    # trades yet dropped scalping +706.6% → +107.9% (win 60.8% → 51.1%) —
    # counter-drift scalps are a top trade family over the year. Kept as a
    # manual emergency switch for hostile chop-with-drift days.
    scalping_day_drift_veto: bool = False

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
    # Paper trading on REAL market data: when True (and a Kite access token is
    # available), PAPER mode sources ticks from the live Kite WebSocket / quotes
    # instead of the GBM simulator. Orders remain fully simulated (_paper_orders)
    # — only the market-data feed is real. Default False keeps the offline GBM
    # simulator so paper trading works without a broker connection / off-hours.
    paper_use_live_data: bool = False

    # Daily capital allocation. Each of the 8 strategy agents gets its own
    # independent pool (capital_per_agent) — no sharing across siblings, so
    # intraday/scalping/mean_reversion/momentum/pairs no longer split one
    # bucket 5 ways. total_capital = capital_per_agent × len(ALL_AGENTS),
    # used only for whole-book risk limits (portfolio VaR, god_mode sizing).
    capital_per_agent:      float = Field(default=1_000_000.0, gt=0)  # ₹ per agent (₹10L)
    total_capital:          float = Field(default=8_000_000.0, gt=0)  # whole-book capital (₹) — VaR/god_mode only
    # Legacy per-type percentages — retained for the /settings/capital-allocation
    # report endpoint's backward-compat fields; max_capital_for_agent() no
    # longer reads these (each agent has its own flat pool above).
    intraday_capital_pct:   float = Field(default=40.0, ge=0, le=100)  # % for equity intraday MIS (intraday + scalping)
    swing_capital_pct:      float = Field(default=25.0, ge=0, le=100)  # % for equity delivery CNC (swing)
    options_capital_pct:    float = Field(default=25.0, ge=0, le=100)  # % for options premium NRML (fno)
    futures_capital_pct:    float = Field(default=10.0, ge=0, le=100)  # % for futures margin NRML (reserved)
    # Index futures (NIFTY/BANKNIFTY/…) are MARGIN products: the agent posts only
    # a fraction of contract notional. Sizing uses this margin % of notional (not
    # full notional) so a realistically-funded futures bucket can afford lots.
    # ~20% ≈ Zerodha NRML index-futures span+exposure margin (i.e. ~5× leverage).
    futures_margin_pct:     float = Field(default=20.0, gt=0, le=100)
    # Hard fat-finger guard for F&O orders (which are exempt from the equity ₹1M
    # per-order value cap): cap the number of lots per single order. 0 = no cap.
    max_futures_lots_per_order: int = Field(default=10, ge=0)

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
        if v == "":
            return secrets.token_hex(32)
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
