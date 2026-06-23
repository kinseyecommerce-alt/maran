import React, { useEffect, useRef } from 'react'
import {
  Activity,
  Terminal,
  Cpu,
  Wifi,
  WifiOff,
  ShieldCheck,
  TrendingUp,
  TrendingDown,
  BarChart3,
  Clock,
  Zap,
  Play,
  Square,
  Database,
  Crosshair,
  X,
} from 'lucide-react'
import { useStore } from '../../store'
import { api } from '../../api/client'

const AGENT_META: Record<string, { strategy: string; displayName: string; id: string }> = {
  intraday: { strategy: 'VWAP Breakout',        displayName: 'INTRADAY', id: 'AGN-01' },
  fno:      { strategy: 'Options CE/PE',         displayName: 'F&O',      id: 'AGN-02' },
  swing:    { strategy: 'Multi-TF Trend',        displayName: 'SWING',    id: 'AGN-03' },
  scalping: { strategy: 'Orderbook Imbalance',   displayName: 'SCALPING', id: 'AGN-04' },
}

const AGENT_ORDER = ['intraday', 'fno', 'swing', 'scalping']

const chartBase = [...Array(30)].map((_, i) => 25 + Math.sin(i * 0.4) * 15 + Math.cos(i * 0.3) * 10)
const chartLinePoints = chartBase.map((h, i) => `${(i / 29) * 100},${100 - h}`).join(' ')

interface Props {
  onClose: () => void
}

