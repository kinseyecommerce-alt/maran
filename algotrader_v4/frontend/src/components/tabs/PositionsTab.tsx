import { useState } from 'react'
import { useStore } from '../../store'
import { Pnl, Badge } from '../ui'
import { SegmentFilter, SimBadge, BookStatus } from './SegmentFilter'

/** Every open position across ALL segments, from the one /portfolio/book
 *  snapshot (App polls it) — the same rows the header counters and Today P&L
 *  are computed from. P&L is the server's figure (lot multiplier applied once). */
export default function PositionsTab() {
  const snap = useStore(s => s.book)
  const [seg, setSeg] = useState('ALL')

  const open = (snap?.positions ?? []).filter(p => p.quantity !== 0)
  const shown = open.filter(p => seg === 'ALL' || p.segment === seg)
  const totalPnl = shown.reduce((acc, p) => acc + (p.pnl || 0), 0)

  return (
    <div className="h-full overflow-auto bg-[#0a0c10]" data-testid="positions-tab">
      <div className="flex items-center justify-between px-4 py-2 border-b border-[#1e2430] bg-[#11141a] sticky top-0 z-20 gap-3">
        <span className="text-xs font-semibold text-slate-200 tracking-wide" data-testid="positions-count" data-value={shown.length}>
          {snap ? `${shown.length} Open Position${shown.length !== 1 ? 's' : ''}` : 'Positions —'}
        </span>
        <SegmentFilter value={seg} onChange={setSeg} rows={open} testId="pos-filter" />
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-wider text-slate-500">Open P&L{seg !== 'ALL' ? ` (${seg})` : ''}</span>
          <span data-testid="positions-total-pnl" data-value={totalPnl}><Pnl value={totalPnl} /></span>
        </div>
      </div>
      <BookStatus testId="pos-book" />
      {!snap ? (
        <div className="flex items-center justify-center h-40 text-slate-500 text-xs font-mono">Loading positions…</div>
      ) : shown.length === 0 ? (
        <div className="flex items-center justify-center h-40 text-slate-500 text-xs">No open positions{seg !== 'ALL' ? ` in ${seg}` : ''}</div>
      ) : (
        <table className="desk-table">
          <thead>
            <tr>
              <th className="text-left">Symbol</th>
              <th className="text-left">Segment</th>
              <th className="text-left">Strategy</th>
              <th className="text-right">Qty</th>
              <th className="text-right">Avg Price</th>
              <th className="text-right">LTP</th>
              <th className="text-right">P&L</th>
              <th className="text-right">Change</th>
            </tr>
          </thead>
          <tbody>
            {shown.map(pos => {
              const key = `${pos.segment || pos.exchange}:${pos.tradingsymbol}`
              const ltp = pos.last_price
              const pct = pos.average_price > 0 ? ((ltp - pos.average_price) / pos.average_price * 100) : 0
              return (
                <tr key={key} data-testid={`pos-row-${pos.tradingsymbol}`} data-segment={pos.segment || ''}>
                  <td>
                    <div className="font-semibold text-slate-100 text-xs">{pos.tradingsymbol}</div>
                    <div className="text-[10px] text-slate-500 font-mono">{pos.product} · {pos.exchange}</div>
                  </td>
                  <td className="font-mono text-[11px] text-slate-400">{pos.segment || '—'}</td>
                  <td className="font-mono text-[11px] text-slate-400">{pos.strategy || '—'}</td>
                  <td className="desk-num">
                    <Badge variant={pos.quantity > 0 ? 'buy' : 'sell'}>
                      {pos.quantity > 0 ? '+' : ''}{pos.quantity}{pos.lots != null && pos.multiplier !== 1 ? ' lots' : ''}
                    </Badge>
                    {pos.multiplier && pos.multiplier !== 1 && (
                      <div className="text-[9px] text-slate-600 mt-0.5">×{pos.multiplier}/pt</div>
                    )}
                  </td>
                  <td className="desk-num text-slate-300">₹{pos.average_price.toFixed(2)}</td>
                  <td className="desk-num font-semibold text-slate-100">
                    ₹{ltp.toFixed(2)}
                    <SimBadge show={pos.simulated} testId={`pos-sim-${pos.tradingsymbol}`} />
                  </td>
                  <td className="desk-num" data-testid={`pos-pnl-${pos.tradingsymbol}`} data-value={pos.pnl}><Pnl value={pos.pnl || 0} /></td>
                  <td className="desk-num">
                    <span className={`text-xs font-mono font-medium ${pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {pct >= 0 ? '+' : ''}{pct.toFixed(2)}%
                    </span>
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </div>
  )
}
