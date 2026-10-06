export interface TickData {
  symbol: string
  ltp: number
  bid: number
  ask: number
  change_pct: number
  volume: number
  day_high: number
  day_low: number
  trend: 'UP' | 'DOWN' | 'NEUTRAL'
  momentum: string
  volatility: string
  rsi: number
  vwap: number
  ema9: number
  ema21: number
  macd_hist: number
  vol_ratio: number
  source: 'KITE' | 'NSE' | 'PAPER' | string
  /** Honest origin of this price: SIMULATED = PAPER simulator, never real. */
  price_source?: 'KITE' | 'TRUEDATA' | 'SIMULATED' | string
  simulated?: boolean
  ts: string
}

/** Single source of truth for every engine/bot status indicator (backend engine_status()). */
export interface EngineStatus {
  state: 'starting' | 'running' | 'stopped' | 'error'
  label: string
  phase: 'idle' | 'scanning_instruments' | 'loading_instruments' | 'started' | 'error'
  error: string | null
  master_running: boolean
  agents: Record<string, boolean>
  agents_running: number
  agents_total: number
  tick_feed: 'running' | 'stopped'
  ts_ms: number
  strategies?: Record<string, StrategyState>
  segments?: SegmentState[]
  segments_running?: number
  /** Header counters + Today P&L over ALL segments (same rows as /portfolio/*) */
  book?: BookSummary
  /** true when served to an unauthenticated caller (no P&L / book) */
  redacted?: boolean
}

export interface BookSegment {
  label: string
  realised: number
  unrealised: number
  pnl: number
  positions: number
  orders: number
  /** exit orders listed today (their pnl sums to `realised`) */
  closed?: number
  realised_source?: 'orders' | 'agents'
  simulated: boolean
}

export interface BookSummary {
  total: { realised: number; unrealised: number; pnl: number; positions: number; orders: number; closed?: number }
  by_segment: Record<string, BookSegment>
  /** changes whenever an order/position is added or removed */
  rev?: string
  ts?: string
}

export interface BookStrategy {
  trades_today: number
  realised: number
  unrealised: number
  total: number
  open_positions: number
}

/** GET /portfolio/book — ONE snapshot (same price tick) of every view. */
export interface BookSnapshot {
  positions: Position[]
  orders: Order[]
  summary: BookSummary
  strategies: Record<string, BookStrategy>
}

/** Segment metadata every position/order row carries (book.py). */
export interface BookRowMeta {
  segment?: string | null
  strategy?: string | null
  price_source?: string | null
  simulated?: boolean
  native?: boolean
  lots?: number | null
  multiplier?: number
}

export type AgentRunState = 'starting' | 'running' | 'paused' | 'closed' | 'killed' | 'stopped'

/** One record per strategy agent — the ONLY input for every badge, Pause/Resume
 *  button and toggle on the dashboard panel and the Agents tab. */
export interface StrategyState {
  segment: string
  state: AgentRunState
  reason: string
  on: boolean
  running: boolean
  enabled: boolean
  native: boolean
  hidden: boolean
  trades_today: number
  /** realised + open P&L (book.py) */
  pnl_today: number
  pnl_realised?: number
  pnl_unrealised?: number
  open_positions?: number
  display?: string | null
  desc?: string | null
  can_resume: boolean
}

export interface SegmentInstrument {
  symbol: string
  price: number | null
  source: 'SIMULATED'
  synthetic_seed: boolean
  ref_close: number | null
  ref_close_date: string | null
  ref_source: string | null
}

export interface SegmentState {
  code: 'NSE_EQ' | 'NSE_FO' | 'BSE_EQ' | 'MCX' | 'CDS'
  label: string
  kite_exchange: string
  state: AgentRunState
  reason: string
  on: boolean
  open: boolean
  hours: string
  mode: 'PAPER' | 'LIVE'
  effective_mode: 'PAPER' | 'LIVE'
  live_supported: boolean
  live_stub_reason: string | null
  feed: 'REAL' | 'MIXED' | 'SIMULATED'
  killed: boolean
  kill_reason: string | null
  capital: number
  capital_used: number
  limits: { capital: number; max_daily_loss: number; max_positions: number; max_trades_per_day: number }
  pnl: { realised: number; unrealised: number; total: number; trades_today: number }
  positions: number
  entries_today: number
  strategies: string[]
  strategies_running: number
  universe: { count: number; feed: string; instruments?: SegmentInstrument[]; symbols?: string[] }
}