export default function AgentCommandCenter({ onClose }: Props) {
  const {
    agents,
    setAgents,
    agentActivity,
    setAgentActivity,
    health,
    wsConnected,
    ticks,
    sparklines,
    positions,
    botStatus,
    addToast,
  } = useStore()

  const scrollRef = useRef<HTMLDivElement>(null)

  const time = new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' })
  const [liveTime, setLiveTime] = React.useState(time)
  const [lastUpdated, setLastUpdated] = React.useState(0)

  useEffect(() => {
    const t = setInterval(() => {
      setLiveTime(new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' }))
      setLastUpdated(p => p + 1)
    }, 1000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    const fetchAgents = () => api.agents().then(r => setAgents(r.data)).catch(() => {})
    fetchAgents()
    const t = setInterval(fetchAgents, 5000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    api.agentActivity()
      .then(r => {
        const entries = Array.isArray(r.data) ? r.data : (r.data?.entries || r.data?.activity || [])
        if (entries.length > 0) setAgentActivity(entries)
      })
      .catch(() => {})
  }, [])

  useEffect(() => {
    setLastUpdated(0)
  }, [agentActivity.length])

  const activeCount  = AGENT_ORDER.filter(k => agents[k]?.running).length
  const pausedCount  = AGENT_ORDER.filter(k => agents[k] && !agents[k].running).length

  const dailyPnl = botStatus?.performance?.daily_pnl
    ?? positions.reduce((sum, p) => sum + (p.pnl || 0), 0)
  const pnlPositive = dailyPnl >= 0
  const pnlDisplay  = `${pnlPositive ? '+' : ''}₹${Math.abs(dailyPnl).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
  const marginUsed  = positions.reduce((sum, p) => sum + Math.abs(p.average_price * p.quantity), 0)

  const isMarketOpen = health?.market_open ?? false
  const mode = health?.mode || 'PAPER'

  const watchlistSymbols = Object.keys(ticks).slice(0, 6)
  const niftyKey = Object.keys(ticks).find(k => k.includes('NIFTY')) || ''
  const niftySpark = niftyKey ? (sparklines[niftyKey] || []) : []
  const niftyTick  = niftyKey ? ticks[niftyKey] : null

  const chartData  = niftySpark.length >= 10
    ? niftySpark.slice(-30).map(v => {
        const min = Math.min(...niftySpark)
        const max = Math.max(...niftySpark)
        const range = max - min || 1
        return 10 + ((v - min) / range) * 80
      })
    : chartBase
  const linePoints = chartData.map((h, i) => `${(i / (chartData.length - 1)) * 100},${100 - h}`).join(' ')

  const handlePause = async (name: string) => {
    try {
      await api.pauseAgent(name)
      addToast(`Agent ${name} paused`, 'info')
      api.agents().then(r => setAgents(r.data)).catch(() => {})
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Pause failed', 'error')
    }
  }

  const handleResume = async (name: string) => {
    try {
      await api.resumeAgent(name)
      addToast(`Agent ${name} resumed`, 'buy')
      api.agents().then(r => setAgents(r.data)).catch(() => {})
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Resume failed', 'error')
    }
  }

  const logs = agentActivity.length > 0 ? agentActivity : [
    { time: '--:--:--', agent: 'SYSTEM', action: 'No activity data — connect to backend to see live signals.', type: 'system' as const, cat: 'SYS' as const },
  ]

  return (
    <div className="fixed inset-0 z-40 bg-slate-950 text-slate-300 font-sans flex flex-col overflow-hidden">

      {/* HEADER */}
      <header className="h-12 bg-slate-900 flex items-center justify-between px-4 shrink-0 relative border-b border-slate-800">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2 text-emerald-400 font-bold tracking-widest text-lg">
            <Cpu className="w-5 h-5" />
            <span>ALGO<span className="text-white">PRO</span> <span className="text-xs text-slate-500 font-mono tracking-normal">{health?.version || 'v4'}</span></span>
          </div>
          <div className="h-5 w-px bg-slate-700 mx-2" />
          <div className="flex items-center gap-2 text-xs font-mono">
            <div className={`flex items-center gap-1.5 px-2 py-1 rounded ${wsConnected ? 'text-emerald-400 bg-emerald-400/10' : 'text-slate-400 bg-slate-800'}`}>
              {wsConnected ? (
                <>
                  <span className="relative flex h-2 w-2">
                    <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                    <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
                  </span>
                  NSE {isMarketOpen ? 'OPEN' : 'CONNECTED'}
                </>
              ) : (
                <><WifiOff className="w-3 h-3" /> OFFLINE</>
              )}
            </div>
            {mode === 'LIVE' && (
              <div className="text-rose-400 bg-rose-400/10 px-2 py-1 rounded text-[10px] font-bold tracking-wider border border-rose-400/20">
                LIVE
              </div>
            )}
          </div>
        </div>

        <div className="flex items-center gap-6">
          <div className="flex flex-col items-end mr-4">
            <div className="flex items-baseline gap-3">
              <span className={`font-mono font-bold text-[28px] leading-none ${pnlPositive ? 'text-emerald-400' : 'text-rose-400'}`}>
                {pnlDisplay}
              </span>
              {botStatus?.performance && (
                <span className={`font-mono text-sm font-medium px-1.5 py-0.5 rounded flex items-center gap-1 ${pnlPositive ? 'text-emerald-500 bg-emerald-500/10' : 'text-rose-400 bg-rose-400/10'}`}>
                  {pnlPositive ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                  Today
                </span>
              )}
            </div>
          </div>
          {marginUsed > 0 && (
            <div className="flex flex-col items-end">
              <span className="text-[10px] text-slate-500 font-mono uppercase tracking-wider">Used Margin</span>
              <span className="text-slate-200 font-mono text-sm">
                ₹{marginUsed.toLocaleString('en-IN', { maximumFractionDigits: 0 })}
              </span>
            </div>
          )}
          <div className="flex items-center gap-3 ml-4 pl-4 border-l border-slate-800 h-8">
            <ShieldCheck className="w-4 h-4 text-emerald-500" />
            <span className="font-mono text-sm text-slate-300">{liveTime} IST</span>
          </div>
          <button
            onClick={onClose}
            className="ml-2 p-1.5 rounded text-slate-400 hover:text-slate-100 hover:bg-slate-800 transition-colors"
            title="Close Agent View"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </header>

      {/* MOOD LINE */}
      <div className={`h-0.5 w-full shrink-0 ${pnlPositive ? 'bg-emerald-500 shadow-[0_0_8px_#10b981]' : 'bg-rose-500 shadow-[0_0_8px_#f43f5e]'}`} />

      {/* MAIN LAYOUT */}
      <div className="flex flex-1 overflow-hidden">

        {/* LEFT — AGENTS + STREAM */}
        <div className="flex-1 flex flex-col min-w-0 border-r border-slate-800 bg-[#070b14]">

          {/* AGENTS */}
          <div className="p-4 shrink-0">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <Zap className="w-4 h-4 text-emerald-500" />
                AUTONOMOUS AGENTS
              </h2>
              <div className="text-xs font-mono text-slate-500 flex gap-4">
                <span>ACTIVE: <span className="text-emerald-400">{activeCount}</span></span>
                <span>PAUSED: <span className="text-amber-500">{pausedCount}</span></span>
                {health?.tick_engine && (
                  <span>ENGINE: <span className="text-slate-300">{health.tick_engine}</span></span>
                )}
              </div>
            </div>

            <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
              {AGENT_ORDER.map(key => {
                const agent  = agents[key]
                const meta   = AGENT_META[key]
                const active = agent?.running ?? false

                return (
                  <div
                    key={key}
                    className={`rounded-lg bg-slate-900/50 relative overflow-hidden flex flex-col transition-colors border ${
                      active ? 'border-slate-800 border-l-2 border-l-emerald-500/60' : 'border-slate-800 opacity-75'
                    }`}
                  >
                    <div className="p-4 flex-1">
                      <div className="flex justify-between items-start mb-1">
                        <div>
                          <div className="flex items-center gap-2">
                            <span className="font-mono text-[10px] text-slate-500">{meta.id}</span>
                            {active ? (
                              <span className="flex h-1.5 w-1.5 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.8)]" />
                            ) : (
                              <span className="flex h-1.5 w-1.5 rounded-full bg-amber-500" />
                            )}
                          </div>
                          <h3 className="text-base font-bold text-white leading-tight mt-1">{meta.displayName}</h3>
                          <p className="text-[11px] text-slate-500 italic mt-0.5">{meta.strategy}</p>
                        </div>
                        <div className={`text-xs font-mono px-2 py-0.5 rounded border ${active ? 'border-emerald-500/20 text-emerald-400 bg-emerald-500/10' : 'border-amber-500/20 text-amber-500 bg-amber-500/10'}`}>
                          {active ? 'ACTIVE' : 'PAUSED'}
                        </div>
                      </div>

                      <div className="mt-3 space-y-1.5 text-xs">
                        {agent?.trades_today !== undefined && (
                          <div className="flex justify-between">
                            <span className="text-slate-500">Trades</span>
                            <span className="font-mono text-slate-300">{agent.trades_today}</span>
                          </div>
                        )}
                        {agent?.win_rate !== undefined && (
                          <div className="flex justify-between">
                            <span className="text-slate-500">Win Rate</span>
                            <span className={`font-mono ${agent.win_rate >= 55 ? 'text-emerald-400' : 'text-rose-400'}`}>
                              {agent.win_rate.toFixed(1)}%
                            </span>
                          </div>
                        )}
                      </div>

                      <div className="mt-3 bg-slate-950 rounded p-2.5 border border-slate-800">
                        <div className="text-[10px] text-slate-500 uppercase tracking-wider mb-1 flex justify-between">
                          <span>Last Signal</span>
                        </div>
                        <div className="font-mono text-xs text-slate-300 truncate" title={agent?.last_signal || '—'}>
                          {agent?.last_signal || '—'}
                        </div>
                      </div>

                      <div className="mt-3 flex gap-2">
                        {active ? (
                          <button
                            className="flex-1 flex items-center justify-center gap-1 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs py-1.5 rounded transition-colors border border-slate-700"
                            onClick={() => handlePause(key)}
                          >
                            <Square className="w-3 h-3" /> Pause
                          </button>
                        ) : (
                          <button
                            className="flex-1 flex items-center justify-center gap-1 bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 text-xs py-1.5 rounded transition-colors border border-emerald-500/30"
                            onClick={() => handleResume(key)}
                          >
                            <Play className="w-3 h-3 fill-current" /> Resume
                          </button>
                        )}
                        <button className="px-2 bg-slate-800 hover:bg-slate-700 text-slate-400 text-xs py-1.5 rounded transition-colors border border-slate-700">
                          <TrendingUp className="w-3 h-3" />
                        </button>
                      </div>
                    </div>

                    <div className={`h-[3px] w-full mt-auto ${active ? 'bg-emerald-500/80 animate-pulse' : 'bg-amber-500/60'}`} />
                  </div>
                )
              })}
            </div>
          </div>

          {/* ACTIVITY STREAM */}
          <div className="flex-1 flex flex-col p-4 border-t border-slate-800 bg-slate-950 overflow-hidden">
            <div className="flex justify-between items-center mb-4 shrink-0">
              <h2 className="text-sm font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <Terminal className="w-4 h-4 text-emerald-500" />
                LIVE DECISION STREAM
              </h2>
              <span className="text-xs text-slate-500 font-mono">
                {lastUpdated < 60 ? `${lastUpdated}s ago` : 'live'}
              </span>
            </div>

            <div ref={scrollRef} className="flex-1 overflow-y-auto space-y-0.5 font-mono text-xs pr-2 acc-scroll">
              {logs.map((log, i) => {
                if (log.group) {
                  return (
                    <div key={i} className="flex items-center gap-3 py-2 mt-2 first:mt-0">
                      <div className="h-px bg-slate-800 flex-1" />
                      <span className="text-[10px] text-slate-500 tracking-widest uppercase">{log.group}</span>
                      <div className="h-px bg-slate-800 flex-1" />
                    </div>
                  )
                }

                const catColor =
                  log.cat === 'EXEC' ? 'bg-emerald-500/80' :
                  log.cat === 'RISK' ? 'bg-rose-500/80' :
                  log.cat === 'WARN' ? 'bg-amber-500/80' :
                  log.cat === 'SIG'  ? 'bg-blue-500/80' : 'bg-slate-500/80'

                const textColor =
                  log.type === 'buy'    ? 'text-emerald-400' :
                  log.type === 'sell'   ? 'text-rose-400' :
                  log.type === 'alert'  ? 'text-amber-400' :
                  log.type === 'loss'   ? 'text-rose-500 font-bold' :
                  'text-slate-300'

                return (
                  <div key={i} className="flex items-stretch hover:bg-slate-900/80 rounded group transition-colors overflow-hidden">
                    <div className={`w-0.5 shrink-0 ${catColor}`} />
                    <div className="flex flex-1 gap-3 py-1.5 px-3 min-w-0">
                      <span className="text-slate-600 shrink-0 w-20">{log.time}</span>
                      {log.cat && (
                        <span className="text-slate-500 shrink-0 w-12 text-center text-[10px] bg-slate-900 py-0.5 rounded">{log.cat}</span>
                      )}
                      {log.agent && (
                        <span className="text-slate-400 shrink-0 w-20 truncate">[{log.agent}]</span>
                      )}
                      <span className={`flex-1 truncate ${textColor}`}>{log.action}</span>
                      {log.confidence && (
                        <span className="text-slate-600 shrink-0 w-16 text-right opacity-0 group-hover:opacity-100 transition-opacity">
                          C: {log.confidence}
                        </span>
                      )}
                    </div>
                  </div>
                )
              })}
              <div className="flex items-stretch rounded overflow-hidden mt-1">
                <div className="w-0.5 shrink-0 bg-transparent" />
                <div className="flex gap-4 py-1.5 px-3">
                  <span className="text-emerald-500/50 shrink-0 w-20 animate-pulse">...</span>
                  <span className="text-slate-600">Waiting for signals…</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* RIGHT SIDEBAR */}
        <div className="w-72 flex flex-col bg-slate-900 shrink-0">

          {/* WATCHLIST */}
          <div className="flex-1 border-b border-slate-800 flex flex-col min-h-0">
            <div className="p-3 border-b border-slate-800 flex justify-between items-center bg-slate-950/50 shrink-0">
              <h3 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <Activity className="w-3.5 h-3.5" />
                MARKET OVERVIEW
              </h3>
            </div>
            <div className="flex-1 overflow-y-auto acc-scroll">
              {watchlistSymbols.length > 0 ? watchlistSymbols.map(sym => {
                const tick = ticks[sym]
                const up   = (tick?.change_pct ?? 0) >= 0
                return (
                  <div key={sym} className="flex justify-between items-center p-3 border-b border-slate-800/50 hover:bg-slate-800/30 cursor-pointer transition-colors">
                    <div>
                      <div className="font-bold text-slate-200 text-sm">{sym}</div>
                      <div className="text-[10px] text-slate-500 mt-0.5">{tick?.source || 'NSE'}</div>
                    </div>
                    <div className="text-right">
                      <div className="font-mono text-sm text-slate-200">
                        {tick ? tick.ltp.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—'}
                      </div>
                      {tick && (
                        <div className={`font-mono text-[10px] flex items-center justify-end gap-1 mt-0.5 ${up ? 'text-emerald-400' : 'text-rose-400'}`}>
                          {up ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                          {up ? '+' : ''}{tick.change_pct?.toFixed(2)}%
                        </div>
                      )}
                    </div>
                  </div>
                )
              }) : (
                <div className="p-4 text-center text-slate-600 text-xs">
                  No ticks yet — connect WebSocket to see live prices
                </div>
              )}
            </div>
          </div>

          {/* NIFTY CHART */}
          <div className="h-56 p-3 bg-slate-950/30 flex flex-col shrink-0">
            <div className="flex justify-between items-center mb-3">
              <h3 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <BarChart3 className="w-3.5 h-3.5" />
                {niftyKey || 'NIFTY50'} TREND
              </h3>
              {niftyTick && (
                <span className="text-[10px] font-mono text-emerald-400">
                  {niftyTick.ltp.toLocaleString('en-IN', { maximumFractionDigits: 2 })}
                </span>
              )}
            </div>

            <div className="flex-1 relative border border-slate-800 rounded bg-[#0b1120] overflow-hidden group">
              <div className="absolute inset-0 pointer-events-none flex flex-col justify-between py-[12.5%] opacity-20">
                <div className="w-full h-px border-t border-dashed border-slate-400" />
                <div className="w-full h-px border-t border-dashed border-slate-400" />
                <div className="w-full h-px border-t border-dashed border-slate-400" />
              </div>
              <div className="absolute inset-0 flex items-end">
                <div className="w-full h-full flex items-end justify-between px-1 opacity-40 group-hover:opacity-60 transition-opacity">
                  {chartData.map((h, i) => (
                    <div key={i} className="w-[2%] bg-emerald-500/20 rounded-t-[1px]" style={{ height: `${h}%` }} />
                  ))}
                </div>
                <svg className="absolute inset-0 h-full w-full opacity-80" viewBox="0 0 100 100" preserveAspectRatio="none">
                  <polyline points={linePoints} fill="none" stroke="rgba(16,185,129,0.8)" strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
                  <polygon points={`0,100 ${linePoints} 100,100`} fill="rgba(16,185,129,0.05)" />
                </svg>
              </div>
              <div className="absolute bottom-2 right-2 flex items-center gap-1.5 bg-slate-900/80 backdrop-blur border border-slate-700/50 px-2 py-1 rounded text-[10px] font-mono text-emerald-400">
                <Crosshair className="w-3 h-3" />
                {niftyTick?.trend || 'LIVE'}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* SESSION SUMMARY */}
      <div className="bg-slate-900/80 border-t border-slate-800 px-4 py-2 shrink-0 flex items-center gap-6 font-mono text-[10px] text-slate-400">
        <span className="font-bold text-slate-300">SESSION</span>
        <span>Agents: <span className="text-slate-200">{AGENT_ORDER.length}</span></span>
        <span>Active: <span className="text-emerald-400">{activeCount}</span></span>
        <span>Signals: <span className="text-slate-200">{agentActivity.filter(e => e.cat === 'SIG').length}</span></span>
        <span>Mode: <span className={mode === 'LIVE' ? 'text-rose-400' : 'text-amber-400'}>{mode}</span></span>
        {botStatus?.performance && (
          <span>Total Trades: <span className="text-slate-200">{botStatus.performance.total_trades}</span></span>
        )}
      </div>

      {/* FOOTER */}
      <footer className="h-8 bg-slate-950 border-t border-slate-900 flex justify-between items-center px-4 text-[10px] font-mono text-slate-500 shrink-0">
        <div className="flex gap-4">
          <span className="flex items-center gap-1"><Database className="w-3 h-3" /> MEM: —</span>
          <span className="flex items-center gap-1"><Clock className="w-3 h-3" /> {health?.version || 'AlgoTrader Pro v4'}</span>
        </div>
        <div className="flex gap-4">
          <span>ENGINE: {health?.master || '—'}</span>
          <span>TICKS: {health?.tick_engine || '—'}</span>
          <span className={wsConnected ? 'text-emerald-500' : 'text-slate-600'}>
            {wsConnected ? '● LIVE' : '○ OFFLINE'}
          </span>
        </div>
      </footer>

      <style>{`
        .acc-scroll::-webkit-scrollbar { width: 4px; }
        .acc-scroll::-webkit-scrollbar-track { background: rgba(15,23,42,0.5); }
        .acc-scroll::-webkit-scrollbar-thumb { background: rgba(51,65,85,0.5); border-radius: 4px; }
        .acc-scroll::-webkit-scrollbar-thumb:hover { background: rgba(71,85,105,0.8); }
      `}</style>
    </div>
  )
}
