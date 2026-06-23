import { useState } from "react";

const BROKERS = [
  {
    id: "zerodha",
    name: "Zerodha Kite",
    logo: "Z",
    color: "#387ed1",
    status: "connected",
    lastSeen: "Just now",
    endpoint: "https://api.kite.trade",
    clientId: "AB1234",
  },
  {
    id: "upstox",
    name: "Upstox",
    logo: "U",
    color: "#6e45e2",
    status: "error",
    lastSeen: "2 min ago",
    endpoint: "https://api.upstox.com",
    clientId: "UX9987",
  },
  {
    id: "angelone",
    name: "Angel One",
    logo: "A",
    color: "#f97316",
    status: "disconnected",
    lastSeen: "Yesterday",
    endpoint: "https://apiconnect.angelbroking.com",
    clientId: "",
  },
  {
    id: "fyers",
    name: "Fyers",
    logo: "F",
    color: "#22c55e",
    status: "not_configured",
    lastSeen: "—",
    endpoint: "",
    clientId: "",
  },
  {
    id: "iifl",
    name: "IIFL Securities",
    logo: "I",
    color: "#eab308",
    status: "not_configured",
    lastSeen: "—",
    endpoint: "",
    clientId: "",
  },
  {
    id: "5paisa",
    name: "5Paisa",
    logo: "5",
    color: "#ec4899",
    status: "not_configured",
    lastSeen: "—",
    endpoint: "",
    clientId: "",
  },
];

const STATUS_META: Record<string, { label: string; dot: string; pill: string }> = {
  connected: {
    label: "Connected",
    dot: "bg-emerald-400",
    pill: "text-emerald-400 bg-emerald-950 border-emerald-800",
  },
  error: {
    label: "Auth Error",
    dot: "bg-rose-500",
    pill: "text-rose-400 bg-rose-950 border-rose-800",
  },
  disconnected: {
    label: "Disconnected",
    dot: "bg-amber-400",
    pill: "text-amber-400 bg-amber-950 border-amber-800",
  },
  not_configured: {
    label: "Not set up",
    dot: "bg-slate-600",
    pill: "text-slate-500 bg-slate-800 border-slate-700",
  },
};

const NAV = [
  { id: "brokers", label: "Brokers", icon: "⚡" },
  { id: "apikeys", label: "API Keys", icon: "🔑" },
  { id: "trading", label: "Trading Config", icon: "⚙️" },
  { id: "risk", label: "Risk Limits", icon: "🛡️" },
  { id: "notifications", label: "Notifications", icon: "🔔" },
  { id: "security", label: "App Security", icon: "🔒" },
];

function StatusBadge({ status }: { status: string }) {
  const m = STATUS_META[status];
  return (
    <span className={`text-[10px] font-mono px-1.5 py-0.5 rounded border ${m.pill}`}>
      {m.label}
    </span>
  );
}

function BrokerDetail({ broker, onBack }: { broker: (typeof BROKERS)[0]; onBack: () => void }) {
  const m = STATUS_META[broker.status];
  return (
    <div className="space-y-5">
      <div className="flex items-center gap-3">
        <button onClick={onBack} className="text-slate-500 hover:text-slate-300 text-sm transition-colors">
          ← Back
        </button>
        <div
          className="w-8 h-8 rounded-lg flex items-center justify-center text-white font-bold text-sm"
          style={{ background: broker.color }}
        >
          {broker.logo}
        </div>
        <div>
          <h3 className="text-sm font-semibold text-slate-100">{broker.name}</h3>
          <div className="flex items-center gap-1.5 mt-0.5">
            <span className={`w-1.5 h-1.5 rounded-full ${m.dot}`} />
            <span className="text-xs text-slate-400">{m.label}</span>
            {broker.lastSeen !== "—" && (
              <span className="text-xs text-slate-600">· {broker.lastSeen}</span>
            )}
          </div>
        </div>
        <button className="ml-auto text-xs px-3 py-1.5 rounded-lg bg-slate-800 hover:bg-slate-700 border border-slate-700 text-slate-300 transition-colors">
          Test Connection
        </button>
      </div>

      {broker.status === "error" && (
        <div className="bg-rose-950/40 border border-rose-900/60 rounded-lg px-4 py-3 text-xs text-rose-300">
          ⚠️ Authentication failed. Token may have expired — re-enter your access token below.
        </div>
      )}

      <div className="space-y-3">
        {[
          { label: "API Endpoint", value: broker.endpoint, placeholder: "https://api.broker.in", hint: "REST base URL" },
          { label: "WebSocket URL", value: "", placeholder: "wss://stream.broker.in/ws", hint: "Live tick feed" },
          { label: "Client ID", value: broker.clientId, placeholder: "Your broker client ID" },
          { label: "API Key", value: broker.clientId ? "••••••••••••" : "", placeholder: "Paste API key", type: "password" },
          { label: "Access Token", value: broker.status === "connected" ? "••••••••••••••••" : "", placeholder: "Session access token", type: "password" },
        ].map((f) => (
          <div key={f.label} className="grid grid-cols-[160px_1fr] items-start gap-3">
            <div className="pt-2">
              <p className="text-xs text-slate-400 font-medium">{f.label}</p>
              {f.hint && <p className="text-[11px] text-slate-600">{f.hint}</p>}
            </div>
            <input
              type={f.type || "text"}
              defaultValue={f.value}
              placeholder={f.placeholder}
              className="bg-slate-900 border border-slate-700 rounded-md px-3 py-2 text-xs text-slate-200 placeholder:text-slate-600 font-mono focus:outline-none focus:border-emerald-500 focus:ring-1 focus:ring-emerald-500/20 transition-all"
            />
          </div>
        ))}
      </div>

      <div className="flex items-center justify-between pt-2 border-t border-slate-800">
        <button className="text-xs text-rose-500 hover:text-rose-400 transition-colors">Remove broker</button>
        <div className="flex gap-2">
          <button className="text-xs px-4 py-2 rounded-lg border border-slate-700 text-slate-400 hover:bg-slate-800 transition-colors">
            Cancel
          </button>
          <button className="text-xs px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors">
            Save Changes
          </button>
        </div>
      </div>
    </div>
  );
}

