import React, { useEffect, useState, useRef } from 'react'
import {
  Activity, Terminal, Cpu, WifiOff, ShieldCheck,
  TrendingUp, TrendingDown, BarChart3,
  Zap, Play, Square, Database, Crosshair,
  LayoutDashboard, ClipboardList, Target, Scale, History,
} from 'lucide-react'
import Header from './components/Header'
import PositionsTab from './components/tabs/PositionsTab'
import OrdersTab from './components/tabs/OrdersTab'
import BracketsTab from './components/tabs/BracketsTab'
import RiskTab from './components/tabs/RiskTab'
import AgentsTab from './components/tabs/AgentsTab'
import SebiTab from './components/tabs/SebiTab'
import TradeHistoryTab from './components/tabs/TradeHistoryTab'
import { connectWS } from './ws/websocket'
import { useStore } from './store'
import { api } from './api/client'
import type { TabId } from './types'

type PageId = TabId | 'dashboard'

const AGENT_META: Record<string, { strategy: string; displayName: string; id: string }> = {
  intraday:      { strategy: 'VWAP Breakout',      displayName: 'INTRADAY',  id: 'AGN-01' },
  options:       { strategy: 'Options CE/PE',       displayName: 'F&O',       id: 'AGN-02' },
  swing:         { strategy: 'Multi-TF Trend',      displayName: 'SWING',     id: 'AGN-03' },
  scalping:      { strategy: 'Orderbook Imbalance', displayName: 'SCALPING',  id: 'AGN-04' },
  futures:       { strategy: 'Futures Momentum',    displayName: 'FUTURES',   id: 'AGN-05' },
  momentum:      { strategy: 'Price Momentum',      displayName: 'MOMENTUM',  id: 'AGN-06' },
  mean_reversion:{ strategy: 'Mean Reversion',      displayName: 'MEAN REV',  id: 'AGN-07' },
  pairs:         { strategy: 'Statistical Arb',     displayName: 'PAIRS ARB', id: 'AGN-08' },
}
const AGENT_ORDER = ['intraday', 'options', 'swing', 'scalping', 'futures', 'momentum', 'mean_reversion', 'pairs']

const TAB_COMPONENTS: Record<string, React.ComponentType> = {
  positions: PositionsTab,
  orders:    OrdersTab,
  brackets:  BracketsTab,
  risk:      RiskTab,
  agents:    AgentsTab,
  sebi:      SebiTab,
  history:   TradeHistoryTab,
}

const SIDEBAR_NAV: { id: PageId; label: string; Icon: React.ComponentType<{ className?: string }> }[] = [
  { id: 'dashboard', label: 'Dashboard',     Icon: LayoutDashboard },
  { id: 'positions', label: 'Positions',     Icon: TrendingUp      },
  { id: 'orders',    label: 'Orders',        Icon: ClipboardList   },
  { id: 'brackets',  label: 'Brackets',      Icon: Target          },
  { id: 'risk',      label: 'Risk',          Icon: ShieldCheck     },
  { id: 'agents',    label: 'Agents',        Icon: Cpu             },
  { id: 'sebi',      label: 'SEBI',          Icon: Scale           },
  { id: 'history',   label: 'Trade History', Icon: History         },
]

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

