/**
 * Research — the master agent's nightly research loop for ALL agents (PAPER).
 *   proposed → backtested → rejected | probation (half size) → promoted | retired
 * Every step carries its plain-English reason. Also shows the last real-data
 * walk-forward backtest per agent (live decision code, Kite data, full costs)
 * and the allocator's evidence-based weights. Nothing here places orders.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { FlaskConical, Play, ShieldCheck } from 'lucide-react'
import { api } from '../../api/client'

const inr = (n: any) => (n === null || n === undefined || n === '') ? '—'
  : (Number(n) < 0 ? '−₹' : '₹') + Math.abs(Math.round(Number(n))).toLocaleString('en-IN')
const pnlCls = (n: any) => Number(n) > 0 ? 'text-emerald-400' : Number(n) < 0 ? 'text-rose-400' : 'text-slate-400'
const v = (x: any, suf = '') => (x === null || x === undefined) ? '—' : `${x}${suf}`

const STAGES = ['proposed', 'backtested', 'rejected', 'probation', 'promoted', 'retired'] as const
const STAGE_CLS: Record<string, string> = {
  proposed: 'bg-slate-700 text-slate-200', backtested: 'bg-sky-900 text-sky-200', rejected: 'bg-rose-900/70 text-rose-200',
  probation: 'bg-amber-900/70 text-amber-200', promoted: 'bg-emerald-900/70 text-emerald-200', retired: 'bg-zinc-800 text-zinc-300',
}

function Card({ title, children, right }: { title: React.ReactNode; children: React.ReactNode; right?: React.ReactNode }) {
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

export default function ResearchTab() {
  const [d, setD] = useState<any>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [filter, setFilter] = useState<string>('')
  const load = useCallback(async () => {
    try { const r = await api.researchPipeline(); setD(r.data); setErr('') }
    catch (e: any) { setErr(e?.response?.data?.detail || e?.message || 'failed') }
  }, [])
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t) }, [load])
  const run = async () => {
    setBusy(true)
    try { await api.researchRun({}); await load() } catch (e: any) { setErr(e?.response?.data?.detail || e?.message) }
    setBusy(false)
  }
  const rows = (d?.rows || []).filter((r: any) => !filter || r.status === filter)
  const bt = d?.backtest || {}
  const rep = d?.last_report || {}
  return (
    <div className="p-3 space-y-3 text-xs text-slate-300" data-testid="research-tab">
      <div className="flex items-center gap-2">
        <FlaskConical className="w-4 h-4 text-violet-400" />
        <div className="text-sm font-semibold text-slate-100">Research — all agents (PAPER)</div>
        <div className="ml-auto flex items-center gap-2">
          {d?.runner?.running && <span className="text-amber-300">running since {d.runner.started?.slice(11, 16)}…</span>}
          <button onClick={run} disabled={busy || d?.runner?.running}
            className="flex items-center gap-1 px-2 py-1 rounded bg-violet-700 hover:bg-violet-600 disabled:opacity-40 text-white">
            <Play className="w-3 h-3" /> Run research cycle
          </button>
        </div>
      </div>
      {err && <div className="text-rose-400">{err}</div>}
      {d?.runner?.last_error && <div className="text-rose-400">last run error: {d.runner.last_error}</div>}

      <div className="grid grid-cols-6 gap-2">
        {STAGES.map(s => (
          <button key={s} onClick={() => setFilter(filter === s ? '' : s)}
            className={`rounded p-2 text-left border ${filter === s ? 'border-violet-400' : 'border-slate-800'} ${STAGE_CLS[s]}`}>
            <div className="uppercase text-[10px] tracking-wider opacity-80">{s}</div>
            <div className="text-lg font-semibold">{d?.counts?.[s] ?? 0}</div>
          </button>
        ))}
      </div>

      <Card title={`Last cycle ${rep.cycle || '—'}`} right={<span className="text-slate-500">{rep.finished?.replace('T', ' ').slice(0, 16)}</span>}>
        <div className="flex flex-wrap gap-4">
          <span>proposed <b>{v(rep.proposed)}</b></span><span>passed OOS <b className="text-emerald-400">{v(rep.passed)}</b></span>
          <span>rejected <b className="text-rose-400">{v(rep.rejected)}</b></span>
          <span>to probation <b className="text-amber-300">{rep.probation?.length ?? '—'}</b></span>
          <span>took {v(rep.elapsed_s, 's')}</span>
        </div>
        {(rep.notes || []).map((n: string, i: number) => <div key={i} className="text-slate-500 mt-1">• {n}</div>)}
      </Card>

      <Card title={`Real-data walk-forward backtest per agent ${bt.n_days ? `(${bt.n_days} days ${bt.days?.[0]} → ${bt.days?.[1]})` : ''}`}>
        <table className="w-full font-mono">
          <thead><tr className="text-slate-500 text-left"><th>Agent</th><th>OOS trades</th><th>Win%</th><th>Exp/trade</th><th>Net</th><th>Costs</th><th>PF</th><th>Max DD</th><th>Alloc w</th><th>Verdict</th></tr></thead>
          <tbody>
            {Object.entries(bt.agents || {}).map(([a, x]: any) => (
              <tr key={a} className="border-t border-slate-800">
                <td className="text-slate-100">{a}</td><td>{x.stats?.trades}</td><td>{v(x.stats?.win_rate, '%')}</td>
                <td className={pnlCls(x.stats?.expectancy)}>{inr(x.stats?.expectancy)}</td>
                <td className={pnlCls(x.stats?.net)}>{inr(x.stats?.net)}</td>
                <td className="text-amber-300">{inr(x.stats?.costs)}</td><td>{v(x.stats?.profit_factor)}</td>
                <td className="text-rose-300">{inr(x.stats?.max_dd)}</td>
                <td>{v(d?.allocator?.[a]?.weight)}</td>
                <td className="font-sans text-slate-400">{x.verdict}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {(bt.notes || []).map((n: string, i: number) => <div key={i} className="text-slate-500 mt-1">• {n}</div>)}
      </Card>

      <Card title={`Pipeline ${filter ? `— ${filter}` : ''} (${rows.length})`}>
        <table className="w-full">
          <thead><tr className="text-slate-500 text-left"><th>Id</th><th>Agent</th><th>Hypothesis</th><th>Params</th><th>Status</th><th>OOS</th><th>Why / outcome</th></tr></thead>
          <tbody>
            {rows.map((r: any) => (
              <tr key={r.id} className="border-t border-slate-800 align-top">
                <td className="font-mono text-slate-500">{r.id}</td><td className="text-slate-100">{r.agent}</td>
                <td>{r.title}<div className="text-slate-500">{r.why}</div></td>
                <td className="font-mono">{Object.entries(r.params || {}).map(([k, x]) => `${k}=${x}`).join(', ')}</td>
                <td><span className={`px-1.5 py-0.5 rounded ${STAGE_CLS[r.status] || ''}`}>{r.status}</span></td>
                <td className="font-mono">{r.evidence?.oos ? <>
                  <span className={pnlCls(r.evidence.oos.net)}>{inr(r.evidence.oos.net)}</span>/{r.evidence.oos.trades}t
                  <div className="text-slate-500">base {inr(r.evidence.baseline?.net)}/{r.evidence.baseline?.trades}t</div></> : '—'}</td>
                <td className="text-slate-400">{r.outcome || '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title={<span className="flex items-center gap-1"><ShieldCheck className="w-3 h-3" /> Guardrails</span>}>
        {(d?.guardrails || []).map((g: string, i: number) => <div key={i}>• {g}</div>)}
      </Card>
    </div>
  )
}