function BrokerList({ onSelect }: { onSelect: (b: (typeof BROKERS)[0]) => void }) {
  const configured = BROKERS.filter((b) => b.status !== "not_configured");
  const available = BROKERS.filter((b) => b.status === "not_configured");

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-sm font-semibold text-slate-100 mb-0.5">Brokers</h3>
        <p className="text-xs text-slate-500">Manage broker connections. Only one can be active at a time.</p>
      </div>

      <div className="space-y-2">
        <p className="text-[10px] text-slate-600 uppercase tracking-widest font-bold">Configured</p>
        {configured.map((b) => {
          const m = STATUS_META[b.status];
          return (
            <button
              key={b.id}
              onClick={() => onSelect(b)}
              className="w-full flex items-center gap-3 bg-slate-900 hover:bg-slate-800/80 border border-slate-800 hover:border-slate-700 rounded-xl px-4 py-3 transition-all text-left group"
            >
              <div
                className="w-9 h-9 rounded-lg flex items-center justify-center text-white font-bold text-sm shrink-0"
                style={{ background: b.color }}
              >
                {b.logo}
              </div>
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-slate-200">{b.name}</p>
                <div className="flex items-center gap-1.5 mt-0.5">
                  <span
                    className={`w-1.5 h-1.5 rounded-full ${m.dot} ${b.status === "connected" ? "animate-pulse" : ""}`}
                  />
                  <span className="text-xs text-slate-500">{m.label}</span>
                  {b.lastSeen !== "—" && (
                    <span className="text-xs text-slate-700">· {b.lastSeen}</span>
                  )}
                </div>
              </div>
              <StatusBadge status={b.status} />
              <span className="text-slate-700 group-hover:text-slate-400 text-xs ml-1">›</span>
            </button>
          );
        })}
      </div>

      <div className="space-y-2">
        <p className="text-[10px] text-slate-600 uppercase tracking-widest font-bold">Add a broker</p>
        <div className="grid grid-cols-3 gap-2">
          {available.map((b) => (
            <button
              key={b.id}
              onClick={() => onSelect(b)}
              className="flex flex-col items-center gap-1.5 bg-slate-900/50 hover:bg-slate-900 border border-dashed border-slate-800 hover:border-slate-700 rounded-xl py-3 px-2 transition-all"
            >
              <div
                className="w-7 h-7 rounded-md flex items-center justify-center text-white font-bold text-xs"
                style={{ background: b.color + "66" }}
              >
                <span style={{ color: b.color }}>{b.logo}</span>
              </div>
              <span className="text-[11px] text-slate-500 text-center leading-tight">{b.name}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

export function SettingsSidebarNav() {
  const [activeNav, setActiveNav] = useState("brokers");
  const [selectedBroker, setSelectedBroker] = useState<(typeof BROKERS)[0] | null>(null);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans text-sm">
      <div className="border-b border-slate-800/60 px-5 py-3 flex items-center gap-2.5 shrink-0">
        <span className="text-emerald-400 font-bold tracking-widest text-xs">ALGOPRO</span>
        <span className="text-slate-700">/</span>
        <span className="text-slate-400 text-xs">Settings</span>
        <button className="ml-auto text-[11px] text-slate-500 hover:text-slate-300 border border-slate-800 hover:border-slate-700 px-2.5 py-1 rounded transition-colors">
          ✕ Close
        </button>
      </div>

      <div className="flex flex-1 overflow-hidden">
        <aside className="w-52 shrink-0 border-r border-slate-800/60 py-3 px-2 flex flex-col">
          <div className="space-y-0.5">
            {NAV.map((item) => (
              <button
                key={item.id}
                onClick={() => { setActiveNav(item.id); setSelectedBroker(null); }}
                className={`w-full flex items-center gap-2.5 px-3 py-2.5 rounded-lg text-xs transition-all ${
                  activeNav === item.id
                    ? "bg-slate-800 text-slate-100"
                    : "text-slate-500 hover:text-slate-300 hover:bg-slate-900"
                }`}
              >
                <span>{item.icon}</span>
                <span>{item.label}</span>
                {item.id === "brokers" && (
                  <span className="ml-auto text-[10px] bg-emerald-950 text-emerald-400 border border-emerald-900 px-1.5 py-0.5 rounded font-mono">
                    {BROKERS.filter((b) => b.status !== "not_configured").length}
                  </span>
                )}
              </button>
            ))}
          </div>
          <div className="mt-auto mx-3 pt-4 border-t border-slate-800">
            <p className="text-[10px] text-slate-700">v4.0.0 · Build 20260623</p>
          </div>
        </aside>

        <main className="flex-1 overflow-y-auto p-7">
          <div className="max-w-lg">
            {activeNav === "brokers" ? (
              selectedBroker ? (
                <BrokerDetail broker={selectedBroker} onBack={() => setSelectedBroker(null)} />
              ) : (
                <BrokerList onSelect={setSelectedBroker} />
              )
            ) : (
              <div className="flex flex-col items-center justify-center h-40 text-slate-600 text-xs gap-2">
                <span className="text-2xl opacity-30">
                  {NAV.find((n) => n.id === activeNav)?.icon}
                </span>
                {NAV.find((n) => n.id === activeNav)?.label} settings
              </div>
            )}
          </div>
        </main>
      </div>
    </div>
  );
}