// ── Login Screen ──────────────────────────────────────────────────────────────
function LoginScreen({ onSuccess }: { onSuccess: () => void }) {
  const { setToken } = useStore()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError]       = useState('')
  const [loading, setLoading]   = useState(false)

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!username) return
    setLoading(true)
    setError('')
    try {
      const r = await api.login(username, password)
      if (r.data?.access_token) {
        setToken(r.data.access_token)
      }
      onSuccess()
    } catch (err: any) {
      setError(err.response?.data?.detail || 'Login failed — check credentials')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-slate-950 flex items-center justify-center z-50">
      <div className="w-full max-w-sm px-4">
        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-8 shadow-2xl shadow-black/50">
          <div className="flex items-center gap-3 mb-8">
            <div className="w-9 h-9 rounded-lg bg-emerald-500/20 border border-emerald-500/30 flex items-center justify-center">
              <span className="text-emerald-400 font-bold text-base">A</span>
            </div>
            <div>
              <div className="text-sm font-bold text-slate-100 tracking-widest font-mono">ALGOPRO</div>
              <div className="text-[10px] text-slate-600 tracking-wider">COMMAND CENTER v4</div>
            </div>
          </div>

          <form onSubmit={handleLogin} className="space-y-4">
            <div>
              <label className="block text-[11px] font-medium text-slate-500 uppercase tracking-widest mb-1.5">Username</label>
              <input
                type="text" autoComplete="username" autoFocus
                value={username} onChange={e => setUsername(e.target.value)}
                className="w-full px-4 py-2.5 bg-slate-800 border border-slate-700 rounded-lg text-slate-200 text-sm font-mono
                           focus:outline-none focus:ring-1 focus:ring-emerald-500 focus:border-emerald-500 transition-all placeholder-slate-600"
                placeholder="admin"
              />
            </div>
            <div>
              <label className="block text-[11px] font-medium text-slate-500 uppercase tracking-widest mb-1.5">Password</label>
              <input
                type="password" autoComplete="current-password"
                value={password} onChange={e => setPassword(e.target.value)}
                className="w-full px-4 py-2.5 bg-slate-800 border border-slate-700 rounded-lg text-slate-200 text-sm font-mono
                           focus:outline-none focus:ring-1 focus:ring-emerald-500 focus:border-emerald-500 transition-all placeholder-slate-600"
                placeholder="••••••••"
              />
            </div>
            {error && (
              <div className="text-xs text-rose-400 bg-rose-950/50 border border-rose-900 rounded-lg px-3 py-2">{error}</div>
            )}
            <button type="submit" disabled={loading || !username}
              className="w-full py-2.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-semibold
                         transition-colors disabled:opacity-40 disabled:cursor-not-allowed mt-2">
              {loading ? 'Authenticating…' : 'Sign In →'}
            </button>
          </form>
          <p className="text-[10px] text-slate-700 text-center mt-6 font-mono">
            AlgoTrader Pro · Authorized Access Only
          </p>
        </div>
      </div>
    </div>
  )
}

