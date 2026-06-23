import React, { useEffect } from 'react'
import { useStore } from '../../store'
import { api } from '../../api/client'
import { Pnl, Badge } from '../ui'

export default function PositionsTab() {
  const { positions, ticks, setPositions } = useStore()

  useEffect(() => {
    api.positions().then(r => setPositions(r.data.net || [])).catch(() => {})
    const t = setInterval(() => api.positions().then(r => setPositions(r.data.net || [])).catch(() => {}), 5000)
    return () => clearInterval(t)
  }, [])

  const openPositions = positions.filter(p => p.quantity !== 0)

  if (openPositions.length === 0) {
    return <div className="flex items-center justify-center h-full text-slate-400 text-sm">No open positions</div>
  }

  const totalPnl = openPositions.reduce((acc, p) => acc + (p.pnl || 0), 0)

  return (
    <div className="h-full overflow-auto">
      <div className="flex items-center justify-between px-4 py-2 border-b border-slate-700/60 bg-slate-900 sticky top-0">
        <span className="text-sm font-semibold text-slate-200">{openPositions.length} Open Position{openPositions.length !== 1 ? 's' : ''}</span>
        <div className="flex items-center gap-2">
          <span className="text-xs text-slate-500">Total P&L:</span>
          <Pnl value={totalPnl} />
        </div>
      </div>
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-700/50 bg-slate-800/40">
            {['Symbol', 'Qty', 'Avg Price', 'LTP', 'P&L', 'Change'].map(h => (
              <th key={h} className="text-left px-4 py-2 text-xs font-medium text-slate-500">{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {openPositions.map(pos => {
            const tick = ticks[pos.tradingsymbol]
            const ltp = tick?.ltp ?? pos.last_price
            const pnl = (ltp - pos.average_price) * pos.quantity
            const pct = pos.average_price > 0 ? ((ltp - pos.average_price) / pos.average_price * 100) : 0
            return (
              <tr key={pos.tradingsymbol} className="border-b border-slate-700/30 hover:bg-slate-800/40 transition-colors">
                <td className="px-4 py-3">
                  <div className="font-semibold text-slate-100">{pos.tradingsymbol}</div>
                  <div className="text-xs text-slate-500">{pos.product} · {pos.exchange}</div>
                </td>
                <td className="px-4 py-3">
                  <Badge variant={pos.quantity > 0 ? 'buy' : 'sell'}>
                    {pos.quantity > 0 ? '+' : ''}{pos.quantity}
                  </Badge>
                </td>
                <td className="px-4 py-3 font-mono text-slate-300">₹{pos.average_price.toFixed(2)}</td>
                <td className="px-4 py-3 font-mono font-semibold text-slate-100">₹{ltp.toFixed(2)}</td>
                <td className="px-4 py-3"><Pnl value={pnl} /></td>
                <td className="px-4 py-3">
                  <span className={`text-xs font-mono font-medium ${pct >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {pct >= 0 ? '+' : ''}{pct.toFixed(2)}%
                  </span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
