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
      className="h-7 bg-slate-950 border-b border-slate-800 flex items-center gap-5 px-4 overflow-x-auto shrink-0 text-[11px] font-mono">
      {indices.map(q => {
        const up = (q.change_pct ?? 0) >= 0
        return (
          <div key={q.symbol} data-testid={`index-${q.symbol}`}
            title={`${q.name} · source ${q.source}${q.stale ? ' (stale)' : ''}${q.exchange_ts ? ` · ${q.exchange_ts}` : ''}`}
            className={clsx('flex items-center gap-1.5 whitespace-nowrap', (q.stale || !q.available) && 'opacity-50')}>
            <span className="text-slate-500">{SHORT[q.symbol] ?? q.symbol}</span>
            <span className="text-slate-200">{fmt(q.ltp)}</span>
            {q.available && q.change_pct !== null && (
              <span className={up ? 'text-emerald-400' : 'text-rose-400'}>
                {up ? '▲' : '▼'} {fmt(Math.abs(q.change_pct))}%
              </span>
            )}
            <span className={clsx('text-[9px] px-1 rounded',
              q.source === 'SIMULATED' ? 'text-amber-400 bg-amber-400/10'
                : q.source === 'UNAVAILABLE' ? 'text-slate-600'
                : 'text-sky-400 bg-sky-400/10')}>
              {SOURCE_LABEL[q.source] ?? q.source}
            </span>
          </div>
        )
      })}
    </div>
  )
}
