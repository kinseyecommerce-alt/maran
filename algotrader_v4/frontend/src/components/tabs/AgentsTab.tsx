import React, { useEffect, useState } from 'react'
import { useStore } from '../../store'
import { api } from '../../api/client'
import type { SegmentState } from '../../types'
import {
  listedStrategies, metaFor, strategyView, segmentBadge, inr, signedInr,
  useAgentControls, SEGMENT_ORDER, cardNumbers, segmentNumbers,
} from '../Agents/shared'

function fmtLastSignal(s: unknown): string {
  if (!s) return '—'
  if (typeof s === 'string') return s || '—'
  if (typeof s === 'object' && s !== null) {
    const o = s as Record<string, unknown>
    if (Object.keys(o).length === 0) return '—'
    const action = String(o.action || o.signal || '')
    const symbol = String(o.symbol || '')
    const skipped = o.skipped ? ` (skipped: ${o.skipped})` : ''
    return action ? `${action}${symbol ? ' ' + symbol : ''}${skipped}` : JSON.stringify(o).slice(0, 30)
  }
  return String(s)
}

/** Per-segment PAPER/LIVE gate. LIVE needs typed SEND (server enforces it too). */
function SegmentModeControl({ s }: { s: SegmentState }) {
  const ctl = useAgentControls()
  const { health } = useStore()
  const [text, setText] = useState('')
  if (s.mode === 'LIVE') {
    return (
      <button data-testid={`segment-mode-${s.code}`} onClick={() => ctl.mode(s.code, 'PAPER')}
        className="text-[10px] px-2 py-1 rounded border border-rose-700 text-rose-300">LIVE · back to PAPER</button>
    )
  }
  if (!s.live_supported) {
    return <span data-testid={`segment-mode-${s.code}`} className="text-[10px] font-mono text-amber-400" title={s.live_stub_reason || ''}>PAPER · LIVE stubbed</span>
  }
  const globalLive = health?.mode === 'LIVE'
  return (
    <span className="flex items-center gap-1" data-testid={`segment-mode-${s.code}`}>
      <span className="text-[10px] font-mono text-amber-400">PAPER</span>
      <input value={text} onChange={e => setText(e.target.value)} disabled={!globalLive}
        placeholder={globalLive ? 'type SEND' : 'global PAPER'} maxLength={8}
        className="w-20 text-[10px] bg-slate-900 border border-slate-700 rounded px-1 py-0.5 disabled:opacity-40" />
      <button disabled={!globalLive || text.trim() !== 'SEND'} onClick={() => { ctl.mode(s.code, 'LIVE', text); setText('') }}
        title={globalLive ? 'Arm this segment for LIVE (real orders)' : 'Global mode is PAPER — segments cannot be armed'}
        className="text-[10px] px-2 py-0.5 rounded border border-rose-800 text-rose-300 disabled:opacity-30 disabled:cursor-not-allowed">Arm LIVE</button>
    </span>
  )
}

