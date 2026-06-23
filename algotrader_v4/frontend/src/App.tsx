import React, { useEffect, useState, useRef } from 'react'
import {
  Activity, Terminal, Cpu, WifiOff, ShieldCheck,
  TrendingUp, TrendingDown, BarChart3, Clock,
  Zap, Play, Square, Database, Crosshair, ChevronUp, ChevronDown,
} from 'lucide-react'
import Header from './components/Header'
import PositionsTab from './components/tabs/PositionsTab'
import OrdersTab from './components/tabs/OrdersTab'
import BracketsTab from './components/tabs/BracketsTab'
import RiskTab from './components/tabs/RiskTab'
import AgentsTab from './components/tabs/AgentsTab'
import SebiTab from './components/tabs/SebiTab'
import { connectWS } from './ws/websocket'
import { useStore } from './store'
import { api } from './api/client'
import type { TabId } from './types'

const AGENT_META: Record<string, { strategy: string; displayName: string; id: string }> = {
  intraday: { strategy: 'VWAP Breakout',         displayName: 'INTRADAY', id: 'AGN-01' },
  fno:      { strategy: 'Options CE/PE',          displayName: 'F&O',      id: 'AGN-02' },
  swing:    { strategy: 'Multi-TF Trend',         displayName: 'SWING',    id: 'AGN-03' },
  scalping: { strategy: 'Orderbook Imbalance',    displayName: 'SCALPING', id: 'AGN-04' },
}
const AGENT_ORDER = ['intraday', 'fno', 'swing', 'scalping']

const OPS_TABS: { id: TabId; label: string }[] = [
  { id: 'positions', label: 'Positions' },
  { id: 'orders',   label: 'Orders'    },
  { id: 'brackets', label: 'Brackets'  },
  { id: 'risk',     label: 'Risk'      },
  { id: 'agents',   label: 'Agents'    },
  { id: 'sebi',     label: 'SEBI'      },
]
const TAB_COMPONENTS: Record<TabId, React.ComponentType> = {
  positions: PositionsTab,
  orders:    OrdersTab,
  brackets:  BracketsTab,
  risk:      RiskTab,
  agents:    AgentsTab,
  sebi:      SebiTab,
}

function Toasts() {
  const { toasts, removeToast } = useStore()
  const colors = { buy: 'bg-emerald-600', sell: 'bg-rose-600', info: 'bg-indigo-600', error: 'bg-rose-700' }
  return (
    <div className="fixed bottom-4 right-4 flex flex-col gap-2 z-50">
      {toasts.map(t => (
        <div
          key={t.id}
          onClick={() => removeToast(t.id)}
          className={`flex items-center gap-3 px-4 py-3 rounded-xl text-white text-sm font-medium shadow-lg cursor-pointer ${colors[t.type]}`}
        >
          {t.msg}
        </div>
      ))}
    </div>
  )
}

