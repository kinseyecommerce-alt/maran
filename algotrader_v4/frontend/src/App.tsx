import React, { useEffect, useState, useRef } from 'react'
import {
  Activity, Terminal, Cpu, WifiOff, ShieldCheck,
  TrendingUp, TrendingDown, BarChart3,
  Zap, Play, Square, Database, Crosshair,
  LayoutDashboard, ClipboardList, Target, Scale, History, Brain,
} from 'lucide-react'
import Header from './components/Header'
import IndexStrip from './components/IndexStrip'
import MarketOverview from './components/MarketOverview'
import { EngineLabel } from './components/EngineStatus'
import AgentsPanel from './components/Agents/AgentsPanel'
import { listedStrategies, strategyView } from './components/Agents/shared'
import PositionsTab from './components/tabs/PositionsTab'
import OrdersTab from './components/tabs/OrdersTab'
import BracketsTab from './components/tabs/BracketsTab'
import RiskTab from './components/tabs/RiskTab'
import AgentsTab from './components/tabs/AgentsTab'
import SebiTab from './components/tabs/SebiTab'
import TradeHistoryTab from './components/tabs/TradeHistoryTab'
import ClaudeGateTab from './components/tabs/ClaudeGateTab'
import { connectWS } from './ws/websocket'
import { useStore } from './store'
import { api } from './api/client'
import type { TabId } from './types'

type PageId = TabId | 'dashboard'

const TAB_COMPONENTS: Record<string, React.ComponentType> = {
  positions: PositionsTab,
  orders:    OrdersTab,
  brackets:  BracketsTab,
  risk:      RiskTab,
  agents:    AgentsTab,
  sebi:      SebiTab,
  history:   TradeHistoryTab,
  gate:      ClaudeGateTab,
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
  { id: 'gate',      label: 'Claude Gate',   Icon: Brain           },
]

