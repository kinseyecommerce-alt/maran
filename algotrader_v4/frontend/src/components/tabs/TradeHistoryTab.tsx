import React, { useEffect, useState, useMemo } from 'react'
import { Eye, RotateCcw, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Download } from 'lucide-react'
import { api } from '../../api/client'
import { useStore } from '../../store'

interface TradeRecord {
  id: number
  symbol: string
  strategy: string
  side: string
  entry_price: number
  exit_price: number
  quantity: number
  gross_pnl: number
  net_pnl: number
  cost: number
  exit_reason: string
  regime: string
  entry_time: string
  exit_time: string
  gate_confidence: number
  trade_date: string
}

interface DetailModalProps {
  trade: TradeRecord
  onClose: () => void
}

function DetailModal({ trade, onClose }: DetailModalProps) {
  const pnlPos = (trade.net_pnl ?? 0) >= 0
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="bg-slate-900 border border-slate-700 rounded-xl shadow-2xl w-full max-w-md mx-4 overflow-hidden"
        onClick={e => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-5 py-4 border-b border-slate-800">
          <div>
            <div className="font-bold text-slate-100 text-base">{trade.symbol}</div>
            <div className="text-[11px] text-slate-500 font-mono mt-0.5">{trade.strategy} · #{trade.id}</div>
          </div>
          <button
            onClick={onClose}
            className="text-slate-500 hover:text-slate-200 transition-colors text-lg leading-none"
          >
            ✕
          </button>
        </div>

        <div className="p-5 space-y-3">
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div className="bg-slate-800/60 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Entry Time</div>
              <div className="font-mono text-slate-200 text-xs">{fmtTime(trade.entry_time)}</div>
            </div>
            <div className="bg-slate-800/60 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Exit Time</div>
              <div className="font-mono text-slate-200 text-xs">{fmtTime(trade.exit_time)}</div>
            </div>
            <div className="bg-slate-800/60 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Entry Price</div>
              <div className="font-mono text-slate-200">₹{(trade.entry_price ?? 0).toFixed(2)}</div>
            </div>
            <div className="bg-slate-800/60 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Exit Price</div>
              <div className="font-mono text-slate-200">₹{(trade.exit_price ?? 0).toFixed(2)}</div>
            </div>
            <div className="bg-slate-800/60 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Quantity</div>
              <div className="font-mono text-slate-200">{trade.quantity}</div>
            </div>
            <div className="bg-slate-800/60 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Charges</div>
              <div className="font-mono text-slate-200">₹{(trade.cost ?? 0).toFixed(2)}</div>
            </div>
            <div className="bg-slate-800/60 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Gross P&L</div>
              <div className={`font-mono font-semibold ${(trade.gross_pnl ?? 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                {(trade.gross_pnl ?? 0) >= 0 ? '+' : ''}₹{(trade.gross_pnl ?? 0).toFixed(2)}
              </div>
            </div>
            <div className="bg-slate-800/60 rounded-lg p-3">
              <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Net P&L</div>
              <div className={`font-mono font-bold text-base ${pnlPos ? 'text-emerald-400' : 'text-rose-400'}`}>
                {pnlPos ? '+' : ''}₹{(trade.net_pnl ?? 0).toFixed(2)}
              </div>
            </div>
          </div>

          <div className="bg-slate-800/60 rounded-lg p-3 space-y-1.5">
            <div className="flex justify-between text-xs">
              <span className="text-slate-500">Exit Reason</span>
              <span className="font-mono text-slate-300">{trade.exit_reason || '—'}</span>
            </div>
            <div className="flex justify-between text-xs">
              <span className="text-slate-500">Regime</span>
              <span className="font-mono text-slate-300">{trade.regime || '—'}</span>
            </div>
            {trade.gate_confidence != null && (
              <div className="flex justify-between text-xs">
                <span className="text-slate-500">AI Confidence</span>
                <span className="font-mono text-emerald-400">{(trade.gate_confidence * 100).toFixed(0)}%</span>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

function fmtTime(ts: string): string {
  if (!ts) return '—'
  try {
    const d = new Date(ts)
    const date = d.toLocaleDateString('en-IN', { day: '2-digit', month: '2-digit', year: 'numeric', timeZone: 'Asia/Kolkata' })
    const time = d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false, timeZone: 'Asia/Kolkata' })
    return `${date}\n${time}`
  } catch {
    return ts.slice(0, 19).replace('T', '\n')
  }
}

function fmtTimeCell(ts: string): React.ReactNode {
  if (!ts) return <span className="text-slate-600">—</span>
  const parts = fmtTime(ts).split('\n')
  return (
    <div className="text-center leading-tight">
      <div className="text-slate-300 text-[11px]">{parts[0]}</div>
      <div className="text-slate-400 text-[11px] font-mono">{parts[1]}</div>
    </div>
  )
}

const PAGE_SIZE_OPTIONS = [10, 25, 50, 100]

export default function TradeHistoryTab() {
  const [trades, setTrades]       = useState<TradeRecord[]>([])
  const [loading, setLoading]     = useState(false)
  const [error, setError]         = useState('')
  const [days, setDays]           = useState(30)

  const [filterType, setFilterType]     = useState('')
  const [filterSymbol, setFilterSymbol] = useState('')
  const [filterSide, setFilterSide]     = useState('')

  const [pageSize, setPageSize] = useState(10)
  const [page, setPage]         = useState(1)

  const [detailTrade, setDetailTrade] = useState<TradeRecord | null>(null)

  const load = async () => {
    setLoading(true)
    setError('')
    try {
      const r = await (api as any).tradeHistory(days)
      setTrades(r.data?.trades ?? [])
    } catch (e: any) {
      setError(e.response?.data?.detail || 'Failed to load trade history')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [days])

  const strategies = useMemo(() => {
    const s = new Set(trades.map(t => t.strategy).filter(Boolean))
    return Array.from(s).sort()
  }, [trades])

  const symbols = useMemo(() => {
    const s = new Set(trades.map(t => t.symbol).filter(Boolean))
    return Array.from(s).sort()
  }, [trades])

  const filtered = useMemo(() => {
    return trades.filter(t => {
      if (filterType   && t.strategy !== filterType)                          return false
      if (filterSymbol && t.symbol !== filterSymbol)                          return false
      if (filterSide   && (t.side || '').toUpperCase() !== filterSide)        return false
      return true
    })
  }, [trades, filterType, filterSymbol, filterSide])

  const totalPnl = useMemo(() => filtered.reduce((s, t) => s + (t.net_pnl ?? 0), 0), [filtered])

  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const safePage   = Math.min(page, totalPages)
  const pageRows   = filtered.slice((safePage - 1) * pageSize, safePage * pageSize)

  const handleReset = () => {
    setFilterType('')
    setFilterSymbol('')
    setFilterSide('')
    setPage(1)
  }

  const handleExport = async () => {
    try {
      const { apiBase, apiKey, token } = useStore.getState()
      const headers: Record<string, string> = {}
      if (apiKey) headers['X-API-Key']     = apiKey
      if (token)  headers['Authorization'] = `Bearer ${token}`
      const res = await fetch(`${apiBase}/portfolio/trades/export?days=${days}`, { headers })
      const blob = await res.blob()
      const url  = URL.createObjectURL(blob)
      const a    = document.createElement('a')
      a.href = url; a.download = `trades_last_${days}d.csv`; a.click()
      URL.revokeObjectURL(url)
    } catch {}
  }

  const pnlPos = totalPnl >= 0

  return (
    <div className="flex flex-col h-full bg-slate-950 text-slate-300">

      {/* FILTER BAR */}
      <div className="flex flex-wrap items-end gap-3 px-4 py-3 border-b border-slate-800 bg-slate-900/60 shrink-0">
        {/* Days range */}
        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-slate-500 uppercase tracking-widest">Period</label>
          <select
            value={days}
            onChange={e => { setDays(Number(e.target.value)); setPage(1) }}
            className="bg-slate-800 border border-slate-700 text-slate-200 text-xs rounded-lg px-3 py-2 focus:outline-none focus:ring-1 focus:ring-emerald-500 focus:border-emerald-500 min-w-[100px]"
          >
            <option value={7}>Last 7 days</option>
            <option value={14}>Last 14 days</option>
            <option value={30}>Last 30 days</option>
            <option value={90}>Last 90 days</option>
            <option value={365}>Last 1 year</option>
          </select>
        </div>

        {/* Strategy / Type */}
        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-slate-500 uppercase tracking-widest">Strategy</label>
          <select
            value={filterType}
            onChange={e => { setFilterType(e.target.value); setPage(1) }}
            className="bg-slate-800 border border-slate-700 text-slate-200 text-xs rounded-lg px-3 py-2 focus:outline-none focus:ring-1 focus:ring-emerald-500 focus:border-emerald-500 min-w-[120px]"
          >
            <option value="">All</option>
            {strategies.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>

        {/* Symbol */}
        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-slate-500 uppercase tracking-widest">Symbol</label>
          <select
            value={filterSymbol}
            onChange={e => { setFilterSymbol(e.target.value); setPage(1) }}
            className="bg-slate-800 border border-slate-700 text-slate-200 text-xs rounded-lg px-3 py-2 focus:outline-none focus:ring-1 focus:ring-emerald-500 focus:border-emerald-500 min-w-[140px]"
          >
            <option value="">All</option>
            {symbols.map(s => <option key={s} value={s}>{s}</option>)}
          </select>
        </div>

        {/* Side */}
        <div className="flex flex-col gap-1">
          <label className="text-[10px] text-slate-500 uppercase tracking-widest">Side</label>
          <select
            value={filterSide}
            onChange={e => { setFilterSide(e.target.value); setPage(1) }}
            className="bg-slate-800 border border-slate-700 text-slate-200 text-xs rounded-lg px-3 py-2 focus:outline-none focus:ring-1 focus:ring-emerald-500 focus:border-emerald-500 min-w-[100px]"
          >
            <option value="">All</option>
            <option value="BUY">BUY</option>
            <option value="SELL">SELL</option>
          </select>
        </div>

        {/* Reset */}
        <button
          onClick={handleReset}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-xs font-medium transition-colors self-end"
        >
          <RotateCcw className="w-3.5 h-3.5" />
          Reset
        </button>

        {/* Export */}
        <button
          onClick={handleExport}
          className="flex items-center gap-1.5 px-4 py-2 rounded-lg bg-slate-700 hover:bg-slate-600 text-slate-200 text-xs font-medium transition-colors self-end ml-auto"
        >
          <Download className="w-3.5 h-3.5" />
          Export CSV
        </button>
      </div>

      {/* TOTAL P&L BANNER */}
      <div className="flex items-center gap-4 px-4 py-2.5 bg-slate-900/40 border-b border-slate-800 shrink-0">
        <span className="text-[11px] text-slate-500 font-medium">Total Realised P&amp;L :</span>
        <span className={`text-lg font-bold font-mono ${pnlPos ? 'text-emerald-400' : 'text-rose-400'}`}>
          {pnlPos ? '+' : ''}₹{Math.abs(totalPnl).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}
        </span>
        <span className="text-[11px] text-slate-600 ml-2">{filtered.length} trade{filtered.length !== 1 ? 's' : ''}</span>
        {loading && <span className="text-xs text-emerald-400 font-mono animate-pulse ml-2">Loading…</span>}
      </div>

      {/* TABLE */}
      <div className="flex-1 overflow-auto acc-scroll">
        {error ? (
          <div className="flex items-center justify-center h-32 text-rose-400 text-sm">{error}</div>
        ) : (
          <table className="w-full text-xs border-collapse min-w-[900px]">
            <thead className="sticky top-0 z-10">
              <tr className="bg-slate-800/80 text-slate-300 uppercase tracking-wide text-[11px] border-b border-slate-700">
                <th className="px-3 py-2.5 text-center font-semibold border-r border-slate-700/60 w-12">S.No.</th>
                <th className="px-3 py-2.5 text-center font-semibold border-r border-slate-700/60 w-32">Entry Time</th>
                <th className="px-3 py-2.5 text-center font-semibold border-r border-slate-700/60 w-32">Exit Time</th>
                <th className="px-3 py-2.5 text-center font-semibold border-r border-slate-700/60">Symbol</th>
                <th className="px-3 py-2.5 text-center font-semibold border-r border-slate-700/60">Strategy</th>
                <th className="px-3 py-2.5 text-center font-semibold border-r border-slate-700/60 w-24">Entry Type</th>
                <th className="px-3 py-2.5 text-center font-semibold border-r border-slate-700/60 w-20">Entry Qty</th>
                <th className="px-3 py-2.5 text-center font-semibold border-r border-slate-700/60 w-24">Entry Price</th>
                <th className="px-3 py-2.5 text-center font-semibold border-r border-slate-700/60 w-24">Exit Price</th>
                <th className="px-3 py-2.5 text-center font-semibold border-r border-slate-700/60 w-28">Total P&L</th>
                <th className="px-3 py-2.5 text-center font-semibold w-14">Detail</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.length === 0 ? (
                <tr>
                  <td colSpan={11} className="text-center py-16 text-slate-600">
                    {loading ? 'Loading trade history…' : 'No trades found for the selected filters.'}
                  </td>
                </tr>
              ) : pageRows.map((trade, idx) => {
                const rowNum  = (safePage - 1) * pageSize + idx + 1
                const pnlPos  = (trade.net_pnl ?? 0) >= 0
                const isBuy   = (trade.side || '').toUpperCase() === 'BUY'
                const rowBg   = idx % 2 === 0 ? 'bg-slate-900/20' : 'bg-slate-900/50'
                return (
                  <tr
                    key={trade.id}
                    className={`${rowBg} hover:bg-emerald-500/5 transition-colors border-b border-slate-800/60`}
                  >
                    <td className="px-3 py-2.5 text-center text-slate-400 font-mono border-r border-slate-800/40">{rowNum}</td>
                    <td className="px-3 py-2.5 border-r border-slate-800/40">{fmtTimeCell(trade.entry_time)}</td>
                    <td className="px-3 py-2.5 border-r border-slate-800/40">{fmtTimeCell(trade.exit_time)}</td>
                    <td className="px-3 py-2.5 text-center border-r border-slate-800/40">
                      <span className="font-bold text-slate-100 font-mono text-[11px] tracking-wide">{trade.symbol}</span>
                    </td>
                    <td className="px-3 py-2.5 text-center border-r border-slate-800/40">
                      <span className="px-2 py-0.5 rounded bg-slate-800 text-slate-300 text-[10px] font-mono">
                        {trade.strategy || '—'}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-center border-r border-slate-800/40">
                      <div className={`inline-flex flex-col items-center px-2 py-0.5 rounded text-[10px] font-semibold leading-tight ${isBuy ? 'bg-emerald-900/50 text-emerald-400 border border-emerald-700/40' : 'bg-rose-900/50 text-rose-400 border border-rose-700/40'}`}>
                        <span>{isBuy ? 'BUY' : 'SELL'}</span>
                        <span className="font-normal opacity-70">ENTRY</span>
                      </div>
                    </td>
                    <td className="px-3 py-2.5 text-center border-r border-slate-800/40 font-mono text-slate-300">{trade.quantity}</td>
                    <td className="px-3 py-2.5 text-center border-r border-slate-800/40 font-mono text-slate-300">₹{(trade.entry_price ?? 0).toFixed(2)}</td>
                    <td className="px-3 py-2.5 text-center border-r border-slate-800/40 font-mono text-slate-300">
                      {trade.exit_price ? `₹${trade.exit_price.toFixed(2)}` : <span className="text-slate-600">—</span>}
                    </td>
                    <td className="px-3 py-2.5 text-center border-r border-slate-800/40">
                      <span className={`font-mono font-bold text-[12px] ${pnlPos ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {pnlPos ? '+' : ''}₹{(trade.net_pnl ?? 0).toFixed(2)}
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-center">
                      <button
                        onClick={() => setDetailTrade(trade)}
                        className="p-1.5 rounded bg-slate-800 hover:bg-emerald-500/20 text-slate-400 hover:text-emerald-400 transition-colors"
                        title="View detail"
                      >
                        <Eye className="w-3.5 h-3.5" />
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* PAGINATION */}
      <div className="flex items-center justify-between px-4 py-2.5 border-t border-slate-800 bg-slate-900/60 shrink-0">
        <div className="flex items-center gap-2">
          <select
            value={pageSize}
            onChange={e => { setPageSize(Number(e.target.value)); setPage(1) }}
            className="bg-slate-800 border border-slate-700 text-slate-300 text-xs rounded px-2 py-1.5 focus:outline-none focus:ring-1 focus:ring-emerald-500"
          >
            {PAGE_SIZE_OPTIONS.map(n => <option key={n} value={n}>{n}</option>)}
          </select>
          <span className="text-xs text-slate-500">
            Showing rows {filtered.length === 0 ? 0 : (safePage - 1) * pageSize + 1} to{' '}
            {Math.min(safePage * pageSize, filtered.length)} of {filtered.length}
          </span>
        </div>

        <div className="flex items-center gap-1">
          <PagBtn onClick={() => setPage(1)}               disabled={safePage === 1}          icon={<ChevronsLeft  className="w-3.5 h-3.5" />} />
          <PagBtn onClick={() => setPage(p => p - 1)}      disabled={safePage === 1}          icon={<ChevronLeft   className="w-3.5 h-3.5" />} />

          {pagNums(safePage, totalPages).map((n, i) =>
            n === '...' ? (
              <span key={`e${i}`} className="px-2 text-slate-600 text-xs">…</span>
            ) : (
              <button
                key={n}
                onClick={() => setPage(Number(n))}
                className={`w-7 h-7 text-xs rounded font-mono transition-colors ${
                  safePage === Number(n)
                    ? 'bg-emerald-600 text-white font-bold'
                    : 'bg-slate-800 text-slate-400 hover:bg-slate-700 hover:text-white'
                }`}
              >
                {n}
              </button>
            )
          )}

          <PagBtn onClick={() => setPage(p => p + 1)}      disabled={safePage === totalPages} icon={<ChevronRight  className="w-3.5 h-3.5" />} />
          <PagBtn onClick={() => setPage(totalPages)}       disabled={safePage === totalPages} icon={<ChevronsRight className="w-3.5 h-3.5" />} />
        </div>
      </div>

      {detailTrade && <DetailModal trade={detailTrade} onClose={() => setDetailTrade(null)} />}

      <style>{`
        .acc-scroll::-webkit-scrollbar { width: 4px; height: 4px; }
        .acc-scroll::-webkit-scrollbar-track { background: rgba(15,23,42,0.5); }
        .acc-scroll::-webkit-scrollbar-thumb { background: rgba(51,65,85,0.5); border-radius: 4px; }
        .acc-scroll::-webkit-scrollbar-thumb:hover { background: rgba(71,85,105,0.8); }
      `}</style>
    </div>
  )
}

function PagBtn({ onClick, disabled, icon }: { onClick: () => void; disabled: boolean; icon: React.ReactNode }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className="w-7 h-7 flex items-center justify-center rounded bg-slate-800 hover:bg-slate-700 text-slate-400 hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
    >
      {icon}
    </button>
  )
}

function pagNums(current: number, total: number): (number | '...')[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
  const pages: (number | '...')[] = [1]
  if (current > 3) pages.push('...')
  for (let i = Math.max(2, current - 1); i <= Math.min(total - 1, current + 1); i++) pages.push(i)
  if (current < total - 2) pages.push('...')
  pages.push(total)
  return pages
}
