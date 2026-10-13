/**
 * Scalper — real-tick backtest + "trade less, better" controls (PAPER).
 *  • Walk-forward out-of-sample result of the fast scalper replayed on REAL
 *    recorded Kite ticks (same decision code as live), with an in-sample
 *    reference and the previous rules on the same ticks.
 *  • Per symbol / entry window / IST hour stats, cost breakdown, sample trades.
 *  • Liquidity whitelist, entry windows, caps, cool-downs, tick recorder.
 * Research only: nothing here places orders.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Gauge, Play, AlertTriangle, Database } from 'lucide-react'
import { api } from '../../api/client'

const inr = (n: any) => (n === null || n === undefined || n === '') ? '—'
  : (Number(n) < 0 ? '−₹' : '₹') + Math.abs(Math.round(Number(n))).toLocaleString('en-IN')
const pnlCls = (n: any) => Number(n) > 0 ? 'text-emerald-400' : Number(n) < 0 ? 'text-rose-400' : 'text-slate-400'
const v = (x: any, suf = '') => (x === null || x === undefined) ? '—' : `${x}${suf}`

function Card({ title, children, right, testid }: { title: React.ReactNode; children: React.ReactNode; right?: React.ReactNode; testid?: string }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-900/60 p-3" data-testid={testid}>
      <div className="flex items-center justify-between mb-2">
        <div className="text-[11px] uppercase tracking-wider text-slate-400 font-semibold">{title}</div>
        {right}
      </div>
      {children}
    </div>
  )
}

function StatRow({ label, s }: { label: string; s: any }) {
  if (!s) return null
  return (
    <tr className="border-t border-slate-800">
      <td className="text-slate-200 pr-2">{label}</td>
      <td>{s.trades ?? 0}</td><td>{v(s.win_rate, '%')}</td>
      <td className={pnlCls(s.expectancy)}>{inr(s.expectancy)}</td>
      <td className={pnlCls(s.net)}>{inr(s.net)}</td>
      <td className="text-amber-300">{inr(s.costs)}</td>
      <td>{v(s.profit_factor)}</td><td>{v(s.sharpe_per_trade)}</td>
      <td className="text-rose-300">{s.max_dd !== undefined ? inr(s.max_dd) : '—'}</td>
      <td>{v(s.avg_hold_sec, 's')}</td>
      <td>{s.fill_rate !== null && s.fill_rate !== undefined ? `${s.fill_rate}% (${s.fills}/${s.orders})` : '—'}</td>
    </tr>
  )
}

const HEAD = (
  <thead><tr className="text-slate-500 text-left">
    <th></th><th>Scalps</th><th>Win%</th><th>Exp/scalp</th><th>Net</th><th>Costs</th><th>PF</th><th>Sharpe/t</th><th>Max DD</th><th>Avg hold</th><th>Fill rate</th>
  </tr></thead>
)

function Breakdown({ title, rows }: { title: string; rows: any }) {
  const ent = Object.entries(rows || {})
  return (
    <Card title={title}>
      <table className="w-full font-mono">
        {HEAD}
        <tbody>
          {ent.length === 0 && <tr><td colSpan={11} className="text-slate-500 py-1">No scalps.</td></tr>}
          {ent.map(([k, s]: any) => <StatRow key={k} label={k} s={s} />)}
        </tbody>
      </table>
    </Card>
  )
}

export default function ScalperTab() {
  const [bt, setBt] = useState<any>(null)
  const [sc, setSc] = useState<any>(null)
  const [wl, setWl] = useState<any>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = useCallback(async () => {
    const [a, b, c] = await Promise.allSettled([api.scalperBacktest(), api.scalperStatus(), api.scalperWhitelist()])
    if (a.status === 'fulfilled') { setBt(a.value.data); setErr('') } else setErr('backtest unavailable')
    if (b.status === 'fulfilled') setSc(b.value.data)
    if (c.status === 'fulfilled') setWl(c.value.data)
  }, [])
  useEffect(() => { refresh(); const t = setInterval(refresh, 15000); return () => clearInterval(t) }, [refresh])

  const run = async () => {
    setBusy(true)
    try { await api.scalperBacktestRun({}); setTimeout(refresh, 3000); setTimeout(refresh, 12000) }
    catch (e: any) { setErr(e?.response?.data?.detail || e.message) }
    finally { setTimeout(() => setBusy(false), 3000) }
  }

  if (!bt) return <div className="p-4 text-xs text-slate-400">{err || 'Loading scalper backtest…'}</div>
  const wf = bt.walk_forward || {}
  const is = bt.in_sample || {}
  const lg = bt.legacy_rules || {}
  const tlb = sc?.trade_less_better || {}
  const p = bt.params?.MCX || {}
  const insufficient = String(bt.verdict || '').startsWith('NOT ENOUGH')

  return (
    <div className="p-3 space-y-3 text-xs text-slate-200" data-testid="scalper-tab">
      <div className="flex items-center gap-2">
        <Gauge className="w-4 h-4 text-amber-400" />
        <div className="font-semibold text-sm">Fast scalper — real-tick backtest</div>
        <span className="px-1.5 py-0.5 rounded bg-amber-900/60 border border-amber-700 text-amber-200 text-[10px]">PAPER · research only</span>
        <span className="text-slate-500">ran {bt.ran_at ? String(bt.ran_at).replace('T', ' ').slice(0, 16) + ' IST' : 'never'}
          {bt.elapsed_sec ? ` (${bt.elapsed_sec}s)` : ''} · latency {v(bt.latency_ms, ' ms')} · days {(bt.days || []).join(', ') || '—'}</span>
        <button onClick={run} disabled={busy || bt.runner?.running}
                className="ml-auto flex items-center gap-1 px-2 py-1 rounded bg-slate-800 border border-slate-700 hover:bg-slate-700 disabled:opacity-50">
          <Play className="w-3 h-3" /> {bt.runner?.running ? 'Running…' : 'Re-run backtest'}
        </button>
      </div>

      <div className={`rounded border p-2 flex items-start gap-2 ${insufficient ? 'border-amber-700 bg-amber-950/30 text-amber-200' : 'border-slate-700 bg-slate-950/40'}`} data-testid="scalper-verdict">
        <AlertTriangle className="w-4 h-4 shrink-0" />
        <div>
          <div className="font-semibold">{bt.verdict || bt.why}</div>
          <div className="text-slate-400">Walk-forward: {wf.mode === 'intraday_split' ? 'only one recorded day → first 70% of the session trains, last 30% tests' : wf.mode === 'day_folds' ? 'train on earlier days → test on the next day' : '—'}.
            Data: {(bt.data?.in_session_ticks ?? 0).toLocaleString('en-IN')} in-session ticks of {(bt.data?.raw_ticks ?? 0).toLocaleString('en-IN')} recorded
            ({(bt.data?.instruments_in_session || []).length} instruments with in-session ticks; {(bt.data?.no_in_session_ticks || []).length} files only after-hours / frozen); depth format {(bt.data?.depth_format || []).join(', ')}.</div>
        </div>
      </div>

      <Card title="Headline — same ticks, three views" testid="scalper-headline">
        <table className="w-full font-mono">
          {HEAD}
          <tbody>
            <StatRow label="Walk-forward OUT-OF-SAMPLE (honest)" s={wf.oos} />
            <StatRow label="In-sample (whitelist from same data)" s={is.all} />
            <StatRow label="Previous rules (before trade-less-better)" s={lg.all} />
          </tbody>
        </table>
        <div className="mt-1 text-slate-500">
          In-sample: {is.signals ?? 0} signals · skipped {Object.entries(is.skips || {}).map(([k, n]: any) => `${k} ${n}`).join(' · ') || '—'}
        </div>
      </Card>

      <div className="grid grid-cols-2 gap-3">
        <Breakdown title="By symbol (in-sample)" rows={is.by_symbol} />
        <Breakdown title="By entry window (in-sample)" rows={is.by_window} />
      </div>
      <div className="grid grid-cols-2 gap-3">
        <Breakdown title="By IST hour (in-sample)" rows={is.by_hour} />
        <Card title="Costs (in-sample) — every rupee of charges">
          <table className="w-full font-mono">
            <tbody>
              {['brokerage', 'stt', 'exchange', 'sebi', 'gst', 'stamp', 'total'].map(k => (
                <tr key={k} className="border-t border-slate-800"><td className="text-slate-400">{k === 'stt' ? 'STT / CTT' : k}</td><td className="text-amber-300">{inr(is.costs?.[k])}</td>
                  <td className="text-slate-500">prev rules {inr(lg.costs?.[k])}</td></tr>
              ))}
            </tbody>
          </table>
        </Card>
      </div>

      <Card title="Walk-forward folds">
        <table className="w-full">
          <thead><tr className="text-slate-500 text-left"><th>Fold</th><th>Whitelist (from train only)</th><th>Signals</th><th>Test scalps</th><th>Exp/scalp</th><th>Fill rate</th><th>Skips</th></tr></thead>
          <tbody>
            {(wf.folds || []).map((f: any, i: number) => (
              <tr key={i} className="border-t border-slate-800 align-top">
                <td className="font-mono">{f.label}</td>
                <td>{['NSE_EQ', 'NSE_FO', 'MCX'].map(s => <div key={s}><span className="text-slate-500">{s}</span> {(f.whitelist?.[s] || []).join(', ')}</div>)}</td>
                <td>{f.signals}</td><td>{f.test?.trades}</td><td className={pnlCls(f.test?.expectancy)}>{inr(f.test?.expectancy)}</td>
                <td>{v(f.test?.fill_rate, '%')}</td>
                <td className="text-slate-400">{Object.entries(f.skips || {}).map(([k, n]: any) => `${k} ${n}`).join(' · ')}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <Card title="Sample scalps (in-sample, newest last)">
        <table className="w-full font-mono">
          <thead><tr className="text-slate-500 text-left"><th>Entry (IST)</th><th>Symbol</th><th>Side</th><th>Lots</th><th>Entry</th><th>Exit</th><th>Why</th><th>Hold</th><th>Gross</th><th>Costs</th><th>Net</th><th>Queue@join</th></tr></thead>
          <tbody>
            {(is.trades || []).length === 0 && <tr><td colSpan={12} className="text-slate-500 py-1">No scalps.</td></tr>}
            {(is.trades || []).slice(-25).map((t: any, i: number) => (
              <tr key={i} className="border-t border-slate-800">
                <td>{t.entry_time}</td><td>{t.symbol}</td><td>{t.side}</td><td>{t.lots}</td><td>{t.entry}</td><td>{t.exit}</td>
                <td>{t.reason}</td><td>{t.hold_sec}s</td><td className={pnlCls(t.gross)}>{inr(t.gross)}</td>
                <td className="text-amber-300">{inr(t.costs?.total)}</td><td className={pnlCls(t.net)}>{inr(t.net)}</td><td>{v(t.queue_at_join)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </Card>

      <div className="grid grid-cols-2 gap-3">
        <Card title="Trade less, better — live settings" testid="scalper-tlb">
          <div className="space-y-1">
            <div><span className="text-slate-400">Whitelist</span> ({wl?.source || '—'}{wl?.as_of ? `, for ${wl.as_of}` : ''}):</div>
            {['NSE_EQ', 'NSE_FO', 'MCX'].map(s => (
              <div key={s} className="pl-2"><span className="text-slate-500 w-14 inline-block">{s}</span>{(wl?.[s] || []).map((r: any) => r.symbol).join(', ') || '—'}</div>
            ))}
            <div className="pl-2"><span className="text-slate-500 w-14 inline-block">Options</span>{tlb.whitelist?.options || 'NIFTY near-ATM'}</div>
            <div className="pt-1"><span className="text-slate-400">Entry windows (IST)</span></div>
            {Object.entries(tlb.windows || {}).filter(([, w]: any) => (w || []).length).map(([s, w]: any) => (
              <div key={s} className="pl-2"><span className="text-slate-500 w-24 inline-block">{s}</span>{w.map((x: any) => `${x[0]}–${x[1]}`).join(' · ')}</div>
            ))}
            <div className="pt-1 font-mono text-slate-300">
              k (edge ≥ k×(costs+spread)) {v(p.edge_cost_mult)} · confluence ≥ {v(p.confluence_min)}/4 · imbalance ≥ {v(p.imb_entry)} · momentum ≥ {v(p.mom_ticks)} ticks ·
              cool-down {v(p.cooldown_sec, 's')} after a loss · OFF after {v(p.max_consec_losses)} losses in a row ·
              ≤ {v(p.symbol_daily_cap)}/symbol/day · daily cap {v(p.daily_cap)} (hard {JSON.stringify(tlb.hard_caps || {})})
            </div>
          </div>
        </Card>
        <Card title={<span className="flex items-center gap-1"><Database className="w-3.5 h-3.5" /> Liquidity ranking & tick recorder</span>}>
          <table className="w-full font-mono">
            <thead><tr className="text-slate-500 text-left"><th>Symbol</th><th>Spread (ticks)</th><th>Spread bps</th><th>Turnover ₹Cr/h</th><th>Ticks/min</th></tr></thead>
            <tbody>
              {[...(wl?.NSE_EQ || []), ...(wl?.NSE_FO || []), ...(wl?.MCX || [])].map((r: any) => (
                <tr key={r.key} className="border-t border-slate-800 text-emerald-300"><td>{r.symbol}</td><td>{v(r.median_spread_ticks)}</td><td>{v(r.median_spread_bps)}</td><td>{v(r.turnover_cr_per_hr)}</td><td>{v(r.ticks_per_min)}{r.source === 'default' ? ' (default)' : ''}</td></tr>
              ))}
              {(wl?.rejected || []).slice(0, 10).map((r: any) => (
                <tr key={'x' + r.key} className="border-t border-slate-800 text-slate-500" title={r.why}><td>{r.symbol} ✗</td><td>{v(r.median_spread_ticks)}</td><td>{v(r.median_spread_bps)}</td><td>{v(r.turnover_cr_per_hr)}</td><td>{r.why}</td></tr>
              ))}
            </tbody>
          </table>
          <div className="mt-1 text-slate-500">Recorder: {tlb.recorder?.format || '—'} · {(tlb.recorder?.days_on_disk || []).length} day(s) on disk ({v(tlb.recorder?.disk_mb, ' MB')}) ·
            rotation keep {v(tlb.recorder?.rotation?.keep_days)} d / ≤ {v(tlb.recorder?.rotation?.max_total_mb)} MB, gzip after {v(tlb.recorder?.rotation?.gzip_after_days)} d</div>
        </Card>
      </div>
    </div>
  )
}
