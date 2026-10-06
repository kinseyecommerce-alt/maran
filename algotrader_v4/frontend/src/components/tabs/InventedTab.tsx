/**
 * Strategies Invented — trend-driven short-lived strategies per segment.
 * PAPER by default. Arm LIVE tiny requires typed SEND + warm-up + Kite.
 */
import React, { useCallback, useEffect, useState } from 'react'
import { Lightbulb, ShieldAlert } from 'lucide-react'
import { api } from '../../api/client'

type InventSnap = {
  status: {
    enabled: boolean
    trading_mode: string
    kite_ready: boolean
    counts: Record<string, number>
    caps: Record<string, number>
    live_tiny_requirements: string[]
  }
  strategies: any[]
  journal: any[]
}

const SEGMENTS = ['NSE_EQ', 'NSE_FO', 'BSE_EQ', 'MCX', 'CDS']

export default function InventedTab() {
  const [snap, setSnap] = useState<InventSnap | null>(null)
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const [sendText, setSendText] = useState('')
  const [armId, setArmId] = useState<string | null>(null)
  const [proposeSeg, setProposeSeg] = useState('NSE_EQ')

  const refresh = useCallback(async () => {
    try {
      const r = await api.inventStatus()
      setSnap(r.data)
      setErr('')
    } catch (e: any) {
      setErr(e?.response?.data?.detail || e.message || 'refresh failed')
    }
  }, [])

  useEffect(() => {
    refresh()
    const t = setInterval(refresh, 3000)
    return () => clearInterval(t)
  }, [refresh])

  const toggle = async (enabled: boolean) => {
    setBusy(true)
    try {
      await api.inventEnabled(enabled)
      await refresh()
    } catch (e: any) {
      setErr(e?.response?.data?.detail || 'toggle failed')
    } finally {
      setBusy(false)
    }
  }

  const propose = async () => {
    setBusy(true)
    try {
      await api.inventPropose(proposeSeg)
      await refresh()
    } catch (e: any) {
      setErr(e?.response?.data?.detail || 'propose failed')
    } finally {
      setBusy(false)
    }
  }

  const arm = async (id: string) => {
    setBusy(true)
    try {
      await api.inventArmLiveTiny(id, true, sendText)
      setArmId(null)
      setSendText('')
      await refresh()
    } catch (e: any) {
      setErr(e?.response?.data?.detail || 'arm refused')
    } finally {
      setBusy(false)
    }
  }

  const disarm = async (id: string) => {
    setBusy(true)
    try {
      await api.inventDisarmLive(id)
      await refresh()
    } catch (e: any) {
      setErr(e?.response?.data?.detail || 'disarm failed')
    } finally {
      setBusy(false)
    }
  }

  const st = snap?.status
  const strategies = snap?.strategies || []
  const journal = snap?.journal || []

  return (
    <div className="flex-1 overflow-auto p-4 space-y-4" data-testid="invented-tab">
      <div className="flex items-center gap-3 flex-wrap">
        <Lightbulb className="w-5 h-5 text-amber-400" />
        <h2 className="text-sm font-semibold text-slate-200 tracking-wide">Strategies Invented</h2>
        <span className="text-[10px] font-mono text-slate-500">INVENTED · PAPER first · LIVE tiny needs SEND</span>
        <div className="flex-1" />
        <button
          data-testid="invent-toggle"
          disabled={busy}
          onClick={() => toggle(!st?.enabled)}
          className={`px-3 py-1.5 rounded text-xs font-semibold border ${
            st?.enabled
              ? 'bg-amber-500/20 border-amber-500/50 text-amber-300'
              : 'bg-slate-800 border-slate-700 text-slate-400'
          }`}
        >
          Invent {st?.enabled ? 'ON' : 'OFF'}
        </button>
      </div>

      {err && (
        <div className="text-xs text-rose-400 border border-rose-900/50 bg-rose-950/30 rounded px-3 py-2" data-testid="invent-error">
          {err}
        </div>
      )}

      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-[11px] font-mono">
        <Card label="Mode" value={st?.trading_mode || '—'} />
        <Card label="Kite" value={st?.kite_ready ? 'ready' : 'not connected'} warn={!st?.kite_ready} />
        <Card label="Active" value={String(st?.counts?.active ?? 0)} />
        <Card label="Paper fills armed" value={`${st?.counts?.paper_active ?? 0} / ${st?.counts?.live_armed ?? 0}`} />
      </div>

      <div className="flex items-center gap-2 flex-wrap">
        <select
          className="bg-slate-900 border border-slate-700 rounded px-2 py-1 text-xs text-slate-200"
          value={proposeSeg}
          onChange={e => setProposeSeg(e.target.value)}
          data-testid="invent-propose-seg"
        >
          {SEGMENTS.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <button
          data-testid="invent-propose"
          disabled={busy || !st?.enabled}
          onClick={propose}
          className="px-3 py-1.5 rounded text-xs bg-emerald-600/20 border border-emerald-700 text-emerald-300 disabled:opacity-40"
        >
          Propose PAPER strategy
        </button>
        <span className="text-[10px] text-slate-500">Invent mode must be ON. Caps: {st?.caps?.max_per_segment}/seg, {st?.caps?.max_concurrent_global} global.</span>
      </div>

      <div className="border border-slate-800 rounded overflow-hidden">
        <div className="px-3 py-2 bg-slate-900/80 text-[10px] uppercase tracking-wider text-slate-500">Active & recent</div>
        <table className="w-full text-xs">
          <thead className="text-[10px] text-slate-500 font-mono">
            <tr className="border-b border-slate-800">
              <th className="text-left px-2 py-1">ID</th>
              <th className="text-left px-2 py-1">Seg</th>
              <th className="text-left px-2 py-1">Name</th>
              <th className="text-left px-2 py-1">Regime</th>
              <th className="text-left px-2 py-1">Status</th>
              <th className="text-right px-2 py-1">Fills</th>
              <th className="text-right px-2 py-1">P&L</th>
              <th className="text-left px-2 py-1">Symbol</th>
              <th className="text-right px-2 py-1">Actions</th>
            </tr>
          </thead>
          <tbody>
            {strategies.length === 0 && (
              <tr><td colSpan={9} className="px-3 py-6 text-center text-slate-600">No invented strategies yet — turn Invent ON and propose, or wait for regime-driven invent.</td></tr>
            )}
            {strategies.slice(0, 40).map((s: any) => (
              <tr key={s.id} className="border-b border-slate-900/80 hover:bg-slate-900/40" data-testid={`invent-row-${s.id}`}>
                <td className="px-2 py-1 font-mono text-[10px] text-slate-400">{s.id}</td>
                <td className="px-2 py-1">{s.segment}</td>
                <td className="px-2 py-1">{s.name} <span className="text-amber-500/80 text-[9px]">{s.label}</span>{s.simulated ? <span className="ml-1 text-[9px] text-yellow-600">SIMULATED</span> : null}</td>
                <td className="px-2 py-1 font-mono text-[10px]">{s.regime}</td>
                <td className="px-2 py-1"><StatusBadge status={s.status} /></td>
                <td className="px-2 py-1 text-right font-mono">{s.paper_fills}</td>
                <td className={`px-2 py-1 text-right font-mono ${(s.paper_pnl || 0) >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>{Number(s.paper_pnl || 0).toFixed(0)}</td>
                <td className="px-2 py-1 font-mono text-[10px]">{s.symbol || '—'}</td>
                <td className="px-2 py-1 text-right space-x-1">
                  {(s.status === 'live_eligible' || s.status === 'paper_active') && s.warm_up_ok && (
                    <button className="text-[10px] text-amber-400 underline" onClick={() => setArmId(s.id)}>Arm LIVE tiny</button>
                  )}
                  {s.status === 'live_armed' && (
                    <button className="text-[10px] text-slate-400 underline" onClick={() => disarm(s.id)}>Disarm</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {armId && (
        <div className="border border-amber-800/60 bg-amber-950/20 rounded p-3 space-y-2" data-testid="invent-arm-panel">
          <div className="flex items-center gap-2 text-amber-300 text-xs font-semibold">
            <ShieldAlert className="w-4 h-4" /> Arm LIVE tiny for {armId}
          </div>
          <p className="text-[11px] text-slate-400">Requires global LIVE, segment LIVE (typed SEND), Kite session, and paper warm-up. Quantity = 1 share / 1 lot. Never auto-armed.</p>
          <ul className="text-[10px] text-slate-500 list-disc pl-4">
            {(st?.live_tiny_requirements || []).map((x, i) => <li key={i}>{x}</li>)}
          </ul>
          <div className="flex gap-2 items-center">
            <input
              data-testid="invent-arm-send"
              className="bg-slate-950 border border-slate-700 rounded px-2 py-1 text-xs font-mono text-slate-200"
              placeholder='Type SEND'
              value={sendText}
              onChange={e => setSendText(e.target.value)}
            />
            <button
              data-testid="invent-arm-confirm"
              disabled={busy || sendText !== 'SEND'}
              onClick={() => arm(armId)}
              className="px-3 py-1.5 rounded text-xs bg-rose-700/40 border border-rose-600 text-rose-200 disabled:opacity-40"
            >
              Confirm Arm
            </button>
            <button className="text-xs text-slate-500" onClick={() => { setArmId(null); setSendText('') }}>Cancel</button>
          </div>
        </div>
      )}

      <div className="border border-slate-800 rounded overflow-hidden">
        <div className="px-3 py-2 bg-slate-900/80 text-[10px] uppercase tracking-wider text-slate-500">Journal</div>
        <div className="max-h-48 overflow-auto font-mono text-[10px] text-slate-400 p-2 space-y-0.5" data-testid="invent-journal">
          {journal.length === 0 && <div className="text-slate-600">No events yet.</div>}
          {journal.map((j, i) => (
            <div key={i}>[{j.ts}] {j.event} {j.segment} {j.id} — {j.detail}</div>
          ))}
        </div>
      </div>
    </div>
  )
}

function Card({ label, value, warn }: { label: string; value: string; warn?: boolean }) {
  return (
    <div className="border border-slate-800 rounded px-3 py-2 bg-slate-900/40">
      <div className="text-[9px] uppercase tracking-wider text-slate-600">{label}</div>
      <div className={`text-sm font-semibold ${warn ? 'text-amber-400' : 'text-slate-200'}`}>{value}</div>
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const colors: Record<string, string> = {
    paper_active: 'text-emerald-400',
    live_eligible: 'text-amber-300',
    live_armed: 'text-rose-400',
    killed: 'text-rose-600',
    expired: 'text-slate-500',
    proposed: 'text-sky-400',
  }
  return <span className={`font-mono text-[10px] ${colors[status] || 'text-slate-400'}`}>{status}</span>
}
