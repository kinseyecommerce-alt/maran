import React, { useEffect, useState, useCallback } from 'react'
import { Cpu, WifiOff, Settings, Zap, ZapOff, Eye, EyeOff, ChevronRight, X } from 'lucide-react'
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
      type={type}
      value={value}
      onChange={onChange}
      placeholder={placeholder}
      readOnly={readOnly}
      className={`w-full px-3 py-2 bg-slate-900 border border-slate-700 rounded-lg text-slate-200 text-xs placeholder:text-slate-600 focus:outline-none focus:ring-1 focus:ring-emerald-500 focus:border-emerald-500 font-mono transition-all disabled:opacity-50 ${className}`}
    />
  )
}

function DarkBtn({ children, onClick, variant = 'default', disabled = false, className = '' }: {
  children: React.ReactNode; onClick?: () => void
  variant?: 'default' | 'danger' | 'buy' | 'outline'; disabled?: boolean; className?: string
}) {
  const base = 'inline-flex items-center justify-center px-3 py-1.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed'
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

// ─── Broker config ────────────────────────────────────────────────────────────

const BROKER_DEFS = [
  { id: 'zerodha',  name: 'Zerodha Kite',    logo: 'Z', color: '#387ed1', supported: true  },
  { id: 'upstox',   name: 'Upstox',           logo: 'U', color: '#6e45e2', supported: false },
  { id: 'angelone', name: 'Angel One',         logo: 'A', color: '#f97316', supported: false },
  { id: 'fyers',    name: 'Fyers',             logo: 'F', color: '#22c55e', supported: false },
  { id: 'iifl',     name: 'IIFL Securities',   logo: 'I', color: '#eab308', supported: false },
  { id: '5paisa',   name: '5Paisa',            logo: '5', color: '#ec4899', supported: false },
]

type BrokerStatus = 'connected' | 'error' | 'disconnected' | 'not_configured'

const STATUS_META: Record<BrokerStatus, { label: string; dot: string; pill: string }> = {
  connected:      { label: 'Connected',      dot: 'bg-emerald-400 animate-pulse', pill: 'text-emerald-400 bg-emerald-950 border-emerald-800' },
  error:          { label: 'Auth Error',      dot: 'bg-rose-500',                  pill: 'text-rose-400 bg-rose-950 border-rose-800' },
  disconnected:   { label: 'Disconnected',    dot: 'bg-amber-400',                 pill: 'text-amber-400 bg-amber-950 border-amber-800' },
  not_configured: { label: 'Not configured',  dot: 'bg-slate-600',                 pill: 'text-slate-500 bg-slate-800 border-slate-700' },
}

const NAV_ITEMS = [
  { id: 'brokers',  label: 'Brokers',        icon: '⚡' },
  { id: 'apikeys',  label: 'API Keys',        icon: '🔑' },
  { id: 'trading',  label: 'Trading Config',  icon: '⚙️' },
  { id: 'risk',     label: 'Risk Limits',     icon: '🛡️' },
  { id: 'security', label: 'App Login',       icon: '🔒' },
]

// ─── Field row ────────────────────────────────────────────────────────────────

function FieldRow({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="grid grid-cols-[168px_1fr] items-start gap-3">
      <div className="pt-2">
        <p className="text-xs font-medium text-slate-400">{label}</p>
        {hint && <p className="text-[11px] text-slate-600 mt-0.5 leading-snug">{hint}</p>}
      </div>
      <div>{children}</div>
    </div>
  )
}

// ─── Broker detail panel ──────────────────────────────────────────────────────

function ZerodhaDetail({ onBack, tempBase, setTempBase, tempKey, setTempKey, credForm, setCredForm,
  credStatus, credSaving, handleSaveZerodha, showFields, setShowFields, brokerStatus }: {
  onBack: () => void
  tempBase: string; setTempBase: (v: string) => void
  tempKey: string; setTempKey: (v: string) => void
  credForm: Record<string, string>; setCredForm: (f: any) => void
  credStatus: Record<string, boolean>; credSaving: boolean
  handleSaveZerodha: () => void
  showFields: Record<string, boolean>; setShowFields: (f: any) => void
  brokerStatus: BrokerStatus
}) {
  const m = STATUS_META[brokerStatus]
  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <button onClick={onBack} className="text-xs text-slate-500 hover:text-slate-300 transition-colors flex items-center gap-1">
          ← Back
        </button>
        <div className="w-8 h-8 rounded-lg flex items-center justify-center text-white font-bold text-sm" style={{ background: '#387ed1' }}>
          Z
        </div>
        <div>
          <p className="text-sm font-semibold text-slate-100">Zerodha Kite</p>
          <div className="flex items-center gap-1.5 mt-0.5">
            <span className={`w-1.5 h-1.5 rounded-full ${m.dot}`} />
            <span className="text-[11px] text-slate-400">{m.label}</span>
          </div>
        </div>
        <button
          onClick={handleSaveZerodha}
          className="ml-auto text-xs px-3 py-1.5 rounded-lg border border-slate-700 text-slate-300 hover:bg-slate-800 transition-colors"
        >
          Test Connection
        </button>
      </div>

      {brokerStatus === 'error' && (
        <div className="bg-rose-950/40 border border-rose-900/60 rounded-lg px-4 py-3 text-xs text-rose-300">
          ⚠️ Not connected — verify your credentials and backend URL below.
        </div>
      )}

      <div className="space-y-3">
        <p className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">AlgoPro Backend</p>

        <FieldRow label="Backend URL" hint="REST endpoint for your running AlgoPro backend">
          <DarkInput value={tempBase} onChange={e => setTempBase(e.target.value)} placeholder="http://localhost:8000" />
        </FieldRow>

        <FieldRow label="Backend API Key" hint="X-API-Key header sent to backend">
          <div className="relative">
            <DarkInput
              type={showFields['api_key'] ? 'text' : 'password'}
              value={tempKey}
              onChange={e => setTempKey(e.target.value)}
              placeholder="Leave empty if not set"
              className="pr-9"
            />
            <button type="button"
              onClick={() => setShowFields((p: any) => ({ ...p, api_key: !p.api_key }))}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
            >
              {showFields['api_key'] ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            </button>
          </div>
        </FieldRow>
      </div>

      <div className="space-y-3 pt-1">
        <p className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">Kite API Credentials</p>

        <FieldRow label="Kite API Key">
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <DarkInput
                type={showFields['kite_api_key'] ? 'text' : 'password'}
                value={credForm.kite_api_key}
                onChange={e => setCredForm((p: any) => ({ ...p, kite_api_key: e.target.value }))}
                placeholder="Leave blank to keep current"
                className="pr-9"
              />
              <button type="button"
                onClick={() => setShowFields((p: any) => ({ ...p, kite_api_key: !p.kite_api_key }))}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
              >
                {showFields['kite_api_key'] ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
              </button>
            </div>
            <StatusBadge variant={credStatus.kite_api_key ? 'ok' : 'warn'}>
              {credStatus.kite_api_key ? 'Set ✓' : 'Not set'}
            </StatusBadge>
          </div>
        </FieldRow>

        <FieldRow label="Kite API Secret">
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <DarkInput
                type={showFields['kite_api_secret'] ? 'text' : 'password'}
                value={credForm.kite_api_secret}
                onChange={e => setCredForm((p: any) => ({ ...p, kite_api_secret: e.target.value }))}
                placeholder="Leave blank to keep current"
                className="pr-9"
              />
              <button type="button"
                onClick={() => setShowFields((p: any) => ({ ...p, kite_api_secret: !p.kite_api_secret }))}
                className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
              >
                {showFields['kite_api_secret'] ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
              </button>
            </div>
            <StatusBadge variant={credStatus.kite_api_secret ? 'ok' : 'warn'}>
              {credStatus.kite_api_secret ? 'Set ✓' : 'Not set'}
            </StatusBadge>
          </div>
        </FieldRow>
      </div>

      <div className="flex items-center justify-between pt-2 border-t border-slate-800">
        <button onClick={onBack} className="text-xs text-slate-500 hover:text-slate-300 transition-colors">
          Cancel
        </button>
        <button
          onClick={handleSaveZerodha}
          disabled={credSaving}
          className="text-xs px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors disabled:opacity-50"
        >
          {credSaving ? 'Saving…' : 'Save & Reconnect'}
        </button>
      </div>
    </div>
  )
}

// ─── Panels ───────────────────────────────────────────────────────────────────

function BrokersPanel(props: {
  wsConnected: boolean; credStatus: Record<string, boolean>; tempBase: string; setTempBase: (v: string) => void
  tempKey: string; setTempKey: (v: string) => void; credForm: Record<string, string>; setCredForm: (f: any) => void
  credSaving: boolean; handleSaveZerodha: () => void; showFields: Record<string, boolean>; setShowFields: (f: any) => void
}) {
  const [selected, setSelected] = useState<string | null>(null)

  const zerodhaStatus: BrokerStatus = props.wsConnected
    ? 'connected'
    : (props.credStatus.kite_api_key && props.credStatus.kite_api_secret)
      ? 'error'
      : 'disconnected'

  const configured = BROKER_DEFS.filter(b => b.id === 'zerodha')
  const available  = BROKER_DEFS.filter(b => !b.supported)

  const brokerStatus = (id: string): BrokerStatus => {
    if (id === 'zerodha') return zerodhaStatus
    return 'not_configured'
  }

  if (selected === 'zerodha') {
    return (
      <ZerodhaDetail
        onBack={() => setSelected(null)}
        brokerStatus={zerodhaStatus}
        {...props}
      />
    )
  }

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-slate-100 mb-0.5">Brokers</h3>
        <p className="text-xs text-slate-500">Manage broker connections. Only one can be active at a time.</p>
      </div>

      <div className="space-y-2">
        <p className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">Configured</p>
        {configured.map(b => {
          const st = brokerStatus(b.id)
          const m = STATUS_META[st]
          return (
            <button key={b.id} onClick={() => setSelected(b.id)}
              className="w-full flex items-center gap-3 bg-slate-900 hover:bg-slate-800/80 border border-slate-800 hover:border-slate-700 rounded-xl px-4 py-3 transition-all text-left group"
            >
              <div className="w-9 h-9 rounded-lg flex items-center justify-center text-white font-bold text-sm shrink-0" style={{ background: b.color }}>
                {b.logo}
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-xs font-semibold text-slate-200">{b.name}</p>
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
        <p className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">Add a broker</p>
        <div className="grid grid-cols-3 gap-2">
          {available.map(b => (
            <button key={b.id}
              className="flex flex-col items-center gap-1.5 bg-slate-900/40 hover:bg-slate-900 border border-dashed border-slate-800 hover:border-slate-700 rounded-xl py-3 px-2 transition-all cursor-not-allowed opacity-60"
              title="Coming soon"
            >
              <div className="w-7 h-7 rounded-md flex items-center justify-center font-bold text-xs" style={{ background: b.color + '33', color: b.color }}>
                {b.logo}
              </div>
              <span className="text-[10px] text-slate-500 text-center leading-tight">{b.name}</span>
            </button>
          ))}
        </div>
        <p className="text-[10px] text-slate-700">Additional brokers coming in a future release.</p>
      </div>
    </div>
  )
}

function ApiKeysPanel({ credForm, setCredForm, credStatus, credSaving, handleSave, showFields, setShowFields }: {
  credForm: Record<string, string>; setCredForm: (f: any) => void; credStatus: Record<string, boolean>
  credSaving: boolean; handleSave: () => void; showFields: Record<string, boolean>; setShowFields: (f: any) => void
}) {
  const secretField = (label: string, key: string, isSecret = true) => (
    <FieldRow key={key} label={label}>
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <DarkInput
            type={isSecret && !showFields[key] ? 'password' : 'text'}
            value={credForm[key] || ''}
            onChange={e => setCredForm((p: any) => ({ ...p, [key]: e.target.value }))}
            placeholder="Leave blank to keep current"
            className={isSecret ? 'pr-9' : ''}
          />
          {isSecret && (
            <button type="button"
              onClick={() => setShowFields((p: any) => ({ ...p, [key]: !p[key] }))}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
            >
              {showFields[key] ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            </button>
          )}
        </div>
        <StatusBadge variant={credStatus[key] ? 'ok' : 'warn'}>
          {credStatus[key] ? 'Set ✓' : 'Not set'}
        </StatusBadge>
      </div>
    </FieldRow>
  )

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-slate-100 mb-0.5">API Keys</h3>
        <p className="text-xs text-slate-500">Third-party service credentials stored in-memory. Restart reverts to env vars.</p>
      </div>

      <div className="space-y-3">
        <p className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">Claude AI (Anthropic)</p>
        {secretField('Anthropic API Key', 'anthropic_api_key')}
      </div>

      <div className="space-y-3">
        <p className="text-[10px] font-bold text-slate-600 uppercase tracking-widest">TrueData</p>
        {secretField('Username', 'truedata_username', false)}
        {secretField('Password', 'truedata_password')}
      </div>

      <div className="flex justify-end pt-2 border-t border-slate-800">
        <button onClick={handleSave} disabled={credSaving}
          className="text-xs px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors disabled:opacity-50"
        >
          {credSaving ? 'Saving…' : 'Save Credentials'}
        </button>
      </div>
    </div>
  )
}

function AppLoginPanel({ appLoginForm, setAppLoginForm, appSaving, handleSave, showAppFields, setShowAppFields }: {
  appLoginForm: { username: string; new_password: string; confirm_password: string }
  setAppLoginForm: (f: any) => void; appSaving: boolean; handleSave: () => void
  showAppFields: Record<string, boolean>; setShowAppFields: (f: any) => void
}) {
  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-slate-100 mb-0.5">App Login</h3>
        <p className="text-xs text-slate-500">Update the admin username and password for this AlgoPro instance.</p>
      </div>

      <div className="space-y-3">
        <FieldRow label="Admin Username">
          <DarkInput
            value={appLoginForm.username}
            onChange={e => setAppLoginForm((p: any) => ({ ...p, username: e.target.value }))}
            placeholder="admin"
          />
        </FieldRow>

        <FieldRow label="New Password" hint="Min 8 characters">
          <div className="relative">
            <DarkInput
              type={showAppFields.new_password ? 'text' : 'password'}
              value={appLoginForm.new_password}
              onChange={e => setAppLoginForm((p: any) => ({ ...p, new_password: e.target.value }))}
              placeholder="Enter new password"
              className="pr-9"
            />
            <button type="button"
              onClick={() => setShowAppFields((p: any) => ({ ...p, new_password: !p.new_password }))}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
            >
              {showAppFields.new_password ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            </button>
          </div>
        </FieldRow>

        <FieldRow label="Confirm Password">
          <div className="relative">
            <DarkInput
              type={showAppFields.confirm_password ? 'text' : 'password'}
              value={appLoginForm.confirm_password}
              onChange={e => setAppLoginForm((p: any) => ({ ...p, confirm_password: e.target.value }))}
              placeholder="Re-enter password"
              className="pr-9"
            />
            <button type="button"
              onClick={() => setShowAppFields((p: any) => ({ ...p, confirm_password: !p.confirm_password }))}
              className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
            >
              {showAppFields.confirm_password ? <EyeOff className="w-3.5 h-3.5" /> : <Eye className="w-3.5 h-3.5" />}
            </button>
          </div>
        </FieldRow>
      </div>

      <p className="text-[11px] text-slate-600">In-memory only — restart reverts to environment variables.</p>

      <div className="flex justify-end pt-2 border-t border-slate-800">
        <button onClick={handleSave} disabled={appSaving}
          className="text-xs px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors disabled:opacity-50"
        >
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
      <p className="text-sm text-slate-600">{label} configuration coming soon</p>
    </div>
  )
}

// ─── Main Header ──────────────────────────────────────────────────────────────

export default function Header() {
  const {
    health, botStatus, wsConnected, apiKey, apiBase,
    setApiKey, setApiBase, setHealth, setBotStatus, addToast,
  } = useStore()

  const [time, setTime]       = useState(new Date())
  const [configOpen, setConfigOpen] = useState(false)
  const [activeNav, setActiveNav]   = useState('brokers')

  const [tempKey, setTempKey]   = useState(apiKey)
  const [tempBase, setTempBase] = useState(apiBase)

  const [credForm, setCredForm] = useState({
    kite_api_key: '', kite_api_secret: '',
    anthropic_api_key: '',
    truedata_username: '', truedata_password: '',
  })
  const [showFields, setShowFields]       = useState<Record<string, boolean>>({})
  const [credStatus, setCredStatus]       = useState<Record<string, boolean>>({})
  const [credSaving, setCredSaving]       = useState(false)

  const [appLoginForm, setAppLoginForm]   = useState({ username: '', new_password: '', confirm_password: '' })
  const [showAppFields, setShowAppFields] = useState<Record<string, boolean>>({})
  const [appSaving, setAppSaving]         = useState(false)
  const [botLoading, setBotLoading]       = useState(false)

  // Clock
  useEffect(() => {
    const t = setInterval(() => setTime(new Date()), 1000)
    return () => clearInterval(t)
  }, [])

  // Health poll
  useEffect(() => {
    const poll = () => {
      api.health().then(r => setHealth(r.data)).catch(() => {})
      api.botStatus().then(r => setBotStatus(r.data)).catch(() => {})
    }
    poll()
    const t = setInterval(poll, 5000)
    return () => clearInterval(t)
  }, [])

  // Load credential status when settings opens
  useEffect(() => {
    if (!configOpen) return
    api.configValidate().then(r => {
      setCredStatus({
        kite_api_key:      r.data.kite_api_key      ?? false,
        kite_api_secret:   r.data.kite_api_secret   ?? false,
        anthropic_api_key: r.data.anthropic_api_key ?? false,
        truedata_username: r.data.truedata_username ?? false,
        truedata_password: r.data.truedata_password ?? false,
      })
      setAppLoginForm(p => ({ ...p, username: r.data.admin_username ?? '' }))
    }).catch(() => {})
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

  // Save Zerodha: backend URL + API key + Kite credentials together
  const handleSaveZerodha = async () => {
    setApiKey(tempKey)
    setApiBase(tempBase)
    const payload: Record<string, string> = {}
    if (credForm.kite_api_key.trim())    payload.kite_api_key    = credForm.kite_api_key.trim()
    if (credForm.kite_api_secret.trim()) payload.kite_api_secret = credForm.kite_api_secret.trim()
    if (!Object.keys(payload).length) {
      addToast('Connection settings saved', 'info')
      return
    }
    setCredSaving(true)
    try {
      const r = await api.updateCredentials(payload)
      const c = r.data.credentials ?? {}
      setCredStatus(p => ({
        ...p,
        kite_api_key:    c.kite_api_key    ?? false,
        kite_api_secret: c.kite_api_secret ?? false,
      }))
      setCredForm(p => ({ ...p, kite_api_key: '', kite_api_secret: '' }))
      addToast('Zerodha credentials saved', 'buy')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Failed to save credentials', 'error')
    } finally { setCredSaving(false) }
  }

  // Save API keys (anthropic, truedata)
  const handleSaveApiKeys = async () => {
    const payload: Record<string, string> = {}
    if (credForm.anthropic_api_key.trim()) payload.anthropic_api_key = credForm.anthropic_api_key.trim()
    if (credForm.truedata_username.trim()) payload.truedata_username  = credForm.truedata_username.trim()
    if (credForm.truedata_password.trim()) payload.truedata_password  = credForm.truedata_password.trim()
    if (!Object.keys(payload).length) { addToast('No keys entered', 'info'); return }
    setCredSaving(true)
    try {
      const r = await api.updateCredentials(payload)
      const c = r.data.credentials ?? {}
      setCredStatus(p => ({
        ...p,
        anthropic_api_key: c.anthropic_api_key ?? false,
        truedata_username: c.truedata_username ?? false,
        truedata_password: c.truedata_password ?? false,
      }))
      setCredForm(p => ({ ...p, anthropic_api_key: '', truedata_username: '', truedata_password: '' }))
      addToast('API keys updated', 'buy')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Failed to update keys', 'error')
    } finally { setCredSaving(false) }
  }

  const handleSaveAppPassword = async () => {
    if (appLoginForm.new_password.length < 8) {
      addToast('Password must be at least 8 characters', 'error'); return
    }
    if (appLoginForm.new_password !== appLoginForm.confirm_password) {
      addToast('Passwords do not match', 'error'); return
    }
    const payload: { username?: string; new_password: string } = { new_password: appLoginForm.new_password }
    if (appLoginForm.username.trim()) payload.username = appLoginForm.username.trim()
    setAppSaving(true)
    try {
      const r = await api.updateAppPassword(payload)
      setAppLoginForm(p => ({ ...p, username: r.data.admin_username, new_password: '', confirm_password: '' }))
      addToast('Login credentials updated', 'buy')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Failed to update login credentials', 'error')
    } finally { setAppSaving(false) }
  }

  const openSettings = () => {
    setTempKey(apiKey)
    setTempBase(apiBase)
    setActiveNav('brokers')
    setCredForm({ kite_api_key: '', kite_api_secret: '', anthropic_api_key: '', truedata_username: '', truedata_password: '' })
    setShowFields({})
    setShowAppFields({})
    setAppLoginForm({ username: '', new_password: '', confirm_password: '' })
    setConfigOpen(true)
  }

  const mode        = health?.mode || 'PAPER'
  const marketOpen  = health?.market_open

  const zerodhaConfigured = credStatus.kite_api_key || credStatus.kite_api_secret
  const configuredCount   = zerodhaConfigured ? 1 : 0

  return (
    <>
      {/* ── Header bar ───────────────────────────────────────────────────── */}
      <header className="h-12 bg-slate-900 border-b border-slate-800 flex items-center px-4 gap-4 shrink-0 z-30">
        <div className="flex items-center gap-2 min-w-max">
          <Cpu className="w-5 h-5 text-emerald-400" />
          <div className="font-bold tracking-widest text-base leading-none">
            <span className="text-emerald-400">ALGO</span>
            <span className="text-white">PRO</span>
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

        <DarkBtn
          variant={botStatus?.master_running ? 'danger' : 'buy'}
          onClick={handleBotToggle}
          disabled={botLoading}
        >
          {botStatus?.master_running
            ? <><ZapOff className="w-3.5 h-3.5 mr-1.5" />Stop Bot</>
            : <><Zap   className="w-3.5 h-3.5 mr-1.5" />Start Bot</>
          }
        </DarkBtn>

        <button onClick={openSettings}
          className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-500 hover:text-slate-300 transition-colors"
        >
          <Settings className="w-4 h-4" />
        </button>
      </header>

      {/* ── Settings overlay ─────────────────────────────────────────────── */}
      {configOpen && (
        <div className="fixed inset-0 z-50 flex flex-col bg-slate-950 font-sans">

          {/* Top bar */}
          <div className="flex items-center gap-2.5 px-5 py-3 border-b border-slate-800/60 shrink-0">
            <span className="text-emerald-400 font-bold tracking-widest text-xs">ALGOPRO</span>
            <span className="text-slate-700 text-sm">/</span>
            <span className="text-slate-400 text-xs">Settings</span>
            <button onClick={() => setConfigOpen(false)}
              className="ml-auto flex items-center gap-1.5 text-xs text-slate-500 hover:text-slate-300 border border-slate-800 hover:border-slate-700 px-2.5 py-1 rounded transition-colors"
            >
              <X className="w-3 h-3" /> Close
            </button>
          </div>

          {/* Body */}
          <div className="flex flex-1 overflow-hidden">

            {/* Sidebar */}
            <aside className="w-52 shrink-0 border-r border-slate-800/60 py-3 px-2 flex flex-col">
              <div className="space-y-0.5">
                {NAV_ITEMS.map(item => (
                  <button key={item.id}
                    onClick={() => setActiveNav(item.id)}
                    className={clsx(
                      'w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-xs transition-all',
                      activeNav === item.id
                        ? 'bg-slate-800 text-slate-100'
                        : 'text-slate-500 hover:text-slate-300 hover:bg-slate-900',
                    )}
                  >
                    <span className="text-sm">{item.icon}</span>
                    <span className="flex-1 text-left">{item.label}</span>
                    {item.id === 'brokers' && configuredCount > 0 && (
                      <span className="text-[10px] bg-emerald-950 text-emerald-400 border border-emerald-900 px-1.5 py-0.5 rounded font-mono">
                        {configuredCount}
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

            {/* Main content */}
            <main className="flex-1 overflow-y-auto p-8">
              <div className="max-w-xl">
                {activeNav === 'brokers' && (
                  <BrokersPanel
                    wsConnected={wsConnected}
                    credStatus={credStatus}
                    tempBase={tempBase} setTempBase={setTempBase}
                    tempKey={tempKey}   setTempKey={setTempKey}
                    credForm={credForm} setCredForm={setCredForm}
                    credSaving={credSaving}
                    handleSaveZerodha={handleSaveZerodha}
                    showFields={showFields} setShowFields={setShowFields}
                  />
                )}
                {activeNav === 'apikeys' && (
                  <ApiKeysPanel
                    credForm={credForm} setCredForm={setCredForm}
                    credStatus={credStatus} credSaving={credSaving}
                    handleSave={handleSaveApiKeys}
                    showFields={showFields} setShowFields={setShowFields}
                  />
                )}
                {activeNav === 'security' && (
                  <AppLoginPanel
                    appLoginForm={appLoginForm} setAppLoginForm={setAppLoginForm}
                    appSaving={appSaving} handleSave={handleSaveAppPassword}
                    showAppFields={showAppFields} setShowAppFields={setShowAppFields}
                  />
                )}
                {activeNav === 'trading' && <ComingSoonPanel label="Trading" icon="⚙️" />}
                {activeNav === 'risk'    && <ComingSoonPanel label="Risk Limits" icon="🛡️" />}
              </div>
            </main>
          </div>
        </div>
      )}
    </>
  )
}
