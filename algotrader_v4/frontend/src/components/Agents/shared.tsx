import { useStore } from '../../store'
import { api } from '../../api/client'
import type { BookSnapshot, EngineStatus, SegmentState, StrategyState, AgentRunState } from '../../types'

/** Display metadata only — never state. State always comes from engine.strategies. */
export const STRATEGY_META: Record<string, { displayName: string; strategy: string; id: string }> = {
  intraday:           { displayName: 'INTRADAY',     strategy: 'VWAP Breakout',        id: 'AGN-01' },
  options:            { displayName: 'F&O',          strategy: 'Options CE/PE',        id: 'AGN-02' },
  swing:              { displayName: 'SWING',        strategy: 'Multi-TF Trend',       id: 'AGN-03' },
  scalping:           { displayName: 'SCALPING',     strategy: 'Orderbook Imbalance',  id: 'AGN-04' },
  futures:            { displayName: 'FUTURES',      strategy: 'Futures Momentum',     id: 'AGN-05' },
  momentum:           { displayName: 'MOMENTUM',     strategy: 'Price Momentum',       id: 'AGN-06' },
  mean_reversion:     { displayName: 'MEAN REV',     strategy: 'Mean Reversion',       id: 'AGN-07' },
  pairs:              { displayName: 'PAIRS ARB',    strategy: 'Statistical Arb',      id: 'AGN-08' },
  option_scalping:    { displayName: 'OPT SCALP',    strategy: 'Option Scalping',      id: 'AGN-09' },
  bse_momentum:       { displayName: 'BSE MOMENTUM', strategy: 'EMA cross · simulated', id: 'BSE-01' },
  bse_mean_reversion: { displayName: 'BSE MEAN REV', strategy: 'z-score · simulated',  id: 'BSE-02' },
  mcx_trend:          { displayName: 'MCX TREND',    strategy: 'EMA cross · simulated', id: 'MCX-01' },
  mcx_mean_reversion: { displayName: 'MCX MEAN REV', strategy: 'z-score · simulated',  id: 'MCX-02' },
  cds_trend:          { displayName: 'CDS TREND',    strategy: 'EMA cross · simulated', id: 'CDS-01' },
  cds_mean_reversion: { displayName: 'CDS MEAN REV', strategy: 'z-score · simulated',  id: 'CDS-02' },
}

export const SEGMENT_ORDER = ['NSE_EQ', 'NSE_FO', 'BSE_EQ', 'MCX', 'CDS'] as const
// Within a segment, keep the familiar dashboard order.
const STRATEGY_ORDER = ['intraday', 'swing', 'scalping', 'momentum', 'mean_reversion', 'pairs',
  'options', 'futures', 'option_scalping', 'bse_momentum', 'bse_mean_reversion',
  'mcx_trend', 'mcx_mean_reversion', 'cds_trend', 'cds_mean_reversion']

export function metaFor(key: string, s?: StrategyState) {
  const m = STRATEGY_META[key]
  return {
    displayName: m?.displayName || s?.display || key.toUpperCase(),
    strategy: m?.strategy || s?.desc || '',
    id: m?.id || key.slice(0, 6).toUpperCase(),
  }
}

/** Listed strategy keys, grouped by segment, from the server record. */
export function listedStrategies(e: EngineStatus | null, segment?: string): string[] {
  const all = e?.strategies || {}
  return Object.keys(all)
    .filter(k => !all[k].hidden && (!segment || all[k].segment === segment))
    .sort((a, b) => {
      const sa = SEGMENT_ORDER.indexOf(all[a].segment as any), sb = SEGMENT_ORDER.indexOf(all[b].segment as any)
      if (sa !== sb) return sa - sb
      return STRATEGY_ORDER.indexOf(a) - STRATEGY_ORDER.indexOf(b)
    })
}

const BADGE: Record<AgentRunState | 'unknown', { text: string; cls: string }> = {
  starting: { text: 'STARTING', cls: 'text-amber-300 bg-amber-500/10 animate-pulse' },
  running:  { text: 'ON',       cls: 'text-emerald-400 bg-emerald-500/10' },
  paused:   { text: 'PAUSED',   cls: 'text-amber-500 bg-amber-500/10' },
  closed:   { text: 'CLOSED',   cls: 'text-sky-400 bg-sky-500/10' },
  killed:   { text: 'KILLED',   cls: 'text-rose-400 bg-rose-500/10' },
  stopped:  { text: 'OFF',      cls: 'text-slate-400 bg-slate-500/10' },
  unknown:  { text: '…',        cls: 'text-slate-500 bg-slate-500/10' },
}

