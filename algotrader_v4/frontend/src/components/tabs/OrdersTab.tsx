import { useState } from 'react'
import { useStore } from '../../store'
import { api } from '../../api/client'
import { Badge, Btn, Pnl } from '../ui'
import { SegmentFilter, SimBadge, BookStatus } from './SegmentFilter'

const statusVariant = (s: string): 'buy' | 'sell' | 'neutral' | 'warning' => {
  if (s === 'COMPLETE') return 'buy'
  if (s === 'CANCELLED' || s === 'REJECTED') return 'sell'
  if (s === 'OPEN' || s === 'TRIGGER PENDING') return 'warning'
  return 'neutral'
}

const hhmmss = (ts?: string) => {
  if (!ts) return '—'
  const m = ts.match(/T(\d\d:\d\d:\d\d)/)
  return m ? m[1] : ts
}

/** Today's orders (IST) across ALL segments, from the one /portfolio/book
 *  snapshot — the same array the header ORDERS counter and nav badge count.
 *  Exit orders carry their realised P&L; their sum is the "Realised" figure
 *  in the sidebar and in Today P&L. */
export default function OrdersTab() {
  const snap = useStore(s => s.book)
  const refreshBook = useStore(s => s.refreshBook)
  const addToast = useStore(s => s.addToast)
  const [seg, setSeg] = useState('ALL')
  const orders = snap?.orders ?? []
  const load = () => refreshBook()

  const handleCancel = async (orderId: string) => {
    try {
      await api.cancelOrder(orderId)
      addToast(`Order ${orderId} cancelled`, 'info')
      load()
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Cancel failed', 'error')
    }
  }

  const shown = [...orders].reverse().filter(o => seg === 'ALL' || o.segment === seg)
  const exits = shown.filter(o => o.pnl != null)
  const realised = exits.reduce((a, o) => a + (o.pnl || 0), 0)

  return (
    <div className="h-full overflow-auto bg-[#0a0c10]" data-testid="orders-tab">
      <div className="flex items-center justify-between px-4 py-2 border-b border-[#1e2430] bg-[#11141a] sticky top-0 z-20 gap-3">
        <span className="text-xs font-semibold text-slate-200 tracking-wide" data-testid="orders-count" data-value={shown.length}>
          {snap ? `${shown.length} order${shown.length !== 1 ? 's' : ''} today` : 'Orders —'}
        </span>
        <SegmentFilter value={seg} onChange={setSeg} rows={orders} testId="ord-filter" />
        <div className="flex items-center gap-2" title="Σ realised P&L of the exit orders listed">
          <span className="text-[10px] uppercase tracking-wider text-slate-500">Realised ({exits.length} exits{seg !== 'ALL' ? `, ${seg}` : ''})</span>
          <span data-testid="orders-realised" data-value={realised}><Pnl value={realised} /></span>
        </div>
      </div>
      <BookStatus testId="ord-book" />
      {!snap ? (
        <div className="flex items-center justify-center h-40 text-slate-500 text-xs font-mono">Loading orders…</div>
      ) : shown.length === 0 ? (
        <div className="flex items-center justify-center h-40 text-slate-500 text-xs">No orders today{seg !== 'ALL' ? ` in ${seg}` : ''}</div>
      ) : (
        <table className="desk-table">
          <thead>
            <tr>
              <th className="text-left">Time (IST)</th>
              <th className="text-left">Order ID</th>
              <th className="text-left">Symbol</th>
              <th className="text-left">Segment</th>
              <th className="text-left">Strategy</th>
              <th className="text-left">Side</th>
              <th className="text-right">Qty</th>
              <th className="text-left">Type</th>
              <th className="text-right">Price</th>
              <th className="text-right">Realised</th>
              <th className="text-left">Status</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {shown.map(o => {
              const px = o.average_price || o.price
              return (
                <tr key={o.order_id} data-testid={`ord-row-${o.order_id}`} data-segment={o.segment || ''}>
                  <td className="font-mono text-[11px] text-slate-500">{hhmmss(o.placed_at)}</td>
                  <td className="font-mono text-[11px] text-slate-500" title={o.order_id}>{o.order_id.slice(-8)}</td>
                  <td>
                    <div className="font-semibold text-slate-100 text-xs">{o.tradingsymbol}</div>
                    <div className="text-[10px] text-slate-500 font-mono">{o.exchange} · {o.product}{o.tag ? ` · ${o.tag}` : ''}</div>
                  </td>
                  <td className="font-mono text-[11px] text-slate-400">{o.segment || '—'}</td>
                  <td className="font-mono text-[11px] text-slate-400">{o.strategy || '—'}</td>
                  <td><Badge variant={o.transaction_type === 'BUY' ? 'buy' : 'sell'}>{o.transaction_type}</Badge></td>
                  <td className="desk-num text-slate-300">{o.quantity}{o.lots != null && o.native && o.segment !== 'BSE_EQ' ? ' lots' : ''}</td>
                  <td className="text-slate-400 text-[11px]">{o.order_type}</td>
                  <td className="desk-num text-slate-300">
                    {px > 0 ? `₹${px.toFixed(2)}` : 'MKT'}
                    <SimBadge show={o.simulated} testId={`ord-sim-${o.order_id}`} />
                  </td>
                  <td className="desk-num" data-testid={`ord-pnl-${o.order_id}`} data-value={o.pnl ?? ''}
                    title={o.pnl != null && o.entry_price != null ? `entry ₹${o.entry_price} → exit ₹${px}` : ''}>
                    {o.pnl != null ? <Pnl value={o.pnl} /> : <span className="text-slate-600 text-[10px]">entry</span>}
                  </td>
                  <td><Badge variant={statusVariant(o.status)}>{o.status}</Badge></td>
                  <td>
                    {!o.native && (o.status === 'OPEN' || o.status === 'TRIGGER PENDING') && (
                      <Btn variant="ghost" size="sm" onClick={() => handleCancel(o.order_id)}
                        className="text-rose-400 hover:bg-rose-950/40 text-[10px] py-0.5">Cancel</Btn>
                    )}
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