export interface OverviewStock {
  symbol: string
  ltp: number | null
  change_pct: number | null
  source: 'KITE' | 'TRUEDATA' | 'SIMULATED' | 'UNAVAILABLE'
  real: boolean
  stale: boolean
  age_sec: number | null
  ts: string | null
  ref_close: number | null
  ref_close_date: string | null
}

export interface MarketOverviewData {
  indices: IndexQuote[]
  stocks: OverviewStock[]
  chart: { symbol: string; interval: string; source: string; live_source: string | null;
           points: { date: string; close: number; live?: boolean }[] }
  trading_mode: 'PAPER' | 'LIVE'
  stock_feed: 'REAL' | 'SIMULATED' | 'MIXED' | 'NONE'
  note: string
  ts: string
}

export interface Position extends BookRowMeta {
  tradingsymbol: string
  exchange: string
  product: string
  quantity: number
  average_price: number
  last_price: number
  pnl: number
  buy_quantity?: number
  sell_quantity?: number
}

export interface Order extends BookRowMeta {
  order_id: string
  tradingsymbol: string
  exchange: string
  transaction_type: 'BUY' | 'SELL'
  quantity: number
  order_type: string
  product: string
  price: number
  status: string
  placed_at?: string
  tag?: string
  average_price?: number
  /** realised P&L — set on exit (reducing) fills only */
  pnl?: number | null
  entry_price?: number | null
}

export interface Bracket {
  bracket_id: string
  strategy: string
  symbol: string
  side: string
  quantity: number
  entry_price: number
  stop_loss?: number
  target_1?: number
  target_2?: number
  status: 'PENDING' | 'ACTIVE' | 'SL_HIT' | 'TARGET_HIT' | 'CANCELLED' | 'FAILED'
  pnl?: number
}

export interface RiskStatus {
  daily_pnl: number
  max_daily_loss: number
  open_positions: number
  max_open_positions: number
  is_halted: boolean
  trades_today: number
}

export interface Agent {
  name: string
  running: boolean
  last_signal?: string
  trades_today?: number
  win_rate?: number
}

export interface BotStatus {
  master_running: boolean
  strategies: string[]
  watchlist: string[]
  start_phase?: 'idle' | 'scanning_instruments' | 'loading_instruments' | 'started' | 'error'
  start_error?: string | null
  engine?: EngineStatus
  status?: string
  performance?: {
    total_trades: number
    daily_pnl: number
  }
}

export interface HealthData {
  status: string
  version: string
  mode: 'PAPER' | 'LIVE'
  market_open: boolean
  master: string
  tick_engine: string
  ticker_source: 'KITE' | 'NSE' | 'PAPER'
  engine?: EngineStatus
  time: string
}

export interface IndexQuote {
  symbol: string
  name: string
  ltp: number | null
  change: number | null
  change_pct: number | null
  prev_close?: number
  open?: number
  high?: number
  low?: number
  source: 'KITE' | 'NSE' | 'SIMULATED' | 'UNAVAILABLE'
  stale: boolean
  available: boolean
  age_sec?: number
  ts?: string
  exchange_ts?: string
}

export type TabId = 'positions' | 'orders' | 'brackets' | 'risk' | 'agents' | 'invented' | 'sebi' | 'history' | 'gate'

export interface AgentActivityEntry {
  time?: string
  agent?: string
  action?: string
  type?: 'buy' | 'sell' | 'alert' | 'loss' | 'system' | 'analyze'
  cat?: 'EXEC' | 'RISK' | 'WARN' | 'SIG' | 'SYS'
  confidence?: string
  group?: string
}
