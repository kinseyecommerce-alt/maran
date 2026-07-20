import { useEffect, useRef } from 'react'
import { createChart, IChartApi, ISeriesApi, IPriceLine, ColorType } from 'lightweight-charts'
import { useStore } from '../../store'
import { api } from '../../api/client'

const CHART_OPTS = {
  layout: { background: { type: ColorType.Solid, color: '#FFFFFF' }, textColor: '#64748B', fontSize: 11 },
  grid: { vertLines: { color: '#F1F5F9' }, horzLines: { color: '#F1F5F9' } },
  crosshair: { mode: 1 },
  timeScale: { borderColor: '#E2E8F0', timeVisible: true, secondsVisible: false },
  rightPriceScale: { borderColor: '#E2E8F0', scaleMargins: { top: 0.05, bottom: 0.28 } },
  handleScroll: true,
  handleScale: true,
}

// The four EMA-pullback strategy EMAs — distinct, candle-safe colours.
const EMA_STYLE: { key: 'ema55' | 'ema89' | 'ema144' | 'ema233'; label: string; color: string }[] = [
  { key: 'ema55',  label: 'EMA55',  color: '#2563EB' },
  { key: 'ema89',  label: 'EMA89',  color: '#F59E0B' },
  { key: 'ema144', label: 'EMA144', color: '#7C3AED' },
  { key: 'ema233', label: 'EMA233', color: '#0891B2' },
]

