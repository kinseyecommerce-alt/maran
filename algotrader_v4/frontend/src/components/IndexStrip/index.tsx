import { useEffect } from 'react'
import { clsx } from 'clsx'
import { useStore } from '../../store'
import { api } from '../../api/client'
import type { IndexQuote } from '../../types'

// Live index ticker strip — NIFTY / BANKNIFTY / FINNIFTY / MIDCPNIFTY / VIX / SENSEX.
// Pushed every few seconds over the WebSocket ("indices" event); this also
// polls /market/indices as a fallback so the strip works before WS connects.
const SHORT: Record<string, string> = {
  NIFTY: 'NIFTY', BANKNIFTY: 'BANKNIFTY', FINNIFTY: 'FINNIFTY',
  MIDCPNIFTY: 'MIDCPNIFTY', INDIAVIX: 'VIX', SENSEX: 'SENSEX',
}

const SOURCE_LABEL: Record<IndexQuote['source'], string> = {
  KITE: 'KITE', NSE: 'NSE', SIMULATED: 'SIM', UNAVAILABLE: '—',
}

function fmt(n: number | null | undefined, dp = 2) {
  if (n === null || n === undefined || Number.isNaN(n)) return '—'
  return n.toLocaleString('en-IN', { minimumFractionDigits: dp, maximumFractionDigits: dp })
}

export default function IndexStrip() {
  const indices = useStore(s => s.indices)
  const setIndices = useStore(s => s.setIndices)

  useEffect(() => {
    let alive = true
    const load = () => api.indices()
      .then(r => { if (alive && Array.isArray(r.data?.indices)) setIndices(r.data.indices) })
      .catch(() => { /* backend offline — keep last values */ })
    load()
    const t = setInterval(load, 15000)
    return () => { alive = false; clearInterval(t) }
  }, [setIndices])

  if (!indices.length) return null

  return (
    <div data-testid="index-strip"
      className="h-8 sticky top-0 z-20 bg-[#0a0c10] border-b border-[#1e2430] flex items-center gap-6 px-4 overflow-x-auto shrink-0 text-[11px] font-mono">
      {indices.map(q => {
        const up = (q.change_pct ?? 0) >= 0
        return (
          <div key={q.symbol} data-testid={`index-${q.symbol}`}
            title={`${q.name} · source ${q.source}${q.stale ? ' (stale)' : ''}${q.exchange_ts ? ` · ${q.exchange_ts}` : ''}`}
            className={clsx('flex items-center gap-2 whitespace-nowrap', (q.stale || !q.available) && 'opacity-50')}>
            <span className="text-slate-500 text-[10px] tracking-wide">{SHORT[q.symbol] ?? q.symbol}</span>
            <span className="text-slate-100 font-semibold tabular-nums">{fmt(q.ltp)}</span>
            {q.available && q.change_pct !== null && (
              <span className={clsx('tabular-nums text-[10px]', up ? 'text-emerald-400' : 'text-rose-400')}>
                {up ? '+' : ''}{fmt(q.change_pct)}%
              </span>
            )}
            <span className={clsx('text-[9px] px-1 rounded border',
              q.source === 'SIMULATED' ? 'text-amber-300 bg-amber-500/10 border-amber-500/40 font-bold'
                : q.source === 'UNAVAILABLE' ? 'text-slate-600 border-transparent'
                : 'text-slate-500 bg-slate-800/50 border-slate-700/50')}>
              {SOURCE_LABEL[q.source] ?? q.source}
            </span>
          </div>
        )
      })}
    </div>
  )
}
