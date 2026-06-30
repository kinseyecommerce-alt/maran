import React, { useEffect, useState, useCallback } from 'react'
import { Brain } from 'lucide-react'
import { api } from '../../api/client'

interface GateEntry {
  time: string
  symbol: string
  strategy: string
  signal: string
  confidence: number
  decision: string
  size_factor: number
  reason: string
  warnings: string[]
  latency_ms: number
}

const AGENT_COLORS: Record<string, string> = {
  intraday:      'text-sky-400',
  options:       'text-purple-400',
  swing:         'text-emerald-400',
  scalping:      'text-amber-400',
  futures:       'text-rose-400',
  momentum:      'text-orange-400',
  mean_reversion:'text-teal-400',
  pairs:         'text-indigo-400',
}

const AGENT_LABELS: Record<string, string> = {
  intraday:      'INTRADAY',
  options:       'F&O',
  swing:         'SWING',
  scalping:      'SCALP',
  futures:       'FUTURES',
  momentum:      'MOMENTUM',
  mean_reversion:'MEAN REV',
  pairs:         'PAIRS',
}

function ConfBadge({ conf }: { conf: number }) {
  const color =
    conf >= 75 ? 'bg-emerald-900 text-emerald-300 border-emerald-700' :
    conf >= 50 ? 'bg-amber-900 text-amber-300 border-amber-700' :
                 'bg-rose-900 text-rose-300 border-rose-700'
  return (
    <span className={`inline-flex items-center justify-center w-9 h-5 text-[10px] font-mono font-bold rounded border ${color}`}>
      {conf}
    </span>
  )
}

function SizeBadge({ sf }: { sf: number }) {
  const label = sf >= 1.0 ? '1.0×' : sf >= 0.75 ? '0.75×' : sf >= 0.5 ? '0.5×' : '0.25×'
  const color = sf >= 1.0 ? 'text-emerald-400' : sf >= 0.75 ? 'text-slate-300' : sf >= 0.5 ? 'text-amber-400' : 'text-rose-400'
  return <span className={`font-mono text-[11px] ${color}`}>{label}</span>
}