export default function App() {
  const {
    agents, setAgents,
    agentActivity, setAgentActivity,
    health, wsConnected,
    ticks, sparklines,
    positions, orders, botStatus, riskStatus,
    addToast,
  } = useStore()

  const [opsOpen, setOpsOpen]   = useState(false)
  const [activeTab, setActiveTab] = useState<TabId>('positions')
  const [liveTime, setLiveTime]   = useState(
    new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' })
  )
  const [tickSince, setTickSince] = useState(0)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    connectWS()
    const ping = setInterval(() => {
      import('./ws/websocket').then(m => m.sendPing())
    }, 30000)
    return () => clearInterval(ping)
  }, [])

  useEffect(() => {
    const t = setInterval(() => {
      setLiveTime(new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' }))
      setTickSince(p => p + 1)
    }, 1000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    const fetch = () => api.agents().then(r => setAgents(r.data)).catch(() => {})
    fetch()
    const t = setInterval(fetch, 5000)
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

  useEffect(() => { setTickSince(0) }, [agentActivity.length])

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

  const activeCount = AGENT_ORDER.filter(k => agents[k]?.running).length
  const pausedCount = AGENT_ORDER.filter(k => agents[k] && !agents[k].running).length

  const dailyPnl    = botStatus?.performance?.daily_pnl ?? positions.reduce((s, p) => s + (p.pnl || 0), 0)
  const pnlPositive = dailyPnl >= 0
  const pnlDisplay  = `${pnlPositive ? '+' : ''}₹${Math.abs(dailyPnl).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
  const marginUsed  = positions.reduce((s, p) => s + Math.abs(p.average_price * p.quantity), 0)
  const isHalted    = riskStatus?.is_halted

  const watchlistSymbols = Object.keys(ticks).slice(0, 8)
  const niftyKey   = Object.keys(ticks).find(k => k.includes('NIFTY')) || ''
  const niftyTick  = niftyKey ? ticks[niftyKey] : null
  const niftySpark = niftyKey ? (sparklines[niftyKey] || []) : []

  const chartData  = niftySpark.length >= 10
    ? niftySpark.slice(-30).map(v => {
        const arr = niftySpark.slice(-30)
        const min = Math.min(...arr); const max = Math.max(...arr)
        return 10 + ((v - min) / (max - min || 1)) * 80
      })
    : [...Array(30)].map((_, i) => 25 + Math.sin(i * 0.4) * 15 + Math.cos(i * 0.3) * 10)
  const linePoints = chartData.map((h, i) => `${(i / (chartData.length - 1)) * 100},${100 - h}`).join(' ')

  const logs = agentActivity.length > 0 ? agentActivity : [
    { time: '--:--:--', agent: 'SYSTEM', action: 'No activity yet — start bot to see live signals.', type: 'system' as const, cat: 'SYS' as const },
  ]

  const openPositionCount = positions.filter(p => p.quantity !== 0).length
  const TabComponent = TAB_COMPONENTS[activeTab]

  return (
    <div className="flex flex-col h-screen bg-slate-950 text-slate-300 overflow-hidden selection:bg-emerald-900 selection:text-emerald-50">

      {/* HEADER */}
      <Header />

      {/* HALTED BANNER */}
      {isHalted && (
        <div className="bg-rose-600/90 text-white text-xs text-center py-1.5 font-medium shrink-0 border-b border-rose-500">
          ⛔ Trading HALTED — daily loss limit reached. Open SEBI tab below to resume.
        </div>
      )}

      {/* MOOD LINE */}
      <div className={`h-0.5 w-full shrink-0 transition-colors ${pnlPositive ? 'bg-emerald-500 shadow-[0_0_8px_#10b981]' : 'bg-rose-500 shadow-[0_0_8px_#f43f5e]'}`} />

      {/* P&L STRIP */}
      <div className="bg-slate-900 border-b border-slate-800 px-4 h-11 flex items-center gap-6 shrink-0">
        <div className="flex items-baseline gap-3">
          <span className={`font-mono font-bold text-2xl leading-none ${pnlPositive ? 'text-emerald-400' : 'text-rose-400'}`}>
            {pnlDisplay}
          </span>
          <span className={`font-mono text-xs font-medium px-1.5 py-0.5 rounded flex items-center gap-1 ${pnlPositive ? 'text-emerald-500 bg-emerald-500/10' : 'text-rose-400 bg-rose-400/10'}`}>
            {pnlPositive ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
            Today P&L
          </span>
        </div>
        {marginUsed > 0 && (
          <div className="flex items-center gap-2 border-l border-slate-800 pl-6">
            <span className="text-[10px] text-slate-500 font-mono uppercase tracking-wider">Margin Used</span>
            <span className="text-slate-200 font-mono text-sm">₹{marginUsed.toLocaleString('en-IN', { maximumFractionDigits: 0 })}</span>
          </div>
        )}
        <div className="flex-1" />
        <div className="flex items-center gap-1.5 text-xs font-mono">
          <span className="text-slate-500">ACTIVE:</span>
          <span className="text-emerald-400 font-bold">{activeCount}</span>
          <span className="text-slate-700 mx-1">·</span>
          <span className="text-slate-500">PAUSED:</span>
          <span className="text-amber-500 font-bold">{pausedCount}</span>
        </div>
        <div className="flex items-center gap-2 border-l border-slate-800 pl-4 text-xs font-mono">
          <ShieldCheck className="w-3.5 h-3.5 text-emerald-500" />
          <span className="text-slate-300">{liveTime} IST</span>
        </div>
      </div>

      {/* MAIN BODY */}
      <div className="flex flex-1 overflow-hidden">

        {/* LEFT — AGENTS + ACTIVITY STREAM */}
        <div className="flex-1 flex flex-col min-w-0 border-r border-slate-800 bg-[#070b14]">

          {/* AGENTS GRID */}
          <div className="p-4 shrink-0">
            <div className="flex items-center justify-between mb-3">
              <h2 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <Zap className="w-4 h-4 text-emerald-500" />
                AUTONOMOUS AGENTS
              </h2>
              <div className="text-xs font-mono text-slate-500 flex gap-4">
                {health?.tick_engine && <span>ENGINE: <span className="text-slate-300">{health.tick_engine}</span></span>}
                {health?.mode      && <span>MODE: <span className={health.mode === 'LIVE' ? 'text-rose-400' : 'text-amber-400'}>{health.mode}</span></span>}
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
                            {active
                              ? <span className="flex h-1.5 w-1.5 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.8)]" />
                              : <span className="flex h-1.5 w-1.5 rounded-full bg-amber-500" />
                            }
                          </div>
                          <h3 className="text-base font-bold text-white leading-tight mt-1">{meta.displayName}</h3>
                          <p className="text-[11px] text-slate-500 italic mt-0.5">{meta.strategy}</p>
                        </div>
                        <div className={`text-xs font-mono px-2 py-0.5 rounded border ${active ? 'border-emerald-500/20 text-emerald-400 bg-emerald-500/10' : 'border-amber-500/20 text-amber-500 bg-amber-500/10'}`}>
                          {active ? 'ACTIVE' : 'PAUSED'}
                        </div>
                      </div>

                      <div className="mt-3 space-y-1 text-xs">
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
                        <div className="text-[10px] text-slate-500 uppercase tracking-wider mb-1">Last Signal</div>
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
            <div className="flex justify-between items-center mb-3 shrink-0">
              <h2 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <Terminal className="w-4 h-4 text-emerald-500" />
                LIVE DECISION STREAM
              </h2>
              <span className="text-xs text-slate-500 font-mono">
                {agentActivity.length > 0 ? `${tickSince}s ago` : 'waiting...'}
              </span>
            </div>

            <div ref={logRef} className="flex-1 overflow-y-auto space-y-0.5 font-mono text-xs pr-2 acc-scroll">
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
                  log.type === 'loss'   ? 'text-rose-500 font-bold' : 'text-slate-300'
                return (
                  <div key={i} className="flex items-stretch hover:bg-slate-900/80 rounded group transition-colors overflow-hidden">
                    <div className={`w-0.5 shrink-0 ${catColor}`} />
                    <div className="flex flex-1 gap-3 py-1.5 px-3 min-w-0">
                      <span className="text-slate-600 shrink-0 w-20">{log.time}</span>
                      {log.cat && <span className="text-slate-500 shrink-0 w-12 text-center text-[10px] bg-slate-900 py-0.5 rounded">{log.cat}</span>}
                      {log.agent && <span className="text-slate-400 shrink-0 w-20 truncate">[{log.agent}]</span>}
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
                <div className="p-4 text-center text-slate-600 text-xs mt-4">
                  <WifiOff className="w-8 h-8 mx-auto mb-2 opacity-30" />
                  No live ticks yet
                </div>
              )}
            </div>
          </div>

          {/* NIFTY CHART */}
          <div className="h-52 p-3 bg-slate-950/30 flex flex-col shrink-0">
            <div className="flex justify-between items-center mb-2">
              <h3 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <BarChart3 className="w-3.5 h-3.5" />
                {niftyKey || 'NIFTY'} TREND
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

      {/* OPERATIONS PANEL (COLLAPSIBLE BOTTOM) */}
      <div className={`shrink-0 flex flex-col bg-slate-900 border-t border-slate-800 transition-all duration-200 ${opsOpen ? 'h-52' : 'h-9'}`}>
        {/* Panel toggle bar */}
        <button
          onClick={() => setOpsOpen(o => !o)}
          className="h-9 shrink-0 flex items-center gap-4 px-4 hover:bg-slate-800/50 transition-colors"
        >
          <span className="font-bold text-slate-400 text-[10px] tracking-widest">OPERATIONS PANEL</span>
          <div className="flex gap-3 font-mono text-[10px] text-slate-500 flex-1">
            <span>POSITIONS: <span className="text-slate-300">{openPositionCount}</span></span>
            <span>ORDERS: <span className="text-slate-300">{orders.length}</span></span>
            {botStatus?.performance && (
              <span>TRADES: <span className="text-slate-300">{botStatus.performance.total_trades}</span></span>
            )}
            {riskStatus && (
              <span>DAILY P&L:
                <span className={riskStatus.daily_pnl >= 0 ? ' text-emerald-400' : ' text-rose-400'}>
                  {' '}{riskStatus.daily_pnl >= 0 ? '+' : ''}₹{Math.abs(riskStatus.daily_pnl).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                </span>
              </span>
            )}
          </div>
          {opsOpen ? <ChevronDown className="w-4 h-4 text-slate-500 shrink-0" /> : <ChevronUp className="w-4 h-4 text-slate-500 shrink-0" />}
        </button>

        {opsOpen && (
          <>
            {/* Tab bar */}
            <div className="flex border-b border-slate-800 shrink-0 overflow-x-auto">
              {OPS_TABS.map(tab => {
                const badge = tab.id === 'positions' && openPositionCount > 0 ? openPositionCount : undefined
                return (
                  <button
                    key={tab.id}
                    onClick={() => setActiveTab(tab.id)}
                    className={`flex items-center gap-1.5 px-4 py-2 text-xs font-medium whitespace-nowrap transition-colors border-b-2 ${
                      activeTab === tab.id
                        ? 'border-emerald-500 text-emerald-400 bg-emerald-500/5'
                        : 'border-transparent text-slate-500 hover:text-slate-300 hover:bg-slate-800/50'
                    }`}
                  >
                    {tab.label}
                    {badge !== undefined && (
                      <span className="bg-emerald-600 text-white rounded-full text-xs w-4 h-4 flex items-center justify-center">{badge}</span>
                    )}
                  </button>
                )
              })}
            </div>
            {/* Tab content */}
            <div className="flex-1 min-h-0 overflow-hidden bg-slate-950/30">
              <TabComponent />
            </div>
          </>
        )}
      </div>

      {/* FOOTER */}
      <footer className="h-8 bg-slate-950 border-t border-slate-900 flex justify-between items-center px-4 text-[10px] font-mono text-slate-500 shrink-0">
        <div className="flex gap-4">
          <span className="flex items-center gap-1"><Database className="w-3 h-3" /> AlgoTrader Pro v4</span>
          <span className="flex items-center gap-1"><Clock className="w-3 h-3" /> {health?.version || '—'}</span>
        </div>
        <div className="flex gap-4">
          <span>ENGINE: {health?.master || '—'}</span>
          <span>TICKS: {health?.tick_engine || '—'}</span>
          <span>TICKER: {health?.ticker_source || '—'}</span>
          <span className={wsConnected ? 'text-emerald-500' : 'text-slate-600'}>
            {wsConnected ? '● LIVE' : '○ OFFLINE'}
          </span>
        </div>
      </footer>

      <Toasts />

      <style>{`
        .acc-scroll::-webkit-scrollbar { width: 4px; }
        .acc-scroll::-webkit-scrollbar-track { background: rgba(15,23,42,0.5); }
        .acc-scroll::-webkit-scrollbar-thumb { background: rgba(51,65,85,0.5); border-radius: 4px; }
        .acc-scroll::-webkit-scrollbar-thumb:hover { background: rgba(71,85,105,0.8); }
      `}</style>
    </div>
  )
}
