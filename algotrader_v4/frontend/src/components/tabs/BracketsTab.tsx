import React, { useEffect } from 'react'
import { useStore } from '../../store'
import { api } from '../../api/client'
import type { Bracket } from '../../types'

const statusColor = (s: Bracket['status']) => {
  const m: Record<string, string> = {
    ACTIVE:      'bg-emerald-900/60 text-emerald-300 border border-emerald-800/60',
    SL_HIT:      'bg-rose-900/60 text-rose-300 border border-rose-800/60',
    TARGET_HIT:  'bg-blue-900/60 text-blue-300 border border-blue-800/60',
    PENDING:     'bg-amber-900/60 text-amber-300 border border-amber-800/60',
    CANCELLED:   'bg-slate-700/60 text-slate-400 border border-slate-600/60',
    FAILED:      'bg-rose-900/60 text-rose-300 border border-rose-800/60',
  }
  return m[s] || 'bg-slate-700/60 text-slate-400 border border-slate-600/60'
}

export default function BracketsTab() {
  const { brackets, setBrackets } = useStore()

  useEffect(() => {
    api.brackets().then(r => setBrackets(r.data.brackets || [])).catch(() => {})
    const t = setInterval(() => api.brackets().then(r => setBrackets(r.data.brackets || [])).catch(() => {}), 5000)
    return () => clearInterval(t)
  }, [])

  if (brackets.length === 0) {
    return <div className="flex items-center justify-center h-full text-slate-400 text-sm">No brackets yet</div>
  }

  return (
    <div className="h-full overflow-auto p-4 space-y-3">
      {brackets.map(b => {
        const range = b.target_1 && b.stop_loss ? b.target_1 - b.stop_loss : 0
        const progress = range > 0 && b.entry_price
          ? Math.max(0, Math.min(100, ((b.entry_price - b.stop_loss!) / range) * 100))
          : 50

        return (
          <div key={b.bracket_id} className="bg-slate-800/60 border border-slate-700/50 rounded-xl p-4">
            <div className="flex items-center justify-between mb-3">
              <div>
                <span className="font-bold text-slate-100">{b.symbol}</span>
                <span className="ml-2 text-xs text-slate-500">{b.strategy} · {b.side}</span>
              </div>
              <span className={`text-xs px-2 py-0.5 rounded-full font-semibold ${statusColor(b.status)}`}>
                {b.status}
              </span>
            </div>
            <div className="grid grid-cols-4 gap-3 text-xs mb-3">
              <div>
                <div className="text-slate-500">Entry</div>
                <div className="font-mono font-medium text-slate-200">₹{b.entry_price.toFixed(2)}</div>
              </div>
              <div>
                <div className="text-slate-500">Stop Loss</div>
                <div className="font-mono font-medium text-rose-400">{b.stop_loss ? `₹${b.stop_loss.toFixed(2)}` : '—'}</div>
              </div>
              <div>
                <div className="text-slate-500">Target 1</div>
                <div className="font-mono font-medium text-emerald-400">{b.target_1 ? `₹${b.target_1.toFixed(2)}` : '—'}</div>
              </div>
              <div>
                <div className="text-slate-500">Qty</div>
                <div className="font-mono font-medium text-slate-200">{b.quantity}</div>
              </div>
            </div>
            {range > 0 && (
              <div className="h-1.5 rounded-full bg-slate-700/60 overflow-hidden">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-rose-500 via-amber-400 to-emerald-500 transition-all"
                  style={{ width: `${progress}%` }}
                />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}
