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

export interface Position {
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

export interface Order {
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

export type TabId = 'positions' | 'orders' | 'brackets' | 'risk' | 'agents' | 'sebi' | 'history' | 'gate'

export interface AgentActivityEntry {
  time?: string
  agent?: string
  action?: string
  type?: 'buy' | 'sell' | 'alert' | 'loss' | 'system' | 'analyze'
  cat?: 'EXEC' | 'RISK' | 'WARN' | 'SIG' | 'SYS'
  confidence?: string
  group?: string
}
