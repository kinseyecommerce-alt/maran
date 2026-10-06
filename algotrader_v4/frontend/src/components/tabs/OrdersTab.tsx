import { useEffect, useState } from 'react'
import { useStore } from '../../store'
import { api } from '../../api/client'
import { Badge, Btn } from '../ui'
import { SegmentFilter, SimBadge } from './SegmentFilter'

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

/** Today's orders across ALL segments (/portfolio/orders → book.py). */
export default function OrdersTab() {
  const { orders, setOrders, addToast } = useStore()
  const [seg, setSeg] = useState('ALL')

  const load = () => api.orders().then(r => setOrders(r.data || [])).catch(() => {})
  useEffect(() => {
    load()
    const t = setInterval(load, 3000)
    return () => clearInterval(t)
  }, [])

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

  return (
    <div className="h-full overflow-auto" data-testid="orders-tab">
      <div className="flex items-center justify-between px-4 py-2 border-b border-slate-700/60 bg-slate-900 sticky top-0 gap-3">
        <span className="text-sm font-semibold text-slate-200" data-testid="orders-count">{shown.length} order{shown.length !== 1 ? 's' : ''} today</span>
        <SegmentFilter value={seg} onChange={setSeg} rows={orders} testId="ord-filter" />
      </div>
      {shown.length === 0 ? (
        <div className="flex items-center justify-center h-40 text-slate-400 text-sm">No orders today{seg !== 'ALL' ? ` in ${seg}` : ''}</div>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-700/50 bg-slate-800/40 sticky top-10">
              {['Time', 'Order ID', 'Symbol', 'Segment', 'Strategy', 'Side', 'Qty', 'Type', 'Price', 'Status', ''].map(h => (
                <th key={h} className="text-left px-3 py-2 text-xs font-medium text-slate-500">{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map(o => {
              const px = o.average_price || o.price
              return (
                <tr key={o.order_id} data-testid={`ord-row-${o.order_id}`} data-segment={o.segment || ''}
                  className="border-b border-slate-700/30 hover:bg-slate-800/40 transition-colors">
                  <td className="px-3 py-2 font-mono text-[11px] text-slate-500">{hhmmss(o.placed_at)}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-slate-500" title={o.order_id}>{o.order_id.slice(-8)}</td>
                  <td className="px-3 py-2 font-semibold text-slate-100">{o.tradingsymbol}
                    <div className="text-[10px] text-slate-500 font-normal">{o.exchange} · {o.product}{o.tag ? ` · ${o.tag}` : ''}</div></td>
                  <td className="px-3 py-2 font-mono text-[11px] text-slate-400">{o.segment || '—'}</td>
                  <td className="px-3 py-2 font-mono text-[11px] text-slate-400">{o.strategy || '—'}</td>
                  <td className="px-3 py-2"><Badge variant={o.transaction_type === 'BUY' ? 'buy' : 'sell'}>{o.transaction_type}</Badge></td>
                  <td className="px-3 py-2 font-mono text-slate-300">{o.quantity}{o.lots != null && o.native && o.segment !== 'BSE_EQ' ? ' lots' : ''}</td>
                  <td className="px-3 py-2 text-slate-400 text-xs">{o.order_type}</td>
                  <td className="px-3 py-2 font-mono text-slate-300">
                    {px > 0 ? `₹${px.toFixed(2)}` : 'MKT'}
                    <SimBadge show={o.simulated} testId={`ord-sim-${o.order_id}`} />
                  </td>
                  <td className="px-3 py-2"><Badge variant={statusVariant(o.status)}>{o.status}</Badge></td>
                  <td className="px-3 py-2">
                    {!o.native && (o.status === 'OPEN' || o.status === 'TRIGGER PENDING') && (
                      <Btn variant="ghost" size="sm" onClick={() => handleCancel(o.order_id)}
                        className="text-rose-400 hover:bg-rose-950/40 text-xs py-1">Cancel</Btn>
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
