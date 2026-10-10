import { useStore } from '../../store'
import type { EngineStatus } from '../../types'

/** Display text + colour for the engine state. Every engine/bot indicator
 *  (header button, agents panel, footer, agent cards) uses this, fed by the
 *  single store.engine object — so they can never disagree. */
export function engineView(e: EngineStatus | null): { text: string; cls: string; state: string } {
  if (!e) return { text: 'CONNECTING…', cls: 'text-slate-500', state: 'unknown' }
  switch (e.state) {
    case 'starting': return { text: `STARTING · ${e.label.replace('…', '').toUpperCase()}…`, cls: 'text-amber-400 animate-pulse', state: e.state }
    case 'running':  return { text: 'RUNNING', cls: 'text-emerald-400', state: e.state }
    case 'error':    return { text: 'START FAILED', cls: 'text-rose-400', state: e.state }
    default:         return { text: 'STOPPED', cls: 'text-slate-400', state: e.state }
  }
}

/** Is an agent ON according to the engine snapshot? (falls back to the agent's own flag) */
export function agentOn(e: EngineStatus | null, name: string, fallback?: boolean): boolean {
  if (e?.strategies && name in e.strategies) return e.strategies[name].on
  if (e && name in e.agents) return e.agents[name]
  return !!fallback
}

export function EngineLabel({ testId, prefix = 'ENGINE:' }: { testId: string; prefix?: string }) {
  const engine = useStore(s => s.engine)
  const v = engineView(engine)
  return (
    <span data-testid={testId} data-engine-state={v.state} title={engine?.error || engine?.label || ''}>
      {prefix} <span className={v.cls}>{v.text}</span>
    </span>
  )
}
