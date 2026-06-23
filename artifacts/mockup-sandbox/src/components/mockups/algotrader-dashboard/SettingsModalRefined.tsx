import { useState } from "react";

const BROKERS = [
  { id: "zerodha", name: "Zerodha Kite", logo: "Z", color: "#387ed1", status: "connected", clientId: "AB1234", lastSeen: "Just now" },
  { id: "upstox", name: "Upstox", logo: "U", color: "#6e45e2", status: "error", clientId: "UX9987", lastSeen: "2 min ago" },
  { id: "angelone", name: "Angel One", logo: "A", color: "#f97316", status: "disconnected", clientId: "AO5521", lastSeen: "Yesterday" },
  { id: "fyers", name: "Fyers", logo: "F", color: "#22c55e", status: "not_configured", clientId: "", lastSeen: "—" },
  { id: "iifl", name: "IIFL Securities", logo: "I", color: "#eab308", status: "not_configured", clientId: "", lastSeen: "—" },
  { id: "5paisa", name: "5Paisa", logo: "5", color: "#ec4899", status: "not_configured", clientId: "", lastSeen: "—" },
];

const STATUS_META: Record<string, { label: string; dot: string; text: string; bar: string }> = {
  connected: { label: "Connected", dot: "bg-emerald-400", text: "text-emerald-400", bar: "bg-emerald-600" },
  error: { label: "Auth Error", dot: "bg-rose-500", text: "text-rose-400", bar: "bg-rose-600" },
  disconnected: { label: "Disconnected", dot: "bg-amber-400", text: "text-amber-400", bar: "bg-amber-600" },
  not_configured: { label: "Not configured", dot: "bg-slate-700", text: "text-slate-600", bar: "bg-slate-700" },
};

const TABS = [
  { id: "brokers", label: "Brokers" },
  { id: "risk", label: "Risk" },
  { id: "login", label: "App Login" },
];

function BrokerCard({ broker, active, onActivate }: {
  broker: (typeof BROKERS)[0];
  active: boolean;
  onActivate: () => void;
}) {
  const m = STATUS_META[broker.status];
  const configured = broker.status !== "not_configured";

  return (
    <div
      className={`rounded-xl border transition-all ${
        active
          ? "border-slate-600 bg-slate-800/80"
          : "border-slate-800 bg-slate-900/50 hover:border-slate-700"
      }`}
    >
      <div className="flex items-start gap-3 px-4 pt-4 pb-3">
        <div
          className="w-8 h-8 rounded-lg flex items-center justify-center text-white font-bold text-sm shrink-0"
          style={{ background: broker.color }}
        >
          {broker.logo}
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center justify-between">
            <p className="text-xs font-semibold text-slate-200">{broker.name}</p>
            {configured && (
              <div className="flex items-center gap-1.5">
                <span className={`w-1.5 h-1.5 rounded-full ${m.dot} ${broker.status === "connected" ? "animate-pulse" : ""}`} />
                <span className={`text-[10px] font-medium ${m.text}`}>{m.label}</span>
              </div>
            )}
          </div>
          {configured ? (
            <p className="text-[11px] text-slate-600 font-mono mt-0.5">{broker.clientId || "—"} · {broker.lastSeen}</p>
          ) : (
            <p className="text-[11px] text-slate-600 mt-0.5">Not set up</p>
          )}
        </div>
      </div>

      {configured && (
        <div className="px-4 pb-3 flex items-center gap-2">
          {broker.status === "error" && (
            <p className="text-[10px] text-rose-400 flex-1">Token expired — update access token</p>
          )}
          <button
            onClick={onActivate}
            className={`ml-auto text-[10px] px-3 py-1 rounded-md border transition-colors font-medium ${
              active
                ? "bg-emerald-600 border-emerald-600 text-white"
                : "border-slate-700 text-slate-400 hover:border-slate-600 hover:text-slate-200"
            }`}
          >
            {active ? "✓ Active" : "Set active"}
          </button>
          <button className="text-[10px] px-3 py-1 rounded-md border border-slate-700 text-slate-500 hover:border-slate-600 hover:text-slate-300 transition-colors">
            Configure
          </button>
        </div>
      )}

      {!configured && (
        <div className="px-4 pb-3">
          <button className="w-full text-[10px] py-1.5 rounded-md border border-dashed border-slate-700 text-slate-500 hover:border-emerald-800 hover:text-emerald-400 transition-colors">
            + Set up {broker.name}
          </button>
        </div>
      )}
    </div>
  );
}

