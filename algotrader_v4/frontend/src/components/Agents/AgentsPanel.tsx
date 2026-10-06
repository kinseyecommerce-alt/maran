import { Play, Square, Zap } from 'lucide-react'
import { useStore } from '../../store'
import { EngineLabel } from '../EngineStatus'
import { listedStrategies, metaFor, strategyView, segmentBadge, signedInr, inr, useAgentControls, SEGMENT_ORDER, cardNumbers, segmentNumbers } from './shared'

/** Dashboard "Autonomous agents" panel: one card per market-segment agent and
 *  one card per strategy inside it. Every state shown comes from store.engine. */
export default function AgentsPanel() {
  const { engine, agents, health } = useStore()
  const snap = useStore(s => s.book)
  const ctl = useAgentControls()
  const keys = listedStrategies(engine)
  const segs = [...(engine?.segments || [])].sort(
    (a, b) => SEGMENT_ORDER.indexOf(a.code) - SEGMENT_ORDER.indexOf(b.code))
  const active = keys.filter(k => strategyView(engine, k).on).length

  return (
    <div className="px-4 pt-4 pb-2 shrink-0">
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
          <Zap className="w-4 h-4 text-emerald-500" />
          AUTONOMOUS AGENTS
          <span className="text-slate-600 font-normal">({segs.length} segments · {keys.length} strategies)</span>
        </h2>
        <div className="text-xs font-mono text-slate-500 flex gap-4">
          <EngineLabel testId="engine-agents-panel" />
          {engine && <span data-testid="engine-agents-count">AGENTS: <span className="text-slate-300">{active}/{keys.length}</span></span>}
          {health?.mode && <span>MODE: <span className={health.mode === 'LIVE' ? 'text-rose-400' : 'text-amber-400'}>{health.mode}</span></span>}
        </div>
      </div>

      {/* Segment agents */}
      <div className="grid grid-cols-5 gap-2 mb-2" data-testid="segment-strip">
        {segs.map(s => {
          const b = segmentBadge(s)
          return (
            <div key={s.code} data-testid={`segment-card-${s.code}`} data-state={s.state}
              className={`rounded-lg border px-2.5 py-2 bg-slate-900/50 ${s.on ? 'border-emerald-700/40' : s.killed ? 'border-rose-800/60' : 'border-slate-800'}`}>
              <div className="flex items-center justify-between gap-1">
                <span className="text-[11px] font-bold text-white truncate">{s.label}</span>
                <span data-testid={`segment-state-${s.code}`} data-state={s.state}
                  className={`text-[9px] font-mono px-1.5 py-0.5 rounded ${b.cls}`} title={s.reason}>{b.text}</span>
              </div>
              <div className="flex items-center gap-1.5 mt-1 text-[9px] font-mono text-slate-500">
                <span>{s.kite_exchange}</span>·<span>{s.hours}</span>
              </div>
              <div className="flex items-center gap-1.5 mt-1 text-[9px] font-mono">
                <span className={s.effective_mode === 'LIVE' ? 'text-rose-400' : 'text-amber-400'}>{s.effective_mode}</span>
                <span data-testid={`segment-feed-${s.code}`}
                  className={s.feed === 'REAL' ? 'text-emerald-400' : 'text-amber-300 border border-amber-500/40 px-1 rounded'}>{s.feed}</span>
                <span data-testid={`segment-pnl-${s.code}`} data-value={segmentNumbers(s, snap).pnl}
                  className={segmentNumbers(s, snap).pnl >= 0 ? 'text-emerald-400 ml-auto' : 'text-rose-400 ml-auto'}>{signedInr(segmentNumbers(s, snap).pnl)}</span>
              </div>
              <div className="text-[9px] text-slate-500 mt-0.5 truncate" title={s.reason}>
                {s.strategies_running}/{s.strategies.length} on · cap {inr(s.capital_used)} / {inr(s.capital)}{s.reason ? ` · ${s.reason}` : ''}
              </div>
            </div>
          )
        })}
      </div>

      {/* Strategy agents (grouped by segment) */}
      <div className="flex gap-3 overflow-x-auto pb-2" style={{ scrollbarWidth: 'thin' }}>
        {keys.map(key => {
          const st   = engine!.strategies![key]
          const nb   = cardNumbers(st, snap, key)
          const meta = metaFor(key, st)
          const v    = strategyView(engine, key)
          const agent = agents[key]
          const ls = agent?.last_signal as unknown
          let sigDisplay = '—'
          if (typeof ls === 'string' && ls) sigDisplay = ls
          else if (ls && typeof ls === 'object') {
            const s = ls as Record<string, unknown>
            sigDisplay = [s.symbol, s.action].filter(Boolean).join(' ') || '—'
          }
          return (
            <div key={key} data-testid={`agent-card-${key}`} data-state={v.state}
              className={`rounded-lg bg-slate-900/50 flex flex-col border shrink-0 transition-colors overflow-hidden ${
                v.on ? 'border-emerald-700/40 border-l-2 border-l-emerald-500' : 'border-slate-800 opacity-80'}`}
              style={{ minWidth: '175px', width: 'calc(12.5% - 10px)' }}>
              <div className="p-3 flex-1">
                <div className="flex items-center justify-between mb-1.5">
                  <div className="flex items-center gap-1.5">
                    <span className="font-mono text-[9px] text-slate-600">{meta.id}</span>
                    <span className={`w-1.5 h-1.5 rounded-full ${v.on ? 'bg-emerald-500 shadow-[0_0_6px_#10b981]' : 'bg-amber-500'}`} />
                    <span className="font-mono text-[8px] text-slate-500 border border-slate-700 rounded px-1">{st.segment}</span>
                  </div>
                  <span data-testid={`agent-state-${key}`} data-state={v.state} title={v.reason}
                    className={`text-[10px] font-mono px-1.5 py-0.5 rounded ${v.badgeCls}`}>{v.badge}</span>
                </div>
                <div className="font-bold text-sm text-white leading-none">{meta.displayName}</div>
                <div className="text-[10px] text-slate-500 italic mt-0.5 truncate">{meta.strategy}</div>
                <div className="text-[9px] text-slate-500 mt-0.5 truncate" data-testid={`agent-reason-${key}`} title={v.reason}>{v.reason || '\u00a0'}</div>
                <div className="mt-2 flex gap-3 text-[10px]">
                  <div>
                    <div className="text-slate-600">Trades</div>
                    <div className="font-mono text-slate-300" data-testid={`agent-trades-${key}`}>{nb.trades}</div>
                  </div>
                  <div>
                    <div className="text-slate-600">P&L</div>
                    <div data-testid={`agent-pnl-${key}`} data-value={nb.pnl} title={`realised ${signedInr(nb.realised)} (its exit orders) · open ${signedInr(nb.unrealised)} (${nb.open} pos)`} className={`font-mono ${nb.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{signedInr(nb.pnl)}</div>
                  </div>
                </div>
                <div className="mt-2 bg-slate-950 rounded px-2 py-1.5 border border-slate-800/60">
                  <div className="text-[9px] text-slate-600 uppercase tracking-wider">Signal</div>
                  <div className="font-mono text-[10px] text-slate-400 truncate mt-0.5" title={sigDisplay}>{sigDisplay}</div>
                </div>
                <div className="mt-2 flex gap-1.5">
                  {v.action === 'pause' ? (
                    <button data-testid={`agent-btn-${key}`} data-action="pause" onClick={() => ctl.pause(key)}
                      className="flex-1 flex items-center justify-center gap-1 bg-slate-800 hover:bg-slate-700 text-slate-300 text-[10px] py-1.5 rounded transition-colors">
                      <Square className="w-2.5 h-2.5" /> Pause
                    </button>
                  ) : (
                    <button data-testid={`agent-btn-${key}`} data-action={v.action} disabled={v.action === 'none'}
                      onClick={() => ctl.resume(key)} title={v.action === 'none' ? v.reason : ''}
                      className="flex-1 flex items-center justify-center gap-1 bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 text-[10px] py-1.5 rounded transition-colors border border-emerald-500/30 disabled:opacity-40 disabled:cursor-not-allowed">
                      <Play className="w-2.5 h-2.5 fill-current" /> Resume
                    </button>
                  )}
                </div>
              </div>
              <div className={`h-[2px] w-full ${v.on ? 'bg-emerald-500 animate-pulse' : 'bg-amber-600/50'}`} />
            </div>
          )
        })}
      </div>
    </div>
  )
}
