import { useState } from "react";

const BROKERS = [
  { id: "zerodha", name: "Zerodha Kite", logo: "Z", color: "#387ed1", status: "connected", clientId: "AB1234" },
  { id: "upstox", name: "Upstox", logo: "U", color: "#6e45e2", status: "error", clientId: "UX9987" },
  { id: "angelone", name: "Angel One", logo: "A", color: "#f97316", status: "disconnected", clientId: "AO5521" },
  { id: "fyers", name: "Fyers", logo: "F", color: "#22c55e", status: "not_configured", clientId: "" },
  { id: "iifl", name: "IIFL Securities", logo: "I", color: "#eab308", status: "not_configured", clientId: "" },
  { id: "5paisa", name: "5Paisa", logo: "5", color: "#ec4899", status: "not_configured", clientId: "" },
];

const STATUS_META: Record<string, { dot: string; label: string; labelColor: string }> = {
  connected: { dot: "bg-emerald-400", label: "Live", labelColor: "text-emerald-400" },
  error: { dot: "bg-rose-500", label: "Error", labelColor: "text-rose-400" },
  disconnected: { dot: "bg-amber-400", label: "Off", labelColor: "text-amber-400" },
  not_configured: { dot: "bg-slate-700", label: "—", labelColor: "text-slate-600" },
};

const BOT_TOGGLES = [
  { label: "Start bot on launch", on: false },
  { label: "Pause on disconnect", on: true },
  { label: "Square off at 3:25 PM", on: true },
  { label: "Auto-reconnect", on: true },
];

const RISK_ITEMS = [
  { label: "Daily loss limit", value: "₹10,000" },
  { label: "Max open orders", value: "20" },
  { label: "Position size", value: "5% cap" },
];

const DISPLAY_TOGGLES = [
  { label: "Show P&L in header", on: true },
  { label: "Compact agent cards", on: false },
  { label: "Sound alerts", on: false },
];

function Toggle({ on }: { on: boolean }) {
  return (
    <div className={`w-8 h-[18px] rounded-full relative cursor-pointer shrink-0 ${on ? "bg-emerald-600" : "bg-slate-700"}`}>
      <div className={`absolute top-[3px] w-3 h-3 rounded-full bg-white shadow transition-transform ${on ? "translate-x-[18px]" : "translate-x-[3px]"}`} />
    </div>
  );
}

function SectionHeader({ title, count }: { title: string; count?: number }) {
  return (
    <div className="flex items-center justify-between py-2 px-4 bg-slate-950/40 border-b border-slate-800/60">
      <p className="text-[9px] font-bold text-slate-600 uppercase tracking-widest">{title}</p>
      {count !== undefined && (
        <span className="text-[9px] text-slate-600">{count}</span>
      )}
    </div>
  );
}

