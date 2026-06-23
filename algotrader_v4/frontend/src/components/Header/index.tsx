import React, { useEffect, useState, useCallback } from 'react'
import { Cpu, Wifi, WifiOff, Settings, Zap, ZapOff, Eye, EyeOff } from 'lucide-react'
import { clsx } from 'clsx'
import { useStore } from '../../store'
import { api } from '../../api/client'

type SettingsTab = 'connection' | 'apikeys' | 'applogin'

// Minimal reusable primitives styled for dark theme
function DarkInput({ type = 'text', value, onChange, placeholder, className = '' }: {
  type?: string; value: string; onChange: (e: React.ChangeEvent<HTMLInputElement>) => void
  placeholder?: string; className?: string
}) {
  return (
    <input
      type={type}
      value={value}
      onChange={onChange}
      placeholder={placeholder}
      className={`w-full px-3 py-2 bg-slate-800 border border-slate-700 rounded-lg text-slate-200 text-sm placeholder:text-slate-500 focus:outline-none focus:ring-1 focus:ring-emerald-500 focus:border-emerald-500 ${className}`}
    />
  )
}

function DarkBtn({ children, onClick, variant = 'default', disabled = false, className = '' }: {
  children: React.ReactNode; onClick?: () => void
  variant?: 'default' | 'danger' | 'buy' | 'outline'; disabled?: boolean; className?: string
}) {
  const base = 'inline-flex items-center justify-center px-3 py-1.5 rounded-lg text-sm font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed'
  const variants = {
    default:  'bg-emerald-600 hover:bg-emerald-500 text-white',
    buy:      'bg-emerald-600 hover:bg-emerald-500 text-white',
    danger:   'bg-rose-600 hover:bg-rose-500 text-white',
    outline:  'bg-transparent border border-slate-600 text-slate-300 hover:bg-slate-800',
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

export default function Header() {
  const { health, botStatus, wsConnected, apiKey, apiBase, setApiKey, setApiBase, setHealth, setBotStatus, addToast } = useStore()
  const [time, setTime]         = useState(new Date())
  const [configOpen, setConfigOpen] = useState(false)
  const [tempKey, setTempKey]   = useState(apiKey)
  const [tempBase, setTempBase] = useState(apiBase)
  const [botLoading, setBotLoading] = useState(false)

  const [settingsTab, setSettingsTab] = useState<SettingsTab>('connection')
  const [credForm, setCredForm] = useState({
    kite_api_key: '', kite_api_secret: '',
    anthropic_api_key: '',
    truedata_username: '', truedata_password: '',
  })
  const [showFields, setShowFields]   = useState<Record<string, boolean>>({})
  const [credStatus, setCredStatus]   = useState<Record<string, boolean>>({})
  const [credSaving, setCredSaving]   = useState(false)
  const [appLoginForm, setAppLoginForm] = useState({ username: '', new_password: '', confirm_password: '' })
  const [showAppFields, setShowAppFields] = useState<Record<string, boolean>>({})
  const [appSaving, setAppSaving] = useState(false)

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
    if (settingsTab === 'apikeys' || settingsTab === 'applogin') {
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
    }
  }, [settingsTab])

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
    } finally {
      setBotLoading(false)
    }
  }, [botStatus])

  const handleSaveCredentials = async () => {
    const payload: Record<string, string> = {}
    if (credForm.kite_api_key.trim())      payload.kite_api_key      = credForm.kite_api_key.trim()
    if (credForm.kite_api_secret.trim())   payload.kite_api_secret   = credForm.kite_api_secret.trim()
    if (credForm.anthropic_api_key.trim()) payload.anthropic_api_key = credForm.anthropic_api_key.trim()
    if (credForm.truedata_username.trim()) payload.truedata_username = credForm.truedata_username.trim()
    if (credForm.truedata_password.trim()) payload.truedata_password = credForm.truedata_password.trim()
    if (!Object.keys(payload).length) { addToast('No credentials entered', 'info'); return }
    setCredSaving(true)
    try {
      const r = await api.updateCredentials(payload)
      const c = r.data.credentials ?? {}
      setCredStatus({
        kite_api_key:      c.kite_api_key      ?? false,
        kite_api_secret:   c.kite_api_secret   ?? false,
        anthropic_api_key: c.anthropic_api_key ?? false,
        truedata_username: c.truedata_username ?? false,
        truedata_password: c.truedata_password ?? false,
      })
      setCredForm({ kite_api_key: '', kite_api_secret: '', anthropic_api_key: '', truedata_username: '', truedata_password: '' })
      addToast('Credentials updated', 'buy')
    } catch (e: any) {
      addToast(e.response?.data?.detail || 'Failed to update credentials', 'error')
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

  const credField = (label: string, key: keyof typeof credForm, isSecret = true) => (
    <div key={key}>
      <div className="flex items-center justify-between mb-1">
        <label className="text-sm font-medium text-slate-300">{label}</label>
        <StatusBadge variant={credStatus[key] ? 'ok' : 'warn'}>{credStatus[key] ? 'Set ✓' : 'Not set'}</StatusBadge>
      </div>
      <div className="relative">
        <DarkInput
          type={isSecret && !showFields[key] ? 'password' : 'text'}
          value={credForm[key]}
          onChange={e => setCredForm(p => ({ ...p, [key]: e.target.value }))}
          placeholder="Leave blank to keep current"
          className={isSecret ? 'pr-9' : ''}
        />
        {isSecret && (
          <button
            type="button"
            onClick={() => setShowFields(p => ({ ...p, [key]: !p[key] }))}
            className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300"
          >
            {showFields[key] ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
          </button>
        )}
      </div>
    </div>
  )

  const mode = health?.mode || 'PAPER'
  const marketOpen = health?.market_open

  const TAB_LABELS: Record<SettingsTab, string> = {
    connection: 'Connection', apikeys: 'API Keys', applogin: 'App Login',
  }

  return (
    <>
      <header className="h-12 bg-slate-900 border-b border-slate-800 flex items-center px-4 gap-4 shrink-0 z-30">

        {/* ALGOPRO Logo */}
        <div className="flex items-center gap-2 min-w-max">
          <Cpu className="w-5 h-5 text-emerald-400" />
          <div className="font-bold tracking-widest text-base leading-none">
            <span className="text-emerald-400">ALGO</span>
            <span className="text-white">PRO</span>
          </div>
          <span className="text-[10px] text-slate-500 font-mono ml-1">{health?.version || 'v4'}</span>
        </div>

        {/* Divider */}
        <div className="h-5 w-px bg-slate-700" />

        {/* Connection status */}
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

        {/* Mode badge */}
        <StatusBadge variant={mode === 'LIVE' ? 'live' : 'paper'}>{mode}</StatusBadge>

        {/* Ticker source */}
        {health?.ticker_source && (
          <span className="text-xs text-slate-500 font-mono hidden sm:block">
            TICKS: <span className="text-slate-300">{health.ticker_source}</span>
          </span>
        )}

        <div className="flex-1" />

        {/* Bot toggle */}
        <DarkBtn
          variant={botStatus?.master_running ? 'danger' : 'buy'}
          onClick={handleBotToggle}
          disabled={botLoading}
        >
          {botStatus?.master_running ? (
            <><ZapOff className="w-3.5 h-3.5 mr-1.5" />Stop Bot</>
          ) : (
            <><Zap className="w-3.5 h-3.5 mr-1.5" />Start Bot</>
          )}
        </DarkBtn>

        {/* Settings */}
        <button
          onClick={() => {
            setTempKey(apiKey); setTempBase(apiBase)
            setSettingsTab('connection')
            setCredForm({ kite_api_key: '', kite_api_secret: '', anthropic_api_key: '', truedata_username: '', truedata_password: '' })
            setShowFields({})
            setAppLoginForm({ username: '', new_password: '', confirm_password: '' })
            setShowAppFields({})
            setConfigOpen(true)
          }}
          className="p-1.5 rounded-lg hover:bg-slate-800 text-slate-500 hover:text-slate-300 transition-colors"
        >
          <Settings className="w-4 h-4" />
        </button>
      </header>

      {/* SETTINGS MODAL */}
      {configOpen && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-slate-950/80 backdrop-blur-sm">
          <div className="bg-slate-900 border border-slate-700 rounded-2xl shadow-2xl w-full max-w-md mx-4">
            {/* Modal header */}
            <div className="flex items-center justify-between px-6 py-4 border-b border-slate-800">
              <h2 className="text-base font-bold text-slate-100">Settings</h2>
              <button onClick={() => setConfigOpen(false)} className="text-slate-500 hover:text-slate-300 text-xl leading-none">&times;</button>
            </div>

            {/* Tab bar */}
            <div className="flex border-b border-slate-800 px-2">
              {(['connection', 'apikeys', 'applogin'] as const).map(tab => (
                <button
                  key={tab}
                  className={clsx(
                    'px-4 py-3 text-sm font-medium border-b-2 transition-colors',
                    settingsTab === tab
                      ? 'border-emerald-500 text-emerald-400'
                      : 'border-transparent text-slate-500 hover:text-slate-300',
                  )}
                  onClick={() => setSettingsTab(tab)}
                >
                  {TAB_LABELS[tab]}
                </button>
              ))}
            </div>

            <div className="p-6 space-y-4">
              {/* Connection tab */}
              {settingsTab === 'connection' && (
                <>
                  <div>
                    <label className="block text-sm font-medium text-slate-300 mb-1">API Base URL</label>
                    <DarkInput value={tempBase} onChange={e => setTempBase(e.target.value)} placeholder="http://localhost:8000" />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-slate-300 mb-1">X-API-Key</label>
                    <DarkInput type="password" value={tempKey} onChange={e => setTempKey(e.target.value)} placeholder="Leave empty if not set" />
                  </div>
                  <div className="flex gap-2 pt-2">
                    <DarkBtn onClick={() => { setApiKey(tempKey); setApiBase(tempBase); setConfigOpen(false) }}>
                      Save &amp; Reconnect
                    </DarkBtn>
                    <DarkBtn variant="outline" onClick={() => setConfigOpen(false)}>Cancel</DarkBtn>
                  </div>
                </>
              )}

              {/* API Keys tab */}
              {settingsTab === 'apikeys' && (
                <>
                  <div>
                    <p className="text-[10px] font-semibold text-slate-500 uppercase tracking-widest mb-2">Kite (Zerodha)</p>
                    <div className="space-y-3">
                      {credField('API Key', 'kite_api_key')}
                      {credField('API Secret', 'kite_api_secret')}
                    </div>
                  </div>
                  <div>
                    <p className="text-[10px] font-semibold text-slate-500 uppercase tracking-widest mb-2">Claude AI (Anthropic)</p>
                    {credField('Anthropic API Key', 'anthropic_api_key')}
                  </div>
                  <div>
                    <p className="text-[10px] font-semibold text-slate-500 uppercase tracking-widest mb-2">TrueData</p>
                    <div className="space-y-3">
                      {credField('Username', 'truedata_username', false)}
                      {credField('Password', 'truedata_password')}
                    </div>
                  </div>
                  <p className="text-xs text-slate-600">In-memory only — restart reverts to environment variables.</p>
                  <div className="flex gap-2 pt-1">
                    <DarkBtn onClick={handleSaveCredentials} disabled={credSaving}>
                      {credSaving ? 'Saving…' : 'Save Credentials'}
                    </DarkBtn>
                    <DarkBtn variant="outline" onClick={() => setConfigOpen(false)}>Cancel</DarkBtn>
                  </div>
                </>
              )}

              {/* App Login tab */}
              {settingsTab === 'applogin' && (
                <>
                  <div>
                    <label className="block text-sm font-medium text-slate-300 mb-1">Admin Username</label>
                    <DarkInput
                      value={appLoginForm.username}
                      onChange={e => setAppLoginForm(p => ({ ...p, username: e.target.value }))}
                      placeholder="admin"
                    />
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-slate-300 mb-1">New Password</label>
                    <div className="relative">
                      <DarkInput
                        type={showAppFields.new_password ? 'text' : 'password'}
                        value={appLoginForm.new_password}
                        onChange={e => setAppLoginForm(p => ({ ...p, new_password: e.target.value }))}
                        placeholder="Min 8 characters"
                        className="pr-9"
                      />
                      <button type="button" onClick={() => setShowAppFields(p => ({ ...p, new_password: !p.new_password }))}
                        className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300">
                        {showAppFields.new_password ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>
                  <div>
                    <label className="block text-sm font-medium text-slate-300 mb-1">Confirm Password</label>
                    <div className="relative">
                      <DarkInput
                        type={showAppFields.confirm_password ? 'text' : 'password'}
                        value={appLoginForm.confirm_password}
                        onChange={e => setAppLoginForm(p => ({ ...p, confirm_password: e.target.value }))}
                        placeholder="Re-enter password"
                        className="pr-9"
                      />
                      <button type="button" onClick={() => setShowAppFields(p => ({ ...p, confirm_password: !p.confirm_password }))}
                        className="absolute right-2.5 top-1/2 -translate-y-1/2 text-slate-500 hover:text-slate-300">
                        {showAppFields.confirm_password ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                  </div>
                  <p className="text-xs text-slate-600">In-memory only — restart reverts to environment variables.</p>
                  <div className="flex gap-2 pt-1">
                    <DarkBtn onClick={handleSaveAppPassword} disabled={appSaving}>
                      {appSaving ? 'Saving…' : 'Update Login'}
                    </DarkBtn>
                    <DarkBtn variant="outline" onClick={() => setConfigOpen(false)}>Cancel</DarkBtn>
                  </div>
                </>
              )}
            </div>
          </div>
        </div>
      )}
    </>
  )
}