export default function AgentsTab() {
  const { agents, setAgents, engine } = useStore()
  const snap = useStore(s => s.book)
  const ctl = useAgentControls()
  const [adaptive, setAdaptive] = useState<any>(null)

  useEffect(() => {
    // Stats only (signals, win-rate). Running/paused/enabled state is NOT read
    // from here — it comes from store.engine (same as the dashboard panel).
    const fetch = () => {
      api.agents().then(r => setAgents(r.data)).catch(() => {})
      api.adaptiveStatus().then(r => setAdaptive(r.data)).catch(() => {})
    }
    fetch()
    const t = setInterval(fetch, 5000)
    return () => clearInterval(t)
  }, [])

  const segs = [...(engine?.segments || [])].sort(
    (a, b) => SEGMENT_ORDER.indexOf(a.code) - SEGMENT_ORDER.indexOf(b.code))

  if (!engine?.strategies) {
    return <div className="p-4 text-xs text-slate-500" data-testid="agents-tab-loading">Waiting for engine state…</div>
  }

  return (
    <div className="h-full overflow-y-auto p-3 space-y-4" data-testid="agents-tab">
      {segs.map(s => {
        const b = segmentBadge(s)
        const keys = listedStrategies(engine, s.code)
        return (
          <section key={s.code} data-testid={`agents-tab-segment-${s.code}`} data-state={s.state}
            className={`border rounded-xl p-3 ${s.killed ? 'border-rose-800/70 bg-rose-950/10' : 'border-slate-700/50 bg-slate-900/30'}`}>
            {/* Segment agent header */}
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mb-2">
              <span className="text-sm font-bold text-slate-100">{s.label}</span>
              <span className="text-[10px] font-mono text-slate-500">Kite {s.kite_exchange}</span>
              <span data-testid={`agents-tab-segment-state-${s.code}`} data-state={s.state} title={s.reason}
                className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${b.cls}`}>{b.text}</span>
              <span className="text-[10px] text-slate-400">{s.hours} · {s.open ? 'OPEN' : 'CLOSED'}{s.reason ? ` · ${s.reason}` : ''}</span>
              <span className={`text-[10px] font-mono px-1 rounded ${s.feed === 'REAL' ? 'text-emerald-400' : 'text-amber-300 border border-amber-500/40'}`}>FEED {s.feed}</span>
              <span className="ml-auto flex items-center gap-2">
                <SegmentModeControl s={s} />
                {s.killed ? (
                  <button data-testid={`segment-kill-${s.code}`} data-action="rearm" onClick={() => ctl.rearm(s.code)}
                    className="text-[10px] px-2 py-1 rounded bg-emerald-800 text-white">Re-arm</button>
                ) : (
                  <button data-testid={`segment-kill-${s.code}`} data-action="kill" onClick={() => ctl.kill(s.code)}
                    className="text-[10px] px-2 py-1 rounded border border-rose-700 text-rose-300 hover:bg-rose-900/40">Kill switch</button>
                )}
              </span>
            </div>
            <div className="grid grid-cols-5 gap-2 text-[10px] mb-3">
              <div><div className="text-slate-500 uppercase">Capital</div><div className="font-mono text-slate-200">{inr(s.capital_used)} / {inr(s.capital)}</div></div>
              <div><div className="text-slate-500 uppercase">P&L today</div>
                <div data-testid={`agents-tab-segment-pnl-${s.code}`} data-value={segmentNumbers(s, snap).pnl}
                  className={`font-mono ${segmentNumbers(s, snap).pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{signedInr(segmentNumbers(s, snap).pnl)}
                  <span className="text-slate-500"> (realised {signedInr(segmentNumbers(s, snap).realised)} · open {signedInr(segmentNumbers(s, snap).unrealised)})</span></div></div>
              <div><div className="text-slate-500 uppercase">Daily loss limit</div><div className="font-mono text-slate-200">{inr(s.limits.max_daily_loss)}</div></div>
              <div><div className="text-slate-500 uppercase">Positions</div><div className="font-mono text-slate-200">{segmentNumbers(s, snap).positions} / {s.limits.max_positions}</div></div>
              <div><div className="text-slate-500 uppercase">Entries today</div><div className="font-mono text-slate-200">{s.entries_today} / {s.limits.max_trades_per_day}</div></div>
            </div>

            {/* Strategy agents inside this segment */}
            <div className="grid grid-cols-2 xl:grid-cols-3 gap-2">
              {keys.map(key => {
                const st = engine.strategies![key]
                const nb = cardNumbers(st, snap, key)
                const v = strategyView(engine, key)
                const meta = metaFor(key, st)
                const agent = agents[key]
                return (
                  <div key={key} data-testid={`agents-tab-card-${key}`} data-state={v.state}
                    className={`bg-slate-800/60 border rounded-xl p-3 ${v.on ? 'border-emerald-800/60' : 'border-slate-700/50'}`}>
                    <div className="flex items-start justify-between mb-2">
                      <div>
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-bold text-slate-100">{meta.displayName}</span>
                          <span data-testid={`agents-tab-state-${key}`} data-state={v.state} title={v.reason}
                            className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${v.badgeCls}`}>{v.state === 'running' ? 'RUNNING' : v.badge}</span>
                        </div>
                        <div className="text-[11px] text-slate-500 mt-0.5">{meta.strategy}</div>
                        <div className="text-[10px] text-slate-500 mt-0.5" data-testid={`agents-tab-reason-${key}`}>{v.reason || '\u00a0'}</div>
                      </div>
                      {/* Toggle = the same on/off as the badge; flipping it pauses / resumes */}
                      <button role="switch" aria-checked={v.on} data-testid={`agents-tab-toggle-${key}`}
                        disabled={v.action === 'none'} onClick={() => ctl.act(key, v)} title={v.reason}
                        className={`relative w-9 h-5 rounded-full transition-colors shrink-0 mt-0.5 ${v.on ? 'bg-emerald-600' : 'bg-slate-700'} disabled:opacity-40 disabled:cursor-not-allowed`}>
                        <span className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${v.on ? 'left-4' : 'left-0.5'}`} />
                      </button>
                    </div>
                    <div className="grid grid-cols-3 gap-2 mb-2 py-1.5 border-y border-slate-700/50 text-[10px]">
                      <div><div className="text-slate-500 uppercase">Trades</div><div className="font-mono text-slate-200" data-testid={`agents-tab-trades-${key}`}>{nb.trades}</div></div>
                      <div><div className="text-slate-500 uppercase">P&L</div><div data-testid={`agents-tab-pnl-${key}`} data-value={nb.pnl} title={`realised ${signedInr(nb.realised)} (its exit orders) · open ${signedInr(nb.unrealised)} (${nb.open} pos)`} className={`font-mono ${nb.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{signedInr(nb.pnl)}</div></div>
                      <div><div className="text-slate-500 uppercase">Last signal</div><div className="font-mono text-slate-400 truncate">{fmtLastSignal(agent?.last_signal)}</div></div>
                    </div>
                    {v.action === 'pause' ? (
                      <button data-testid={`agents-tab-btn-${key}`} data-action="pause" onClick={() => ctl.pause(key)}
                        className="w-full text-xs py-1.5 rounded-lg border border-slate-600 text-slate-300 hover:bg-slate-700">Pause</button>
                    ) : (
                      <button data-testid={`agents-tab-btn-${key}`} data-action={v.action} disabled={v.action === 'none'}
                        onClick={() => ctl.resume(key)} title={v.action === 'none' ? v.reason : ''}
                        className="w-full text-xs py-1.5 rounded-lg bg-emerald-700 hover:bg-emerald-600 text-white font-semibold disabled:opacity-40 disabled:cursor-not-allowed">Resume</button>
                    )}
                  </div>
                )
              })}
            </div>

            {/* Universe (simulated instruments are labelled as such) */}
            {s.universe?.instruments && (
              <div className="mt-2 flex flex-wrap gap-1.5" data-testid={`segment-universe-${s.code}`}>
                {s.universe.instruments.map(i => (
                  <span key={i.symbol} className="text-[10px] font-mono bg-slate-900 border border-slate-800 rounded px-1.5 py-0.5"
                    title={i.synthetic_seed ? 'SIMULATED from a synthetic start level — not a market price'
                      : (i.ref_close != null ? `SIMULATED · real ${i.ref_source} close ${i.ref_close_date}: ${i.ref_close}` : 'SIMULATED')}>
                    {i.symbol} <span className="text-amber-300 italic">{i.price != null ? i.price.toLocaleString('en-IN', { maximumFractionDigits: 2 }) : '—'}</span>
                    <span className="text-amber-500"> SIM</span>
                  </span>
                ))}
                <span className="text-[10px] text-amber-400/80 italic">
                  SIMULATED prices{s.code === 'BSE_EQ' ? ' (start at the real NSE close)' : ' (synthetic levels, not market prices)'}
                </span>
              </div>
            )}
            {s.universe?.symbols && (
              <div className="mt-2 text-[10px] text-slate-500" data-testid={`segment-universe-${s.code}`}>
                Universe: {s.universe.count} symbols{s.universe.count ? ` — ${s.universe.symbols.slice(0, 12).join(', ')}${s.universe.count > 12 ? '…' : ''}` : ''}
              </div>
            )}
          </section>
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
