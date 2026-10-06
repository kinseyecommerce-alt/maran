import { useStore } from '../../store'
import { SEGMENT_ORDER } from '../Agents/shared'

/** ALL / NSE_EQ / NSE_FO / BSE_EQ / MCX / CDS chips with row counts. */
export function SegmentFilter({ value, onChange, rows, testId }:
  { value: string; onChange: (v: string) => void; rows: { segment?: string | null }[]; testId: string }) {
  const count = (c: string) => rows.filter(r => c === 'ALL' || r.segment === c).length
  return (
    <div className="flex items-center gap-0.5" data-testid={testId} role="tablist" aria-label="Segment filter">
      {['ALL', ...SEGMENT_ORDER].map(c => (
        <button key={c} data-testid={`${testId}-${c}`} onClick={() => onChange(c)}
          role="tab" aria-selected={value === c}
          className={`text-[10px] font-mono px-2 py-1 rounded border transition-colors focus-ring ${value === c
            ? 'border-slate-500 text-slate-100 bg-slate-800'
            : 'border-transparent text-slate-500 hover:text-slate-300 hover:bg-slate-800/60'}`}>
          {c} <span className="text-slate-600">{count(c)}</span>
        </button>
      ))}
    </div>
  )
}

export function SimBadge({ show, testId }: { show?: boolean; testId?: string }) {
  if (!show) return null
  return (
    <span data-testid={testId} title="Paper simulator price — not a market quote"
      className="ml-1.5 text-[9px] font-bold px-1 py-px rounded text-amber-300 bg-amber-500/15 border border-amber-500/50 tracking-wide">
      SIMULATED
    </span>
  )
}

const inr = (v: number) => `${v >= 0 ? '+' : '-'}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`

/** Status strip for the Positions / Orders tabs: the snapshot's realised +
 *  open = Today P&L (same numbers as the sidebar), when it was taken, and any
 *  load error — never a silent "0". */
export function BookStatus({ testId }: { testId: string }) {
  const snap  = useStore(s => s.book)
  const error = useStore(s => s.bookError)
  const at    = useStore(s => s.bookAt)
  const t = snap?.summary.total
  return (
    <div className="flex items-center gap-4 px-4 py-1.5 border-b border-[#1e2430] bg-[#0a0c10] text-[10px] font-mono text-slate-500"
      data-testid={testId}>
      {t ? (
        <span data-testid={`${testId}-equation`}>
          Today P&L <span className={t.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}>{inr(t.pnl)}</span>
          {' '}= realised <span className={t.realised >= 0 ? 'text-emerald-400' : 'text-rose-400'}>{inr(t.realised)}</span>
          {' '}({t.closed ?? 0} exit orders) + open <span className={t.unrealised >= 0 ? 'text-emerald-400' : 'text-rose-400'}>{inr(t.unrealised)}</span>
          {' '}({t.positions} positions)
        </span>
      ) : <span>{error ? '' : 'Loading book…'}</span>}
      <span className="flex-1" />
      {at > 0 && <span>snapshot {new Date(at).toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' })} IST</span>}
      {error && <span className="text-amber-400" data-testid={`${testId}-error`}>⚠ couldn't refresh: {error}</span>}
    </div>
  )
}
