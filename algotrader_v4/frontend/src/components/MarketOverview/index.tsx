import { useEffect } from 'react'
import { useStore } from '../../store'
import { api } from '../../api/client'
import type { IndexQuote, OverviewStock } from '../../types'

const fmt = (v: number | null | undefined, d = 2) =>
  v == null ? '—' : v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d })

/** Source badge. Real sources muted, SIMULATED amber + explicit word, never ambiguous. */
function SourceBadge({ source, stale, testId }: { source: string; stale?: boolean; testId?: string }) {
  let cls = 'text-slate-500 bg-slate-800/60 border-slate-700/50'
  let text = source
  if (source === 'SIMULATED') { cls = 'text-amber-300 bg-amber-500/15 border-amber-500/50 font-bold'; text = 'SIMULATED' }
  else if (source === 'UNAVAILABLE') { cls = 'text-slate-600 bg-transparent border-transparent'; text = 'NO DATA' }
  else if (stale) { cls = 'text-amber-400/80 bg-amber-950/30 border-amber-900/40'; text = `${source} · STALE` }
  else { cls = 'text-emerald-500/90 bg-emerald-950/30 border-emerald-900/40' }
  return <span data-testid={testId} className={`text-[9px] font-mono px-1 py-px rounded border ${cls}`}>{text}</span>
}

function Change({ pct, muted }: { pct: number | null | undefined; muted?: boolean }) {
  if (pct == null) return null
  const up = pct >= 0
  return (
    <div className={`font-mono text-[10px] tabular-nums text-right mt-0.5 ${muted ? 'text-slate-500' : up ? 'text-emerald-400' : 'text-rose-400'}`}>
      {up ? '+' : ''}{pct.toFixed(2)}%
    </div>
  )
}

function IndexRow({ q }: { q: IndexQuote }) {
  const sim = q.source === 'SIMULATED'
  return (
    <div data-testid={`mo-index-${q.symbol}`} data-source={q.source}
         className="flex justify-between items-center px-3 py-1.5 border-b border-[#1e2430]/60 hover:bg-[#161a22]/40">
      <div>
        <div className="font-semibold text-slate-200 text-[11px] tracking-wide">{q.symbol === 'INDIAVIX' ? 'INDIA VIX' : q.symbol}</div>
        <div className="mt-0.5"><SourceBadge source={q.source} stale={q.stale} /></div>
      </div>
      <div className="text-right">
        <div className={`font-mono text-sm tabular-nums font-semibold ${sim ? 'text-amber-200/80 italic' : q.stale ? 'text-slate-400' : 'text-slate-100'}`}>{fmt(q.ltp)}</div>
        <Change pct={q.change_pct} muted={sim || q.stale} />
      </div>
    </div>
  )
}

function StockRow({ s }: { s: OverviewStock }) {
  const sim = s.source === 'SIMULATED'
  return (
    <div data-testid={`mo-stock-${s.symbol}`} data-source={s.source}
         className={`flex justify-between items-center px-3 py-1.5 border-b border-[#1e2430]/60 hover:bg-[#161a22]/40 ${sim ? 'bg-amber-500/[0.03]' : ''}`}>
      <div className="min-w-0">
        <div className="font-semibold text-slate-200 text-[11px]">{s.symbol}</div>
        <div className="mt-0.5"><SourceBadge source={s.source} stale={s.stale && s.real} /></div>
      </div>
      <div className="text-right">
        <div className={`font-mono text-sm tabular-nums font-semibold ${sim ? 'text-amber-200/80 italic' : s.real && !s.stale ? 'text-slate-100' : 'text-slate-400'}`}
             title={sim ? 'Paper simulator price — not a market quote' : ''}>
          {fmt(s.ltp)}
        </div>
        <Change pct={s.change_pct} muted={sim} />
        {sim && s.ref_close != null && (
          <div className="text-[9px] font-mono text-slate-600 mt-0.5" title="Real NSE end-of-day close">
            NSE close {s.ref_close_date?.slice(5)}: {fmt(s.ref_close)}
          </div>
        )}
      </div>
    </div>
  )
}