// ── Main App ──────────────────────────────────────────────────────────────────
export default function App() {
  const {
    agents, setAgents,
    agentActivity, setAgentActivity,
    health, wsConnected,
    ticks, sparklines,
    positions, orders, botStatus, riskStatus,
    addToast, token, clearToken,
  } = useStore()

  const [isAuthed, setIsAuthed] = useState<boolean | null>(null)
  const [activePage, setActivePage] = useState<PageId>('dashboard')
  const [liveTime, setLiveTime] = useState(
    new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' })
  )
  const [tickSince, setTickSince] = useState(0)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    api.authMe()
      .then(() => setIsAuthed(true))
      .catch(err => {
        if (err.response?.status === 401) setIsAuthed(false)
        else setIsAuthed(true)
      })
  }, [token])

  useEffect(() => {
    if (!isAuthed) return
    connectWS()
    const ping = setInterval(() => {
      import('./ws/websocket').then(m => m.sendPing())
    }, 30000)
    return () => clearInterval(ping)
  }, [isAuthed])

  useEffect(() => {
    const t = setInterval(() => {
      setLiveTime(new Date().toLocaleTimeString('en-IN', { hour12: false, timeZone: 'Asia/Kolkata' }))
      setTickSince(p => p + 1)
    }, 1000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    if (!isAuthed) return
    const fetch = () => api.agents().then(r => setAgents(r.data)).catch(() => {})
    fetch()
    const t = setInterval(fetch, 5000)
    return () => clearInterval(t)
  }, [isAuthed])

  useEffect(() => {
    if (!isAuthed) return
    api.agentActivity()
      .then(r => {
        const entries = Array.isArray(r.data)
          ? r.data
          : (r.data?.events || r.data?.entries || r.data?.activity || [])
        if (entries.length > 0) setAgentActivity(entries)
      })
      .catch(() => {})
  }, [isAuthed])

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

  const handleLogout = async () => {
    try { await api.authLogout() } catch {}
    clearToken()
    setIsAuthed(false)
  }

  const navBadge = (id: PageId): number | undefined => {
    if (id === 'positions') return openPositionCount > 0 ? openPositionCount : undefined
    if (id === 'orders')    return orders.length > 0 ? orders.length : undefined
    return undefined
  }

  // ── Auth gates ──────────────────────────────────────────────────────────────
  if (isAuthed === null) {
    return (
      <div className="fixed inset-0 bg-slate-950 flex items-center justify-center">
        <div className="text-emerald-500 font-mono text-xs animate-pulse tracking-widest">AUTHENTICATING…</div>
      </div>
    )
  }
  if (isAuthed === false) {
    return <LoginScreen onSuccess={() => setIsAuthed(true)} />
  }

  const PageComponent = activePage !== 'dashboard' ? TAB_COMPONENTS[activePage] : null

  return (
    <div className="flex flex-col h-screen bg-slate-950 text-slate-300 overflow-hidden selection:bg-emerald-900 selection:text-emerald-50">

      {/* HEADER */}
      <Header />

      {/* HALTED BANNER */}
      {isHalted && (
        <div className="bg-rose-600/90 text-white text-xs text-center py-1.5 font-medium shrink-0 border-b border-rose-500">
          ⛔ Trading HALTED — daily loss limit reached. Open Risk or SEBI page to resume.
        </div>
      )}

      {/* MOOD LINE */}
      <div className={`h-0.5 w-full shrink-0 transition-colors ${pnlPositive ? 'bg-emerald-500 shadow-[0_0_8px_#10b981]' : 'bg-rose-500 shadow-[0_0_8px_#f43f5e]'}`} />

      {/* BODY: SIDEBAR + CONTENT */}
      <div className="flex flex-1 overflow-hidden">

        {/* ── LEFT SIDEBAR ─────────────────────────────────────────────────── */}
        <aside className="w-44 shrink-0 flex flex-col bg-slate-900 border-r border-slate-800">

          {/* P&L block */}
          <div className="px-4 py-3 border-b border-slate-800 shrink-0">
            <div className={`font-mono font-bold text-xl leading-none ${pnlPositive ? 'text-emerald-400' : 'text-rose-400'}`}>
              {pnlDisplay}
            </div>
            <div className={`text-[10px] mt-1 flex items-center gap-1 font-mono ${pnlPositive ? 'text-emerald-500/70' : 'text-rose-400/70'}`}>
              {pnlPositive ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
              Today P&L
            </div>
            <div className="flex gap-3 mt-2 text-[10px] font-mono">
              <span className="text-slate-500">ACTIVE: <span className="text-emerald-400">{activeCount}</span></span>
              <span className="text-slate-500">PAUSED: <span className="text-amber-500">{pausedCount}</span></span>
            </div>
          </div>

          {/* Nav items */}
          <nav className="flex-1 p-2 space-y-0.5 overflow-y-auto acc-scroll">
            {SIDEBAR_NAV.map(item => {
              const badge = navBadge(item.id)
              const active = activePage === item.id
              return (
                <button
                  key={item.id}
                  onClick={() => setActivePage(item.id)}
                  className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-xs font-medium transition-all ${
                    active
                      ? 'bg-emerald-500/15 text-emerald-400 border border-emerald-500/25 shadow-[0_0_8px_rgba(16,185,129,0.08)]'
                      : 'text-slate-500 hover:text-slate-200 hover:bg-slate-800/60 border border-transparent'
                  }`}
                >
                  <item.Icon className={`w-3.5 h-3.5 shrink-0 ${active ? 'text-emerald-400' : 'text-slate-600'}`} />
                  <span className="flex-1 text-left">{item.label}</span>
                  {badge !== undefined && (
                    <span className="bg-emerald-600 text-white rounded-full text-[9px] w-4 h-4 flex items-center justify-center shrink-0 font-bold">
                      {badge > 9 ? '9+' : badge}
                    </span>
                  )}
                </button>
              )
            })}
          </nav>

          {/* Status footer */}
          <div className="px-4 py-3 border-t border-slate-800 shrink-0 space-y-2">
            <div className="flex items-center gap-1.5 text-[10px] font-mono">
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${wsConnected ? 'bg-emerald-500 shadow-[0_0_4px_#10b981]' : 'bg-slate-600'}`} />
              <span className={wsConnected ? 'text-emerald-400' : 'text-slate-600'}>{wsConnected ? 'LIVE' : 'OFFLINE'}</span>
              <span className="text-slate-700 ml-1">·</span>
              <span className="text-slate-500">{liveTime}</span>
            </div>
            {health?.mode && (
              <div className={`text-[10px] font-mono font-bold px-2 py-0.5 rounded w-fit ${health.mode === 'LIVE' ? 'text-rose-400 bg-rose-400/10' : 'text-amber-400 bg-amber-400/10'}`}>
                {health.mode} MODE
              </div>
            )}
            <button onClick={handleLogout}
              className="text-[10px] text-slate-600 hover:text-rose-400 transition-colors font-mono">
              ⏻ Logout
            </button>
          </div>
        </aside>

        {/* ── CONTENT AREA ──────────────────────────────────────────────────── */}
        <div className="flex-1 min-w-0 overflow-hidden flex flex-col">

          {activePage === 'dashboard' ? (

            /* ── DASHBOARD VIEW ── */
            <div className="flex flex-1 overflow-hidden">

              {/* LEFT: AGENTS + ACTIVITY STREAM */}
              <div className="flex-1 flex flex-col min-w-0 border-r border-slate-800 bg-[#070b14]">

                {/* AGENTS GRID */}
                <div className="px-4 pt-4 pb-2 shrink-0">
                  <div className="flex items-center justify-between mb-3">
                    <h2 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                      <Zap className="w-4 h-4 text-emerald-500" />
                      AUTONOMOUS AGENTS
                      <span className="text-slate-600 font-normal">({AGENT_ORDER.length})</span>
                    </h2>
                    <div className="text-xs font-mono text-slate-500 flex gap-4">
                      {health?.tick_engine && <span>ENGINE: <span className="text-slate-300">{health.tick_engine}</span></span>}
                      {health?.mode        && <span>MODE: <span className={health.mode === 'LIVE' ? 'text-rose-400' : 'text-amber-400'}>{health.mode}</span></span>}
                    </div>
                  </div>

                  <div className="flex gap-3 overflow-x-auto pb-2" style={{ scrollbarWidth: 'thin' }}>
                    {AGENT_ORDER.map(key => {
                      const agent  = agents[key]
                      const meta   = AGENT_META[key]
                      const active = agent?.running ?? false

                      const ls = agent?.last_signal as unknown
                      let sigDisplay = '—'
                      if (typeof ls === 'string' && ls) sigDisplay = ls
                      else if (ls && typeof ls === 'object') {
                        const s = ls as Record<string, unknown>
                        sigDisplay = [s.symbol, s.action].filter(Boolean).join(' ') || '—'
                      }

                      return (
                        <div
                          key={key}
                          className={`rounded-lg bg-slate-900/50 flex flex-col border shrink-0 transition-colors overflow-hidden ${
                            active ? 'border-emerald-700/40 border-l-2 border-l-emerald-500' : 'border-slate-800 opacity-80'
                          }`}
                          style={{ minWidth: '175px', width: 'calc(12.5% - 10px)' }}
                        >
                          <div className="p-3 flex-1">
                            <div className="flex items-center justify-between mb-1.5">
                              <div className="flex items-center gap-1.5">
                                <span className="font-mono text-[9px] text-slate-600">{meta.id}</span>
                                <span className={`w-1.5 h-1.5 rounded-full ${active ? 'bg-emerald-500 shadow-[0_0_6px_#10b981]' : 'bg-amber-500'}`} />
                              </div>
                              <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${active ? 'text-emerald-400 bg-emerald-500/10' : 'text-amber-500 bg-amber-500/10'}`}>
                                {active ? 'ON' : 'OFF'}
                              </span>
                            </div>
                            <div className="font-bold text-sm text-white leading-none">{meta.displayName}</div>
                            <div className="text-[10px] text-slate-500 italic mt-0.5 truncate">{meta.strategy}</div>

                            <div className="mt-2 flex gap-3 text-[10px]">
                              <div>
                                <div className="text-slate-600">Trades</div>
                                <div className="font-mono text-slate-300">{agent?.trades_today ?? 0}</div>
                              </div>
                              {agent?.win_rate != null && (
                                <div>
                                  <div className="text-slate-600">Win%</div>
                                  <div className={`font-mono ${Number(agent.win_rate) >= 55 ? 'text-emerald-400' : 'text-rose-400'}`}>
                                    {Number(agent.win_rate).toFixed(0)}%
                                  </div>
                                </div>
                              )}
                            </div>

                            <div className="mt-2 bg-slate-950 rounded px-2 py-1.5 border border-slate-800/60">
                              <div className="text-[9px] text-slate-600 uppercase tracking-wider">Signal</div>
                              <div className="font-mono text-[10px] text-slate-400 truncate mt-0.5" title={sigDisplay}>{sigDisplay}</div>
                            </div>

                            <div className="mt-2 flex gap-1.5">
                              {active ? (
                                <button
                                  className="flex-1 flex items-center justify-center gap-1 bg-slate-800 hover:bg-slate-700 text-slate-300 text-[10px] py-1.5 rounded transition-colors"
                                  onClick={() => handlePause(key)}
                                >
                                  <Square className="w-2.5 h-2.5" /> Pause
                                </button>
                              ) : (
                                <button
                                  className="flex-1 flex items-center justify-center gap-1 bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 text-[10px] py-1.5 rounded transition-colors border border-emerald-500/30"
                                  onClick={() => handleResume(key)}
                                >
                                  <Play className="w-2.5 h-2.5 fill-current" /> Resume
                                </button>
                              )}
                            </div>
                          </div>
                          <div className={`h-[2px] w-full ${active ? 'bg-emerald-500 animate-pulse' : 'bg-amber-600/50'}`} />
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

              {/* RIGHT: WATCHLIST + NIFTY CHART */}
              <div className="w-64 flex flex-col bg-slate-900 shrink-0">

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
                <div className="h-48 p-3 bg-slate-950/30 flex flex-col shrink-0">
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

          ) : (

            /* ── FULL-PAGE OPS TAB ── */
            <div className="flex-1 flex flex-col overflow-hidden">
              {/* Page header strip */}
              <div className="h-10 shrink-0 flex items-center gap-3 px-5 border-b border-slate-800 bg-slate-900/50">
                {(() => {
                  const nav = SIDEBAR_NAV.find(n => n.id === activePage)
                  return nav ? (
                    <>
                      <nav.Icon className="w-4 h-4 text-emerald-500" />
                      <span className="text-sm font-semibold text-slate-200 tracking-wide">{nav.label}</span>
                    </>
                  ) : null
                })()}
                <div className="flex-1" />
                <div className="flex items-center gap-3 text-[10px] font-mono text-slate-500">
                  <span>POSITIONS: <span className="text-slate-300">{openPositionCount}</span></span>
                  <span>ORDERS: <span className="text-slate-300">{orders.length}</span></span>
                  {riskStatus && (
                    <span>DAILY P&L:
                      <span className={riskStatus.daily_pnl >= 0 ? ' text-emerald-400' : ' text-rose-400'}>
                        {' '}{riskStatus.daily_pnl >= 0 ? '+' : ''}₹{Math.abs(riskStatus.daily_pnl).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                      </span>
                    </span>
                  )}
                </div>
              </div>
              <div className="flex-1 overflow-hidden">
                {PageComponent && React.createElement(PageComponent)}
              </div>
            </div>

          )}
        </div>
      </div>

      {/* FOOTER */}
      <footer className="h-8 bg-slate-950 border-t border-slate-900 flex justify-between items-center px-4 text-[10px] font-mono text-slate-500 shrink-0">
        <div className="flex gap-4">
          <span className="flex items-center gap-1"><Database className="w-3 h-3" /> AlgoTrader Pro v4</span>
          <span className="flex items-center gap-1">{health?.version || '—'}</span>
        </div>
        <div className="flex items-center gap-4">
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
