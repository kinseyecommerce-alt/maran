/**
 * Discovery — the continuous, overfit-resistant strategy-discovery loop (PAPER research).
 *   grammar → DISCOVERY window (oldest 70%) → dedupe → DSR + Hansen SPA (all trials counted)
 *   → finalists tested ONCE on the SEALED HOLDOUT (newest 30%) → registry (PAPER probation, half size)
 * Shows the seal, cumulative trial count, runs, finalists and the top near-misses with why
 * each failed. Nothing here places orders; LIVE gates are untouched.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Telescope, Play, Lock } from 'lucide-react'
import { api } from '../../api/client'

const inr = (n: any) => (n === null || n === undefined || n === '') ? '—'
  : (Number(n) < 0 ? '−₹' : '₹') + Math.abs(Math.round(Number(n))).toLocaleString('en-IN')
const pnlCls = (n: any) => Number(n) > 0 ? 'text-emerald-400' : Number(n) < 0 ? 'text-rose-400' : 'text-slate-400'
const v = (x: any, suf = '') => (x === null || x === undefined) ? '—' : `${x}${suf}`
const INSTS = ['', 'NIFTY_OPT_BUY', 'NIFTY_OPT_CREDIT', 'NIFTY_FUT', 'STOCK_INTRA', 'STOCK_SWING', 'MCX_SWING']

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

export default function DiscoveryTab() {
  const [st, setSt] = useState<any>(null)
  const [lb, setLb] = useState<any>(null)
  const [inst, setInst] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const load = useCallback(async () => {
    try {
      const [a, b] = await Promise.all([api.discoveryStatus(), api.discoveryLeaderboard(40, inst || undefined)])
      setSt(a.data); setLb(b.data); setErr('')
    } catch (e: any) { setErr(e?.response?.data?.detail || e?.message || 'failed') }
  }, [inst])
  useEffect(() => { load(); const t = setInterval(load, 20000); return () => clearInterval(t) }, [load])
  const run = async () => {
    setBusy(true)
    try { const r = await api.discoveryRun(120); if (r.data && r.data.ok === false) setErr(r.data.why); await load() }
    catch (e: any) { setErr(e?.response?.data?.detail || e?.message) }
    setBusy(false)
  }
  const seal = st?.seal || {}
  const last = st?.last_run || {}
  const reg = st?.registered || {}
  return (
    <div className="p-3 space-y-3 text-xs text-slate-300" data-testid="discovery-tab">
      <div className="flex items-center gap-2">
        <Telescope className="w-4 h-4 text-violet-400" />
        <div className="text-sm font-semibold text-slate-100">Strategy discovery (PAPER research)</div>
        <div className="ml-auto flex items-center gap-2">
          {st?.running && <span className="text-amber-300">running (pid {st.pid})…</span>}
          <button onClick={run} disabled={busy || st?.running}
            className="flex items-center gap-1 px-2 py-1 rounded bg-violet-800 hover:bg-violet-700 disabled:opacity-40 text-slate-100">
            <Play className="w-3 h-3" /> Run 2 h now
          </button>
        </div>
      </div>
      {err && <div className="text-rose-400">{err}</div>}
      <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
        <Card title="Trials (all counted)">
          <div className="text-xl font-mono text-slate-100">{(st?.trials_total ?? 0).toLocaleString('en-IN')}</div>
          <div className="text-slate-500">+ {v(st?.prior_research_trials)} from earlier research → used for DSR</div>
        </Card>
        <Card title={<span className="flex items-center gap-1"><Lock className="w-3 h-3" /> Seal</span>}>
          <div>Discovery ≤ <span className="font-mono">{v(seal.discovery_end)}</span> ({v(seal.n_discovery_sessions)} sessions)</div>
          <div>Sealed holdout <span className="font-mono">{v(seal.holdout_start)} … {v(seal.holdout_end)}</span> ({v(seal.n_holdout_sessions)})</div>
          <div className="text-slate-500">Forward holdout: every later session · ledger {v(st?.holdout_ledger_size)} rules tested</div>
        </Card>
        <Card title="Last run">
          <div>{v(last.run)} · {v(last.elapsed_min)} min · stopped: {v(last.stopped)}</div>
          <div>{(last.evaluated ?? 0).toLocaleString('en-IN')} trials · dup {v(last.dup_exact)} exact / {v(last.dup_corr)} corr</div>
          <div>finalists {v(last.finalists)} · holdout tested {v(last.holdout_tested)} · passed {v(last.holdout_passed)}</div>
        </Card>
        <Card title="Registered (PAPER probation, ½ size)">
          {Object.keys(reg).length === 0
            ? <div className="text-slate-400">None — nothing has passed every criterion on the sealed holdout.</div>
            : Object.entries(reg).map(([k, r]: any) => (
              <div key={k} className="font-mono">{k} · {r.executable ? 'agent' : 'not wired'} · {inr(r.metrics?.net)}</div>))}
        </Card>
      </div>
      <Card title="Finalists (one look at the sealed holdout each)">
        {(st?.finalists || []).length === 0 ? <div className="text-slate-400">No rule has passed discovery + deflated Sharpe ≥ 0.95 + SPA p ≤ 0.10 yet.</div> :
          <table className="w-full"><tbody>
            {(st.finalists || []).map((f: any) => (
              <tr key={f.h} className="border-t border-slate-800">
                <td className="py-1 font-mono">{f.h}</td><td>{f.desc}</td><td>{f.status}</td><td>{f.tested?.slice(0, 16)}</td>
              </tr>))}
          </tbody></table>}
      </Card>
      <Card title={`Leaderboard — top distinct near-misses (${lb?.window || ''})`}
        right={<select value={inst} onChange={e => setInst(e.target.value)} className="bg-slate-800 text-slate-200 rounded px-1">
          {INSTS.map(i => <option key={i} value={i}>{i || 'all instruments'}</option>)}</select>}>
        <div className="overflow-x-auto">
          <table className="w-full text-[11px]">
            <thead className="text-slate-500"><tr>
              <th className="text-left">#</th><th className="text-left">rule</th><th>trades</th><th>net</th><th>PF</th>
              <th>w/o best 5</th><th>max DD</th><th>folds+</th><th>stress</th><th>DSR</th><th>SPA p</th><th className="text-left">why it failed</th>
            </tr></thead>
            <tbody>
              {(lb?.rows || []).map((r: any, i: number) => (
                <tr key={r.h} className="border-t border-slate-800 align-top">
                  <td className="pr-1 text-slate-500">{i + 1}</td>
                  <td className="pr-2 max-w-[28rem]">{r.desc}{r.lineage_seen_holdout ? <span className="text-amber-400"> · seed lineage</span> : null}</td>
                  <td className="text-right">{r.trades}</td>
                  <td className={`text-right ${pnlCls(r.net)}`}>{inr(r.net)}</td>
                  <td className="text-right">{v(r.pf)}</td>
                  <td className={`text-right ${pnlCls(r.net_wo_best5)}`}>{inr(r.net_wo_best5)}</td>
                  <td className="text-right">{inr(r.max_dd)}</td>
                  <td className="text-right">{r.folds}</td>
                  <td className={`text-right ${pnlCls(r.stress_net)}`}>{inr(r.stress_net)}</td>
                  <td className="text-right">{v(r.dsr)}</td>
                  <td className="text-right">{v(r.spa_p)}</td>
                  <td className="pl-2 text-rose-300/80">{(r.why_failed || []).join('; ') || '—'}</td>
                </tr>))}
            </tbody>
          </table>
        </div>
      </Card>
      <div className="text-slate-500">{st?.mode} · {st?.schedule}</div>
    </div>
  )
}