export default function MarketOverview() {
  const { overview, setOverview, indices: wsIndices } = useStore()

  useEffect(() => {
    const load = () => api.marketOverview(20).then(r => setOverview(r.data)).catch(() => {})
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [])

  const indices = (wsIndices.length ? wsIndices : overview?.indices || []).filter(q => q.symbol !== 'SENSEX' || q.available)
  const stocks = overview?.stocks || []
  const chart = overview?.chart
  const pts = chart?.points || []
  const closes = pts.map(p => p.close)
  const min = Math.min(...closes), max = Math.max(...closes)
  const ys = closes.map(v => 10 + ((v - min) / (max - min || 1)) * 80)
  const line = ys.map((h, i) => `${(i / Math.max(ys.length - 1, 1)) * 100},${100 - h}`).join(' ')
  const last = pts[pts.length - 1]

  return (
    <div className="w-72 flex flex-col bg-[#11141a] border-l border-[#1e2430] shrink-0" data-testid="market-overview">
      <div className="flex-1 border-b border-[#1e2430] flex flex-col min-h-0">
        <div className="px-3 py-2 border-b border-[#1e2430] flex justify-between items-center bg-[#0a0c10]/50 shrink-0">
          <h3 className="text-[10px] font-semibold tracking-[0.14em] uppercase text-slate-500">
            Market Overview
          </h3>
        </div>
        <div className="flex-1 overflow-y-auto acc-scroll">
          <div className="px-3 pt-2 pb-1 text-[9px] tracking-[0.12em] uppercase text-slate-600">Indices · Live Feed</div>
          {indices.length ? indices.map(q => <IndexRow key={q.symbol} q={q} />)
            : <div className="px-3 py-2 text-[10px] text-slate-600">Loading index feed…</div>}
          <div className="px-3 pt-3 pb-1 text-[9px] tracking-[0.12em] uppercase text-slate-600 flex justify-between">
            <span>Stocks</span>
            {overview && <span data-testid="mo-stock-feed"
              className={overview.stock_feed === 'REAL' ? 'text-emerald-500' : 'text-amber-400 font-bold'}>{overview.stock_feed}</span>}
          </div>
          {overview?.note && (
            <div data-testid="mo-note" className="mx-3 mb-1 px-2 py-1 rounded text-[9px] leading-snug text-amber-300/90 bg-amber-500/10 border border-amber-500/30">
              {overview.note}
            </div>
          )}
          {stocks.length ? stocks.map(s => <StockRow key={s.symbol} s={s} />)
            : <div className="px-3 py-2 text-[10px] text-slate-600">No subscribed stocks yet — start the bot.</div>}
        </div>
      </div>

      <div className="h-44 p-3 bg-[#0a0c10]/40 flex flex-col shrink-0" data-testid="nifty-chart" data-source={chart?.source || 'UNAVAILABLE'}>
        <div className="flex justify-between items-center mb-2">
          <h3 className="text-[10px] font-semibold tracking-[0.14em] uppercase text-slate-500">
            Nifty · {pts.length ? `${pts.length}D` : ''}
          </h3>
          {last && <span className="text-[10px] font-mono text-emerald-400 tabular-nums">{fmt(last.close)}</span>}
        </div>
        <div className="flex-1 relative border border-[#1e2430] rounded bg-[#0a0c10] overflow-hidden">
          {pts.length >= 2 ? (
            <svg className="absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none">
              <polyline points={line} fill="none" stroke="rgba(34,197,94,0.75)" strokeWidth="1.25" vectorEffect="non-scaling-stroke" />
              <polygon points={`0,100 ${line} 100,100`} fill="rgba(34,197,94,0.05)" />
            </svg>
          ) : (
            <div className="absolute inset-0 flex items-center justify-center text-[10px] text-slate-600">No real NIFTY history available</div>
          )}
          <div className="absolute bottom-1.5 right-1.5 text-[9px] font-mono px-1.5 py-0.5 rounded bg-[#11141a]/90 border border-[#1e2430] text-slate-500">
            {chart?.source === 'NSE' ? `NSE daily close${chart.live_source ? ` + live ${chart.live_source}` : ''}` : 'NO DATA'}
          </div>
        </div>
      </div>
    </div>
  )
}
