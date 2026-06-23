import React, { useEffect, useState } from 'react'
import { useStore } from '../../store'
import { api } from '../../api/client'
import { PieChart, Pie, Cell, ResponsiveContainer } from 'recharts'

function StatCard({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="bg-slate-800/60 border border-slate-700/50 rounded-xl p-4">
      <div className="text-[10px] font-bold text-slate-500 uppercase tracking-widest mb-2">{label}</div>
      {children}
    </div>
  )
}

function Row({ label, value, sub }: { label: string; value: React.ReactNode; sub?: string }) {
  return (
    <div className="flex items-center justify-between py-1 border-b border-slate-800 last:border-0">
      <span className="text-xs text-slate-400">{label}</span>
      <div className="text-right">
        <span className="text-xs font-mono font-semibold text-slate-200">{value}</span>
        {sub && <div className="text-[10px] text-slate-600">{sub}</div>}
      </div>
    </div>
  )
}

const REGIME_COLOR: Record<string, string> = {
  BULL: 'text-emerald-400', BEAR: 'text-rose-400', SIDEWAYS: 'text-amber-400', VOLATILE: 'text-orange-400',
}

export default function RiskTab() {
  const { riskStatus, setRiskStatus } = useStore()
  const [regime, setRegime]       = useState<any>(null)
  const [capital, setCapital]     = useState<any>(null)
  const [connections, setConns]   = useState<any>(null)
  const [reconciler, setRecon]    = useState<any>(null)
  const [loading, setLoading]     = useState(true)

  useEffect(() => {
    const load = () => {
      api.riskStatus().then(r => setRiskStatus(r.data)).catch(() => {})
      api.regimeStatus().then(r => setRegime(r.data)).catch(() => {})
      api.capitalStatus().then(r => setCapital(r.data)).catch(() => {})
      api.connectionsStatus().then(r => setConns(r.data)).catch(() => {})
      api.reconcilerStatus().then(r => setRecon(r.data)).catch(() => {})
    }
    load()
    setLoading(false)
    const t = setInterval(load, 8000)
    return () => clearInterval(t)
  }, [])

  if (!riskStatus && loading) return (
    <div className="flex items-center justify-center h-full text-slate-500 text-xs">Loading…</div>
  )

  const lossUsed   = riskStatus ? Math.min(100, Math.abs(Math.min(0, riskStatus.daily_pnl)) / Math.max(1, riskStatus.max_daily_loss) * 100) : 0
  const posUsed    = riskStatus ? (riskStatus.open_positions / Math.max(1, riskStatus.max_open_positions)) * 100 : 0
  const limitColor = lossUsed > 80 ? '#f43f5e' : lossUsed > 50 ? '#f59e0b' : '#10b981'
  const gaugeData  = [{ v: lossUsed, fill: limitColor }, { v: 100 - lossUsed, fill: '#1e293b' }]

  const fmt = (n: number) => `₹${Math.abs(n).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`

  return (
    <div className="h-full overflow-y-auto p-3 space-y-3 text-slate-200">

      {riskStatus?.is_halted && (
        <div className="bg-rose-950/60 border border-rose-800 rounded-xl px-4 py-3 flex items-center gap-3 text-sm font-semibold text-rose-300">
          🛑 Trading HALTED — daily loss limit reached. Resume from SEBI tab.
        </div>
      )}

      {/* P&L + Positions */}
      <div className="grid grid-cols-2 gap-3">
        <StatCard label="Daily P&L">
          <div className={`text-2xl font-bold font-mono ${riskStatus?.daily_pnl >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
            {riskStatus ? (riskStatus.daily_pnl >= 0 ? '+' : '') + fmt(riskStatus.daily_pnl) : '—'}
          </div>
          <div className="text-[11px] text-slate-600 mt-1">Limit: {riskStatus ? fmt(riskStatus.max_daily_loss) : '—'}</div>
        </StatCard>

        <StatCard label="Open Positions">
          <div className="text-2xl font-bold font-mono text-slate-100">
            {riskStatus ? `${riskStatus.open_positions} / ${riskStatus.max_open_positions}` : '—'}
          </div>
          <div className="mt-2 h-1.5 rounded-full bg-slate-700 overflow-hidden">
            <div className="h-full rounded-full transition-all" style={{ width: `${posUsed}%`, background: posUsed > 80 ? '#f43f5e' : '#6366f1' }} />
          </div>
        </StatCard>
      </div>

      {/* Loss limit gauge */}
      <StatCard label="Loss Limit Usage">
        <div className="flex items-center gap-4">
          <div className="w-28 h-14 shrink-0">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie data={gaugeData} dataKey="v" startAngle={180} endAngle={0}
                  innerRadius="60%" outerRadius="85%" strokeWidth={0}>
                  {gaugeData.map((d, i) => <Cell key={i} fill={d.fill} />)}
                </Pie>
              </PieChart>
            </ResponsiveContainer>
          </div>
          <div>
            <div className="text-3xl font-bold font-mono" style={{ color: limitColor }}>{lossUsed.toFixed(0)}%</div>
            <div className="text-[11px] text-slate-500 mt-0.5">
              {riskStatus ? `${fmt(Math.abs(Math.min(0, riskStatus.daily_pnl)))} of ${fmt(riskStatus.max_daily_loss)} used` : '—'}
            </div>
            {lossUsed > 80 && <div className="text-[11px] text-rose-400 font-medium mt-1">⚠ Approaching limit</div>}
          </div>
          <div className="ml-auto text-right">
            <div className="text-xs text-slate-500">Trades Today</div>
            <div className="text-xl font-bold font-mono text-slate-100">{riskStatus?.trades_today ?? '—'}</div>
          </div>
        </div>
      </StatCard>

      {/* Market Regime */}
      {regime && (
        <StatCard label="Market Regime">
          <div className="flex items-center justify-between">
            <div>
              <span className={`text-base font-bold font-mono ${REGIME_COLOR[regime.regime] ?? 'text-slate-300'}`}>
                {regime.regime ?? '—'}
              </span>
              {regime.sub_regime && <span className="text-xs text-slate-500 ml-2">{regime.sub_regime}</span>}
            </div>
            <div className="text-right">
              <div className="text-xs text-slate-500">Confidence</div>
              <div className="text-sm font-mono text-slate-300">{regime.confidence ? `${(regime.confidence * 100).toFixed(0)}%` : '—'}</div>
            </div>
          </div>
          {regime.drivers && (
            <div className="mt-2 flex flex-wrap gap-1">
              {Object.entries(regime.drivers as Record<string, string>).slice(0, 4).map(([k, v]) => (
                <span key={k} className="text-[10px] bg-slate-700 text-slate-400 px-1.5 py-0.5 rounded font-mono">{k}: {v}</span>
              ))}
            </div>
          )}
        </StatCard>
      )}

      {/* Capital Allocator */}
      {capital && (
        <StatCard label="Adaptive Capital">
          <div className="space-y-1.5">
            {capital.buckets && Object.entries(capital.buckets as Record<string, any>).map(([k, v]: [string, any]) => (
              <div key={k} className="flex items-center gap-2">
                <span className="text-[11px] text-slate-400 w-20 capitalize">{k}</span>
                <div className="flex-1 h-1.5 bg-slate-700 rounded-full overflow-hidden">
                  <div className="h-full bg-emerald-500 rounded-full transition-all"
                    style={{ width: `${Math.min(100, (v.weight ?? 0) * 100)}%` }} />
                </div>
                <span className="text-[11px] font-mono text-slate-300 w-10 text-right">
                  {v.weight ? `${(v.weight * 100).toFixed(0)}%` : '—'}
                </span>
                {v.sharpe !== undefined && (
                  <span className={`text-[10px] font-mono w-14 text-right ${v.sharpe >= 0 ? 'text-emerald-400' : 'text-rose-400'}`}>
                    Sr {v.sharpe?.toFixed(2) ?? '—'}
                  </span>
                )}
              </div>
            ))}
          </div>
        </StatCard>
      )}

      {/* Connections */}
      {connections && (
        <StatCard label="Live Connections">
          <div className="space-y-1">
            {[
              { label: 'Zerodha Kite', ok: connections.kite?.connected, sub: connections.kite?.account_id },
              { label: 'TrueData',     ok: connections.truedata?.connected, sub: connections.truedata?.active ? 'active feed' : connections.truedata?.configured ? 'configured' : 'not set' },
              { label: 'Claude AI',    ok: connections.claude?.available, sub: connections.claude?.available ? 'API key set' : 'not configured' },
              ...((connections.secondary ?? []) as any[]).map((b: any) => ({
                label: b.name, ok: b.connected, sub: 'secondary broker',
              })),
            ].map(c => (
              <div key={c.label} className="flex items-center gap-2">
                <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${c.ok ? 'bg-emerald-400' : 'bg-slate-600'}`} />
                <span className="text-xs text-slate-400 flex-1">{c.label}</span>
                <span className="text-[11px] text-slate-600">{c.sub ?? ''}</span>
              </div>
            ))}
          </div>
        </StatCard>
      )}

      {/* Reconciler */}
      {reconciler && (
        <StatCard label="Position Reconciler">
          <div className="space-y-1">
            <Row label="Drift events" value={reconciler.drift_events ?? 0} />
            <Row label="Last run" value={reconciler.last_run_ago ?? '—'} />
            <Row label="Status" value={
              <span className={reconciler.ok ? 'text-emerald-400' : 'text-rose-400'}>
                {reconciler.ok ? 'OK' : 'DRIFT DETECTED'}
              </span>
            } />
          </div>
        </StatCard>
      )}
    </div>
  )
}
