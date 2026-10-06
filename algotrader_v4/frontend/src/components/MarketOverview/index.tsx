import { useEffect } from 'react'
import { Activity, BarChart3, TrendingUp, TrendingDown } from 'lucide-react'
import { useStore } from '../../store'
import { api } from '../../api/client'
import type { IndexQuote, OverviewStock } from '../../types'

const fmt = (v: number | null | undefined, d = 2) =>
  v == null ? '—' : v.toLocaleString('en-IN', { minimumFractionDigits: d, maximumFractionDigits: d })

/** Source badge. Real sources green, SIMULATED amber + explicit word, never ambiguous. */
function SourceBadge({ source, stale, testId }: { source: string; stale?: boolean; testId?: string }) {
  let cls = 'text-slate-500 bg-slate-800'
  let text = source
  if (source === 'SIMULATED') { cls = 'text-amber-300 bg-amber-500/15 border border-amber-500/40'; text = 'SIMULATED' }
  else if (source === 'UNAVAILABLE') { cls = 'text-slate-500 bg-slate-800'; text = 'NO DATA' }
  else if (stale) { cls = 'text-amber-400 bg-amber-500/10'; text = `${source} · STALE` }
  else { cls = 'text-emerald-400 bg-emerald-500/10' }
  return <span data-testid={testId} className={`text-[9px] font-mono px-1 py-px rounded ${cls}`}>{text}</span>
}

function Change({ pct, muted }: { pct: number | null | undefined; muted?: boolean }) {
  if (pct == null) return null
  const up = pct >= 0
  return (
    <div className={`font-mono text-[10px] flex items-center justify-end gap-1 mt-0.5 ${muted ? 'text-slate-500' : up ? 'text-emerald-400' : 'text-rose-400'}`}>
      {up ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
      {up ? '+' : ''}{pct.toFixed(2)}%
    </div>
  )
}

function IndexRow({ q }: { q: IndexQuote }) {
  const sim = q.source === 'SIMULATED'
  return (
    <div data-testid={`mo-index-${q.symbol}`} data-source={q.source}
         className="flex justify-between items-center px-3 py-2 border-b border-slate-800/50">
      <div>
        <div className="font-bold text-slate-200 text-xs">{q.symbol === 'INDIAVIX' ? 'INDIA VIX' : q.symbol}</div>
        <div className="mt-0.5"><SourceBadge source={q.source} stale={q.stale} /></div>
      </div>
      <div className="text-right">
        <div className={`font-mono text-sm ${sim ? 'text-amber-200/80 italic' : q.stale ? 'text-slate-400' : 'text-slate-100'}`}>{fmt(q.ltp)}</div>
        <Change pct={q.change_pct} muted={sim || q.stale} />
      </div>
    </div>
  )
}

function StockRow({ s }: { s: OverviewStock }) {
  const sim = s.source === 'SIMULATED'
  return (
    <div data-testid={`mo-stock-${s.symbol}`} data-source={s.source}
         className={`flex justify-between items-center px-3 py-2 border-b border-slate-800/50 ${sim ? 'bg-amber-500/[0.03]' : ''}`}>
      <div className="min-w-0">
        <div className="font-bold text-slate-200 text-xs">{s.symbol}</div>
        <div className="mt-0.5 flex items-center gap-1">
          <SourceBadge source={s.source} stale={s.stale && s.real} />
        </div>
      </div>
      <div className="text-right">
        <div className={`font-mono text-sm ${sim ? 'text-amber-200/80 italic' : s.real && !s.stale ? 'text-slate-100' : 'text-slate-400'}`}
             title={sim ? 'Paper simulator price — not a market quote' : ''}>
          {fmt(s.ltp)}
        </div>
        <Change pct={s.change_pct} muted={sim} />
        {sim && s.ref_close != null && (
          <div className="text-[9px] font-mono text-slate-500 mt-0.5" title="Real NSE end-of-day close">
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

  // Index rows: the same feed as the index strip (WS 'indices' pushes are newer).
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
    <div className="w-64 flex flex-col bg-slate-900 shrink-0" data-testid="market-overview">
      <div className="flex-1 border-b border-slate-800 flex flex-col min-h-0">
        <div className="p-3 border-b border-slate-800 flex justify-between items-center bg-slate-950/50 shrink-0">
          <h3 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
            <Activity className="w-3.5 h-3.5" /> MARKET OVERVIEW
          </h3>
        </div>
        <div className="flex-1 overflow-y-auto acc-scroll">
          <div className="px-3 pt-2 pb-1 text-[9px] tracking-widest text-slate-600">INDICES · LIVE FEED</div>
          {indices.length ? indices.map(q => <IndexRow key={q.symbol} q={q} />)
            : <div className="px-3 py-2 text-[10px] text-slate-600">Loading index feed…</div>}
          <div className="px-3 pt-3 pb-1 text-[9px] tracking-widest text-slate-600 flex justify-between">
            <span>STOCKS</span>
            {overview && <span data-testid="mo-stock-feed"
              className={overview.stock_feed === 'REAL' ? 'text-emerald-500' : 'text-amber-400'}>{overview.stock_feed}</span>}
          </div>
          {overview?.note && (
            <div data-testid="mo-note" className="mx-3 mb-1 px-2 py-1 rounded text-[9px] leading-snug text-amber-300/90 bg-amber-500/10 border border-amber-500/20">
              {overview.note}
            </div>
          )}
          {stocks.length ? stocks.map(s => <StockRow key={s.symbol} s={s} />)
            : <div className="px-3 py-2 text-[10px] text-slate-600">No subscribed stocks yet — start the bot.</div>}
        </div>
      </div>

      <div className="h-48 p-3 bg-slate-950/30 flex flex-col shrink-0" data-testid="nifty-chart" data-source={chart?.source || 'UNAVAILABLE'}>
        <div className="flex justify-between items-center mb-2">
          <h3 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
            <BarChart3 className="w-3.5 h-3.5" /> NIFTY · {pts.length ? `${pts.length}D` : ''}
          </h3>
          {last && <span className="text-[10px] font-mono text-emerald-400">{fmt(last.close)}</span>}
        </div>
        <div className="flex-1 relative border border-slate-800 rounded bg-[#0b1120] overflow-hidden">
          {pts.length >= 2 ? (
            <svg className="absolute inset-0 h-full w-full" viewBox="0 0 100 100" preserveAspectRatio="none">
              <polyline points={line} fill="none" stroke="rgba(16,185,129,0.85)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
              <polygon points={`0,100 ${line} 100,100`} fill="rgba(16,185,129,0.06)" />
            </svg>
          ) : (
            <div className="absolute inset-0 flex items-center justify-center text-[10px] text-slate-600">No real NIFTY history available</div>
          )}
          <div className="absolute bottom-1.5 right-1.5 text-[9px] font-mono px-1.5 py-0.5 rounded bg-slate-900/80 border border-slate-700/50 text-slate-400">
            {chart?.source === 'NSE' ? `NSE daily close${chart.live_source ? ` + live ${chart.live_source}` : ''}` : 'NO DATA'}
          </div>
        </div>
      </div>
    </div>
  )
}
