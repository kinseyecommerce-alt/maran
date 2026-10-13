/**
 * Options — PAPER only.
 *  • Multi-leg defined-risk baskets grouped as ONE row each: legs, net credit,
 *    max loss / max profit, breakevens, margin (with hedge benefit), Greeks.
 *  • Option buys (cost/theta gate, Greek sizing).
 *  • Option scalper on Kite WS depth: ATM window, stats, caps, probation.
 *  • Last DRY-RUN replay (market closed) — clearly labelled, never in the book.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Layers, ShieldCheck, Zap, History } from 'lucide-react'
import { api } from '../../api/client'

const inr = (n: any) => (n === null || n === undefined || n === '' || !isFinite(Number(n))) ? '—'
  : (Number(n) < 0 ? '−₹' : '₹') + Math.abs(Math.round(Number(n))).toLocaleString('en-IN')
const num = (n: any, d = 2) => (n === null || n === undefined || !isFinite(Number(n))) ? '—' : Number(n).toFixed(d)
const pnlCls = (n: any) => Number(n) > 0 ? 'text-emerald-400' : Number(n) < 0 ? 'text-rose-400' : 'text-slate-400'
const hm = (ts: any) => ts ? String(ts).replace('T', ' ').slice(11, 19) : '—'

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

function Stat({ label, value, cls = '', sub }: { label: string; value: React.ReactNode; cls?: string; sub?: React.ReactNode }) {
  return (
    <div className="rounded bg-slate-950/50 border border-slate-800 px-2 py-1.5">
      <div className="text-[10px] uppercase tracking-wide text-slate-500">{label}</div>
      <div className={`font-mono text-sm ${cls}`}>{value}</div>
      {sub && <div className="text-[10px] text-slate-500">{sub}</div>}
    </div>
  )
}

const STATUS_CLS: Record<string, string> = {
  OPEN: 'bg-sky-900/60 border-sky-700 text-sky-200', CLOSED: 'bg-slate-800 border-slate-600 text-slate-200',
  UNWOUND: 'bg-rose-900/60 border-rose-700 text-rose-200', PENDING: 'bg-amber-900/60 border-amber-700 text-amber-200',
}

export function BasketCard({ b }: { b: any }) {
  const q = (b.lots || 0) * (b.lot_size || 0)
  const credTot = b.net_credit_total ?? (b.credit * q)
  const pnl = b.status === 'OPEN' ? b.mtm : b.pnl_net
  const costs = (b.costs_entry?.total || 0) + (b.costs_exit?.total || 0)
  const g = b.greeks || {}
  const m = b.margin || {}
  return (
    <div className="rounded border border-slate-700 bg-slate-950/40 p-2.5 space-y-2" data-testid="basket">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="font-semibold text-sm text-slate-100">{b.underlying} {String(b.structure).replace('_', ' ')}</span>
        <span className={`px-1.5 py-0.5 rounded border text-[10px] font-bold ${STATUS_CLS[b.status] || STATUS_CLS.PENDING}`}>{b.status}</span>
        {b.probation && <span className="px-1.5 py-0.5 rounded border border-amber-700 bg-amber-950/50 text-amber-200 text-[10px]">PROBATION {num(b.size_factor, 2)}×</span>}
        <span className="text-slate-400">exp {b.expiry} · {b.lots} lot × {b.lot_size} = {q} qty · spot {num(b.spot_entry, 1)} · {b.id}</span>
        <span className="ml-auto text-slate-500 font-mono">{hm(b.opened)} → {b.closed ? hm(b.closed) : 'open'}</span>
      </div>
      <div className="grid grid-cols-8 gap-1.5">
        <Stat label="Net credit" value={inr(credTot)} cls="text-emerald-300" sub={`₹${num(b.credit)}/unit · width ${b.width}`} />
        <Stat label="Max loss" value={inr(b.max_loss)} cls="text-rose-300" sub={`incl. costs ${inr(b.max_loss_per_lot * (b.lots || 1))}`} />
        <Stat label="Max profit" value={inr(b.max_profit)} cls="text-emerald-300" />
        <Stat label="Breakevens" value={(b.breakevens || []).map((x: number) => x.toLocaleString('en-IN', { maximumFractionDigits: 1 })).join(' / ') || '—'} />
        <Stat label="Margin (est.)" value={inr(m.total)} sub={`hedge benefit ${inr(m.hedge_benefit)}`} />
        <Stat label="Greeks Δ / Γ" value={`${num(g.delta, 1)} / ${num(g.gamma, 3)}`} sub="position, per ₹1 / pt" />
        <Stat label="Θ/day · Vega" value={`${inr(g.theta)} · ${inr(g.vega)}`} cls={pnlCls(g.theta)} sub="per 1 vol pt" />
        <Stat label={b.status === 'OPEN' ? 'MTM (at touch)' : 'P&L net of costs'} value={inr(pnl)} cls={pnlCls(pnl)}
              sub={`costs ${inr(costs)}${b.status !== 'OPEN' ? ` · gross ${inr(b.pnl_gross)}` : ''}`} />
      </div>
      <table className="w-full font-mono text-[11px]">
        <thead><tr className="text-slate-500 text-left">
          <th>#</th><th>Leg</th><th>Contract</th><th>Strike</th><th>Qty</th><th>Entry</th><th>{b.status === 'OPEN' ? 'Mark' : 'Exit'}</th><th>IV</th><th>Δ</th><th>Θ/day</th><th>Leg P&L</th>
        </tr></thead>
        <tbody>
          {(b.legs || []).slice().sort((a: any, c: any) => (a.opt_type + a.strike).localeCompare(c.opt_type + c.strike)).map((l: any, i: number) => {
            const px = b.status === 'OPEN' ? l.mark : l.exit
            const lp = l.side * ((px || 0) - l.entry) * l.qty
            return (
              <tr key={l.symbol} className="border-t border-slate-800">
                <td className="text-slate-500">{i + 1}</td>
                <td className={l.side > 0 ? 'text-emerald-400' : 'text-rose-400'}>{l.side > 0 ? 'BUY wing' : 'SELL short'}</td>
                <td className="text-slate-200">{l.symbol}</td><td>{l.strike} {l.opt_type}</td><td>{l.side > 0 ? '+' : '−'}{l.qty}</td>
                <td>{num(l.entry)}</td><td>{num(px)}</td><td>{l.iv ? (l.iv * 100).toFixed(1) + '%' : '—'}</td>
                <td>{num(l.delta, 2)}</td><td>{num(l.theta, 2)}</td><td className={pnlCls(lp)}>{inr(lp)}</td>
              </tr>
            )
          })}
        </tbody>
      </table>
      <div className="text-[10px] text-slate-400 leading-snug">
        <span className="text-slate-500">entry:</span> {b.reason || '—'}
        {b.exit_reason && <> · <span className="text-slate-500">exit:</span> {b.exit_reason}</>}
        {b.rules && <> · <span className="text-slate-500">rules:</span> target {Math.round((b.rules.target_frac || 0) * 100)}% credit · stop {b.rules.stop_mult}× credit / short-strike breach · time exit {b.rules.exit_time} · expiry-day {b.rules.expiry_day_exit}</>}
      </div>
    </div>
  )
}

function ScalpTable({ rows }: { rows: any[] }) {
  return (
    <table className="w-full font-mono text-[11px]">
      <thead><tr className="text-slate-500 text-left">
        <th>In</th><th>Out</th><th>Contract</th><th>Lots</th><th>Entry</th><th>Exit</th><th>Hold</th><th>Reason</th><th>Gross</th><th>Costs</th><th>Net</th>
      </tr></thead>
      <tbody>
        {rows.length === 0 && <tr><td colSpan={11} className="text-slate-500 py-1">No option scalps.</td></tr>}
        {rows.map((s: any, i: number) => (
          <tr key={i} className="border-t border-slate-800">
            <td>{hm(s.entry_ts || s.ts)}</td><td>{hm(s.exit_ts || s.ts)}</td><td className="text-slate-200">{s.symbol}</td><td>{s.lots}</td>
            <td>{num(s.entry)}</td><td>{num(s.exit)}</td><td>{s.hold_sec}s</td>
            <td className={s.reason === 'target' || s.reason === 'scalp_target' ? 'text-emerald-400' : s.reason?.includes('stop') ? 'text-rose-300' : 'text-slate-300'}>{s.reason}</td>
            <td className={pnlCls(s.gross)}>{inr(s.gross)}</td><td className="text-amber-300">{inr(s.costs)}</td><td className={pnlCls(s.net)}>{inr(s.net)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export default function OptionsTab() {
  const [st, setSt] = useState<any>(null)
  const [sc, setSc] = useState<any>(null)
  const [demo, setDemo] = useState<any>(null)
  const [err, setErr] = useState('')

  const refresh = useCallback(async () => {
    try {
      const [a, b, c] = await Promise.allSettled([api.optionsStatus(), api.optionsScalper(), api.optionsDemo()])
      if (a.status === 'fulfilled') setSt(a.value.data)
      if (b.status === 'fulfilled') setSc(b.value.data)
      if (c.status === 'fulfilled') setDemo(c.value.data)
      setErr(a.status === 'rejected' ? String((a.reason as any)?.message || 'options status failed') : '')
    } catch (e: any) { setErr(e.message || 'refresh failed') }
  }, [])
  useEffect(() => { refresh(); const t = setInterval(refresh, 5000); return () => clearInterval(t) }, [refresh])

  if (!st) return <div className="p-4 text-xs text-slate-400">{err || 'Loading options engine…'}</div>
  const baskets: any[] = st.baskets || []
  const buys: any[] = st.buys || []
  const dEng = demo?.engine || {}
  const dBaskets: any[] = (dEng.baskets || [])
  const dScalps: any[] = demo?.scalps || []
  const dNet = dScalps.reduce((a: number, s: any) => a + (s.net || 0), 0)
  const dWins = dScalps.filter((s: any) => s.net > 0).length
  const caps = sc?.caps || {}
  const n = (sc?.wins || 0) + (sc?.losses || 0)

  return (
    <div className="p-3 space-y-3 text-xs text-slate-200">
      <div className="flex items-center gap-2 flex-wrap">
        <Layers className="w-4 h-4 text-sky-400" />
        <div className="font-semibold text-sm">Options engine</div>
        <span className="px-1.5 py-0.5 rounded bg-amber-900/60 border border-amber-700 text-amber-200 text-[10px]">{st.mode} ONLY</span>
        <span className="px-1.5 py-0.5 rounded bg-emerald-900/50 border border-emerald-700 text-emerald-200 text-[10px]">NO NAKED SHORTS</span>
        <span className="text-slate-400">entries {st.enabled ? 'enabled' : 'paused'} · market: {st.market} · open {st.open} · realised today <span className={pnlCls(st.realised_today)}>{inr(st.realised_today)}</span> · margin used {inr(st.margin_used)}</span>
        {st.last_error && <span className="text-rose-400">· {st.last_error}</span>}
      </div>

      <Card title="Multi-leg baskets — live paper (today)" testid="live-baskets"
            right={<span className="text-slate-500">opened {st.stats?.baskets_opened ?? 0} · rejected {st.stats?.baskets_rejected ?? 0} · unwinds {st.stats?.unwinds ?? 0} · guard blocks {st.stats?.guard_blocks ?? 0}</span>}>
        {baskets.length === 0 ? <div className="text-slate-500">No baskets today (NSE F&amp;O closed or no defined-risk setup passed the gates).</div>
          : <div className="space-y-2">{baskets.map(b => <BasketCard key={b.id} b={b} />)}</div>}
      </Card>

      {demo && demo.engine && (
        <Card testid="demo-baskets"
              title={<span className="flex items-center gap-1"><History className="w-3.5 h-3.5 text-violet-400" /> Dry-run replay — {demo.session} (market closed)</span>}
              right={<span className="px-1.5 py-0.5 rounded border border-violet-700 bg-violet-950/50 text-violet-200 text-[10px]">DRY-RUN · NOT IN PAPER BOOK</span>}>
          <div className="text-[10px] text-slate-400 mb-2">{demo.label}. {(demo.data_notes || []).join(' · ')}</div>
          <div className="space-y-2">{dBaskets.map((b: any) => <BasketCard key={b.id} b={b} />)}</div>
          {(dEng.buys || []).length > 0 && (
            <div className="mt-2 text-slate-400">Engine option buys in the replay: {(dEng.buys || []).map((p: any) =>
              <span key={p.id} className="mr-3 font-mono">{p.leg?.symbol} {p.leg?.lots}L {num(p.leg?.entry)}→{num(p.leg?.exit)} <span className={pnlCls(p.pnl_net)}>{inr(p.pnl_net)}</span> ({p.exit_reason})</span>)}</div>
          )}
        </Card>
      )}

      <div className="grid grid-cols-3 gap-3">
        <Card title="Option buys — cost/θ gate, Greek sizing" testid="buys">
          {buys.length === 0 ? <div className="text-slate-500">No option buys today.</div> : (
            <table className="w-full font-mono text-[11px]"><thead><tr className="text-slate-500 text-left"><th>Contract</th><th>Lots</th><th>Entry</th><th>SL/TGT</th><th>P&L</th><th>Status</th></tr></thead>
              <tbody>{buys.map((p: any) => (
                <tr key={p.id} className="border-t border-slate-800"><td>{p.leg?.symbol}</td><td>{p.leg?.lots}</td><td>{num(p.leg?.entry)}</td>
                  <td>{num(p.sl)}/{num(p.tgt)}</td><td className={pnlCls(p.status === 'OPEN' ? p.mtm : p.pnl_net)}>{inr(p.status === 'OPEN' ? p.mtm : p.pnl_net)}</td><td>{p.status}</td></tr>))}
              </tbody></table>)}
        </Card>
        <Card title="Strategy families — backtest gate / probation" testid="gates">
          <table className="w-full text-[11px]"><thead><tr className="text-slate-500 text-left"><th>Family</th><th>Gate</th><th>Size×</th><th>Ready</th></tr></thead>
            <tbody>{Object.entries(st.gates || {}).map(([f, g]: any) => {
              const rd = st.readiness?.[f]
              return (
                <tr key={f} className="border-t border-slate-800 align-top" title={g.note || g.why || ''}>
                  <td className="font-mono">{f}</td>
                  <td className={g.status === 'pass' ? 'text-emerald-400' : g.status === 'fail' ? 'text-rose-400' : 'text-amber-300'}>{g.status}{g.entry_ok === false ? ' (blocked)' : ''}</td>
                  <td className="font-mono">{g.size_mult !== undefined ? num(g.size_mult, 2) : '—'}</td>
                  <td className="text-slate-400">{rd ? `${rd.passed}/${rd.of}` : '—'}</td>
                </tr>)
            })}</tbody></table>
        </Card>
        <Card title="Guards" right={<ShieldCheck className="w-3.5 h-3.5 text-emerald-400" />} testid="guards">
          <ul className="list-disc pl-4 space-y-0.5 text-slate-300 text-[11px]">{(st.guards || []).map((g: string) => <li key={g}>{g}</li>)}</ul>
        </Card>
      </div>

      <Card testid="option-scalper"
            title={<span className="flex items-center gap-1"><Zap className="w-3.5 h-3.5 text-amber-400" /> Option scalper — NIFTY/BANKNIFTY near-ATM on Kite WS depth (PAPER)</span>}
            right={caps.probation ? <span className="px-1.5 py-0.5 rounded border border-amber-700 bg-amber-950/50 text-amber-200 text-[10px]">PROBATION — {caps.probation_rule}</span> : null}>
        {!sc ? <div className="text-slate-500">Scalper status unavailable.</div> : (
          <>
            <div className="grid grid-cols-8 gap-1.5 mb-2">
              <Stat label="Window" value={`${sc.instruments ?? 0} contracts`} sub={Object.entries(sc.window || {}).map(([u, w]: any) => `${u} ATM ${w.atm} · ${w.expiry}`).join(' | ') || 'not subscribed (market closed)'} />
              <Stat label="Signals" value={sc.signals ?? 0} sub={`bearish-book skips ${sc.short_skips ?? 0}`} />
              <Stat label="Orders / fills" value={`${sc.orders ?? 0} / ${sc.fills ?? 0}`} sub={`cancels ${sc.cancels ?? 0} (limit at touch, queue model)`} />
              <Stat label="Skips" value={`${(sc.cost_skips ?? 0) + (sc.cap_skips ?? 0) + (sc.gate_skips ?? 0)}`} sub={`cost ${sc.cost_skips ?? 0} · caps ${sc.cap_skips ?? 0} · gates ${sc.gate_skips ?? 0}`} />
              <Stat label="Exits / win%" value={`${sc.exits ?? 0} · ${n ? sc.win_rate + '%' : '—'}`} sub={`${sc.wins ?? 0}W / ${sc.losses ?? 0}L`} />
              <Stat label="Gross" value={inr(sc.gross)} cls={pnlCls(sc.gross)} />
              <Stat label="Costs" value={inr(sc.costs)} cls="text-amber-300" sub="STT·exch·SEBI·GST·stamp·₹20/order" />
              <Stat label="Net today" value={inr(sc.net)} cls={pnlCls(sc.net)} />
            </div>
            <div className="text-[10px] text-slate-400 mb-2">
              params: imbalance ≥ {sc.params?.imb_entry} · momentum {sc.params?.mom_ticks} ticks · stop {sc.params?.sl_ticks} ticks · target {sc.params?.tp_ticks} ticks · time stop {sc.params?.time_stop_sec}s · max spread {sc.params?.max_spread_ticks} ticks · edge ≥ {sc.params?.edge_cost_mult}× costs
              {' '}| caps: {caps.daily}/day · {caps.per_symbol_daily}/contract/day · {caps.max_concurrent} concurrent · ≤{caps.max_lots} lots · risk {caps.risk_pct}% · premium ≤ {caps.max_notional_pct}% capital · long premium only
            </div>
            <div className="text-slate-400 mb-1">Today (live paper){(sc.open || []).length ? ` · ${sc.open.length} open` : ''}</div>
            <ScalpTable rows={(sc.recent || []).slice().reverse()} />
          </>
        )}
        {demo && dScalps.length > 0 && (
          <div className="mt-3" data-testid="demo-scalps">
            <div className="flex items-center gap-2 mb-1">
              <span className="px-1.5 py-0.5 rounded border border-violet-700 bg-violet-950/50 text-violet-200 text-[10px]">DRY-RUN {demo.session}</span>
              <span className="text-slate-400">replay of {Object.keys(demo.scalp_contracts || {}).join(', ')} 10:00–12:00 · {dScalps.length} scalps · {dWins}W/{dScalps.length - dWins}L · net <span className={pnlCls(dNet)}>{inr(dNet)}</span> after costs</span>
            </div>
            <ScalpTable rows={dScalps} />
          </div>
        )}
      </Card>
    </div>
  )
}