function Toasts() {
  const { toasts, removeToast } = useStore()
  const colors = { buy: 'bg-emerald-800 border-emerald-700', sell: 'bg-rose-900 border-rose-800', info: 'bg-slate-800 border-slate-700', error: 'bg-rose-950 border-rose-800' }
  return (
    <div className="fixed bottom-4 right-4 flex flex-col gap-1.5 z-50">
      {toasts.map(t => (
        <div
          key={t.id}
          onClick={() => removeToast(t.id)}
          role="status"
          className={`flex items-center gap-2 px-3 py-2 rounded border text-slate-100 text-xs font-mono shadow-lg cursor-pointer ${colors[t.type]}`}
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
    <div className="fixed inset-0 bg-[#0a0c10] flex items-center justify-center z-50">
      <div className="w-full max-w-sm px-4">
        <div className="bg-[#11141a] border border-[#1e2430] rounded-lg p-8 shadow-2xl">
          <div className="flex items-center gap-3 mb-8">
            <div className="w-8 h-8 rounded border border-emerald-900/50 bg-emerald-950/40 flex items-center justify-center">
              <span className="text-emerald-400 font-bold text-sm font-mono">A</span>
            </div>
            <div>
              <div className="text-sm font-bold text-slate-100 tracking-[0.2em] font-mono">ALGOPRO</div>
              <div className="text-[10px] text-slate-600 tracking-wider uppercase">Trading Desk v4</div>
            </div>
          </div>

          <form onSubmit={handleLogin} className="space-y-4">
            <div>
              <label className="block text-[11px] font-medium text-slate-500 uppercase tracking-widest mb-1.5">Username</label>
              <input
                type="text" autoComplete="username" autoFocus
                value={username} onChange={e => setUsername(e.target.value)}
                className="w-full px-3 py-2 bg-[#0a0c10] border border-[#1e2430] rounded text-slate-200 text-sm font-mono
                           focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-600/50 focus:border-emerald-800 transition-all placeholder-slate-600"
                placeholder="admin"
              />
            </div>
            <div>
              <label className="block text-[11px] font-medium text-slate-500 uppercase tracking-widest mb-1.5">Password</label>
              <input
                type="password" autoComplete="current-password"
                value={password} onChange={e => setPassword(e.target.value)}
                className="w-full px-3 py-2 bg-[#0a0c10] border border-[#1e2430] rounded text-slate-200 text-sm font-mono
                           focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-600/50 focus:border-emerald-800 transition-all placeholder-slate-600"
                placeholder="••••••••"
              />
            </div>
            {error && (
              <div className="text-xs text-rose-400 bg-rose-950/50 border border-rose-900 rounded-lg px-3 py-2">{error}</div>
            )}
            <button type="submit" disabled={loading || !username}
              className="w-full py-2 rounded bg-emerald-800 hover:bg-emerald-700 text-white text-sm font-semibold border border-emerald-700/40
                         transition-colors disabled:opacity-40 disabled:cursor-not-allowed mt-2 focus-ring">
              {loading ? 'Authenticating…' : 'Sign In'}
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
    riskStatus,
    addToast, token, clearToken, engine,
  } = useStore()
  // ONE portfolio snapshot (GET /portfolio/book): header counters, nav badges,
  // Today P&L split, Positions, Orders and agent-card P&L all read it.
  const snap          = useStore(s => s.book)
  const bookError     = useStore(s => s.bookError)
  const bookAt        = useStore(s => s.bookAt)
  const refreshBook   = useStore(s => s.refreshBook)
  const sessionExpired = useStore(s => s.sessionExpired)

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
    if (sessionExpired) { clearToken(); setIsAuthed(false) }
  }, [sessionExpired])

  // Snapshot poller: every 2 s, and immediately when the engine push says the
  // book changed (new order / position). Single-flight inside refreshBook.
  const bookRev = engine?.book?.rev
  useEffect(() => {
    if (!isAuthed) return
    refreshBook()
    const t = setInterval(refreshBook, 2000)
    return () => clearInterval(t)
  }, [isAuthed])
  useEffect(() => { if (isAuthed && bookRev) refreshBook() }, [bookRev])

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

  // Sidebar counters use the same server records as every agent card.
  const listedKeys  = listedStrategies(engine)
  const activeCount = listedKeys.filter(k => strategyView(engine, k).on).length
  const pausedCount = listedKeys.length - activeCount

  // Today P&L, POSITIONS and ORDERS: the one /portfolio/book snapshot (the
  // same rows the Positions and Orders tabs list). Until it loads: "—".
  const book        = snap?.summary
  const dailyPnl    = book?.total.pnl ?? 0
  const pnlPositive = dailyPnl >= 0
  const pnlDisplay  = `${pnlPositive ? '+' : '-'}₹${Math.abs(dailyPnl).toLocaleString('en-IN', { maximumFractionDigits: 2 })}`
  const isHalted    = riskStatus?.is_halted


  const logs = agentActivity.length > 0 ? agentActivity : [
    { time: '--:--:--', agent: 'SYSTEM', action: engine?.state === 'running'
        ? 'Engine running — waiting for the first agent signal…'
        : engine?.state === 'starting' ? `Engine starting — ${engine.label}` : 'No activity yet — start bot to see live signals.',
      type: 'system' as const, cat: 'SYS' as const },
  ]

  const openPositionCount = snap ? snap.positions.length : null
  const orderCount        = snap ? snap.orders.length : null
  const fmtInr = (v: number, d = 0) => `${v >= 0 ? '+' : '-'}₹${Math.abs(v).toLocaleString('en-IN', { maximumFractionDigits: d })}`
  const bookAge = bookAt ? Math.round((Date.now() - bookAt) / 1000) : null
  const bookStale = !!bookError || (bookAge !== null && bookAge > 10)

  const handleLogout = async () => {
    try { await api.authLogout() } catch {}
    clearToken()
    setIsAuthed(false)
  }

  const navBadge = (id: PageId): number | undefined => {
    if (id === 'positions') return openPositionCount ? openPositionCount : undefined
    if (id === 'orders')    return orderCount ? orderCount : undefined
    return undefined
  }

  // ── Auth gates ──────────────────────────────────────────────────────────────
  if (isAuthed === null) {
    return (
      <div className="fixed inset-0 bg-[#0a0c10] flex items-center justify-center">
        <div className="text-emerald-500/80 font-mono text-[10px] animate-pulse tracking-[0.2em]">AUTHENTICATING…</div>
      </div>
    )
  }
  if (isAuthed === false) {
    return <LoginScreen onSuccess={() => { useStore.getState().setSessionExpired(false); setIsAuthed(true) }} />
  }

  const PageComponent = activePage !== 'dashboard' ? TAB_COMPONENTS[activePage] : null

  return (
    <div className="flex flex-col h-screen bg-[#0a0c10] text-slate-300 overflow-hidden selection:bg-emerald-950 selection:text-emerald-100">

      {/* HEADER */}
      <Header />

      {/* LIVE INDEX LEVELS */}
      <IndexStrip />

      {/* HALTED BANNER */}
      {isHalted && (
        <div className="bg-rose-950 text-rose-200 text-[11px] text-center py-1.5 font-mono shrink-0 border-b border-rose-900">
          TRADING HALTED — daily loss limit reached. Open Risk or SEBI page to resume.
        </div>
      )}

      {/* MOOD LINE */}
      <div className={`h-px w-full shrink-0 ${pnlPositive ? 'bg-emerald-800' : 'bg-rose-900'}`} />

      {/* BODY: SIDEBAR + CONTENT */}
      <div className="flex flex-1 overflow-hidden">

        {/* ── LEFT SIDEBAR ─────────────────────────────────────────────────── */}
        <aside className="w-48 shrink-0 flex flex-col bg-[#11141a] border-r border-[#1e2430]">

          {/* P&L block */}
          <div className="px-3 py-3 border-b border-[#1e2430] shrink-0">
            <div className="text-[9px] uppercase tracking-[0.14em] text-slate-600 mb-1">Today P&L</div>
            <div data-testid="today-pnl" data-value={dailyPnl}
              title={book ? `Realised ${book.total.realised.toFixed(0)} · open ${book.total.unrealised.toFixed(0)} — all segments` : ''}
              className={`font-mono font-bold text-2xl leading-none tabular-nums ${pnlPositive ? 'text-emerald-400' : 'text-rose-400'}`}>
              {book ? pnlDisplay : '—'}
            </div>
            <div className="text-[10px] mt-1 text-slate-600 font-mono">all segments</div>
            {book && (
              <div className="mt-2 text-[10px] font-mono leading-relaxed space-y-0.5" data-testid="pnl-split">
                <div className="flex justify-between" data-testid="pnl-realised" data-value={book.total.realised}
                  title="Σ realised P&L of today's exit orders (Orders tab, Realised column)">
                  <span className="text-slate-600">Realised · {book.total.closed ?? 0}</span>
                  <span className={`tabular-nums ${book.total.realised >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{fmtInr(book.total.realised)}</span>
                </div>
                <div className="flex justify-between" data-testid="pnl-open" data-value={book.total.unrealised}
                  title="Σ P&L of the open positions (Positions tab)">
                  <span className="text-slate-600">Open · {book.total.positions}</span>
                  <span className={`tabular-nums ${book.total.unrealised >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{fmtInr(book.total.unrealised)}</span>
                </div>
              </div>
            )}
            {(bookError || bookStale) && (
              <div className="mt-1 text-[9px] font-mono text-amber-400" data-testid="book-stale"
                title={bookError || ''}>{bookError ? `book: ${bookError}` : `book ${bookAge}s old`}</div>
            )}
            {book && (
              <div className="mt-2 space-y-px border-t border-[#1e2430] pt-2" data-testid="pnl-breakdown">
                {Object.entries(book.by_segment).map(([code, s]) => (
                  <div key={code} data-testid={`pnl-seg-${code}`} data-value={s.pnl}
                    className="flex justify-between text-[9px] font-mono text-slate-600"
                    title={`${s.label}: realised ${s.realised.toFixed(0)} · open ${s.unrealised.toFixed(0)} · ${s.positions} pos · ${s.orders} orders${s.simulated ? ' · SIMULATED prices' : ''}`}>
                    <span>{code}{s.positions ? ` · ${s.positions}p` : ''}</span>
                    <span className={`tabular-nums ${s.pnl > 0 ? 'text-emerald-400' : s.pnl < 0 ? 'text-rose-400' : 'text-slate-500'}`}>
                      {s.pnl >= 0 ? '+' : '-'}₹{Math.abs(s.pnl).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
                    </span>
                  </div>
                ))}
              </div>
            )}
            <div className="flex gap-3 mt-2 text-[10px] font-mono text-slate-600">
              <span>ACTIVE <span className="text-emerald-400">{activeCount}</span></span>
              <span>PAUSED <span className="text-amber-500">{pausedCount}</span></span>
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
                  className={`w-full flex items-center gap-2.5 px-2.5 py-1.5 rounded text-xs font-medium transition-colors focus-ring ${
                    active
                      ? 'bg-slate-800 text-slate-100 border border-slate-700'
                      : 'text-slate-500 hover:text-slate-200 hover:bg-slate-800/40 border border-transparent'
                  }`}
                >
                  <item.Icon className={`w-3.5 h-3.5 shrink-0 ${active ? 'text-slate-300' : 'text-slate-600'}`} />
                  <span className="flex-1 text-left">{item.label}</span>
                  {badge !== undefined && (
                    <span className="bg-slate-700 text-slate-200 rounded text-[9px] min-w-[1rem] h-4 px-1 flex items-center justify-center shrink-0 font-mono">
                      {badge > 99 ? '99+' : badge}
                    </span>
                  )}
                </button>
              )
            })}
          </nav>

          {/* Status footer */}
          <div className="px-3 py-3 border-t border-[#1e2430] shrink-0 space-y-2">
            <div className="flex items-center gap-1.5 text-[10px] font-mono">
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${wsConnected ? 'bg-emerald-500' : 'bg-slate-600'}`} />
              <span className={wsConnected ? 'text-emerald-400' : 'text-slate-600'}>{wsConnected ? 'WS' : 'OFFLINE'}</span>
              <span className="text-slate-700">·</span>
              <span className="text-slate-500 tabular-nums">{liveTime} IST</span>
            </div>
            {health?.mode && (
              <div className={`text-[10px] font-mono font-bold px-1.5 py-0.5 rounded border w-fit ${health.mode === 'LIVE' ? 'text-rose-400 bg-rose-950/40 border-rose-900/50' : 'text-amber-400 bg-amber-950/30 border-amber-900/40'}`}>
                {health.mode}
              </div>
            )}
            <button onClick={handleLogout}
              className="text-[10px] text-slate-600 hover:text-rose-400 transition-colors font-mono focus-ring rounded">
              Logout
            </button>
          </div>
        </aside>

        {/* ── CONTENT AREA ──────────────────────────────────────────────────── */}
        <div className="flex-1 min-w-0 overflow-hidden flex flex-col">

          {activePage === 'dashboard' ? (

            /* ── DASHBOARD VIEW ── */
            <div className="flex flex-1 overflow-hidden">

              {/* LEFT: AGENTS + ACTIVITY STREAM */}
              <div className="flex-1 flex flex-col min-w-0 border-r border-[#1e2430] bg-[#0a0c10] overflow-hidden">

                {/* AGENTS: segment agents + their strategies */}
                <AgentsPanel />

                {/* ACTIVITY STREAM */}
                <div className="flex-1 flex flex-col px-3 py-2 border-t border-[#1e2430] bg-[#0a0c10] overflow-hidden min-h-0">
                  <div className="flex justify-between items-center mb-2 shrink-0">
                    <h2 className="text-[10px] font-semibold tracking-[0.14em] uppercase text-slate-500 flex items-center gap-2">
                      <Terminal className="w-3.5 h-3.5 text-slate-500" />
                      Decision Stream
                    </h2>
                    <span className="text-[10px] text-slate-600 font-mono">
                      {agentActivity.length > 0 ? `${tickSince}s ago` : 'waiting…'}
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
                        <div key={i} className="flex items-stretch hover:bg-[#161a22]/60 group transition-colors overflow-hidden">
                          <div className={`w-0.5 shrink-0 ${catColor}`} />
                          <div className="flex flex-1 gap-3 py-1 px-2.5 min-w-0">
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

              {/* RIGHT: MARKET OVERVIEW — real index feed + honestly-labelled stock prices */}
              <MarketOverview />
            </div>

          ) : (

            /* ── FULL-PAGE OPS TAB ── */
            <div className="flex-1 flex flex-col overflow-hidden">
              {/* Page header strip */}
              <div className="h-9 shrink-0 flex items-center gap-3 px-4 border-b border-[#1e2430] bg-[#11141a]">
                {(() => {
                  const nav = SIDEBAR_NAV.find(n => n.id === activePage)
                  return nav ? (
                    <>
                      <nav.Icon className="w-3.5 h-3.5 text-slate-400" />
                      <span className="text-xs font-semibold text-slate-200 tracking-wide uppercase">{nav.label}</span>
                    </>
                  ) : null
                })()}
                <div className="flex-1" />
                <div className="flex items-center gap-3 text-[10px] font-mono text-slate-500">
                  <span data-testid="hdr-positions" data-value={openPositionCount ?? ''}>POSITIONS: <span className="text-slate-300">{openPositionCount ?? '—'}</span></span>
                  <span data-testid="hdr-orders" data-value={orderCount ?? ''}>ORDERS: <span className="text-slate-300">{orderCount ?? '—'}</span></span>
                  {book && (
                    <span data-testid="hdr-pnl" data-value={dailyPnl}>DAILY P&L:
                      <span className={dailyPnl >= 0 ? ' text-emerald-400' : ' text-rose-400'}>
                        {' '}{dailyPnl >= 0 ? '+' : '-'}₹{Math.abs(dailyPnl).toLocaleString('en-IN', { maximumFractionDigits: 0 })}
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
      <footer className="h-7 bg-[#0a0c10] border-t border-[#1e2430] flex justify-between items-center px-4 text-[10px] font-mono text-slate-600 shrink-0">
        <div className="flex gap-4">
          <span className="flex items-center gap-1"><Database className="w-3 h-3" /> AlgoTrader Pro v4</span>
          <span className="flex items-center gap-1">{health?.version || '—'}</span>
        </div>
        <div className="flex items-center gap-4">
          <EngineLabel testId="engine-footer" />
          <span>FEED: {engine?.tick_feed || health?.tick_engine || '—'}</span>
          <span>TICKER: {health?.ticker_source || '—'}</span>
          <span className={wsConnected ? 'text-emerald-500' : 'text-slate-600'}>
            {wsConnected ? '● LIVE' : '○ OFFLINE'}
          </span>
        </div>
      </footer>

      <Toasts />

      
    </div>
  )
}
