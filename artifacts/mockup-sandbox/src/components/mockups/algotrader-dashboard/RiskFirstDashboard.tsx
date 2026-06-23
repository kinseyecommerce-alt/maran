import React, { useState } from "react";
import { 
  AlertTriangle, 
  ArrowRight, 
  ArrowUpRight, 
  ArrowDownRight, 
  BarChart2, 
  CheckCircle2, 
  Clock, 
  Database, 
  LineChart, 
  Power, 
  PowerOff, 
  Server, 
  ShieldAlert, 
  ShieldCheck, 
  Activity,
  Bot
} from "lucide-react";

export function RiskFirstDashboard() {
  const [isSystemLive, setIsSystemLive] = useState(true);
  const [killSwitchConfirm, setKillSwitchConfirm] = useState(false);

  const handleKillSwitch = () => {
    if (!killSwitchConfirm) {
      setKillSwitchConfirm(true);
      setTimeout(() => setKillSwitchConfirm(false), 3000);
    } else {
      setIsSystemLive(false);
      setKillSwitchConfirm(false);
    }
  };

  const positions = [
    { id: "RELIANCE", type: "LONG", qty: 250, avgPrice: 2845.50, cmp: 2862.10, pnl: 4150, pnlPct: 0.58, slDist: 1.2, slStatus: "safe" },
    { id: "HDFCBANK", type: "SHORT", qty: 500, avgPrice: 1422.30, cmp: 1410.80, pnl: 5750, pnlPct: 0.81, slDist: 0.8, slStatus: "caution" },
    { id: "INFY", type: "LONG", qty: 300, avgPrice: 1650.00, cmp: 1644.80, pnl: -1560, pnlPct: -0.32, slDist: 0.3, slStatus: "danger" },
  ];

  const agents = [
    { name: "Alpha Scalper V2", status: "active", pnl: 4200, trades: 28 },
    { name: "Nifty Gamma Flow", status: "active", pnl: 5120, trades: 14 },
    { name: "BankNifty Reversion", status: "paused", pnl: -980, trades: 5 },
  ];

  const watchlist = [
    { symbol: "NIFTY 50", price: 22450.80, change: 120.45, pctChange: 0.54 },
    { symbol: "BANKNIFTY", price: 47890.20, change: -45.60, pctChange: -0.10 },
    { symbol: "RELIANCE", price: 2862.10, change: 18.50, pctChange: 0.65 },
    { symbol: "HDFCBANK", price: 1410.80, change: -12.40, pctChange: -0.87 },
    { symbol: "INFY", price: 1644.80, change: 8.20, pctChange: 0.50 },
    { symbol: "TCS", price: 4020.15, change: 25.30, pctChange: 0.63 },
  ];

  return (
    <div className="flex h-screen bg-slate-50 text-slate-900 font-sans overflow-hidden">
      {/* Main Content Area */}
      <div className="flex-1 flex flex-col h-full overflow-y-auto">
        
        {/* Header / Top Nav */}
        <header className="flex items-center justify-between px-8 py-4 bg-white border-b border-slate-200 sticky top-0 z-10 shadow-sm">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <div className="w-8 h-8 bg-slate-900 rounded-lg flex items-center justify-center">
                <Activity className="w-5 h-5 text-white" />
              </div>
              <div>
                <h1 className="text-xl font-bold tracking-tight text-slate-900 leading-none">AlgoTrader Pro</h1>
                <span className="text-[10px] uppercase font-bold tracking-widest text-slate-500">v4.2 Live Engine</span>
              </div>
            </div>
            <div className="h-6 w-px bg-slate-200 mx-2"></div>
            <div className="flex items-center gap-2 px-3 py-1 bg-emerald-50 text-emerald-700 rounded-full border border-emerald-100">
              <div className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></div>
              <span className="text-xs font-semibold uppercase tracking-wider">System Operational</span>
            </div>
          </div>
          
          <div className="flex items-center gap-6">
            <div className="flex items-center gap-4 text-sm text-slate-500">
              <div className="flex items-center gap-1">
                <Clock className="w-4 h-4" />
                <span>14:28:45 IST</span>
              </div>
              <div className="flex items-center gap-1">
                <Server className="w-4 h-4" />
                <span>28ms latency</span>
              </div>
            </div>
            
            <button 
              onClick={handleKillSwitch}
              className={`flex items-center gap-2 px-6 py-2 rounded-lg font-bold text-sm uppercase tracking-wide transition-all ${
                !isSystemLive 
                  ? 'bg-slate-200 text-slate-500 cursor-not-allowed' 
                  : killSwitchConfirm
                    ? 'bg-red-600 text-white animate-pulse shadow-lg shadow-red-500/30'
                    : 'bg-red-100 text-red-700 hover:bg-red-200'
              }`}
              disabled={!isSystemLive}
            >
              <Power className="w-4 h-4" />
              {!isSystemLive ? 'System Halted' : killSwitchConfirm ? 'Confirm Halt?' : 'Kill Switch'}
            </button>
          </div>
        </header>

        {!isSystemLive && (
          <div className="bg-red-600 text-white px-8 py-3 flex items-center justify-between">
            <div className="flex items-center gap-3">
              <AlertTriangle className="w-5 h-5" />
              <span className="font-medium">TRADING HALTED: Kill switch engaged. All active agents suspended and open orders cancelled.</span>
            </div>
            <button 
              onClick={() => setIsSystemLive(true)}
              className="px-4 py-1.5 bg-white text-red-600 rounded-md text-sm font-semibold hover:bg-red-50 transition-colors"
            >
              Resume Trading
            </button>
          </div>
        )}

        <main className="p-8 max-w-7xl mx-auto w-full space-y-8 flex-1">
          
          {/* 1. Risk Health Bar */}
          <section className="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <div className="flex items-center gap-2">
                <ShieldCheck className="w-6 h-6 text-emerald-500" />
                <h2 className="text-lg font-semibold">Risk Health</h2>
              </div>
              <div className="text-right">
                <div className="text-sm text-slate-500 font-medium uppercase tracking-wider">Drawdown Limit</div>
                <div className="text-lg font-bold text-emerald-600">₹6,660 <span className="text-sm font-normal text-slate-400">used of ₹15,000</span></div>
              </div>
            </div>
            
            <div className="w-full h-4 bg-slate-100 rounded-full overflow-hidden relative">
              <div 
                className="h-full bg-emerald-500 rounded-full transition-all duration-1000 ease-out"
                style={{ width: '44.4%' }}
              />
              {/* Markers */}
              <div className="absolute top-0 bottom-0 left-[70%] w-px bg-amber-400/50"></div>
              <div className="absolute top-0 bottom-0 left-[90%] w-px bg-red-400/50"></div>
            </div>
            <div className="flex justify-between mt-2 text-xs font-medium text-slate-400">
              <span>0%</span>
              <span>44% Exposure</span>
              <span>100%</span>
            </div>
          </section>

          {/* 2. Key Metrics */}
          <section className="grid grid-cols-3 gap-6">
            <div className="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm flex flex-col justify-between">
              <div className="text-slate-500 text-sm font-medium uppercase tracking-wider mb-2">Net Daily P&L</div>
              <div className="flex items-end gap-3">
                <div className="text-4xl font-bold text-emerald-600 tracking-tight">+₹8,340</div>
                <div className="flex items-center text-emerald-600 text-sm font-semibold pb-1">
                  <ArrowUpRight className="w-4 h-4" />
                  <span>1.2%</span>
                </div>
              </div>
              <div className="mt-4 pt-4 border-t border-slate-100 text-xs text-slate-400 flex justify-between">
                <span>Realized: +₹9,900</span>
                <span>Unrealized: -₹1,560</span>
              </div>
            </div>

            <div className="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm flex flex-col justify-between">
              <div className="text-slate-500 text-sm font-medium uppercase tracking-wider mb-2">Open Positions</div>
              <div className="flex items-end gap-3">
                <div className="text-4xl font-bold text-slate-800 tracking-tight">3</div>
              </div>
              <div className="mt-4 pt-4 border-t border-slate-100 text-xs text-slate-400 flex justify-between">
                <span>Margin Blocked: ₹3.4L</span>
                <span>Available: ₹11.6L</span>
              </div>
            </div>

            <div className="bg-white p-6 rounded-2xl border border-slate-200 shadow-sm flex flex-col justify-between">
              <div className="text-slate-500 text-sm font-medium uppercase tracking-wider mb-2">Trades Today</div>
              <div className="flex items-end gap-3">
                <div className="text-4xl font-bold text-slate-800 tracking-tight">47</div>
              </div>
              <div className="mt-4 pt-4 border-t border-slate-100 text-xs text-slate-400 flex justify-between">
                <span>Win Rate: 68%</span>
                <span>Brokerage est: ₹1,240</span>
              </div>
            </div>
          </section>

          {/* 3. Open Positions */}
          <section className="bg-white rounded-2xl border border-slate-200 shadow-sm overflow-hidden flex-1 min-h-[300px]">
            <div className="px-6 py-5 border-b border-slate-200 flex items-center justify-between bg-slate-50/50">
              <h3 className="font-semibold text-lg text-slate-800">Active Exposure</h3>
              <button className="text-sm font-medium text-slate-500 hover:text-slate-800 transition-colors">View All Orders &rarr;</button>
            </div>
            
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <thead className="bg-slate-50 text-slate-500 uppercase text-xs font-semibold tracking-wider">
                  <tr>
                    <th className="px-6 py-4">Symbol</th>
                    <th className="px-6 py-4">Type</th>
                    <th className="px-6 py-4 text-right">Qty</th>
                    <th className="px-6 py-4 text-right">Avg Price</th>
                    <th className="px-6 py-4 text-right">CMP</th>
                    <th className="px-6 py-4 text-right">P&L</th>
                    <th className="px-6 py-4 w-48">Stop Loss Distance</th>
                    <th className="px-6 py-4 text-right">Action</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-100">
                  {positions.map((pos) => (
                    <tr key={pos.id} className="hover:bg-slate-50 transition-colors group">
                      <td className="px-6 py-4 font-semibold text-slate-900">{pos.id}</td>
                      <td className="px-6 py-4">
                        <span className={`px-2 py-1 rounded text-xs font-bold ${pos.type === 'LONG' ? 'bg-emerald-100 text-emerald-700' : 'bg-red-100 text-red-700'}`}>
                          {pos.type}
                        </span>
                      </td>
                      <td className="px-6 py-4 text-right font-medium">{pos.qty}</td>
                      <td className="px-6 py-4 text-right text-slate-500">₹{pos.avgPrice.toFixed(2)}</td>
                      <td className="px-6 py-4 text-right font-medium">₹{pos.cmp.toFixed(2)}</td>
                      <td className={`px-6 py-4 text-right font-bold ${pos.pnl >= 0 ? 'text-emerald-600' : 'text-red-500'}`}>
                        {pos.pnl >= 0 ? '+' : ''}₹{pos.pnl.toLocaleString()}
                        <div className="text-xs font-medium opacity-80">{pos.pnl >= 0 ? '+' : ''}{pos.pnlPct}%</div>
                      </td>
                      <td className="px-6 py-4">
                        <div className="flex items-center gap-2">
                          <div className="flex-1 h-2 bg-slate-100 rounded-full overflow-hidden">
                            <div 
                              className={`h-full rounded-full ${
                                pos.slStatus === 'safe' ? 'bg-emerald-500' : 
                                pos.slStatus === 'caution' ? 'bg-amber-400' : 'bg-red-500'
                              }`}
                              style={{ width: `${Math.min(100, pos.slDist * 30)}%` }}
                            />
                          </div>
                          <span className="text-xs font-medium text-slate-500 w-8 text-right">{pos.slDist}%</span>
                        </div>
                      </td>
                      <td className="px-6 py-4 text-right">
                        <button className="text-xs font-semibold px-3 py-1.5 border border-slate-200 rounded-md text-slate-600 hover:bg-slate-100 hover:border-slate-300 transition-colors opacity-0 group-hover:opacity-100 focus:opacity-100">
                          SQUARE OFF
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

        </main>
      </div>

      {/* Right Sidebar - Secondary Context */}
      <aside className="w-80 bg-white border-l border-slate-200 flex flex-col shadow-xl z-20">
        
        {/* Market Context */}
        <div className="p-5 border-b border-slate-200">
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 mb-4 flex items-center gap-2">
            <LineChart className="w-4 h-4" /> Market Pulse
          </h3>
          <div className="space-y-3">
            {watchlist.slice(0, 2).map((item) => (
              <div key={item.symbol} className="flex items-center justify-between">
                <span className="font-semibold text-sm text-slate-800">{item.symbol}</span>
                <div className="text-right">
                  <div className="text-sm font-medium">{item.price.toFixed(2)}</div>
                  <div className={`text-xs font-bold ${item.change >= 0 ? 'text-emerald-500' : 'text-red-500'}`}>
                    {item.change >= 0 ? '+' : ''}{item.change.toFixed(2)} ({item.pctChange}%)
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Agents */}
        <div className="p-5 border-b border-slate-200 flex-1 overflow-y-auto">
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 mb-4 flex items-center gap-2">
            <Bot className="w-4 h-4" /> Active Agents
          </h3>
          <div className="space-y-4">
            {agents.map((agent) => (
              <div key={agent.name} className="p-3 bg-slate-50 rounded-xl border border-slate-100">
                <div className="flex items-center justify-between mb-2">
                  <span className="font-semibold text-sm text-slate-800">{agent.name}</span>
                  <span className={`w-2 h-2 rounded-full ${agent.status === 'active' ? 'bg-emerald-500' : 'bg-slate-300'}`} />
                </div>
                <div className="flex justify-between items-center text-xs">
                  <span className="text-slate-500">{agent.trades} trades</span>
                  <span className={`font-bold ${agent.pnl >= 0 ? 'text-emerald-600' : 'text-red-500'}`}>
                    {agent.pnl >= 0 ? '+' : ''}₹{agent.pnl.toLocaleString()}
                  </span>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Watchlist */}
        <div className="p-5 flex-1 overflow-y-auto bg-slate-50/50">
          <h3 className="text-xs font-bold uppercase tracking-wider text-slate-400 mb-4 flex items-center gap-2">
            <Database className="w-4 h-4" /> Watchlist
          </h3>
          <div className="space-y-3">
            {watchlist.slice(2).map((item) => (
              <div key={item.symbol} className="flex items-center justify-between py-1">
                <span className="font-medium text-xs text-slate-600">{item.symbol}</span>
                <div className="text-right">
                  <div className={`text-xs font-bold ${item.change >= 0 ? 'text-emerald-500' : 'text-red-500'}`}>
                    {item.price.toFixed(2)}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

      </aside>
    </div>
  );
}
