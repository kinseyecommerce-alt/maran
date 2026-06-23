import React, { useEffect, useState } from 'react'
import { useStore } from '../../store'
import { api } from '../../api/client'

const AGENT_META: Record<string, { emoji: string; name: string; desc: string }> = {
  intraday: { emoji: '⚡', name: 'INTRADAY',  desc: 'VWAP+EMA momentum · MIS · 8 trades/day' },
  fno:      { emoji: '📊', name: 'F&O',        desc: 'Options CE/PE · NRML · 4 trades/day' },
  swing:    { emoji: '🌊', name: 'SWING',      desc: 'EMA200 trend · CNC · 3 trades/day' },
  scalping: { emoji: '🎯', name: 'SCALPING',   desc: 'Micro EMA cross · MIS · 20 trades/day' },
}

type EnableMap = Record<string, boolean>

export default function AgentsTab() {
  const { agents, setAgents, addToast } = useStore()
  const [enables, setEnables] = useState<EnableMap>({})
  const [enableBusy, setEnableBusy] = useState<Record<string, boolean>>({})
  const [adaptive, setAdaptive] = useState<any>(null)

  useEffect(() => {
    const fetch = () => {
      api.agents().then(r => setAgents(r.data)).catch(() => {})
      api.getAgentEnables().then(r => setEnables(r.data)).catch(() => {})
      api.adaptiveStatus().then(r => setAdaptive(r.data)).catch(() => {})
    }
    fetch()
    const t = setInterval(fetch, 5000)
    return () => clearInterval(t)
  }, [])

  const handlePause = async (name: string) => {
    try { await api.pauseAgent(name); addToast(`${name} paused`, 'info') }
    catch (e: any) { addToast(e.response?.data?.detail || 'Pause failed', 'error') }
  }

  const handleResume = async (name: string) => {
    try { await api.resumeAgent(name); addToast(`${name} resumed`, 'buy') }
    catch (e: any) { addToast(e.response?.data?.detail || 'Resume failed', 'error') }
  }

  const handleToggleEnable = async (name: string, val: boolean) => {
    setEnableBusy(p => ({ ...p, [name]: true }))
    try {
      const r = await api.setAgentEnables({ [name]: val })
      setEnables(r.data)
      addToast(`${name} ${val ? 'enabled' : 'disabled'}`, val ? 'buy' : 'info')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Failed', 'error')
    } finally { setEnableBusy(p => ({ ...p, [name]: false })) }
  }

  return (
    <div className="h-full overflow-y-auto p-3 space-y-3">

      {/* Agent cards */}
      {Object.entries(AGENT_META).map(([key, meta]) => {
        const agent   = agents[key]
        const running = agent?.running ?? false
        const enabled = enables[key] !== false

        return (
          <div key={key} className={`bg-slate-800/60 border rounded-xl p-4 transition-all ${
            running ? 'border-emerald-800/60 shadow-md shadow-emerald-900/20' :
            enabled ? 'border-slate-700/50' : 'border-slate-800 opacity-60'
          }`}>
            <div className="flex items-start justify-between mb-3">
              <div className="flex items-center gap-2.5">
                <span className="text-xl">{meta.emoji}</span>
                <div>
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-bold text-slate-100">{meta.name}</span>
                    {running && (
                      <span className="inline-flex items-center gap-1 text-[10px] text-emerald-400 font-mono bg-emerald-950 border border-emerald-800 px-1.5 py-0.5 rounded">
                        <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-pulse" />RUNNING
                      </span>
                    )}
                    {!enabled && (
                      <span className="text-[10px] text-slate-500 bg-slate-700 px-1.5 py-0.5 rounded">DISABLED</span>
                    )}
                  </div>
                  <div className="text-[11px] text-slate-500 mt-0.5">{meta.desc}</div>
                </div>
              </div>

              {/* Enable toggle */}
              <button
                disabled={enableBusy[key]}
                onClick={() => handleToggleEnable(key, !enabled)}
                className={`relative w-9 h-5 rounded-full transition-colors shrink-0 mt-0.5 ${
                  enabled ? 'bg-emerald-600' : 'bg-slate-700'
                } ${enableBusy[key] ? 'opacity-50 cursor-not-allowed' : 'cursor-pointer'}`}
              >
                <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${enabled ? 'left-4' : 'left-0.5'}`} />
              </button>
            </div>

            {/* Stats */}
            {agent && (
              <div className="grid grid-cols-3 gap-3 mb-3 py-2 border-y border-slate-700/50">
                <div>
                  <div className="text-[10px] text-slate-500 uppercase tracking-wider">Trades</div>
                  <div className="text-sm font-mono font-bold text-slate-200">{agent.trades_today ?? 0}</div>
                </div>
                <div>
                  <div className="text-[10px] text-slate-500 uppercase tracking-wider">Win Rate</div>
                  <div className={`text-sm font-mono font-bold ${(agent.win_rate ?? 0) >= 55 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {agent.win_rate != null ? `${agent.win_rate.toFixed(1)}%` : '—'}
                  </div>
                </div>
                <div>
                  <div className="text-[10px] text-slate-500 uppercase tracking-wider">Last Signal</div>
                  <div className="text-[11px] font-mono text-slate-400 truncate">{agent.last_signal || '—'}</div>
                </div>
              </div>
            )}

            {/* Controls */}
            <div className="flex gap-2">
              {running ? (
                <button onClick={() => handlePause(key)}
                  className="flex-1 text-xs py-1.5 rounded-lg border border-slate-600 text-slate-300 hover:bg-slate-700 transition-colors">
                  Pause
                </button>
              ) : (
                <button onClick={() => handleResume(key)} disabled={!enabled}
                  className="flex-1 text-xs py-1.5 rounded-lg bg-emerald-700 hover:bg-emerald-600 text-white font-semibold transition-colors disabled:opacity-40 disabled:cursor-not-allowed">
                  Resume
                </button>
              )}
            </div>
          </div>
        )
      })}

      {/* Adaptive allocator */}
      {adaptive && (
        <div className="bg-slate-800/40 border border-slate-700/40 rounded-xl p-4">
          <div className="text-[10px] font-bold text-slate-600 uppercase tracking-widest mb-3">Adaptive Capital Allocator</div>
          <div className="grid grid-cols-2 gap-2">
            {adaptive.buckets && Object.entries(adaptive.buckets as Record<string, any>).map(([k, v]: [string, any]) => (
              <div key={k} className="flex justify-between items-center text-xs">
                <span className="text-slate-500 capitalize">{k}</span>
                <span className="font-mono text-slate-300">{v.weight ? `${(v.weight * 100).toFixed(0)}%` : '—'}</span>
              </div>
            ))}
          </div>
          <div className="flex gap-2 mt-3">
            <button onClick={() => api.adaptiveReview().catch(() => {})}
              className="text-[11px] px-2.5 py-1 rounded border border-slate-600 text-slate-400 hover:bg-slate-700 transition-colors">
              Force Review
            </button>
            <button onClick={() => api.capitalRebalance().catch(() => {})}
              className="text-[11px] px-2.5 py-1 rounded border border-slate-600 text-slate-400 hover:bg-slate-700 transition-colors">
              Rebalance Capital
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
