import React from 'react';
import { 
  Activity, 
  Wifi, 
  WifiOff, 
  TerminalSquare, 
  ArrowUpRight, 
  ArrowDownRight, 
  Clock, 
  Zap, 
  ShieldAlert,
  BarChart2,
  ListFilter,
  CheckCircle2,
  Crosshair
} from 'lucide-react';

export function PrecisionInstrument() {
  const isMarketOpen = true;

  const watchlist = [
    { symbol: 'NIFTY50', price: '24,156.80', change: '+0.32%', up: true, bid: '24156.00', ask: '24157.10' },
    { symbol: 'RELIANCE', price: '2,845.30', change: '+1.24%', up: true, bid: '2845.00', ask: '2845.50' },
    { symbol: 'TCS', price: '3,921.45', change: '-0.45%', up: false, bid: '3921.00', ask: '3922.00' },
    { symbol: 'INFY', price: '1,842.50', change: '+0.81%', up: true, bid: '1842.00', ask: '1843.00' },
    { symbol: 'HDFCBANK', price: '1,580.20', change: '-1.12%', up: false, bid: '1580.00', ask: '1580.50' },
    { symbol: 'ICICIBANK', price: '1,124.65', change: '+0.15%', up: true, bid: '1124.50', ask: '1125.00' },
  ];

  const positions = [
    { symbol: 'RELIANCE', type: 'LONG', qty: 250, entry: '2,800.00', current: '2,845.30', pnl: '+11,325.00', up: true, sl: '2,750.00', risk: 30 },
    { symbol: 'INFY', type: 'LONG', qty: 400, entry: '1,820.00', current: '1,842.50', pnl: '+9,000.00', up: true, sl: '1,790.00', risk: 45 },
    { symbol: 'HDFCBANK', type: 'SHORT', qty: 500, entry: '1,600.00', current: '1,580.20', pnl: '+9,900.00', up: true, sl: '1,620.00', risk: 60 },
    { symbol: 'TCS', type: 'LONG', qty: 100, entry: '3,950.00', current: '3,921.45', pnl: '-2,855.00', up: false, sl: '3,880.00', risk: 85 },
    { symbol: 'ITC', type: 'SHORT', qty: 1200, entry: '410.50', current: '417.60', pnl: '-8,520.00', up: false, sl: '425.00', risk: 90 },
  ];

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-400 font-mono text-xs flex flex-col selection:bg-emerald-500/20">
      {/* Top Header */}
      <header className="h-10 border-b border-zinc-800/80 flex items-center justify-between px-4 bg-zinc-950 z-10 shrink-0">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2 text-zinc-100 font-semibold tracking-wider">
            <TerminalSquare className="w-4 h-4 text-emerald-400" />
            <span>ALGO<span className="text-zinc-500">PRO</span>_v4</span>
          </div>
          <div className="w-px h-4 bg-zinc-800"></div>
          <div className="flex items-center gap-2">
            <div className={`w-2 h-2 rounded-full ${isMarketOpen ? 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.5)]' : 'bg-zinc-600'}`}></div>
            <span className="text-zinc-300">NSE OPEN</span>
          </div>
        </div>
        
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-2">
            <Zap className="w-3.5 h-3.5 text-sky-400" />
            <span>LATENCY: 12ms</span>
          </div>
          <div className="flex items-center gap-2">
            <Activity className="w-3.5 h-3.5 text-zinc-500" />
            <span>CPU: 14%</span>
          </div>
          <div className="flex items-center gap-2 text-zinc-300">
            <Clock className="w-3.5 h-3.5" />
            <span>14:28:45 IST</span>
          </div>
        </div>
      </header>

      {/* Hero P&L */}
      <section className="py-12 border-b border-zinc-800/50 flex flex-col items-center justify-center relative overflow-hidden shrink-0">
        <div className="absolute inset-0 bg-[radial-gradient(ellipse_at_center,rgba(52,211,153,0.03)_0%,rgba(9,9,11,0)_60%)] pointer-events-none"></div>
        <div className="text-zinc-500 mb-2 tracking-widest text-[10px] uppercase font-semibold flex items-center gap-2">
          <Crosshair className="w-3 h-3" /> Net Realized & Unrealized P&L
        </div>
        <div className="flex items-baseline gap-3">
          <span className="text-emerald-400 font-light text-6xl tracking-tight">+₹18,850</span>
          <span className="text-emerald-400/50 text-2xl">.00</span>
        </div>
        <div className="mt-3 flex items-center gap-4 text-sm">
          <div className="flex items-center gap-1 text-emerald-400 bg-emerald-400/10 px-2 py-0.5 rounded border border-emerald-400/20">
            <ArrowUpRight className="w-3.5 h-3.5" />
            <span>1.4% Day</span>
          </div>
          <div className="text-zinc-500">Max DD: <span className="text-rose-400">-₹2,100</span></div>
          <div className="text-zinc-500">Win Rate: <span className="text-sky-400">68%</span></div>
        </div>
      </section>

      {/* Micro Chart Band */}
      <section className="h-24 border-b border-zinc-800/50 flex flex-col justify-end p-2 relative shrink-0">
        <div className="absolute top-2 left-4 text-[10px] text-zinc-600 uppercase tracking-widest flex items-center gap-1">
          <BarChart2 className="w-3 h-3" /> Portfolio Equity Curve (Intraday)
        </div>
        <div className="w-full h-12 flex items-end gap-1 px-2 opacity-80">
          {Array.from({ length: 60 }).map((_, i) => {
            // Generate some random looking path
            const height = 20 + Math.sin(i * 0.2) * 10 + Math.cos(i * 0.5) * 5 + (i * 0.2);
            const isUp = i === 0 || height >= (20 + Math.sin((i-1) * 0.2) * 10 + Math.cos((i-1) * 0.5) * 5 + ((i-1) * 0.2));
            return (
              <div 
                key={i} 
                className={`flex-1 ${isUp ? 'bg-emerald-400/80' : 'bg-rose-400/80'} rounded-t-sm`}
                style={{ height: `${height}%`, minHeight: '4px' }}
              ></div>
            )
          })}
        </div>
      </section>

      {/* Two Column Layout */}
      <section className="flex-1 flex overflow-hidden">
        {/* Left: Watchlist */}
        <div className="w-1/3 min-w-[320px] border-r border-zinc-800/50 flex flex-col bg-zinc-950/50">
          <div className="h-8 border-b border-zinc-800/50 px-4 flex items-center justify-between text-[10px] text-zinc-500 uppercase tracking-widest bg-zinc-900/20">
            <div className="flex items-center gap-1"><ListFilter className="w-3 h-3" /> Watchlist</div>
            <div>Bid / Ask</div>
          </div>
          <div className="flex-1 overflow-auto overflow-x-hidden p-2 space-y-1">
            {watchlist.map((item, idx) => (
              <div key={idx} className="group flex items-center justify-between p-2 hover:bg-zinc-800/30 rounded border border-transparent hover:border-zinc-800/50 transition-colors cursor-pointer">
                <div>
                  <div className="text-zinc-200 font-medium">{item.symbol}</div>
                  <div className="flex items-center gap-2 mt-0.5">
                    <span className={item.up ? 'text-emerald-400' : 'text-rose-400'}>{item.price}</span>
                    <span className={`text-[10px] \${item.up ? 'text-emerald-400/70' : 'text-rose-400/70'}`}>{item.change}</span>
                  </div>
                </div>
                <div className="text-right text-[10px] flex flex-col gap-0.5">
                  <div className="text-sky-400/70 flex justify-end gap-2"><span>B:</span> <span className="w-12 text-zinc-300">{item.bid}</span></div>
                  <div className="text-rose-400/70 flex justify-end gap-2"><span>A:</span> <span className="w-12 text-zinc-300">{item.ask}</span></div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Right: Positions */}
        <div className="flex-1 flex flex-col bg-zinc-950">
          <div className="h-8 border-b border-zinc-800/50 px-4 flex items-center justify-between text-[10px] text-zinc-500 uppercase tracking-widest bg-zinc-900/20">
            <div className="flex items-center gap-1"><ShieldAlert className="w-3 h-3" /> Active Positions (5)</div>
            <div className="flex gap-4">
              <span className="w-16 text-right">Qty</span>
              <span className="w-20 text-right">Entry</span>
              <span className="w-20 text-right">Current</span>
              <span className="w-24 text-right">Unrealized</span>
            </div>
          </div>
          <div className="flex-1 overflow-auto p-4 space-y-2">
            {positions.map((pos, idx) => (
              <div key={idx} className="flex flex-col bg-zinc-900/20 border border-zinc-800/50 rounded p-3 hover:border-zinc-700 transition-colors">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-3">
                    <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold \${pos.type === 'LONG' ? 'bg-sky-500/10 text-sky-400 border border-sky-500/20' : 'bg-rose-500/10 text-rose-400 border border-rose-500/20'}`}>
                      {pos.type}
                    </span>
                    <span className="text-zinc-100 font-medium text-sm">{pos.symbol}</span>
                  </div>
                  <div className="flex gap-4 text-right items-center">
                    <span className="w-16 text-zinc-300">{pos.qty}</span>
                    <span className="w-20 text-zinc-400">{pos.entry}</span>
                    <span className="w-20 text-zinc-200">{pos.current}</span>
                    <span className={`w-24 font-medium \${pos.up ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {pos.pnl}
                    </span>
                  </div>
                </div>
                {/* Micro SL Bar */}
                <div className="flex items-center gap-3 text-[10px]">
                  <span className="text-zinc-600 w-8">SL Dist</span>
                  <div className="flex-1 h-1 bg-zinc-800 rounded-full overflow-hidden flex">
                    <div 
                      className={`h-full \${pos.risk < 50 ? 'bg-emerald-400' : pos.risk < 80 ? 'bg-amber-400' : 'bg-rose-400'}`} 
                      style={{ width: `\${pos.risk}%` }}
                    ></div>
                  </div>
                  <span className="text-zinc-500 w-16 text-right">Stop: {pos.sl}</span>
                </div>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}