export function SettingsDrawer() {
  const [query, setQuery] = useState("");
  const [activeBroker, setActiveBroker] = useState("zerodha");
  const [mode, setMode] = useState("PAPER");

  const configured = BROKERS.filter((b) => b.status !== "not_configured");
  const available = BROKERS.filter((b) => b.status === "not_configured");

  return (
    <div className="min-h-screen bg-slate-950 flex font-sans">
      <div className="flex-1" />

      <div className="w-[320px] bg-slate-900 border-l border-slate-800 flex flex-col shadow-2xl">
        <div className="flex items-center justify-between px-4 py-3.5 border-b border-slate-800 shrink-0">
          <div className="flex items-center gap-2">
            <span className="text-emerald-400 font-bold text-[10px] tracking-widest">ALGOPRO</span>
            <span className="text-slate-600 text-xs">/</span>
            <span className="text-slate-300 text-xs font-semibold">Settings</span>
          </div>
          <button className="text-slate-600 hover:text-slate-300 transition-colors text-base">×</button>
        </div>

        <div className="px-3 py-2.5 border-b border-slate-800 shrink-0">
          <div className="flex items-center gap-2 bg-slate-800/60 border border-slate-700/50 rounded-lg px-2.5 py-1.5">
            <span className="text-slate-500 text-[11px]">⌕</span>
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search settings…"
              className="flex-1 bg-transparent text-[11px] text-slate-200 placeholder:text-slate-600 focus:outline-none"
            />
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          <div className="px-3 py-2 border-b border-slate-800">
            <div className="flex gap-1.5 items-center">
              <span className="text-[10px] text-slate-500 flex-1">Mode</span>
              <div className="flex gap-1 bg-slate-800 border border-slate-700 rounded-md p-0.5">
                {["PAPER", "LIVE"].map((m) => (
                  <button
                    key={m}
                    onClick={() => setMode(m)}
                    className={`px-2.5 py-1 rounded text-[10px] font-bold transition-all ${
                      mode === m
                        ? m === "PAPER" ? "bg-emerald-600 text-white" : "bg-rose-600 text-white"
                        : "text-slate-500 hover:text-slate-300"
                    }`}
                  >
                    {m}
                  </button>
                ))}
              </div>
            </div>
          </div>

          <SectionHeader
            title="Brokers"
            count={configured.length}
          />
          <div className="divide-y divide-slate-800/40">
            {configured.map((b) => {
              const m = STATUS_META[b.status];
              const isActive = activeBroker === b.id;
              return (
                <div key={b.id} className={`flex items-center gap-2.5 px-4 py-2.5 transition-colors ${isActive ? "bg-slate-800/50" : "hover:bg-slate-800/30"}`}>
                  <div
                    className="w-6 h-6 rounded flex items-center justify-center text-white font-bold text-[10px] shrink-0"
                    style={{ background: b.color }}
                  >
                    {b.logo}
                  </div>
                  <div className="flex-1 min-w-0">
                    <p className="text-[11px] font-medium text-slate-200 truncate">{b.name}</p>
                    <div className="flex items-center gap-1 mt-0.5">
                      <span className={`w-1.5 h-1.5 rounded-full ${m.dot} ${b.status === "connected" ? "animate-pulse" : ""}`} />
                      <span className={`text-[10px] ${m.labelColor}`}>{m.label}</span>
                      {b.clientId && <span className="text-[10px] text-slate-700 font-mono">· {b.clientId}</span>}
                    </div>
                  </div>
                  <button
                    onClick={() => setActiveBroker(b.id)}
                    className={`text-[9px] px-2 py-0.5 rounded border font-semibold transition-all ${
                      isActive
                        ? "bg-emerald-600 border-emerald-600 text-white"
                        : "border-slate-700 text-slate-600 hover:text-slate-300 hover:border-slate-600"
                    }`}
                  >
                    {isActive ? "Active" : "Use"}
                  </button>
                </div>
              );
            })}
          </div>

          {available.length > 0 && (
            <>
              <div className="px-4 py-2 border-t border-slate-800/60">
                <p className="text-[9px] text-slate-700 uppercase tracking-widest font-bold mb-1.5">Add broker</p>
                <div className="flex flex-wrap gap-1.5">
                  {available.map((b) => (
                    <button
                      key={b.id}
                      className="flex items-center gap-1.5 text-[10px] text-slate-500 hover:text-slate-300 border border-dashed border-slate-800 hover:border-slate-700 rounded px-2 py-1 transition-colors"
                    >
                      <span style={{ color: b.color }} className="font-bold text-[11px]">{b.logo}</span>
                      {b.name.split(" ")[0]}
                    </button>
                  ))}
                </div>
              </div>
            </>
          )}

          <SectionHeader title="Bot Behaviour" />
          <div className="divide-y divide-slate-800/40">
            {BOT_TOGGLES.map((t) => (
              <div key={t.label} className="flex items-center justify-between px-4 py-2.5">
                <span className="text-[11px] text-slate-300">{t.label}</span>
                <Toggle on={t.on} />
              </div>
            ))}
          </div>

          <SectionHeader title="Risk Limits" />
          <div className="divide-y divide-slate-800/40">
            {RISK_ITEMS.map((r) => (
              <div key={r.label} className="flex items-center justify-between px-4 py-2.5">
                <span className="text-[11px] text-slate-300">{r.label}</span>
                <input
                  defaultValue={r.value}
                  className="w-20 text-right text-[11px] bg-slate-800 border border-slate-700 rounded px-2 py-0.5 font-mono text-slate-200 focus:outline-none focus:border-emerald-600"
                />
              </div>
            ))}
            <div className="flex items-center justify-between px-4 py-2.5">
              <span className="text-[11px] text-slate-300">Auto-halt on breach</span>
              <Toggle on={true} />
            </div>
          </div>

          <SectionHeader title="Display" />
          <div className="divide-y divide-slate-800/40">
            {DISPLAY_TOGGLES.map((t) => (
              <div key={t.label} className="flex items-center justify-between px-4 py-2.5">
                <span className="text-[11px] text-slate-300">{t.label}</span>
                <Toggle on={t.on} />
              </div>
            ))}
          </div>

          <div className="h-4" />
        </div>

        <div className="px-4 py-3 border-t border-slate-800 shrink-0">
          <div className="flex gap-2">
            <button className="flex-1 py-2 text-[11px] border border-slate-700 text-slate-400 rounded-lg hover:bg-slate-800 transition-colors">
              Discard
            </button>
            <button className="flex-1 py-2 text-[11px] bg-emerald-600 hover:bg-emerald-500 text-white font-semibold rounded-lg transition-colors">
              Save All
            </button>
          </div>
          <p className="text-[10px] text-slate-700 text-center mt-2">v4.0.0 · Build 20260623</p>
        </div>
      </div>
    </div>
  );
}