export default function ClaudeGateTab() {
  const [entries, setEntries] = useState<GateEntry[]>([])
  const [filter, setFilter] = useState<string>('all')
  const [loading, setLoading] = useState(true)

  const load = useCallback(() => {
    api.gateLog()
      .then(r => {
        const data = r.data
        const list: GateEntry[] = data.decisions || []
        setEntries(list)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
    const t = setInterval(load, 5000)
    return () => clearInterval(t)
  }, [load])

  const filtered = filter === 'all' ? entries : entries.filter(e => e.strategy === filter)

  const approvals = entries.filter(e => e.decision === 'ENTER').length
  const total = entries.length
  const approvalRate = total > 0 ? Math.round(approvals / total * 100) : null
  const avgConf = total > 0 ? Math.round(entries.reduce((s, e) => s + e.confidence, 0) / total) : null
  const avgLatency = total > 0 ? Math.round(entries.filter(e => e.latency_ms > 0).reduce((s, e) => s + e.latency_ms, 0) / Math.max(1, entries.filter(e => e.latency_ms > 0).length)) : null

  const strategies = Array.from(new Set(entries.map(e => e.strategy))).sort()

  return (
    <div className="h-full flex flex-col overflow-hidden">

      {/* Stats bar */}
      <div className="flex items-center gap-4 px-4 py-2.5 border-b border-slate-700/60 bg-slate-900/80 shrink-0 flex-wrap">
        <div className="flex items-center gap-2">
          <Brain className="w-4 h-4 text-violet-400" />
          <span className="text-[11px] font-bold text-violet-300 tracking-widest uppercase">Claude Opus Gate</span>
        </div>
        <div className="h-4 w-px bg-slate-700" />
        {approvalRate !== null && (
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] text-slate-500 uppercase tracking-wider">Approval</span>
            <span className={`text-sm font-mono font-bold ${approvalRate >= 70 ? 'text-emerald-400' : approvalRate >= 50 ? 'text-amber-400' : 'text-rose-400'}`}>
              {approvalRate}%
            </span>
            <span className="text-[10px] text-slate-600">({approvals}/{total})</span>
          </div>
        )}
        {avgConf !== null && (
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] text-slate-500 uppercase tracking-wider">Avg Conf</span>
            <span className="text-sm font-mono font-bold text-slate-200">{avgConf}</span>
          </div>
        )}
        {avgLatency !== null && avgLatency > 0 && (
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] text-slate-500 uppercase tracking-wider">Avg Latency</span>
            <span className="text-sm font-mono font-bold text-slate-400">{avgLatency}ms</span>
          </div>
        )}
        <div className="ml-auto flex items-center gap-1">
          <button
            onClick={() => setFilter('all')}
            className={`text-[10px] px-2 py-0.5 rounded font-mono transition-colors ${filter === 'all' ? 'bg-violet-700 text-white' : 'text-slate-500 hover:text-slate-300'}`}
          >ALL</button>
          {strategies.map(s => (
            <button
              key={s}
              onClick={() => setFilter(s === filter ? 'all' : s)}
              className={`text-[10px] px-2 py-0.5 rounded font-mono transition-colors ${filter === s ? 'bg-slate-600 text-white' : 'text-slate-600 hover:text-slate-400'}`}
            >
              {AGENT_LABELS[s] || s.toUpperCase()}
            </button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="flex-1 overflow-y-auto">
        {loading && (
          <div className="flex items-center justify-center h-24 text-slate-600 text-xs">Loading gate decisions…</div>
        )}
        {!loading && filtered.length === 0 && (
          <div className="flex flex-col items-center justify-center h-32 gap-2 text-slate-600">
            <Brain className="w-6 h-6 opacity-30" />
            <span className="text-xs">
              {total === 0
                ? 'No gate decisions yet — gate activates when agents generate signals'
                : 'No decisions for this filter'}
            </span>
          </div>
        )}
        {!loading && filtered.length > 0 && (
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-slate-900 border-b border-slate-800">
              <tr>
                <th className="text-left px-3 py-2 text-[10px] font-bold text-slate-500 uppercase tracking-wider w-16">Time</th>
                <th className="text-left px-2 py-2 text-[10px] font-bold text-slate-500 uppercase tracking-wider w-16">Agent</th>
                <th className="text-left px-2 py-2 text-[10px] font-bold text-slate-500 uppercase tracking-wider w-24">Symbol</th>
                <th className="text-left px-2 py-2 text-[10px] font-bold text-slate-500 uppercase tracking-wider w-12">Dir</th>
                <th className="text-center px-2 py-2 text-[10px] font-bold text-slate-500 uppercase tracking-wider w-14">Conf</th>
                <th className="text-center px-2 py-2 text-[10px] font-bold text-slate-500 uppercase tracking-wider w-20">Verdict</th>
                <th className="text-center px-2 py-2 text-[10px] font-bold text-slate-500 uppercase tracking-wider w-14">Size</th>
                <th className="text-left px-2 py-2 text-[10px] font-bold text-slate-500 uppercase tracking-wider">Reason</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((e, i) => {
                const isEnter = e.decision === 'ENTER'
                return (
                  <tr key={i} className={`border-b border-slate-800/50 hover:bg-slate-800/30 transition-colors ${isEnter ? '' : 'opacity-60'}`}>
                    <td className="px-3 py-1.5 font-mono text-[10px] text-slate-500">{e.time}</td>
                    <td className="px-2 py-1.5">
                      <span className={`font-mono text-[10px] font-bold ${AGENT_COLORS[e.strategy] || 'text-slate-400'}`}>
                        {AGENT_LABELS[e.strategy] || e.strategy.toUpperCase()}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 font-mono text-[11px] text-slate-200 font-semibold">{e.symbol}</td>
                    <td className="px-2 py-1.5">
                      <span className={`font-mono text-[10px] font-bold ${e.signal === 'BUY' ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {e.signal}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-center"><ConfBadge conf={e.confidence} /></td>
                    <td className="px-2 py-1.5 text-center">
                      <span className={`inline-flex items-center gap-1 text-[10px] font-bold px-1.5 py-0.5 rounded font-mono ${
                        isEnter
                          ? 'bg-emerald-950 text-emerald-400 border border-emerald-800'
                          : 'bg-rose-950 text-rose-400 border border-rose-900'
                      }`}>
                        {isEnter ? '✅ ENTER' : '🚫 SKIP'}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-center"><SizeBadge sf={e.size_factor} /></td>
                    <td className="px-2 py-1.5 text-slate-400 text-[11px] leading-tight">
                      <span className="line-clamp-1">{e.reason}</span>
                      {e.warnings?.length > 0 && (
                        <span className="text-amber-500 text-[10px] ml-1">⚠ {e.warnings[0]}</span>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>

      {/* Footer note */}
      <div className="px-4 py-1.5 border-t border-slate-800 bg-slate-900/60 shrink-0">
        <span className="text-[10px] text-slate-700 font-mono">
          Powered by Claude Opus · 5-min regime cache · extended thinking · auto-refreshes every 5s
        </span>
      </div>
    </div>
  )
}
