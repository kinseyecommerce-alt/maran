import { SEGMENT_ORDER } from '../Agents/shared'

/** ALL / NSE_EQ / NSE_FO / BSE_EQ / MCX / CDS chips with row counts. */
export function SegmentFilter({ value, onChange, rows, testId }:
  { value: string; onChange: (v: string) => void; rows: { segment?: string | null }[]; testId: string }) {
  const count = (c: string) => rows.filter(r => c === 'ALL' || r.segment === c).length
  return (
    <div className="flex items-center gap-1" data-testid={testId}>
      {['ALL', ...SEGMENT_ORDER].map(c => (
        <button key={c} data-testid={`${testId}-${c}`} onClick={() => onChange(c)}
          className={`text-[10px] font-mono px-2 py-0.5 rounded border ${value === c
            ? 'border-emerald-600 text-emerald-300 bg-emerald-900/30' : 'border-slate-700 text-slate-400 hover:bg-slate-800'}`}>
          {c} <span className="text-slate-500">{count(c)}</span>
        </button>
      ))}
    </div>
  )
}

export function SimBadge({ show, testId }: { show?: boolean; testId?: string }) {
  if (!show) return null
  return (
    <span data-testid={testId} title="Paper simulator price — not a market quote"
      className="ml-1.5 text-[9px] font-normal px-1 py-px rounded text-amber-300 bg-amber-500/15 border border-amber-500/40">SIMULATED</span>
  )
}
