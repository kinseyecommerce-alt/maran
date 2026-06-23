import React, { useEffect, useState, useCallback } from 'react'
import { Cpu, WifiOff, Settings, Zap, ZapOff, Eye, EyeOff, ChevronRight, X, ExternalLink, RefreshCw } from 'lucide-react'
import { clsx } from 'clsx'
import { useStore } from '../../store'
import { api } from '../../api/client'

// ─── Primitives ──────────────────────────────────────────────────────────────

function DarkInput({ type = 'text', value, onChange, placeholder, className = '', readOnly = false }: {
  type?: string; value: string; onChange?: (e: React.ChangeEvent<HTMLInputElement>) => void
  placeholder?: string; className?: string; readOnly?: boolean
}) {
  return (
    <input
      type={type} value={value} onChange={onChange} placeholder={placeholder}
      readOnly={readOnly}
      className={`w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-slate-200 text-xs placeholder:text-slate-600 focus:outline-none focus:ring-1 focus:ring-emerald-500 focus:border-emerald-500 font-mono transition-all ${readOnly ? 'opacity-50 cursor-default' : ''} ${className}`}
    />
  )
}

function DarkBtn({ children, onClick, variant = 'default', disabled = false, className = '' }: {
  children: React.ReactNode; onClick?: () => void
  variant?: 'default' | 'danger' | 'buy' | 'outline'; disabled?: boolean; className?: string
}) {
  const base = 'inline-flex items-center justify-center px-3 py-1.5 rounded-lg text-xs font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed'
  const variants = {
    default: 'bg-emerald-600 hover:bg-emerald-500 text-white',
    buy:     'bg-emerald-600 hover:bg-emerald-500 text-white',
    danger:  'bg-rose-600 hover:bg-rose-500 text-white',
    outline: 'bg-transparent border border-slate-600 text-slate-300 hover:bg-slate-800',
  }
  return (
    <button className={`${base} ${variants[variant]} ${className}`} onClick={onClick} disabled={disabled}>
      {children}
    </button>
  )
}