export default function MainChart() {
  const { selectedSymbol, ticks, positions } = useStore()
  const chartRef = useRef<HTMLDivElement>(null)
  const chart    = useRef<IChartApi | null>(null)
  const candles  = useRef<ISeriesApi<'Candlestick'> | null>(null)
  const emas     = useRef<Record<string, ISeriesApi<'Line'>>>({})
  const rsi      = useRef<ISeriesApi<'Line'> | null>(null)
  const pdLines  = useRef<IPriceLine[]>([])
  const tradeLines = useRef<IPriceLine[]>([])

  // Init chart + all series once.
  useEffect(() => {
    if (!chartRef.current) return
    const c = createChart(chartRef.current, { ...CHART_OPTS, width: chartRef.current.clientWidth, height: 380 })
    chart.current = c

    candles.current = c.addCandlestickSeries({
      upColor: '#16A34A', downColor: '#DC2626',
      borderUpColor: '#16A34A', borderDownColor: '#DC2626',
      wickUpColor: '#16A34A', wickDownColor: '#DC2626',
    })
    for (const e of EMA_STYLE) {
      emas.current[e.key] = c.addLineSeries({
        color: e.color, lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      })
    }
    // RSI in a bottom pane on its own price scale, with 40/60 reference lines.
    rsi.current = c.addLineSeries({
      color: '#A855F7', lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
      priceScaleId: 'rsi',
    })
    c.priceScale('rsi').applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } })
    rsi.current.createPriceLine({ price: 60, color: '#F87171', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '60' })
    rsi.current.createPriceLine({ price: 40, color: '#34D399', lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '40' })

    const ro = new ResizeObserver(entries => { for (const en of entries) c.applyOptions({ width: en.contentRect.width }) })
    ro.observe(chartRef.current)
    return () => { ro.disconnect(); c.remove() }
  }, [])

  // Load strategy chart data on symbol change + refresh every 15s for new bars.
  useEffect(() => {
    if (!selectedSymbol || !candles.current) return
    let alive = true

    const load = () => {
      api.marketChart(selectedSymbol).then(r => {
        if (!alive || !candles.current) return
        const d: any = r.data
        candles.current.setData(d.bars || [])
        for (const e of EMA_STYLE) emas.current[e.key]?.setData(d[e.key] || [])
        rsi.current?.setData(d.rsi || [])

        // Prev-day high/low reference lines on the candle series.
        pdLines.current.forEach(pl => candles.current?.removePriceLine(pl))
        pdLines.current = []
        if (d.pdh) pdLines.current.push(candles.current.createPriceLine({
          price: d.pdh, color: '#94A3B8', lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: 'PDH' }))
        if (d.pdl) pdLines.current.push(candles.current.createPriceLine({
          price: d.pdl, color: '#94A3B8', lineWidth: 1, lineStyle: 3, axisLabelVisible: true, title: 'PDL' }))
      }).catch(() => {})
    }
    load()
    const t = setInterval(load, 15000)
    return () => { alive = false; clearInterval(t) }
  }, [selectedSymbol])

  // Live SL / target / entry lines from the open position for this symbol.
  useEffect(() => {
    if (!candles.current || !selectedSymbol) return
    const pos: any = (positions as any[]).find(p =>
      p.symbol === selectedSymbol || p.tradingsymbol === selectedSymbol)
    tradeLines.current.forEach(pl => candles.current?.removePriceLine(pl))
    tradeLines.current = []
    if (!pos) return
    const entry = pos.avg_price ?? pos.entry_price ?? pos.average_price
    const sl  = pos.sl ?? pos.stop_loss
    const tgt = pos.target
    if (entry) tradeLines.current.push(candles.current.createPriceLine({
      price: entry, color: '#2563EB', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'ENTRY' }))
    if (sl) tradeLines.current.push(candles.current.createPriceLine({
      price: sl, color: '#DC2626', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'SL' }))
    if (tgt) tradeLines.current.push(candles.current.createPriceLine({
      price: tgt, color: '#16A34A', lineWidth: 1, lineStyle: 0, axisLabelVisible: true, title: 'TGT' }))
  }, [positions, selectedSymbol])

  // Update the forming 3-min candle from live ticks.
  useEffect(() => {
    if (!selectedSymbol) return
    const tick = ticks[selectedSymbol]
    if (!tick || !candles.current) return
    const now = Math.floor(Date.now() / 1000)
    const barTs = Math.floor(now / 180) * 180
    candles.current.update({ time: barTs as any, open: tick.ltp, high: tick.day_high, low: tick.day_low, close: tick.ltp })
  }, [ticks, selectedSymbol])

  const tick = selectedSymbol ? ticks[selectedSymbol] : null

  return (
    <div className="flex flex-col h-full bg-white">
      <div className="flex items-center gap-4 px-4 py-2 border-b border-slate-100">
        {tick ? (
          <>
            <span className="text-lg font-bold text-slate-900">{selectedSymbol}</span>
            <span className="text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-500 font-medium">3-min</span>
            <span className={`text-xl font-mono font-bold ${tick.change_pct >= 0 ? 'text-green-600' : 'text-red-600'}`}>
              ₹{tick.ltp.toLocaleString('en-IN', { minimumFractionDigits: 2 })}
            </span>
            <span className={`text-sm font-mono ${tick.change_pct >= 0 ? 'text-green-600' : 'text-red-600'}`}>
              {tick.change_pct >= 0 ? '+' : ''}{tick.change_pct.toFixed(2)}%
            </span>
            <div className="flex gap-3 ml-2 text-xs text-slate-500">
              <span>H: <span className="text-slate-700 font-mono">{tick.day_high.toFixed(2)}</span></span>
              <span>L: <span className="text-slate-700 font-mono">{tick.day_low.toFixed(2)}</span></span>
              <span>RSI: <span className={`font-mono font-medium ${tick.rsi > 60 ? 'text-red-600' : tick.rsi < 40 ? 'text-green-600' : 'text-slate-700'}`}>{tick.rsi.toFixed(1)}</span></span>
            </div>
            <div className="ml-auto flex gap-2 text-xs">
              <span className={`px-1.5 py-0.5 rounded font-medium ${tick.trend === 'UP' ? 'bg-green-100 text-green-700' : tick.trend === 'DOWN' ? 'bg-red-100 text-red-700' : 'bg-slate-100 text-slate-600'}`}>{tick.trend}</span>
              <span className="text-slate-400 font-mono">{tick.source}</span>
            </div>
          </>
        ) : (
          <span className="text-slate-400 text-sm">Select a symbol from the watchlist</span>
        )}
      </div>

      <div ref={chartRef} className="flex-1 min-h-0" />

      {/* EMA / RSI legend */}
      <div className="flex gap-4 px-4 py-2 bg-slate-50 border-t border-slate-100 text-xs font-mono text-slate-600">
        {EMA_STYLE.map(e => (
          <span key={e.key} className="flex items-center gap-1">
            <span className="inline-block w-3 h-0.5" style={{ background: e.color }} />
            <span style={{ color: e.color }}>{e.label}</span>
          </span>
        ))}
        <span className="flex items-center gap-1"><span className="inline-block w-3 h-0.5" style={{ background: '#A855F7' }} /><span className="text-purple-600">RSI (40/60)</span></span>
        <span className="ml-auto text-slate-400">EMA-pullback strategy view</span>
      </div>
    </div>
  )
}
