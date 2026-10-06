import { Play, Square } from 'lucide-react'
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
    <div className="px-3 pt-3 pb-2 shrink-0 space-y-3">
      {/* Segment agents — full-width strip */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <h2 className="text-[10px] font-semibold tracking-[0.14em] uppercase text-slate-500">
            Segments
            <span className="text-slate-600 font-normal ml-2 normal-case tracking-normal">
              {segs.length} · {keys.length} strategies
            </span>
          </h2>
          <div className="text-[10px] font-mono text-slate-500 flex gap-3">
            <EngineLabel testId="engine-agents-panel" />
            {engine && <span data-testid="engine-agents-count">AGENTS <span className="text-slate-300">{active}/{keys.length}</span></span>}
            {health?.mode && (
              <span className={health.mode === 'LIVE' ? 'text-rose-400' : 'text-amber-400'}>{health.mode}</span>
            )}
          </div>
        </div>

        <div className="grid gap-1.5" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))' }} data-testid="segment-strip">
          {segs.map(s => {
            const b = segmentBadge(s)
            const nums = segmentNumbers(s, snap)
            return (
              <div key={s.code} data-testid={`segment-card-${s.code}`} data-state={s.state}
                className={`rounded border px-2.5 py-2 bg-[#11141a] ${
                  s.on ? 'border-emerald-900/50' : s.killed ? 'border-rose-900/50' : 'border-[#1e2430]'
                }`}>
                <div className="flex items-center justify-between gap-1 mb-1">
                  <span className="text-[11px] font-bold text-slate-100 truncate tracking-wide">{s.label}</span>
                  <span data-testid={`segment-state-${s.code}`} data-state={s.state}
                    className={`desk-chip ${b.cls}`} title={s.reason}>{b.text}</span>
                </div>
                <div className="flex items-center gap-1.5 text-[9px] font-mono text-slate-500">
                  <span>{s.kite_exchange}</span>
                  <span className="text-slate-700">·</span>
                  <span>{s.hours}</span>
                </div>
                <div className="flex items-center gap-1.5 mt-1.5 text-[10px] font-mono">
                  <span className={s.effective_mode === 'LIVE' ? 'text-rose-400' : 'text-amber-400'}>{s.effective_mode}</span>
                  <span data-testid={`segment-feed-${s.code}`}
                    className={s.feed === 'REAL'
                      ? 'text-emerald-500'
                      : 'text-amber-300 border border-amber-500/40 px-1 rounded font-bold'}
                    title={s.feed === 'SIMULATED' ? 'Paper simulator prices — not market quotes' : s.feed}>
                    {s.feed}
                  </span>
                  <span data-testid={`segment-pnl-${s.code}`} data-value={nums.pnl}
                    className={`ml-auto font-semibold tabular-nums ${nums.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    {signedInr(nums.pnl)}
                  </span>
                </div>
                <div className="text-[9px] text-slate-600 mt-1 truncate font-mono" title={s.reason}>
                  {s.strategies_running}/{s.strategies.length} on · {inr(s.capital_used)}/{inr(s.capital)}
                  {s.reason ? ` · ${s.reason}` : ''}
                </div>
              </div>
            )
          })}
        </div>
      </div>

      {/* Strategy agents */}
      <div>
        <h2 className="text-[10px] font-semibold tracking-[0.14em] uppercase text-slate-500 mb-2">Strategy Agents</h2>
        <div className="grid gap-2" style={{ gridTemplateColumns: 'repeat(auto-fill, minmax(168px, 1fr))' }} data-testid="strategy-agents-grid">
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
                className={`rounded border bg-[#11141a] flex flex-col min-w-0 overflow-hidden ${
                  v.on ? 'border-emerald-900/50 border-l-2 border-l-emerald-700' : 'border-[#1e2430] opacity-85'
                }`}>
                <div className="p-2.5 flex-1">
                  <div className="flex items-center justify-between mb-1">
                    <div className="flex items-center gap-1.5 min-w-0">
                      <span className="font-mono text-[9px] text-slate-600">{meta.id}</span>
                      <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${v.on ? 'bg-emerald-500' : 'bg-amber-600/70'}`} />
                      <span className="font-mono text-[8px] text-slate-500 border border-slate-800 rounded px-1 truncate">{st.segment}</span>
                    </div>
                    <span data-testid={`agent-state-${key}`} data-state={v.state} title={v.reason}
                      className={`desk-chip shrink-0 ${v.badgeCls}`}>{v.badge}</span>
                  </div>
                  <div className="font-bold text-[13px] text-slate-100 leading-none tracking-wide">{meta.displayName}</div>
                  <div className="text-[10px] text-slate-500 mt-0.5 truncate">{meta.strategy}</div>
                  <div className="text-[9px] text-slate-600 mt-0.5 truncate" data-testid={`agent-reason-${key}`} title={v.reason}>{v.reason || '\u00a0'}</div>
                  <div className="mt-2 flex gap-3 text-[10px]">
                    <div>
                      <div className="text-slate-600 uppercase tracking-wider text-[9px]">Trades</div>
                      <div className="font-mono text-slate-200 tabular-nums" data-testid={`agent-trades-${key}`}>{nb.trades}</div>
                    </div>
                    <div>
                      <div className="text-slate-600 uppercase tracking-wider text-[9px]">P&L</div>
                      <div data-testid={`agent-pnl-${key}`} data-value={nb.pnl}
                        title={`realised ${signedInr(nb.realised)} · open ${signedInr(nb.unrealised)} (${nb.open} pos)`}
                        className={`font-mono font-semibold tabular-nums ${nb.pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                        {signedInr(nb.pnl)}
                      </div>
                    </div>
                  </div>
                  <div className="mt-2 bg-[#0a0c10] rounded px-2 py-1 border border-[#1e2430]">
                    <div className="text-[9px] text-slate-600 uppercase tracking-wider">Signal</div>
                    <div className="font-mono text-[10px] text-slate-400 truncate mt-0.5" title={sigDisplay}>{sigDisplay}</div>
                  </div>
                  <div className="mt-2">
                    {v.action === 'pause' ? (
                      <button data-testid={`agent-btn-${key}`} data-action="pause" onClick={() => ctl.pause(key)}
                        className="w-full flex items-center justify-center gap-1 bg-slate-800/80 hover:bg-slate-700 text-slate-300 text-[10px] py-1.5 rounded border border-slate-700/50 transition-colors focus-ring">
                        <Square className="w-2.5 h-2.5" /> Pause
                      </button>
                    ) : (
                      <button data-testid={`agent-btn-${key}`} data-action={v.action} disabled={v.action === 'none'}
                        onClick={() => ctl.resume(key)} title={v.action === 'none' ? v.reason : ''}
                        className="w-full flex items-center justify-center gap-1 bg-emerald-950/40 hover:bg-emerald-900/50 text-emerald-400 text-[10px] py-1.5 rounded border border-emerald-900/40 transition-colors disabled:opacity-40 disabled:cursor-not-allowed focus-ring">
                        <Play className="w-2.5 h-2.5 fill-current" /> Resume
                      </button>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