function BrokersTab() {
  const [activeBroker, setActiveBroker] = useState("zerodha");
  const [mode, setMode] = useState("PAPER");

  const configured = BROKERS.filter((b) => b.status !== "not_configured");
  const available = BROKERS.filter((b) => b.status === "not_configured");
  const connected = configured.filter((b) => b.status === "connected").length;
  const errors = configured.filter((b) => b.status === "error").length;

  return (
    <div className="space-y-4">
      <div className="flex gap-2">
        <div className="flex-1 bg-slate-900 border border-slate-800 rounded-lg px-3 py-2.5 flex items-center gap-2">
          <span className="w-2 h-2 rounded-full bg-emerald-400 animate-pulse" />
          <span className="text-[11px] text-slate-400">{connected} connected</span>
        </div>
        {errors > 0 && (
          <div className="flex-1 bg-rose-950/40 border border-rose-900/50 rounded-lg px-3 py-2.5 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-rose-500" />
            <span className="text-[11px] text-rose-400">{errors} auth error</span>
          </div>
        )}
        <div className="flex gap-1 bg-slate-900 border border-slate-800 rounded-lg p-1">
          {["PAPER", "LIVE"].map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={`px-3 py-1 rounded-md text-[10px] font-bold transition-all ${
                mode === m
                  ? m === "PAPER"
                    ? "bg-emerald-600 text-white"
                    : "bg-rose-600 text-white"
                  : "text-slate-500 hover:text-slate-300"
              }`}
            >
              {m}
            </button>
          ))}
        </div>
      </div>

      <div className="space-y-2">
        <p className="text-[10px] text-slate-600 uppercase tracking-widest font-bold">Your brokers</p>
        {configured.map((b) => (
          <BrokerCard
            key={b.id}
            broker={b}
            active={activeBroker === b.id}
            onActivate={() => setActiveBroker(b.id)}
          />
        ))}
      </div>

      <div>
        <p className="text-[10px] text-slate-600 uppercase tracking-widest font-bold mb-2">Add broker</p>
        <div className="grid grid-cols-3 gap-2">
          {available.map((b) => (
            <button
              key={b.id}
              className="flex items-center gap-2 bg-slate-900/50 hover:bg-slate-900 border border-dashed border-slate-800 hover:border-slate-700 rounded-lg px-3 py-2.5 transition-all"
            >
              <div
                className="w-6 h-6 rounded flex items-center justify-center font-bold text-xs"
                style={{ background: b.color + "33", color: b.color }}
              >
                {b.logo}
              </div>
              <span className="text-[11px] text-slate-500">{b.name}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

function RiskTab() {
  const rules = [
    { label: "Max daily loss", value: "₹10,000", enabled: true },
    { label: "Max position size", value: "5% capital", enabled: true },
    { label: "Kill-switch at", value: "₹15,000", enabled: false },
    { label: "Max open orders", value: "20", enabled: true },
  ];
  return (
    <div className="space-y-2">
      {rules.map((r) => (
        <div key={r.label} className="flex items-center gap-3 bg-slate-900 border border-slate-800 rounded-lg px-4 py-3">
          <div className={`w-1.5 h-1.5 rounded-full ${r.enabled ? "bg-emerald-500" : "bg-slate-600"}`} />
          <span className="flex-1 text-xs text-slate-300">{r.label}</span>
          <input
            defaultValue={r.value}
            className="w-28 text-right text-xs bg-slate-800 border border-slate-700 rounded px-2 py-1 font-mono text-slate-200 focus:outline-none focus:border-emerald-600"
          />
          <div className={`w-8 h-[18px] rounded-full relative cursor-pointer ${r.enabled ? "bg-emerald-600" : "bg-slate-700"}`}>
            <div className={`absolute top-[3px] w-3 h-3 rounded-full bg-white shadow transition-transform ${r.enabled ? "translate-x-[18px]" : "translate-x-[3px]"}`} />
          </div>
        </div>
      ))}
    </div>
  );
}

function LoginTab() {
  return (
    <div className="space-y-3">
      {[
        { label: "4-digit PIN", type: "password", placeholder: "****", hint: "Required to open the app" },
        { label: "Confirm PIN", type: "password", placeholder: "****" },
      ].map((f) => (
        <div key={f.label} className="grid grid-cols-[140px_1fr] items-start gap-3">
          <div className="pt-2">
            <p className="text-xs text-slate-400 font-medium">{f.label}</p>
            {f.hint && <p className="text-[11px] text-slate-600">{f.hint}</p>}
          </div>
          <input
            type={f.type}
            placeholder={f.placeholder}
            className="bg-slate-900 border border-slate-700 rounded px-3 py-2 text-xs font-mono text-slate-200 placeholder:text-slate-600 focus:outline-none focus:border-emerald-600"
          />
        </div>
      ))}
      <div className="grid grid-cols-[140px_1fr] items-center gap-3">
        <p className="text-xs text-slate-400 font-medium">Auto-lock</p>
        <select className="bg-slate-900 border border-slate-700 rounded px-3 py-2 text-xs text-slate-200 focus:outline-none focus:border-emerald-600 w-28">
          {["5 min", "15 min", "30 min", "Never"].map((o) => <option key={o}>{o}</option>)}
        </select>
      </div>
    </div>
  );
}

export function SettingsModalRefined() {
  const [tab, setTab] = useState("brokers");

  return (
    <div className="min-h-screen bg-slate-950/90 flex items-center justify-center p-4 font-sans">
      <div className="w-[660px] bg-slate-900 border border-slate-700/60 rounded-2xl shadow-2xl overflow-hidden">
        <div className="flex items-center gap-2.5 px-5 py-4 border-b border-slate-800">
          <span className="text-emerald-400 font-bold tracking-widest text-xs">ALGOPRO</span>
          <span className="text-slate-700 text-xs">/</span>
          <span className="text-slate-400 text-xs">Settings</span>
          <button className="ml-auto text-slate-600 hover:text-slate-300 transition-colors text-base leading-none">×</button>
        </div>

        <div className="flex gap-0 border-b border-slate-800 px-4">
          {TABS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`px-4 py-3 text-xs font-medium border-b-2 transition-all -mb-px ${
                tab === t.id
                  ? "border-emerald-500 text-emerald-400"
                  : "border-transparent text-slate-500 hover:text-slate-300"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="px-5 py-5 max-h-[520px] overflow-y-auto">
          {tab === "brokers" && <BrokersTab />}
          {tab === "risk" && <RiskTab />}
          {tab === "login" && <LoginTab />}
        </div>

        <div className="flex items-center justify-between px-5 py-3.5 border-t border-slate-800 bg-slate-950/40">
          <span className="text-[11px] text-slate-700 font-mono">v4.0.0</span>
          <div className="flex gap-2">
            <button className="text-xs px-4 py-2 rounded-lg border border-slate-700 text-slate-400 hover:bg-slate-800 transition-colors">Discard</button>
            <button className="text-xs px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white font-semibold transition-colors">Save & Close</button>
          </div>
        </div>
      </div>
    </div>
  );
}
