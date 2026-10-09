/**
 * Learning — what the self-improvement loop changed, why, and the effect.
 * PAPER only. The go-live readiness scorecard is display-only: READY never
 * arms LIVE (jag types SEND).
 */
import React, { useCallback, useEffect, useState } from 'react'
import { GraduationCap, ShieldCheck, CheckCircle2, XCircle } from 'lucide-react'
import { api } from '../../api/client'

const inr = (n: any) => (n === null || n === undefined || n === '') ? '—'
  : (Number(n) < 0 ? '−₹' : '₹') + Math.abs(Math.round(Number(n))).toLocaleString('en-IN')
const pnlCls = (n: any) => Number(n) > 0 ? 'text-emerald-400' : Number(n) < 0 ? 'text-rose-400' : 'text-slate-400'
const SEGMENTS = ['NSE_EQ', 'NSE_FO', 'BSE_EQ', 'MCX', 'CDS']

function Card({ title, children, right }: { title: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-900/60 p-3">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[11px] uppercase tracking-wider text-slate-400 font-semibold">{title}</div>
        {right}
      </div>
      {children}
    </div>
  )
}

export default function LearningTab() {
  const [r, setR] = useState<any>(null)
  const [err, setErr] = useState('')
  const [sc, setSc] = useState<any>(null)

  const refresh = useCallback(async () => {
    try {
      const res = await api.learningReport()
      setR(res.data); setErr('')
      try { const s2 = await api.scalperStatus(); setSc(s2.data) } catch { /* scalper optional */ }
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e.message || 'refresh failed')
    }
  }, [])
  useEffect(() => { refresh(); const t = setInterval(refresh, 15000); return () => clearInterval(t) }, [refresh])

  if (!r) return <div className="p-4 text-xs text-slate-400">{err || 'Loading learning report…'}</div>
  const today: any[] = r.summary?.today_by_segment || []
  const lc = r.last_cycle || {}
  const retunes: any[] = lc.retunes || []
  const lat = r.latency || {}

  return (
    <div className="p-3 space-y-3 text-xs text-slate-200">
      <div className="flex items-center gap-2">
        <GraduationCap className="w-4 h-4 text-violet-400" />
        <div className="font-semibold text-sm">Self-improvement</div>
        <span className="px-1.5 py-0.5 rounded bg-amber-900/60 border border-amber-700 text-amber-200 text-[10px]">{r.mode} ONLY</span>
        <span className="text-slate-500">journal {r.summary?.journal_trades ?? 0} trades · last cycle {lc.ts ? lc.ts.replace('T', ' ').slice(0, 16) + ' IST' : 'never'}
          {lc.elapsed_sec !== undefined && lc.elapsed_sec !== null ? ` (${lc.elapsed_sec}s)` : ''}</span>
      </div>

      <Card title="Go-live readiness (display only — READY never arms LIVE; jag types SEND)">
        <div className="grid grid-cols-5 gap-2">
          {SEGMENTS.map(code => {
            const s = r.readiness?.[code]
            if (!s) return null
            return (
              <div key={code} className={`rounded border p-2 ${s.ready ? 'border-emerald-700 bg-emerald-950/30' : 'border-slate-700 bg-slate-950/40'}`}>
                <div className="flex items-center justify-between">
                  <span className="font-semibold">{code}</span>
                  <span className={`text-[10px] font-bold ${s.ready ? 'text-emerald-400' : 'text-rose-300'}`}>{s.status}</span>
                </div>
                <div className="text-[10px] text-slate-500 mb-1">{s.passed}/{s.of} criteria</div>
                {s.criteria.map((c: any) => (
                  <div key={c.key} className="flex items-start gap-1 leading-tight py-0.5">
                    {c.pass ? <CheckCircle2 className="w-3 h-3 text-emerald-500 shrink-0 mt-px" /> : <XCircle className="w-3 h-3 text-rose-500 shrink-0 mt-px" />}
                    <span className="text-slate-300">{c.label}</span>
                    <span className="ml-auto text-slate-400 font-mono">{['net', 'dd'].includes(c.key) ? inr(c.value) : c.value}</span>
                  </div>
                ))}
              </div>
            )
          })}
        </div>
      </Card>

      <div className="grid grid-cols-2 gap-3">
        <Card title="Today's journal (after realistic costs)">
          <table className="w-full font-mono">
            <thead><tr className="text-slate-500 text-left"><th>Segment</th><th>Trades</th><th>Gross</th><th>Costs</th><th>Slippage</th><th>Net</th></tr></thead>
            <tbody>
              {today.length === 0 && <tr><td colSpan={6} className="text-slate-500 py-1">No closed paper trades journaled today.</td></tr>}
              {today.map((t: any) => (
                <tr key={t.segment} className="border-t border-slate-800">
                  <td>{t.segment}</td><td>{t.n}</td><td className={pnlCls(t.gross)}>{inr(t.gross)}</td>
                  <td className="text-amber-300">{inr(t.costs)}</td><td className="text-slate-400">{inr(t.slippage)}</td>
                  <td className={pnlCls(t.net)}>{inr(t.net)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="Guardrails" right={<ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />}>
          <ul className="list-disc pl-4 space-y-0.5 text-slate-300">
            {(r.guardrails || []).map((g: string) => <li key={g}>{g}</li>)}
          </ul>
          {Object.keys(lat).length > 0 && (
            <div className="mt-2 text-slate-400">Tick→decision latency: {Object.entries(lat).map(([k, v]: any) =>
              <span key={k} className="mr-3 font-mono">{k} p50 {v.p50_ms}ms · p95 {v.p95_ms}ms (n={v.n})</span>)}</div>
          )}
        </Card>
      </div>


      <Card title="Fast scalper — Kite WebSocket, per-tick decisions (PAPER)">
        {!sc ? <div className="text-slate-500">Scalper status unavailable.</div> : (
          <div className="grid grid-cols-4 gap-3">
            <div>
              <div className="text-slate-400">Feed</div>
              <div className={sc.feed?.connected ? 'text-emerald-400' : 'text-rose-400'}>{sc.feed?.connected ? 'WS connected' : 'WS down'} · {sc.feed?.subscribed ?? 0} tokens</div>
              <div className="text-slate-500">{(sc.feed?.ticks ?? 0).toLocaleString('en-IN')} ticks{sc.feed?.last_error ? ` · ${sc.feed.last_error}` : ''}</div>
            </div>
            <div>
              <div className="text-slate-400">Tick → decision latency</div>
              <div className="font-mono">{sc.latency ? `p50 ${sc.latency.p50_ms} ms · p95 ${sc.latency.p95_ms} ms` : '—'}</div>
              <div className="text-slate-500">{sc.latency ? `${sc.latency.n} ticks` : 'no ticks yet'}</div>
            </div>
            <div>
              <div className="text-slate-400">Activity</div>
              <div className="font-mono">{sc.stats?.signals ?? 0} signals · {sc.stats?.fills ?? 0} fills · {sc.stats?.exits ?? 0} exits</div>
              <div className="text-slate-500">skips: cost {sc.stats?.cost_skips ?? 0} · caps {sc.stats?.cap_skips ?? 0} · gates {sc.stats?.gate_skips ?? 0}</div>
            </div>
            <div>
              <div className="text-slate-400">Scalp P&L today (paper)</div>
              <div className={`font-mono ${pnlCls(sc.stats?.pnl)}`}>{inr(sc.stats?.pnl)}</div>
              <div className="text-slate-500">{sc.instruments} instruments · {Object.entries(sc.by_segment_instruments || {}).map(([k, v]: any) => `${k} ${v}`).join(' · ')}</div>
            </div>
          </div>
        )}
      </Card>

      <Card title="Strategies — stats after costs, active params, status">
        <table className="w-full font-mono">
          <thead><tr className="text-slate-500 text-left">
            <th>Strategy</th><th>Seg</th><th>n</th><th>Win%</th><th>Exp/trade</th><th>Net</th><th>Costs</th><th>PF</th><th>Max DD</th><th>Size×</th><th>Status</th><th>Version</th>
          </tr></thead>
          <tbody>
            {(r.strategies || []).map((s: any) => (
              <tr key={s.strategy} className="border-t border-slate-800">
                <td className="text-slate-100">{s.strategy}</td><td>{s.segment}</td><td>{s.stats.n}</td>
                <td>{s.stats.win_rate}</td><td className={pnlCls(s.stats.expectancy)}>{inr(s.stats.expectancy)}</td>
                <td className={pnlCls(s.stats.net)}>{inr(s.stats.net)}</td><td className="text-amber-300">{inr(s.stats.costs)}</td>
                <td>{s.stats.profit_factor}</td><td className="text-rose-300">{inr(s.stats.max_dd)}</td>
                <td>{Number(s.params?.size_factor ?? 1).toFixed(2)}</td>
                <td className={s.retired ? 'text-rose-400' : s.cooloff_until ? 'text-amber-300' : 'text-emerald-400'}
                    title={s.retired_reason || ''}>{s.retired ? 'RETIRED' : s.cooloff_until ? 'COOL-OFF' : 'active'}</td>
                <td className="text-slate-500 text-[10px]">{s.version}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="Changes — what changed, why, and the effect (versioned, auto-rollback)">
        <table className="w-full">
          <thead><tr className="text-slate-500 text-left"><th>Time (IST)</th><th>Strategy</th><th>Kind</th><th>Change</th><th>Why</th><th>Status / effect</th></tr></thead>
          <tbody>
            {(r.changes || []).length === 0 && <tr><td colSpan={6} className="text-slate-500 py-1">No parameter changes yet.</td></tr>}
            {(r.changes || []).map((v: any) => (
              <tr key={v.version_id} className="border-t border-slate-800 align-top">
                <td className="font-mono text-slate-400 whitespace-nowrap">{String(v.ts).replace('T', ' ').slice(5, 16)}</td>
                <td>{v.strategy}</td><td>{v.kind}</td>
                <td className="font-mono">{Object.entries(v.changed || {}).map(([k, ab]: any) => <div key={k}>{k}: {String(ab[0])} → {String(ab[1])}</div>)}</td>
                <td className="text-slate-300">{v.reason}</td>
                <td>{v.status}{v.effect?.live_trades ? ` · ${v.effect.live_trades} trades ${inr(v.effect.live_expectancy)}/trade` : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <div className="grid grid-cols-2 gap-3">
        <Card title="Last cycle — retunes (train → out-of-sample test)">
          <table className="w-full">
            <thead><tr className="text-slate-500 text-left"><th>Strategy</th><th>Current OOS</th><th>Candidate OOS</th><th>Result</th></tr></thead>
            <tbody>
              {retunes.length === 0 && <tr><td colSpan={4} className="text-slate-500 py-1">No retune run yet.</td></tr>}
              {retunes.map((x: any) => (
                <tr key={x.strategy} className="border-t border-slate-800 align-top">
                  <td>{x.strategy}</td>
                  <td className="font-mono">{x.current_test ? `${inr(x.current_test.expectancy)} ×${x.current_test.n}` : '—'}</td>
                  <td className="font-mono">{x.candidate_test ? `${inr(x.candidate_test.expectancy)} ×${x.candidate_test.n}` : '—'}</td>
                  <td className={x.accepted ? 'text-emerald-400' : 'text-slate-400'}>{x.accepted ? 'ACCEPTED' : 'kept'} — {x.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
        <Card title="Events — retire / promote / cool-off / rollback / lessons">
          <div className="max-h-64 overflow-auto">
            {(r.events || []).map((e: any, i: number) => (
              <div key={i} className="border-t border-slate-800 py-0.5">
                <span className="font-mono text-slate-500">{String(e.ts).slice(11, 16)}</span>{' '}
                <span className="text-violet-300">{e.kind}</span>{' '}
                <span className="text-slate-400">{e.segment} {e.strategy}</span>{' '}
                <span className="text-slate-300">{e.detail}</span>
              </div>
            ))}
            {(r.lessons?.avoid || []).map((l: any, i: number) => (
              <div key={'a' + i} className="border-t border-slate-800 py-0.5 text-rose-300">avoid {l.idea} in {l.regime} ({l.segment}): {inr(l.net)} over {l.n}</div>
            ))}
            {(r.lessons?.favor || []).map((l: any, i: number) => (
              <div key={'f' + i} className="border-t border-slate-800 py-0.5 text-emerald-300">favour {l.idea} in {l.regime} ({l.segment}): {inr(l.net)} over {l.n}</div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  )
}
