import React, { useEffect, useState, useCallback } from 'react'
import { Brain, CheckCircle, XCircle, Clock, TrendingUp, Activity, Zap } from 'lucide-react'
import { api } from '../../api/client'

interface GateDecision {
  time: string
  symbol: string
  strategy: string
  signal: string
  confidence: number
  decision: string
  enter: boolean
  size_factor: number
  reason: string
  warnings?: string[]
  latency_ms: number
}

// Alias for compatibility if needed elsewhere, but internal use is GateDecision
type GateEntry = GateDecision;

interface GateLog {
  decisions: GateDecision[]
  count: number
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

const STRAT_LABELS: Record<string, string> = {
  intraday: 'INTRA',
  options: 'F&O',
  swing: 'SWING',
  scalping: 'SCALP',
  futures: 'FUT',
  momentum: 'MOM',
  mean_reversion: 'MEANREV',
  pairs: 'PAIRS',
}

function ConfidenceBadge({ confidence }: { confidence: number }) {
  const color =
    confidence >= 70 ? 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30' :
    confidence >= 50 ? 'bg-amber-500/20 text-amber-400 border-amber-500/30' :
                       'bg-rose-500/20 text-rose-400 border-rose-500/30'
  return (
    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-mono font-bold border ${color}`}>
      {confidence}%
    </span>
  )
}

// Alias for ConfBadge
const ConfBadge = ({ conf }: { conf: number }) => <ConfidenceBadge confidence={conf} />;

function VerdictBadge({ enter, decision }: { enter: boolean; decision: string }) {
  return enter ? (
    <span className="inline-flex items-center gap-1 text-emerald-400 text-[10px] font-bold">
      <CheckCircle className="w-3 h-3" /> ENTER
    </span>
  ) : (
    <span className="inline-flex items-center gap-1 text-rose-400 text-[10px] font-bold">
      <XCircle className="w-3 h-3" /> SKIP
    </span>
  )
}

function SizeBadge({ sf }: { sf: number }) {
  const color = sf >= 1.0 ? 'text-emerald-400' : sf >= 0.75 ? 'text-amber-400' : 'text-rose-400'
  return <span className={`font-mono text-[10px] font-bold ${color}`}>{(sf ?? 1.0).toFixed(2)}×</span>
}

export default function ClaudeGateTab() {
  const [log, setLog]         = useState<GateDecision[]>([])
  const [filter, setFilter]   = useState<string>('all')
  const [loading, setLoading] = useState(true)
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null)

  const fetchLog = useCallback(async () => {
    try {
      const r = await api.gateLog()
      const data: GateLog = r.data
      setLog(data.decisions || [])
      setLastRefresh(new Date())
    } catch (err) {
      console.error("Failed to fetch gate log", err)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    fetchLog()
    const t = setInterval(fetchLog, 5000)
    return () => clearInterval(t)
  }, [fetchLog])

  const filtered = filter === 'all' ? log : log.filter(d => d.strategy === filter)

  const entered  = log.filter(d => d.enter)
  const skipped  = log.filter(d => !d.enter)
  const total = log.length
  
  const approvalRate = total > 0 ? Math.round(entered.length / total * 100) : 0
  const avgConf  = total > 0 ? Math.round(log.reduce((s, d) => s + d.confidence, 0) / total) : 0
  const avgLat   = total > 0 ? Math.round(log.reduce((s, d) => s + (d.latency_ms || 0), 0) / total) : 0

  const strategies = Array.from(new Set(log.map(d => d.strategy))).sort()

  return (
    <div className="flex flex-col h-full overflow-hidden bg-[#070b14]">

      {/* Header */}
      <div className="px-5 py-3 border-b border-slate-800 bg-slate-900/60 shrink-0">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2.5">
            <Brain className="w-4 h-4 text-violet-400" />
            <span className="text-sm font-semibold text-slate-200 tracking-wide">Claude Opus Gate</span>
            <span className="text-[10px] font-mono text-slate-500 bg-slate-800 px-2 py-0.5 rounded border border-slate-700">
              claude-opus-4-5 · extended thinking
            </span>
          </div>
          <div className="flex items-center gap-2 text-[10px] font-mono text-slate-500">
            <Activity className="w-3 h-3 text-violet-400 animate-pulse" />
            {lastRefresh
              ? `Last: ${lastRefresh.toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' })}`
              : 'Polling…'}
          </div>
        </div>
      </div>

      {/* Aggregate stats */}
      <div className="grid grid-cols-4 gap-3 px-5 py-3 shrink-0 border-b border-slate-800">
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
          <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Decisions</div>
          <div className="text-xl font-mono font-bold text-slate-200">{total}</div>
          <div className="text-[10px] text-slate-600 mt-0.5">last 500 stored</div>
        </div>
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
          <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Approval Rate</div>
          <div className={`text-xl font-mono font-bold ${approvalRate >= 60 ? 'text-emerald-400' : approvalRate >= 40 ? 'text-amber-400' : 'text-rose-400'}`}>
            {approvalRate}%
          </div>
          <div className="text-[10px] text-slate-600 mt-0.5">{entered.length} enter / {skipped.length} skip</div>
        </div>
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
          <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Avg Confidence</div>
          <div className={`text-xl font-mono font-bold ${avgConf >= 70 ? 'text-emerald-400' : avgConf >= 50 ? 'text-amber-400' : 'text-rose-400'}`}>
            {avgConf}%
          </div>
          <div className="text-[10px] text-slate-600 mt-0.5">0–100 scale</div>
        </div>
        <div className="bg-slate-900 border border-slate-800 rounded-lg p-3">
          <div className="text-[10px] text-slate-500 uppercase tracking-widest mb-1">Avg Latency</div>
          <div className="text-xl font-mono font-bold text-violet-400">
            {avgLat > 0 ? `${(avgLat / 1000).toFixed(1)}s` : '—'}
          </div>
          <div className="text-[10px] text-slate-600 mt-0.5">per Opus call</div>
        </div>
      </div>

      {/* Filter Bar */}
      <div className="flex items-center gap-1 px-5 py-2 border-b border-slate-800 bg-slate-900/40 shrink-0 overflow-x-auto no-scrollbar">
        <button
          onClick={() => setFilter('all')}
          className={`text-[10px] px-2 py-0.5 rounded font-mono transition-colors whitespace-nowrap ${filter === 'all' ? 'bg-violet-700 text-white' : 'text-slate-500 hover:text-slate-300'}`}
        >ALL</button>
        {strategies.map(s => (
          <button
            key={s}
            onClick={() => setFilter(s === filter ? 'all' : s)}
            className={`text-[10px] px-2 py-0.5 rounded font-mono transition-colors whitespace-nowrap ${filter === s ? 'bg-slate-600 text-white' : 'text-slate-600 hover:text-slate-400'}`}
          >
            {AGENT_LABELS[s] || s.toUpperCase()}
          </button>
        ))}
      </div>

      {/* Table */}
      <div className="flex-1 overflow-y-auto acc-scroll">
        {loading ? (
          <div className="flex items-center justify-center h-32">
            <div className="text-[10px] font-mono text-violet-400 animate-pulse tracking-widest flex items-center gap-2">
              <Zap className="w-3 h-3" /> LOADING GATE DECISIONS…
            </div>
          </div>
        ) : filtered.length === 0 ? (
          <div className="flex flex-col items-center justify-center h-48 gap-3">
            <Brain className="w-10 h-10 text-slate-700" />
            <div className="text-slate-600 text-sm">{total === 0 ? 'No gate decisions yet' : 'No decisions for this filter'}</div>
            <div className="text-slate-700 text-xs text-center max-w-xs">
              {total === 0 ? 'Claude Opus will evaluate every trade signal before an order is placed. Start an agent to see decisions appear here.' : 'Try changing your filter settings.'}
            </div>
          </div>
        ) : (
          <table className="w-full text-[11px] border-collapse">
            <thead className="sticky top-0 z-10">
              <tr className="bg-slate-900/95 backdrop-blur border-b border-slate-800">
                <th className="px-3 py-2 text-left text-[10px] text-slate-500 font-medium tracking-widest w-20">TIME</th>
                <th className="px-3 py-2 text-left text-[10px] text-slate-500 font-medium tracking-widest">SYMBOL</th>
                <th className="px-3 py-2 text-left text-[10px] text-slate-500 font-medium tracking-widest w-20">STRATEGY</th>
                <th className="px-3 py-2 text-left text-[10px] text-slate-500 font-medium tracking-widest w-16">SIGNAL</th>
                <th className="px-3 py-2 text-left text-[10px] text-slate-500 font-medium tracking-widest w-16">CONF</th>
                <th className="px-3 py-2 text-left text-[10px] text-slate-500 font-medium tracking-widest w-20">VERDICT</th>
                <th className="px-3 py-2 text-left text-[10px] text-slate-500 font-medium tracking-widest w-14">SIZE</th>
                <th className="px-3 py-2 text-left text-[10px] text-slate-500 font-medium tracking-widest">REASON</th>
                <th className="px-3 py-2 text-right text-[10px] text-slate-500 font-medium tracking-widest w-16">LAT</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((d, i) => (
                <tr
                  key={i}
                  className={`border-b border-slate-800/50 hover:bg-slate-900/60 transition-colors ${
                    !d.enter ? 'opacity-70' : ''
                  }`}
                >
                  <td className="px-3 py-2 font-mono text-slate-500 whitespace-nowrap">{d.time}</td>
                  <td className="px-3 py-2 font-mono font-bold text-slate-200 whitespace-nowrap">{d.symbol}</td>
                  <td className="px-3 py-2">
                    <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${AGENT_COLORS[d.strategy] || 'text-slate-400 border-slate-700 bg-slate-800/50'} ${AGENT_COLORS[d.strategy]?.replace('text-', 'bg-')?.replace('-400', '-500/10') || ''} ${AGENT_COLORS[d.strategy]?.replace('text-', 'border-')?.replace('-400', '-500/20') || ''}`}>
                      {STRAT_LABELS[d.strategy] || d.strategy?.toUpperCase()}
                    </span>
                  </td>
                  <td className="px-3 py-2 font-mono text-slate-400 whitespace-nowrap">{d.signal}</td>
                  <td className="px-3 py-2"><ConfidenceBadge confidence={d.confidence} /></td>
                  <td className="px-3 py-2"><VerdictBadge enter={d.enter} decision={d.decision} /></td>
                  <td className="px-3 py-2"><SizeBadge sf={d.size_factor} /></td>
                  <td className="px-3 py-2 text-slate-400 max-w-xs">
                    <span className="truncate block" title={d.reason}>{d.reason}</span>
                    {d.warnings && d.warnings.length > 0 && (
                      <span className="text-amber-500/80 text-[9px] block truncate">⚠ {d.warnings[0]}</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right font-mono text-slate-600 whitespace-nowrap">
                    {d.latency_ms > 0 ? `${(d.latency_ms / 1000).toFixed(1)}s` : '—'}
                  </td>
                </tr>
              ))}
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

    </div>
  )
}