function StatusBadge({ children, variant }: { children: React.ReactNode; variant: 'live' | 'paper' | 'ok' | 'warn' }) {
  const styles = {
    live:  'bg-rose-500/20 text-rose-400 border border-rose-500/30',
    paper: 'bg-amber-500/20 text-amber-400 border border-amber-500/30',
    ok:    'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30',
    warn:  'bg-slate-700 text-slate-400 border border-slate-600',
  }
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-[10px] font-bold tracking-wider font-mono ${styles[variant]}`}>
      {children}
    </span>
  )
}

// ─── Broker definitions ───────────────────────────────────────────────────────

type BrokerStatus = 'connected' | 'error' | 'disconnected'

const STATUS_META: Record<BrokerStatus, { label: string; dot: string; pill: string }> = {
  connected:    { label: 'Connected',   dot: 'bg-emerald-400 animate-pulse', pill: 'text-emerald-400 bg-emerald-950 border-emerald-800' },
  error:        { label: 'Auth Error',  dot: 'bg-rose-500',                  pill: 'text-rose-400 bg-rose-950 border-rose-800' },
  disconnected: { label: 'Disconnected',dot: 'bg-slate-500',                 pill: 'text-slate-500 bg-slate-800 border-slate-700' },
}

interface BrokerState { status: BrokerStatus; loading: boolean }

const REAL_BROKERS = [
  { id: 'zerodha', name: 'Zerodha Kite',   logo: 'Z', color: '#387ed1' },
  { id: 'upstox',  name: 'Upstox',          logo: 'U', color: '#6e45e2' },
  { id: 'kotak',   name: 'Kotak Neo',        logo: 'K', color: '#e63946' },
]

const COMING_BROKERS = [
  { id: 'angelone', name: 'Angel One',       logo: 'A', color: '#f97316' },
  { id: 'fyers',    name: 'Fyers',            logo: 'F', color: '#22c55e' },
  { id: '5paisa',   name: '5Paisa',           logo: '5', color: '#ec4899' },
]

const NAV_ITEMS = [
  { id: 'brokers',  label: 'Brokers',       icon: '⚡' },
  { id: 'apikeys',  label: 'API Keys',       icon: '🔑' },
  { id: 'trading',  label: 'Trading Config', icon: '⚙️' },
  { id: 'risk',     label: 'Risk Limits',    icon: '🛡️' },
  { id: 'security', label: 'App Login',      icon: '🔒' },
]

// ─── Shared layout helpers ────────────────────────────────────────────────────

function SectionLabel({ children }: { children: React.ReactNode }) {
  return <p className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">{children}</p>
}

function FieldRow({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[160px_1fr] items-start gap-3">
      <div className="pt-2">
        <p className="text-xs font-medium text-slate-400">{label}</p>
        {hint && <p className="text-[11px] text-slate-600 mt-0.5 leading-snug">{hint}</p>}
      </div>
      <div>{children}</div>
    </div>
  )
}

function SecretField({ label, hint, fieldKey, form, setForm, vis, setVis, credStatus }: {
  label: string; hint?: string; fieldKey: string
  form: Record<string, string>; setForm: (fn: (p: any) => any) => void
  vis: Record<string, boolean>; setVis: (fn: (p: any) => any) => void
  credStatus?: Record<string, boolean>
}) {
  return (
    <FieldRow label={label} hint={hint}>
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <DarkInput
            type={vis[fieldKey] ? 'text' : 'password'}
            value={form[fieldKey] || ''}
            onChange={e => setForm(p => ({ ...p, [fieldKey]: e.target.value }))}
            placeholder="Leave blank to keep current"
            className="pr-9"
          />
          <button type="button"
            onClick={() => setVis(p => ({ ...p, [fieldKey]: !p[fieldKey] }))}
            className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
          >
            {vis[fieldKey] ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
          </button>
        </div>
        {credStatus && (
          <StatusBadge variant={credStatus[fieldKey] ? 'ok' : 'warn'}>
            {credStatus[fieldKey] ? 'Set ✓' : 'Not set'}
          </StatusBadge>
        )}
      </div>
    </FieldRow>
  )
}

function BrokerDetailHeader({ brokerId, brokerName, color, logo, status, onBack }: {
  brokerId: string; brokerName: string; color: string; logo: string
  status: BrokerStatus; onBack: () => void
}) {
  const m = STATUS_META[status]
  return (
    <div className="flex items-center gap-3">
      <button onClick={onBack} className="text-xs text-slate-500 hover:text-slate-300 transition-colors">← Back</button>
      <div className="w-8 h-8 rounded-lg flex items-center justify-center text-white font-bold text-sm shrink-0" style={{ background: color }}>{logo}</div>
      <div>
        <p className="text-sm font-semibold text-slate-100">{brokerName}</p>
        <div className="flex items-center gap-1.5 mt-0.5">
          <span className={`w-1.5 h-1.5 rounded-full ${m.dot}`} />
          <span className="text-[11px] text-slate-400">{m.label}</span>
        </div>
      </div>
    </div>
  )
}

function SaveBar({ onCancel, onSave, saving, label = 'Save & Reconnect' }: {
  onCancel: () => void; onSave: () => void; saving: boolean; label?: string
}) {
  return (
    <div className="flex items-center justify-between pt-3 border-t border-slate-800">
      <button onClick={onCancel} className="text-xs text-slate-500 hover:text-slate-300 transition-colors">Cancel</button>
      <button onClick={onSave} disabled={saving}
        className="text-xs px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors disabled:opacity-50"
      >
        {saving ? 'Saving…' : label}
      </button>
    </div>
  )
}

// ─── Zerodha detail ───────────────────────────────────────────────────────────

function ZerodhaDetail({ onBack, tempBase, setTempBase, tempKey, setTempKey, credForm, setCredForm,
  credStatus, credSaving, onSave, vis, setVis, brokerStatus }: {
  onBack: () => void; brokerStatus: BrokerStatus
  tempBase: string; setTempBase: (v: string) => void
  tempKey: string; setTempKey: (v: string) => void
  credForm: Record<string, string>; setCredForm: (fn: (p: any) => any) => void
  credStatus: Record<string, boolean>; credSaving: boolean; onSave: () => void
  vis: Record<string, boolean>; setVis: (fn: (p: any) => any) => void
}) {
  const connectKite = async () => {
    try {
      const r = await api.kiteStatus()
      if (!r.data.login_url) { window.open('/auth/kite/login', '_blank'); return }
      window.open(r.data.login_url, '_blank')
    } catch { window.open(tempBase + '/auth/kite/login', '_blank') }
  }

  return (
    <div className="space-y-5">
      <BrokerDetailHeader brokerId="zerodha" brokerName="Zerodha Kite" color="#387ed1" logo="Z" status={brokerStatus} onBack={onBack} />

      {brokerStatus === 'error' && (
        <div className="bg-rose-950/40 border border-rose-900/60 rounded-lg px-4 py-3 text-xs text-rose-300">
          ⚠️ Not connected — verify credentials and backend URL below.
        </div>
      )}

      <div className="space-y-3">
        <SectionLabel>AlgoPro Backend</SectionLabel>
        <FieldRow label="Backend URL" hint="FastAPI server URL">
          <DarkInput value={tempBase} onChange={e => setTempBase(e.target.value)} placeholder="http://localhost:8000" />
        </FieldRow>
        <FieldRow label="Backend API Key" hint="X-API-Key header">
          <div className="relative">
            <DarkInput type={vis['_api_key'] ? 'text' : 'password'} value={tempKey}
              onChange={e => setTempKey(e.target.value)} placeholder="Leave empty if not set" className="pr-9" />
            <button type="button" onClick={() => setVis(p => ({ ...p, _api_key: !p._api_key }))}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300">
              {vis['_api_key'] ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            </button>
          </div>
        </FieldRow>
      </div>

      <div className="space-y-3">
        <SectionLabel>Kite API Credentials</SectionLabel>
        <SecretField label="Kite API Key" fieldKey="kite_api_key" form={credForm} setForm={setCredForm} vis={vis} setVis={setVis} credStatus={credStatus} />
        <SecretField label="Kite API Secret" fieldKey="kite_api_secret" form={credForm} setForm={setCredForm} vis={vis} setVis={setVis} credStatus={credStatus} />
      </div>

      <FieldRow label="OAuth Login" hint="Complete the Kite session via browser">
        <button onClick={connectKite}
          className="flex items-center gap-1.5 text-xs px-3 py-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800 transition-colors">
          <ExternalLink className="w-3 h-3" /> Connect Kite Account
        </button>
      </FieldRow>

      <SaveBar onCancel={onBack} onSave={onSave} saving={credSaving} />
    </div>
  )
}

// ─── Upstox detail ────────────────────────────────────────────────────────────

function UpstoxDetail({ onBack, credForm, setCredForm, credStatus, credSaving, onSave, vis, setVis, brokerStatus, addToast }: {
  onBack: () => void; brokerStatus: BrokerStatus
  credForm: Record<string, string>; setCredForm: (fn: (p: any) => any) => void
  credStatus: Record<string, boolean>; credSaving: boolean; onSave: () => void
  vis: Record<string, boolean>; setVis: (fn: (p: any) => any) => void
  addToast: (msg: string, type?: any) => void
}) {
  const [tokenInput, setTokenInput] = useState('')
  const [tokenSaving, setTokenSaving] = useState(false)

  const handleOAuth = async () => {
    try {
      const r = await api.upstoxLoginUrl()
      if (r.data.login_url) window.open(r.data.login_url, '_blank')
      else addToast('Set Upstox API Key first', 'error')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Failed to get Upstox login URL', 'error')
    }
  }

  const handleSetToken = async () => {
    if (!tokenInput.trim()) return
    setTokenSaving(true)
    try {
      await api.upstoxSetToken(tokenInput.trim())
      setTokenInput('')
      addToast('Upstox token set', 'buy')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Failed to set Upstox token', 'error')
    } finally { setTokenSaving(false) }
  }

  return (
    <div className="space-y-5">
      <BrokerDetailHeader brokerId="upstox" brokerName="Upstox" color="#6e45e2" logo="U" status={brokerStatus} onBack={onBack} />

      <div className="space-y-3">
        <SectionLabel>API Credentials</SectionLabel>
        <SecretField label="API Key" fieldKey="upstox_api_key" form={credForm} setForm={setCredForm} vis={vis} setVis={setVis} credStatus={credStatus} />
        <SecretField label="API Secret" fieldKey="upstox_api_secret" form={credForm} setForm={setCredForm} vis={vis} setVis={setVis} credStatus={credStatus} />
        <FieldRow label="Redirect URL" hint="Must match Upstox developer portal">
          <DarkInput value={credForm.upstox_redirect_url || ''} onChange={e => setCredForm(p => ({ ...p, upstox_redirect_url: e.target.value }))}
            placeholder="https://yourapp.com/auth/upstox/callback" />
        </FieldRow>
      </div>

      <SaveBar onCancel={onBack} onSave={onSave} saving={credSaving} label="Save Credentials" />

      <div className="space-y-3 pt-1">
        <SectionLabel>Authorize & Connect</SectionLabel>
        <FieldRow label="Step 1 — OAuth" hint="Opens Upstox login in browser">
          <button onClick={handleOAuth}
            className="flex items-center gap-1.5 text-xs px-3 py-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800 transition-colors">
            <ExternalLink className="w-3 h-3" /> Authorize with Upstox
          </button>
        </FieldRow>
        <FieldRow label="Step 2 — Token" hint="Paste access token from callback">
          <div className="flex gap-2">
            <div className="relative flex-1">
              <DarkInput type={vis['_upstox_token'] ? 'text' : 'password'} value={tokenInput}
                onChange={e => setTokenInput(e.target.value)} placeholder="Paste access token" className="pr-9" />
              <button type="button" onClick={() => setVis(p => ({ ...p, _upstox_token: !p._upstox_token }))}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300">
                {vis['_upstox_token'] ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
              </button>
            </div>
            <button onClick={handleSetToken} disabled={tokenSaving || !tokenInput.trim()}
              className="text-xs px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors disabled:opacity-50">
              {tokenSaving ? '…' : 'Set'}
            </button>
          </div>
        </FieldRow>
      </div>
    </div>
  )
}

// ─── Kotak detail ─────────────────────────────────────────────────────────────

function KotakDetail({ onBack, credForm, setCredForm, credStatus, credSaving, onSave, vis, setVis, brokerStatus, addToast }: {
  onBack: () => void; brokerStatus: BrokerStatus
  credForm: Record<string, string>; setCredForm: (fn: (p: any) => any) => void
  credStatus: Record<string, boolean>; credSaving: boolean; onSave: () => void
  vis: Record<string, boolean>; setVis: (fn: (p: any) => any) => void
  addToast: (msg: string, type?: any) => void
}) {
  const [otpSent, setOtpSent] = useState(false)
  const [otpInput, setOtpInput] = useState('')
  const [otpBusy, setOtpBusy] = useState(false)
  const [tokenInput, setTokenInput] = useState('')
  const [sidInput, setSidInput] = useState('')
  const [tokenSaving, setTokenSaving] = useState(false)

  const handleSendOtp = async () => {
    setOtpBusy(true)
    try {
      await api.kotakSendOtp()
      setOtpSent(true)
      addToast('OTP sent to registered mobile', 'info')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'OTP send failed — set Consumer Key & Mobile first', 'error')
    } finally { setOtpBusy(false) }
  }

  const handleVerifyOtp = async () => {
    if (!otpInput.trim()) return
    setOtpBusy(true)
    try {
      await api.kotakVerifyOtp(otpInput.trim(), credForm.kotak_password || '')
      setOtpInput('')
      addToast('Kotak OTP verified — session active', 'buy')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'OTP verification failed', 'error')
    } finally { setOtpBusy(false) }
  }

  const handleSetToken = async () => {
    if (!tokenInput.trim() || !sidInput.trim()) return
    setTokenSaving(true)
    try {
      await api.kotakSetToken(tokenInput.trim(), sidInput.trim())
      setTokenInput(''); setSidInput('')
      addToast('Kotak token set', 'buy')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Failed to set Kotak token', 'error')
    } finally { setTokenSaving(false) }
  }

  return (
    <div className="space-y-5">
      <BrokerDetailHeader brokerId="kotak" brokerName="Kotak Neo" color="#e63946" logo="K" status={brokerStatus} onBack={onBack} />

      <div className="space-y-3">
        <SectionLabel>API Credentials</SectionLabel>
        <SecretField label="Consumer Key" fieldKey="kotak_consumer_key" form={credForm} setForm={setCredForm} vis={vis} setVis={setVis} credStatus={credStatus} />
        <SecretField label="Consumer Secret" fieldKey="kotak_consumer_secret" form={credForm} setForm={setCredForm} vis={vis} setVis={setVis} credStatus={credStatus} />
        <FieldRow label="Registered Mobile" hint="e.g. +919876543210">
          <div className="flex items-center gap-2">
            <DarkInput value={credForm.kotak_mobile_number || ''} onChange={e => setCredForm(p => ({ ...p, kotak_mobile_number: e.target.value }))}
              placeholder="+91..." />
            <StatusBadge variant={credStatus.kotak_mobile_number ? 'ok' : 'warn'}>
              {credStatus.kotak_mobile_number ? 'Set ✓' : 'Not set'}
            </StatusBadge>
          </div>
        </FieldRow>
        <SecretField label="Login Password" hint="Used to trigger OTP" fieldKey="kotak_password" form={credForm} setForm={setCredForm} vis={vis} setVis={setVis} />
      </div>

      <SaveBar onCancel={onBack} onSave={onSave} saving={credSaving} label="Save Credentials" />

      <div className="space-y-3 pt-1">
        <SectionLabel>OTP Authentication</SectionLabel>
        <FieldRow label="Step 1 — Send OTP" hint="Sends SMS to registered mobile">
          <div className="flex gap-2">
            <button onClick={handleSendOtp} disabled={otpBusy}
              className="flex items-center gap-1.5 text-xs px-3 py-2 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800 transition-colors disabled:opacity-50">
              <RefreshCw className={`w-3 h-3 ${otpBusy ? 'animate-spin' : ''}`} /> {otpSent ? 'Resend OTP' : 'Send OTP'}
            </button>
            {otpSent && <span className="text-[11px] text-emerald-400 self-center">OTP sent ✓</span>}
          </div>
        </FieldRow>
        {otpSent && (
          <FieldRow label="Step 2 — Enter OTP">
            <div className="flex gap-2">
              <DarkInput value={otpInput} onChange={e => setOtpInput(e.target.value)} placeholder="6-digit OTP" />
              <button onClick={handleVerifyOtp} disabled={otpBusy || !otpInput.trim()}
                className="text-xs px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors disabled:opacity-50">
                {otpBusy ? '…' : 'Verify'}
              </button>
            </div>
          </FieldRow>
        )}
      </div>

      <div className="space-y-3 pt-1">
        <SectionLabel>Or — Set Token Manually</SectionLabel>
        <FieldRow label="Access Token">
          <div className="relative">
            <DarkInput type={vis['_kotak_token'] ? 'text' : 'password'} value={tokenInput}
              onChange={e => setTokenInput(e.target.value)} placeholder="Paste access token" className="pr-9" />
            <button type="button" onClick={() => setVis(p => ({ ...p, _kotak_token: !p._kotak_token }))}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300">
              {vis['_kotak_token'] ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            </button>
          </div>
        </FieldRow>
        <FieldRow label="Session ID (SID)">
          <div className="flex gap-2">
            <DarkInput value={sidInput} onChange={e => setSidInput(e.target.value)} placeholder="SID from login response" />
            <button onClick={handleSetToken} disabled={tokenSaving || !tokenInput.trim() || !sidInput.trim()}
              className="text-xs px-3 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors disabled:opacity-50">
              {tokenSaving ? '…' : 'Set'}
            </button>
          </div>
        </FieldRow>
      </div>
    </div>
  )
}

// ─── Brokers panel ────────────────────────────────────────────────────────────

function BrokersPanel(props: {
  wsConnected: boolean
  credStatus: Record<string, boolean>
  brokerStatuses: Record<string, BrokerStatus>
  tempBase: string; setTempBase: (v: string) => void
  tempKey: string; setTempKey: (v: string) => void
  credForm: Record<string, string>; setCredForm: (fn: (p: any) => any) => void
  credSaving: boolean
  handleSaveZerodha: () => void
  handleSaveUpstox: () => void
  handleSaveKotak: () => void
  vis: Record<string, boolean>; setVis: (fn: (p: any) => any) => void
  activeBroker: string
  addToast: (msg: string, type?: any) => void
}) {
  const [selected, setSelected] = useState<string | null>(null)

  const bStatus = (id: string): BrokerStatus => {
    if (id in props.brokerStatuses) return props.brokerStatuses[id]
    if (id === 'zerodha') return props.wsConnected ? 'connected' : 'disconnected'
    return 'disconnected'
  }

  if (selected === 'zerodha') return (
    <ZerodhaDetail onBack={() => setSelected(null)} brokerStatus={bStatus('zerodha')}
      tempBase={props.tempBase} setTempBase={props.setTempBase}
      tempKey={props.tempKey} setTempKey={props.setTempKey}
      credForm={props.credForm} setCredForm={props.setCredForm}
      credStatus={props.credStatus} credSaving={props.credSaving}
      onSave={props.handleSaveZerodha} vis={props.vis} setVis={props.setVis} />
  )
  if (selected === 'upstox') return (
    <UpstoxDetail onBack={() => setSelected(null)} brokerStatus={bStatus('upstox')}
      credForm={props.credForm} setCredForm={props.setCredForm}
      credStatus={props.credStatus} credSaving={props.credSaving}
      onSave={props.handleSaveUpstox} vis={props.vis} setVis={props.setVis}
      addToast={props.addToast} />
  )
  if (selected === 'kotak') return (
    <KotakDetail onBack={() => setSelected(null)} brokerStatus={bStatus('kotak')}
      credForm={props.credForm} setCredForm={props.setCredForm}
      credStatus={props.credStatus} credSaving={props.credSaving}
      onSave={props.handleSaveKotak} vis={props.vis} setVis={props.setVis}
      addToast={props.addToast} />
  )

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-slate-100 mb-0.5">Brokers</h3>
        <p className="text-xs text-slate-500">
          Active: <span className="text-slate-300 font-mono">{props.activeBroker || 'zerodha'}</span>
          &nbsp;· Configure credentials for each broker below.
        </p>
      </div>

      <div className="space-y-2">
        <SectionLabel>Available Brokers</SectionLabel>
        {REAL_BROKERS.map(b => {
          const st = bStatus(b.id)
          const m = STATUS_META[st]
          const isActive = (props.activeBroker || 'zerodha') === b.id
          return (
            <button key={b.id} onClick={() => setSelected(b.id)}
              className="w-full flex items-center gap-3 bg-slate-900 hover:bg-slate-800/80 border border-slate-800 hover:border-slate-700 rounded-xl px-4 py-3 transition-all text-left group"
            >
              <div className="w-9 h-9 rounded-lg flex items-center justify-center text-white font-bold text-sm shrink-0" style={{ background: b.color }}>
                {b.logo}
              </div>
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <p className="text-xs font-semibold text-slate-200">{b.name}</p>
                  {isActive && (
                    <span className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-emerald-950 text-emerald-400 border border-emerald-900">ACTIVE</span>
                  )}
                </div>
                <div className="flex items-center gap-1.5 mt-0.5">
                  <span className={`w-1.5 h-1.5 rounded-full ${m.dot}`} />
                  <span className="text-[11px] text-slate-500">{m.label}</span>
                </div>
              </div>
              <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${m.pill}`}>{m.label}</span>
              <ChevronRight className="w-3.5 h-3.5 text-slate-700 group-hover:text-slate-400 transition-colors ml-1" />
            </button>
          )
        })}
      </div>

      <div className="space-y-2">
        <SectionLabel>Coming Soon</SectionLabel>
        <div className="grid grid-cols-3 gap-2">
          {COMING_BROKERS.map(b => (
            <div key={b.id}
              className="flex flex-col items-center gap-1.5 bg-slate-900/40 border border-dashed border-slate-800 rounded-xl py-3 px-2 opacity-50 cursor-not-allowed"
            >
              <div className="w-7 h-7 rounded-md flex items-center justify-center font-bold text-xs"
                style={{ background: b.color + '22', color: b.color }}>
                {b.logo}
              </div>
              <span className="text-[10px] text-slate-500 text-center leading-tight">{b.name}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

// ─── Other panels ─────────────────────────────────────────────────────────────

function ApiKeysPanel({ credForm, setCredForm, credStatus, credSaving, onSave, vis, setVis }: {
  credForm: Record<string, string>; setCredForm: (fn: (p: any) => any) => void
  credStatus: Record<string, boolean>; credSaving: boolean; onSave: () => void
  vis: Record<string, boolean>; setVis: (fn: (p: any) => any) => void
}) {
  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-slate-100 mb-0.5">API Keys</h3>
        <p className="text-xs text-slate-500">Third-party service credentials — restart reverts to env vars.</p>
      </div>
      <div className="space-y-3">
        <SectionLabel>Claude AI (Anthropic)</SectionLabel>
        <SecretField label="Anthropic API Key" fieldKey="anthropic_api_key" form={credForm} setForm={setCredForm} vis={vis} setVis={setVis} credStatus={credStatus} />
      </div>
      <div className="space-y-3">
        <SectionLabel>TrueData</SectionLabel>
        <FieldRow label="Username">
          <div className="flex items-center gap-2">
            <DarkInput value={credForm.truedata_username || ''} onChange={e => setCredForm(p => ({ ...p, truedata_username: e.target.value }))} placeholder="Leave blank to keep current" />
            <StatusBadge variant={credStatus.truedata_username ? 'ok' : 'warn'}>{credStatus.truedata_username ? 'Set ✓' : 'Not set'}</StatusBadge>
          </div>
        </FieldRow>
        <SecretField label="Password" fieldKey="truedata_password" form={credForm} setForm={setCredForm} vis={vis} setVis={setVis} credStatus={credStatus} />
      </div>
      <div className="flex justify-end pt-2 border-t border-slate-800">
        <button onClick={onSave} disabled={credSaving}
          className="text-xs px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors disabled:opacity-50">
          {credSaving ? 'Saving…' : 'Save Credentials'}
        </button>
      </div>
    </div>
  )
}

function AppLoginPanel({ appForm, setAppForm, appSaving, onSave, vis, setVis }: {
  appForm: { username: string; new_password: string; confirm_password: string }
  setAppForm: (fn: (p: any) => any) => void; appSaving: boolean; onSave: () => void
  vis: Record<string, boolean>; setVis: (fn: (p: any) => any) => void
}) {
  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-slate-100 mb-0.5">App Login</h3>
        <p className="text-xs text-slate-500">Update the admin login for this AlgoPro instance.</p>
      </div>
      <div className="space-y-3">
        <FieldRow label="Admin Username">
          <DarkInput value={appForm.username} onChange={e => setAppForm(p => ({ ...p, username: e.target.value }))} placeholder="admin" />
        </FieldRow>
        <FieldRow label="New Password" hint="Min 8 characters">
          <div className="relative">
            <DarkInput type={vis._pw1 ? 'text' : 'password'} value={appForm.new_password}
              onChange={e => setAppForm(p => ({ ...p, new_password: e.target.value }))} placeholder="Enter new password" className="pr-9" />
            <button type="button" onClick={() => setVis(p => ({ ...p, _pw1: !p._pw1 }))}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300">
              {vis._pw1 ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            </button>
          </div>
        </FieldRow>
        <FieldRow label="Confirm Password">
          <div className="relative">
            <DarkInput type={vis._pw2 ? 'text' : 'password'} value={appForm.confirm_password}
              onChange={e => setAppForm(p => ({ ...p, confirm_password: e.target.value }))} placeholder="Re-enter password" className="pr-9" />
            <button type="button" onClick={() => setVis(p => ({ ...p, _pw2: !p._pw2 }))}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300">
              {vis._pw2 ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            </button>
          </div>
        </FieldRow>
      </div>
      <p className="text-[11px] text-slate-600">In-memory only — restart reverts to environment variables.</p>
      <div className="flex justify-end pt-2 border-t border-slate-800">
        <button onClick={onSave} disabled={appSaving}
          className="text-xs px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors disabled:opacity-50">
          {appSaving ? 'Saving…' : 'Update Login'}
        </button>
      </div>
    </div>
  )
}

function ComingSoonPanel({ label, icon }: { label: string; icon: string }) {
  return (
    <div className="flex flex-col items-center justify-center h-48 gap-3">
      <span className="text-3xl opacity-20">{icon}</span>
      <p className="text-sm text-slate-600">{label} coming soon</p>
    </div>
  )
}

// ─── Main Header ──────────────────────────────────────────────────────────────

export default function Header() {
  const { health, botStatus, wsConnected, apiKey, apiBase, setApiKey, setApiBase, setHealth, setBotStatus, addToast } = useStore()

  const [time, setTime]         = useState(new Date())
  const [configOpen, setConfigOpen] = useState(false)
  const [activeNav, setActiveNav]   = useState('brokers')

  const [tempKey, setTempKey]   = useState(apiKey)
  const [tempBase, setTempBase] = useState(apiBase)

  const [credForm, setCredForm] = useState<Record<string, string>>({
    kite_api_key: '', kite_api_secret: '',
    upstox_api_key: '', upstox_api_secret: '', upstox_redirect_url: '',
    kotak_consumer_key: '', kotak_consumer_secret: '', kotak_mobile_number: '', kotak_password: '',
    anthropic_api_key: '',
    truedata_username: '', truedata_password: '',
  })
  const [vis, setVis]       = useState<Record<string, boolean>>({})
  const [credStatus, setCredStatus]   = useState<Record<string, boolean>>({})
  const [credSaving, setCredSaving]   = useState(false)
  const [brokerStatuses, setBrokerStatuses] = useState<Record<string, BrokerStatus>>({})
  const [activeBroker, setActiveBroker]     = useState('zerodha')

  const [appForm, setAppForm]   = useState({ username: '', new_password: '', confirm_password: '' })
  const [appSaving, setAppSaving] = useState(false)
  const [botLoading, setBotLoading] = useState(false)

  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    const poll = () => {
      api.health().then(r => setHealth(r.data)).catch(() => {})
      api.botStatus().then(r => setBotStatus(r.data)).catch(() => {})
    }
    poll()
    const t = setInterval(poll, 5000)
    return () => clearInterval(t)
  }, [])

  useEffect(() => {
    if (!configOpen) return
    // Load credential set/unset status
    api.configValidate().then(r => {
      const d = r.data
      setCredStatus({
        kite_api_key:       d.kite_api_key        ?? false,
        kite_api_secret:    d.kite_api_secret      ?? false,
        anthropic_api_key:  d.anthropic_api_key    ?? false,
        truedata_username:  d.truedata_username    ?? false,
        truedata_password:  d.truedata_password    ?? false,
        upstox_api_key:     d.upstox_api_key       ?? false,
        upstox_api_secret:  d.upstox_api_secret    ?? false,
        kotak_consumer_key:  d.kotak_consumer_key   ?? false,
        kotak_consumer_secret: d.kotak_consumer_secret ?? false,
        kotak_mobile_number: d.kotak_mobile_number  ?? false,
      })
      setAppForm(p => ({ ...p, username: d.admin_username ?? '' }))
    }).catch(() => {})
    // Load live broker connection statuses
    Promise.allSettled([
      api.kiteStatus(),
      api.upstoxStatus(),
      api.kotakStatus(),
      api.brokerStatus(),
    ]).then(([kite, upstox, kotak, bst]) => {
      const statuses: Record<string, BrokerStatus> = {}
      if (kite.status === 'fulfilled')
        statuses.zerodha = kite.value.data.connected ? 'connected' : (kite.value.data.message?.includes('No valid') ? 'disconnected' : 'error')
      if (upstox.status === 'fulfilled')
        statuses.upstox = upstox.value.data.connected ? 'connected' : 'disconnected'
      if (kotak.status === 'fulfilled')
        statuses.kotak = kotak.value.data.connected ? 'connected' : 'disconnected'
      if (bst.status === 'fulfilled')
        setActiveBroker(bst.value.data.active_broker ?? 'zerodha')
      setBrokerStatuses(statuses)
    })
  }, [configOpen])

  const handleBotToggle = useCallback(async () => {
    setBotLoading(true)
    try {
      if (botStatus?.master_running) {
        await api.botStop()
        addToast('Bot stopped', 'info')
        setBotStatus(null)
      } else {
        const r = await api.botStart(['intraday', 'scalping'])
        addToast(`Bot started — ${r.data.watchlist?.length || 0} symbols`, 'buy')
        setBotStatus(r.data)
      }
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Bot toggle failed', 'error')
    } finally { setBotLoading(false) }
  }, [botStatus])

  const saveBrokerCreds = async (fields: string[], successMsg: string) => {
    const payload: Record<string, string> = {}
    fields.forEach(k => { if (credForm[k]?.trim()) payload[k] = credForm[k].trim() })
    if (!Object.keys(payload).length) { addToast('No changes to save', 'info'); return }
    setCredSaving(true)
    try {
      const r = await api.updateCredentials(payload)
      const c = r.data.credentials ?? {}
      setCredStatus(p => ({ ...p, ...Object.fromEntries(Object.keys(payload).map(k => [k, c[k] ?? true])) }))
      setCredForm(p => ({ ...p, ...Object.fromEntries(fields.map(k => [k, ''])) }))
      addToast(successMsg, 'buy')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Failed to save credentials', 'error')
    } finally { setCredSaving(false) }
  }

  const handleSaveZerodha = async () => {
    setApiKey(tempKey)
    setApiBase(tempBase)
    await saveBrokerCreds(['kite_api_key', 'kite_api_secret'], 'Zerodha credentials saved')
  }

  const handleSaveUpstox = async () => {
    await saveBrokerCreds(['upstox_api_key', 'upstox_api_secret', 'upstox_redirect_url'], 'Upstox credentials saved')
  }

  const handleSaveKotak = async () => {
    await saveBrokerCreds(['kotak_consumer_key', 'kotak_consumer_secret', 'kotak_mobile_number', 'kotak_password'], 'Kotak credentials saved')
  }

  const handleSaveApiKeys = async () => {
    await saveBrokerCreds(['anthropic_api_key', 'truedata_username', 'truedata_password'], 'API keys updated')
  }

  const handleSaveAppLogin = async () => {
    if (appForm.new_password.length < 8) { addToast('Password must be ≥ 8 characters', 'error'); return }
    if (appForm.new_password !== appForm.confirm_password) { addToast('Passwords do not match', 'error'); return }
    const payload: { username?: string; new_password: string } = { new_password: appForm.new_password }
    if (appForm.username.trim()) payload.username = appForm.username.trim()
    setAppSaving(true)
    try {
      const r = await api.updateAppPassword(payload)
      setAppForm(p => ({ ...p, username: r.data.admin_username, new_password: '', confirm_password: '' }))
      addToast('Login credentials updated', 'buy')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Failed to update login credentials', 'error')
    } finally { setAppSaving(false) }
  }

  const openSettings = () => {
    setTempKey(apiKey); setTempBase(apiBase)
    setActiveNav('brokers')
    setCredForm({
      kite_api_key: '', kite_api_secret: '',
      upstox_api_key: '', upstox_api_secret: '', upstox_redirect_url: '',
      kotak_consumer_key: '', kotak_consumer_secret: '', kotak_mobile_number: '', kotak_password: '',
      anthropic_api_key: '',
      truedata_username: '', truedata_password: '',
    })
    setVis({})
    setAppForm({ username: '', new_password: '', confirm_password: '' })
    setConfigOpen(true)
  }

  const mode       = health?.mode || 'PAPER'
  const marketOpen = health?.market_open

  const connectedCount = Object.values(brokerStatuses).filter(s => s === 'connected').length

  return (
    <>
      {/* ── Header bar ───────────────────────────────────────────────────── */}
      <header className="h-12 bg-slate-900 border-b border-slate-800 flex items-center px-4 gap-4 shrink-0 z-30">
        <div className="flex items-center gap-2 min-w-max">
          <Cpu className="w-5 h-5 text-emerald-400" />
          <div className="font-bold tracking-widest text-base leading-none">
            <span className="text-emerald-400">ALGO</span><span className="text-white">PRO</span>
          </div>
          <span className="text-[10px] text-slate-500 font-mono ml-1">{health?.version || 'v4'}</span>
        </div>

        <div className="h-5 w-px bg-slate-700" />

        <div className={clsx(
          'flex items-center gap-1.5 px-2 py-1 rounded text-xs font-mono',
          wsConnected ? 'text-emerald-400 bg-emerald-400/10' : 'text-slate-500 bg-slate-800',
        )}>
          {wsConnected ? (
            <>
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75" />
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500" />
              </span>
              NSE {marketOpen ? 'OPEN' : 'CONNECTED'}
            </>
          ) : (
            <><WifiOff className="w-3 h-3" /> OFFLINE</>
          )}
        </div>

        <StatusBadge variant={mode === 'LIVE' ? 'live' : 'paper'}>{mode}</StatusBadge>

        {health?.ticker_source && (
          <span className="text-xs text-slate-500 font-mono hidden sm:block">
            TICKS: <span className="text-slate-300">{health.ticker_source}</span>
          </span>
        )}

        <div className="flex-1" />

        <DarkBtn variant={botStatus?.master_running ? 'danger' : 'buy'} onClick={handleBotToggle} disabled={botLoading}>
          {botStatus?.master_running
            ? <><ZapOff className="w-3.5 h-3.5 mr-1.5" />Stop Bot</>
            : <><Zap   className="w-3.5 h-3.5 mr-1.5" />Start Bot</>}
        </DarkBtn>

        <button onClick={openSettings}
          className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-500 hover:text-slate-300 transition-colors relative"
        >
          <Settings className="w-4 h-4" />
          {connectedCount > 0 && (
            <span className="absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full bg-emerald-400" />
          )}
        </button>
      </header>

      {/* ── Settings overlay ─────────────────────────────────────────────── */}
      {configOpen && (
        <div className="fixed inset-0 z-50 flex flex-col bg-slate-950 font-sans">
          <div className="flex items-center gap-2.5 px-5 py-3 border-b border-slate-800/60 shrink-0">
            <span className="text-emerald-400 font-bold tracking-widest text-xs">ALGOPRO</span>
            <span className="text-slate-700 text-sm">/</span>
            <span className="text-slate-400 text-xs">Settings</span>
            <button onClick={() => setConfigOpen(false)}
              className="ml-auto flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 border border-slate-800 hover:border-slate-700 px-2.5 py-1 rounded transition-colors">
              <X className="w-3 h-3" /> Close
            </button>
          </div>

          <div className="flex flex-1 overflow-hidden">
            <aside className="w-52 shrink-0 border-r border-slate-800/60 py-3 px-2 flex flex-col">
              <div className="space-y-0.5">
                {NAV_ITEMS.map(item => (
                  <button key={item.id} onClick={() => setActiveNav(item.id)}
                    className={clsx(
                      'w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-xs transition-all',
                      activeNav === item.id ? 'bg-slate-800 text-slate-100' : 'text-slate-500 hover:text-slate-300 hover:bg-slate-900',
                    )}
                  >
                    <span className="text-sm">{item.icon}</span>
                    <span className="flex-1 text-left">{item.label}</span>
                    {item.id === 'brokers' && connectedCount > 0 && (
                      <span className="text-[10px] bg-emerald-950 text-emerald-400 border border-emerald-900 px-1.5 py-0.5 rounded font-mono">
                        {connectedCount}
                      </span>
                    )}
                  </button>
                ))}
              </div>
              <div className="mt-auto mx-3 pt-4 border-t border-slate-800">
                <p className="text-[10px] text-slate-700">v{health?.version || '4.0.0'}</p>
                <p className="text-[10px] text-slate-700">Build 20260623</p>
              </div>
            </aside>

            <main className="flex-1 overflow-y-auto p-8">
              <div className="max-w-xl">
                {activeNav === 'brokers' && (
                  <BrokersPanel
                    wsConnected={wsConnected}
                    credStatus={credStatus}
                    brokerStatuses={brokerStatuses}
                    activeBroker={activeBroker}
                    tempBase={tempBase} setTempBase={setTempBase}
                    tempKey={tempKey}   setTempKey={setTempKey}
                    credForm={credForm} setCredForm={setCredForm}
                    credSaving={credSaving}
                    handleSaveZerodha={handleSaveZerodha}
                    handleSaveUpstox={handleSaveUpstox}
                    handleSaveKotak={handleSaveKotak}
                    vis={vis} setVis={setVis}
                    addToast={addToast}
                  />
                )}
                {activeNav === 'apikeys' && (
                  <ApiKeysPanel credForm={credForm} setCredForm={setCredForm}
                    credStatus={credStatus} credSaving={credSaving} onSave={handleSaveApiKeys}
                    vis={vis} setVis={setVis} />
                )}
                {activeNav === 'security' && (
                  <AppLoginPanel appForm={appForm} setAppForm={setAppForm}
                    appSaving={appSaving} onSave={handleSaveAppLogin}
                    vis={vis} setVis={setVis} />
                )}
                {activeNav === 'trading' && <ComingSoonPanel label="Trading Config" icon="⚙️" />}
                {activeNav === 'risk'    && <ComingSoonPanel label="Risk Limits" icon="🛡️" />}
              </div>
            </main>
          </div>
        </div>
      )}
    </>
  )
}
