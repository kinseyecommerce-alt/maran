import { useEffect, useMemo, useRef, useState } from 'react'
import { createChart, IChartApi, ColorType } from 'lightweight-charts'
import { api } from '../../api/client'

type Trade = {
  symbol: string; strategy?: string; side?: string
  net_pnl?: number; gross_pnl?: number
  trade_date?: string; exit_time?: string
}

const RANGES = [7, 30, 90] as const

const BASE_OPTS = {
  layout: { background: { type: ColorType.Solid, color: '#FFFFFF' }, textColor: '#64748B', fontSize: 11 },
  grid: { vertLines: { color: '#F1F5F9' }, horzLines: { color: '#F1F5F9' } },
  timeScale: { borderColor: '#E2E8F0' },
  rightPriceScale: { borderColor: '#E2E8F0' },
  handleScroll: false, handleScale: false,
}

const inr = (n: number) => `₹${Math.round(n).toLocaleString('en-IN')}`

// A self-contained lightweight-charts panel that (re)builds a single series.
function MiniChart({ kind, data, height = 200 }:
  { kind: 'area' | 'hist' | 'ddArea'; data: { time: string; value: number; color?: string }[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null)
  const chart = useRef<IChartApi | null>(null)
  const series = useRef<any>(null)

  useEffect(() => {
    if (!ref.current) return
    const c = createChart(ref.current, { ...BASE_OPTS, width: ref.current.clientWidth, height })
    chart.current = c
    if (kind === 'hist') {
      series.current = c.addHistogramSeries({ priceFormat: { type: 'volume' } })
    } else if (kind === 'ddArea') {
      series.current = c.addAreaSeries({ lineColor: '#DC2626', topColor: 'rgba(220,38,38,0.05)', bottomColor: 'rgba(220,38,38,0.28)', lineWidth: 2 })
    } else {
      series.current = c.addAreaSeries({ lineColor: '#4F46E5', topColor: 'rgba(79,70,229,0.28)', bottomColor: 'rgba(79,70,229,0.02)', lineWidth: 2 })
    }
    const ro = new ResizeObserver(e => { for (const en of e) c.applyOptions({ width: en.contentRect.width }) })
    ro.observe(ref.current)
    return () => { ro.disconnect(); c.remove() }
  }, [])

  useEffect(() => {
    if (!series.current) return
    series.current.setData(data as any)
    chart.current?.timeScale().fitContent()
  }, [data])

  return <div ref={ref} className="w-full" style={{ height }} />
}

export default function ChartsTab() {
  const [days, setDays] = useState<number>(30)
  const [trades, setTrades] = useState<Trade[]>([])

  useEffect(() => {
    const load = () => api.tradeHistory(days).then(r => setTrades(r.data.trades || [])).catch(() => {})
    load()
    const t = setInterval(load, 20000)
    return () => clearInterval(t)
  }, [days])

  const m = useMemo(() => {
    const pnl = (t: Trade) => (t.net_pnl ?? t.gross_pnl ?? 0)
    const sorted = [...trades].sort((a, b) =>
      (a.trade_date || '').localeCompare(b.trade_date || '') ||
      (a.exit_time || '').localeCompare(b.exit_time || ''))

    // Daily aggregation → equity curve (end-of-day cum), daily P&L bars, drawdown.
    const byDay = new Map<string, number>()
    for (const t of sorted) {
      const d = (t.trade_date || (t.exit_time || '').slice(0, 10)) || '—'
      byDay.set(d, (byDay.get(d) || 0) + pnl(t))
    }
    const dates = [...byDay.keys()].sort()
    let cum = 0, peak = 0
    const equity: any[] = [], daily: any[] = [], drawdown: any[] = []
    for (const d of dates) {
      const dp = byDay.get(d) || 0
      cum += dp; peak = Math.max(peak, cum)
      equity.push({ time: d, value: Math.round(cum) })
      daily.push({ time: d, value: Math.round(dp), color: dp >= 0 ? '#16A34A' : '#DC2626' })
      drawdown.push({ time: d, value: Math.round(cum - peak) })
    }

    // Stats
    const wins = sorted.filter(t => pnl(t) > 0)
    const losses = sorted.filter(t => pnl(t) < 0)
    const total = sorted.reduce((s, t) => s + pnl(t), 0)
    const grossWin = wins.reduce((s, t) => s + pnl(t), 0)
    const grossLoss = Math.abs(losses.reduce((s, t) => s + pnl(t), 0))
    const stats = {
      total, n: sorted.length,
      winRate: sorted.length ? (wins.length / sorted.length) * 100 : 0,
      avgWin: wins.length ? grossWin / wins.length : 0,
      avgLoss: losses.length ? grossLoss / losses.length : 0,
      profitFactor: grossLoss ? grossWin / grossLoss : (grossWin > 0 ? Infinity : 0),
      maxDD: drawdown.length ? Math.min(...drawdown.map(d => d.value)) : 0,
    }

    // Per-symbol P&L (top movers both ways)
    const bySym = new Map<string, number>()
    for (const t of sorted) bySym.set(t.symbol, (bySym.get(t.symbol) || 0) + pnl(t))
    const symbols = [...bySym.entries()].map(([s, v]) => ({ s, v: Math.round(v) }))
      .sort((a, b) => b.v - a.v)
    const maxAbs = Math.max(1, ...symbols.map(x => Math.abs(x.v)))

    return { equity, daily, drawdown, stats, symbols, maxAbs }
  }, [trades])

  const Tile = ({ label, value, tone }: { label: string; value: string; tone?: 'up' | 'down' }) => (
    <div className="bg-white border border-slate-200 rounded-lg px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-slate-400">{label}</div>
      <div className={`text-lg font-mono font-bold ${tone === 'up' ? 'text-green-600' : tone === 'down' ? 'text-red-600' : 'text-slate-800'}`}>{value}</div>
    </div>
  )

  return (
    <div className="h-full overflow-y-auto p-3 space-y-3 bg-slate-50">
      <div className="flex items-center gap-2">
        <span className="text-sm font-semibold text-slate-700">Performance Charts</span>
        <div className="ml-auto flex gap-1">
          {RANGES.map(r => (
            <button key={r} onClick={() => setDays(r)}
              className={`px-2 py-1 text-xs rounded font-medium ${days === r ? 'bg-indigo-600 text-white' : 'bg-white border border-slate-200 text-slate-600'}`}>
              {r}d
            </button>
          ))}
        </div>
      </div>

      {/* Stat tiles */}
      <div className="grid grid-cols-3 md:grid-cols-6 gap-2">
        <Tile label="Net P&L" value={inr(m.stats.total)} tone={m.stats.total >= 0 ? 'up' : 'down'} />
        <Tile label="Trades" value={String(m.stats.n)} />
        <Tile label="Win Rate" value={`${m.stats.winRate.toFixed(1)}%`} tone={m.stats.winRate >= 50 ? 'up' : 'down'} />
        <Tile label="Avg Win" value={inr(m.stats.avgWin)} tone="up" />
        <Tile label="Avg Loss" value={inr(m.stats.avgLoss)} tone="down" />
        <Tile label="Profit Factor" value={m.stats.profitFactor === Infinity ? '∞' : m.stats.profitFactor.toFixed(2)}
          tone={m.stats.profitFactor >= 1 ? 'up' : 'down'} />
      </div>

      {m.equity.length === 0 ? (
        <div className="bg-white border border-slate-200 rounded-lg p-8 text-center text-slate-400 text-sm">
          No closed trades in the last {days} days yet.
        </div>
      ) : (
        <>
          {/* ② Equity curve */}
          <div className="bg-white border border-slate-200 rounded-lg p-3">
            <div className="text-xs font-semibold text-slate-600 mb-1">Equity Curve — cumulative net P&L</div>
            <MiniChart kind="area" data={m.equity} height={220} />
          </div>

          {/* ③ Daily P&L + drawdown */}
          <div className="grid md:grid-cols-2 gap-3">
            <div className="bg-white border border-slate-200 rounded-lg p-3">
              <div className="text-xs font-semibold text-slate-600 mb-1">Daily P&L</div>
              <MiniChart kind="hist" data={m.daily} height={200} />
            </div>
            <div className="bg-white border border-slate-200 rounded-lg p-3">
              <div className="text-xs font-semibold text-slate-600 mb-1">Drawdown (max {inr(m.stats.maxDD)})</div>
              <MiniChart kind="ddArea" data={m.drawdown} height={200} />
            </div>
          </div>

          {/* ④ Per-symbol P&L */}
          <div className="bg-white border border-slate-200 rounded-lg p-3">
            <div className="text-xs font-semibold text-slate-600 mb-2">P&L by Symbol</div>
            <div className="space-y-1">
              {m.symbols.slice(0, 20).map(({ s, v }) => (
                <div key={s} className="flex items-center gap-2 text-xs">
                  <span className="w-24 shrink-0 font-mono text-slate-600 truncate">{s}</span>
                  <div className="flex-1 flex items-center">
                    <div className="flex-1 flex justify-end">
                      {v < 0 && <div className="h-3 rounded-l bg-red-500" style={{ width: `${(Math.abs(v) / m.maxAbs) * 100}%` }} />}
                    </div>
                    <div className="w-px h-4 bg-slate-300" />
                    <div className="flex-1">
                      {v >= 0 && <div className="h-3 rounded-r bg-green-500" style={{ width: `${(v / m.maxAbs) * 100}%` }} />}
                    </div>
                  </div>
                  <span className={`w-20 shrink-0 text-right font-mono font-medium ${v >= 0 ? 'text-green-600' : 'text-red-600'}`}>{inr(v)}</span>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
