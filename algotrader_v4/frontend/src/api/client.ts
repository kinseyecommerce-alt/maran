import axios from 'axios'
import { useStore } from '../store'

const getConfig = () => {
  const { apiBase, apiKey } = useStore.getState()
  return { base: apiBase, key: apiKey }
}

const ax = () => {
  const { base, key } = getConfig()
  return axios.create({
    baseURL: base,
    headers: key ? { 'X-API-Key': key } : {},
    timeout: 10000,
  })
}

export const api = {
  // ── Health / System ────────────────────────────────────────────────────────
  health:          () => ax().get('/health'),
  configValidate:  () => ax().get('/config/validate'),
  connectionsStatus: () => ax().get('/connections/status'),
  brokerStatus:    () => ax().get('/broker/status'),

  // ── Auth ────────────────────────────────────────────────────────────────────
  authMe:          () => ax().get('/auth/me'),
  kiteStatus:      () => ax().get('/auth/kite/status'),
  kiteRefresh:     () => ax().post('/auth/kite/refresh'),
  upstoxStatus:    () => ax().get('/auth/upstox/status'),
  upstoxLoginUrl:  () => ax().get('/auth/upstox/login-url'),
  upstoxSetToken:  (access_token: string) => ax().post('/auth/upstox/token', { access_token }),
  kotakStatus:     () => ax().get('/auth/kotak/status'),
  kotakSendOtp:    () => ax().post('/auth/kotak/send-otp'),
  kotakVerifyOtp:  (otp: string, password: string) => ax().post('/auth/kotak/verify-otp', { otp, password }),
  kotakSetToken:   (access_token: string, sid: string) => ax().post('/auth/kotak/token', { access_token, sid }),

  // ── Bot ─────────────────────────────────────────────────────────────────────
  botStart:    (strategies: string[], watchlist?: { symbol: string; exchange: string }[]) =>
                 ax().post('/bot/start', { strategies, watchlist }),
  botStop:     () => ax().post('/bot/stop'),
  botStatus:   () => ax().get('/bot/status'),
  botDirectives: () => ax().get('/bot/directives'),
  botTestOrder: (data: { symbol: string; side: 'BUY' | 'SELL'; qty: number }) =>
                 ax().post('/bot/test-order', data),

  // ── Market ──────────────────────────────────────────────────────────────────
  marketLive:       () => ax().get('/market/live'),
  marketLiveSymbol: (symbol: string) => ax().get(`/market/live/${symbol}`),
  marketStatus:     () => ax().get('/market/status'),
  marketCandles:    (symbol: string, tf: '1min' | '5min' = '1min') =>
                      ax().get(`/market/candles/${symbol}`, { params: { tf } }),
  marketDepth:      (symbol: string) => ax().get(`/market/depth/${symbol}`),
  optionChain:      (symbol: string) => ax().get(`/market/option-chain/${symbol}`),

  // ── Orders ──────────────────────────────────────────────────────────────────
  placeOrder: (data: {
    symbol: string; exchange: string; transaction_type: string;
    quantity: number; order_type: string; product: string;
    price?: number; trigger_price?: number
  }) => ax().post('/orders/place', data),
  cancelOrder:   (orderId: string) => ax().delete(`/orders/${orderId}`),
  squareoffAll:  () => ax().post('/orders/squareoff'),
  multiLegOrder: (data: object) => ax().post('/orders/multi-leg', data),

  // ── Portfolio ───────────────────────────────────────────────────────────────
  positions: () => ax().get('/portfolio/positions'),
  orders:    () => ax().get('/portfolio/orders'),

  // ── Risk ────────────────────────────────────────────────────────────────────
  riskStatus:      () => ax().get('/risk/status'),
  riskUpdate:      (data: { max_daily_loss?: number; max_position_size?: number; stop_loss_pct?: number; target_pct?: number }) =>
                     ax().patch('/risk/update', data),
  reconcilerStatus: () => ax().get('/reconciler/status'),
  patternsStatus:   () => ax().get('/patterns/status'),
  signalsBus:       () => ax().get('/signals/bus'),

  // ── Capital ─────────────────────────────────────────────────────────────────
  capitalStatus:    () => ax().get('/capital/status'),
  capitalRebalance: () => ax().post('/capital/rebalance'),
  capitalReset:     () => ax().post('/capital/reset'),

  // ── Agents ──────────────────────────────────────────────────────────────────
  agents:       () => ax().get('/agents'),
  agentActivity:() => ax().get('/agents/activity'),
  pauseAgent:   (name: string) => ax().post(`/agents/${name}/pause`),
  resumeAgent:  (name: string) => ax().post(`/agents/${name}/resume`),

  // ── Regime ──────────────────────────────────────────────────────────────────
  regimeStatus:  () => ax().get('/regime/status'),
  regimeRefresh: () => ax().post('/regime/refresh'),
  regimeHistory: () => ax().get('/regime/history'),
  regimePlans:   () => ax().get('/regime/plans'),

  // ── Adaptive Engine ─────────────────────────────────────────────────────────
  adaptiveStatus: () => ax().get('/adaptive/status'),
  adaptiveReview: () => ax().post('/adaptive/review'),

  // ── Symbol Scanner ──────────────────────────────────────────────────────────
  symbolsScan:      (strategies?: string[]) => ax().post('/symbols/scan', { strategies }),
  symbolsSelected:  () => ax().get('/symbols/selected'),
  symbolsScores:    (strategy: string) => ax().get(`/symbols/scores/${strategy}`),
  symbolsCriteria:  () => ax().get('/symbols/criteria'),
  symbolsUniverse:  () => ax().get('/symbols/universe'),

  // ── Trailing SL ─────────────────────────────────────────────────────────────
  trailingSLStatus:  () => ax().get('/trailing-sl/status'),
  trailingSLConfigs: () => ax().get('/trailing-sl/configs'),
  trailingSLUpdate:  (data: object) => ax().patch('/trailing-sl/config', data),

  // ── Brackets ────────────────────────────────────────────────────────────────
  brackets:      () => ax().get('/brackets'),
  manualBracket: (data: {
    strategy: string; symbol: string; exchange: string; side: string;
    quantity: number; signal_price: number; product?: string;
    stop_loss?: number; target_1?: number; target_2?: number
  }) => ax().post('/brackets/manual', data),

  // ── AI Signal Gate ──────────────────────────────────────────────────────────
  gateLog:        () => ax().get('/gate/log'),
  generateSignal: (data: { symbol: string; strategy: string }) =>
                    ax().post('/signals/generate', data),

  // ── SEBI Compliance ─────────────────────────────────────────────────────────
  sebiStatus:         () => ax().get('/sebi/status'),
  sebiDisclosures:    () => ax().get('/sebi/disclosures'),
  sebiKillSwitch:     (reason?: string) =>
                        ax().post('/sebi/kill-switch', null, { params: { reason: reason || 'Manual' } }),
  sebiResume:         () => ax().post('/sebi/resume'),
  sebiPause:          () => ax().post('/sebi/pause'),
  sebiResetKillSwitch:(secret: string) => ax().post('/sebi/reset-kill-switch', { secret }),
  sebiAuditLog:       (date: string) => ax().get(`/sebi/audit-log?date=${date}`),
  sebiAlgoIds:        () => ax().get('/sebi/algo-ids'),
  sebiStrategyDisc:   (strategy: string) => ax().get(`/sebi/strategy-disclosure/${strategy}`),

  // ── Settings — Credentials ──────────────────────────────────────────────────
  updateCredentials: (data: {
    kite_api_key?: string; kite_api_secret?: string
    upstox_api_key?: string; upstox_api_secret?: string; upstox_redirect_url?: string
    kotak_consumer_key?: string; kotak_consumer_secret?: string
    kotak_mobile_number?: string; kotak_password?: string
    anthropic_api_key?: string; truedata_username?: string; truedata_password?: string
  }) => ax().post('/settings/credentials', data),
  updateAppPassword: (data: { username?: string; new_password: string }) =>
                       ax().post('/settings/app-password', data),

  // ── Settings — Trading Mode ─────────────────────────────────────────────────
  getTradingMode: () => ax().get('/settings/trading-mode'),
  setTradingMode: (mode: 'PAPER' | 'LIVE', confirm: boolean = false) =>
                    ax().post('/settings/trading-mode', { mode, confirm }),

  // ── Settings — Capital Allocation ───────────────────────────────────────────
  getCapitalAllocation: () => ax().get('/settings/capital-allocation'),
  patchCapitalAllocation: (data: {
    total_capital?: number
    intraday_capital_pct?: number; swing_capital_pct?: number
    options_capital_pct?: number; futures_capital_pct?: number
    max_intraday_positions?: number; max_scalping_positions?: number; max_swing_positions?: number
  }) => ax().patch('/settings/capital-allocation', data),

  // ── Settings — Trading Limits ───────────────────────────────────────────────
  getTradingLimits: () => ax().get('/settings/trading-limits'),
  patchTradingLimits: (data: {
    max_trades_intraday?: number; max_trades_options?: number
    max_trades_futures?: number; max_trades_swing?: number
    max_trades_scalping?: number; cooldown_after_loss_sec?: number
  }) => ax().patch('/settings/trading-limits', data),

  // ── Settings — Agent Enables ────────────────────────────────────────────────
  getAgentEnables: () => ax().get('/settings/agent-enables'),
  setAgentEnables: (data: { intraday?: boolean; fno?: boolean; swing?: boolean; scalping?: boolean }) =>
                     ax().post('/settings/agent-enables', data),

  // ── Settings — Intelligence ─────────────────────────────────────────────────
  getIntelligence: () => ax().get('/settings/intelligence'),
  patchIntelligence: (data: {
    use_claude_trade_gate?: boolean; claude_gate_threshold?: number
    use_multi_timeframe?: boolean; mtf_min_alignment?: number; use_kelly_sizing?: boolean
  }) => ax().patch('/settings/intelligence', data),

  // ── Settings — Agent Risk ───────────────────────────────────────────────────
  getAgentRisk: () => ax().get('/settings/agent-risk'),
  patchAgentRisk: (data: {
    sl_pct_intraday?: number; sl_pct_scalping?: number; sl_pct_options?: number
    sl_pct_futures?: number; sl_pct_swing?: number
    tgt_pct_intraday?: number; tgt_pct_scalping?: number; tgt_pct_options?: number
    tgt_pct_futures?: number; tgt_pct_swing?: number
    min_score_intraday?: number; min_score_scalping?: number; min_score_options?: number
    min_score_futures?: number; min_score_swing?: number
    cooldown_intraday?: number; cooldown_scalping?: number
    cooldown_options?: number; cooldown_futures?: number
  }) => ax().patch('/settings/agent-risk', data),

  // ── Settings — Pattern Toggles ──────────────────────────────────────────────
  getPatternToggles: () => ax().get('/settings/pattern-toggles'),
  patchPatternToggle: (agent: string, pattern: string, enabled: boolean) =>
                        ax().patch('/settings/pattern-toggles', { agent, pattern, enabled }),

  // ── Accounts (Kite multi-account) ───────────────────────────────────────────
  listAccounts:     () => ax().get('/accounts'),
  addAccount:       (data: { name: string; api_key: string; api_secret: string }) =>
                      ax().post('/accounts', data),
  deleteAccount:    (name: string) => ax().delete(`/accounts/${name}`),
  activateAccount:  (name: string) => ax().post(`/accounts/${name}/activate`),

  // ── Backtest ─────────────────────────────────────────────────────────────────
  backtestRun: (data: { symbol: string; strategy: string; days?: number }) =>
                 ax().post('/backtest/run', data),
}