export interface StrategyView {
  state: AgentRunState | 'unknown'
  on: boolean
  badge: string
  badgeCls: string
  reason: string
  canResume: boolean
  /** Pause when on, Resume when off — identical on every surface. */
  action: 'pause' | 'resume' | 'none'
}

/** THE view of one strategy. Dashboard card, Agents-tab card, badge, button
 *  and toggle all call this with the same engine object. */
export function strategyView(e: EngineStatus | null, key: string): StrategyView {
  const s = e?.strategies?.[key]
  if (!s) return { state: 'unknown', on: false, badge: BADGE.unknown.text, badgeCls: BADGE.unknown.cls,
                   reason: 'waiting for server state', canResume: false, action: 'none' }
  const b = BADGE[s.state]
  const action = s.state === 'running' ? 'pause' : (s.can_resume ? 'resume' : 'none')
  return { state: s.state, on: s.on, badge: b.text, badgeCls: b.cls, reason: s.reason,
           canResume: s.can_resume, action }
}

export function segmentBadge(s: SegmentState): { text: string; cls: string } {
  const b = BADGE[s.state] || BADGE.unknown
  return { text: s.state === 'running' ? 'RUNNING' : b.text, cls: b.cls }
}

export const inr = (v: number | null | undefined, d = 0) =>
  v == null ? '—' : `₹${Number(v).toLocaleString('en-IN', { maximumFractionDigits: d })}`

export const signedInr = (v: number) => `${v >= 0 ? '+' : '-'}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`

/** Every control updates the store from the server's own engine snapshot
 *  returned by the action, so all surfaces flip together. */
export function useAgentControls() {
  const { addToast, setEngine } = useStore()
  const run = async (p: Promise<any>, ok: string, kind: 'info' | 'buy' = 'info') => {
    try {
      const r = await p
      if (r?.data?.engine) setEngine(r.data.engine)
      addToast(ok, kind)
    } catch (e: any) {
      addToast(e?.response?.data?.detail || 'Request failed', 'error')
    }
  }
  return {
    pause:  (k: string) => run(api.pauseAgent(k), `${k} paused`),
    resume: (k: string) => run(api.resumeAgent(k), `${k} resumed`, 'buy'),
    act:    (k: string, v: StrategyView) =>
              v.action === 'pause' ? run(api.pauseAgent(k), `${k} paused`)
              : v.action === 'resume' ? run(api.resumeAgent(k), `${k} resumed`, 'buy')
              : Promise.resolve(),
    kill:   (c: string) => run(api.segmentKill(c), `${c} kill switch ON`),
    rearm:  (c: string) => run(api.segmentRearm(c), `${c} re-armed`, 'buy'),
    mode:   (c: string, m: 'PAPER' | 'LIVE', text = '') =>
              run(api.segmentMode(c, m, m === 'LIVE', text), `${c} → ${m}`),
  }
}

/** Segment card P&L / positions: the same /portfolio/book snapshot. */
export function segmentNumbers(s: SegmentState, snap: BookSnapshot | null) {
  const b = snap?.summary.by_segment?.[s.code]
  if (b) return { pnl: b.pnl, realised: b.realised, unrealised: b.unrealised, positions: b.positions, closed: b.closed ?? 0 }
  return { pnl: s.pnl.total, realised: s.pnl.realised, unrealised: s.pnl.unrealised, positions: s.positions, closed: 0 }
}

/** Card trades / P&L: the /portfolio/book snapshot (same rows as Positions,
 *  Orders, header and Today P&L); engine.strategies only until it loads. */
export function cardNumbers(st: StrategyState | undefined, snap: BookSnapshot | null, key: string) {
  const b = snap?.strategies?.[key]
  if (b) return { trades: b.trades_today, pnl: b.total, realised: b.realised, unrealised: b.unrealised, open: b.open_positions }
  return { trades: st?.trades_today ?? 0, pnl: st?.pnl_today ?? 0, realised: st?.pnl_realised ?? 0,
           unrealised: st?.pnl_unrealised ?? 0, open: st?.open_positions ?? 0 }
}
