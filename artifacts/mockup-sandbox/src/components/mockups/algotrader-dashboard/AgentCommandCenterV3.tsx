import React, { useState, useEffect } from "react";
import { 
  Activity, 
  Terminal, 
  Cpu, 
  Wifi, 
  ShieldCheck, 
  TrendingUp, 
  TrendingDown,
  BarChart3,
  Clock,
  Zap,
  Play,
  Square,
  Database,
  Crosshair
} from "lucide-react";

export function AgentCommandCenterV3() {
  const [time, setTime] = useState(new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" }));
  const [lastUpdated, setLastUpdated] = useState(12);

  useEffect(() => {
    const timer = setInterval(() => {
      setTime(new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" }));
      setLastUpdated(prev => (prev + 1) % 60);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const agents = [
    {
      id: "AGN-01",
      name: "SCALPING",
      status: "ACTIVE",
      strategy: "Orderbook Imbalance",
      pnl: "+₹14,250",
      pnlClass: "text-emerald-400",
      uptime: "4h 12m",
      lastAction: "Bought 150 INFY",
      confidence: "84%",
      memory: "128 MB",
    },
    {
      id: "AGN-02",
      name: "MOMENTUM",
      status: "ACTIVE",
      strategy: "VWAP Breakout",
      pnl: "+₹32,100",
      pnlClass: "text-emerald-400",
      uptime: "4h 12m",
      lastAction: "Holding RELIANCE",
      confidence: "92%",
      memory: "256 MB",
    },
    {
      id: "AGN-03",
      name: "INTRADAY",
      status: "PAUSED",
      strategy: "Mean Reversion",
      pnl: "-₹1,450",
      pnlClass: "text-rose-500",
      uptime: "0h 0m",
      lastAction: "Stopped out HDFC",
      confidence: "--",
      memory: "0 MB",
    },
    {
      id: "AGN-04",
      name: "SWING",
      status: "ACTIVE",
      strategy: "Multi-Timeframe Trend",
      pnl: "+₹8,900",
      pnlClass: "text-emerald-400",
      uptime: "4h 12m",
      lastAction: "Sold 50 TCS",
      confidence: "76%",
      memory: "512 MB",
    }
  ];

  const logs = [
    { group: "09:40 — High Frequency Scalps" },
    { time: "09:42:15", agent: "SCALPING", action: "BOUGHT 150 INFY @ 1,842.50", conf: "84%", type: "buy", cat: "EXEC" },
    { time: "09:41:02", agent: "MOMENTUM", action: "TRAILING SL UPDATED RELIANCE TO 2,840.00", conf: "--", type: "system", cat: "RISK" },
    { group: "09:30 — Market Open" },
    { time: "09:38:45", agent: "SWING", action: "SOLD 50 TCS @ 3,920.10 (PROFIT BOOKING)", conf: "91%", type: "sell", cat: "EXEC" },
    { time: "09:35:12", agent: "SYSTEM", action: "MARKET REGIME DETECTED: HIGH VOLATILITY", conf: "98%", type: "alert", cat: "WARN" },
    { time: "09:31:05", agent: "SCALPING", action: "DETECTED ORDERBOOK IMBALANCE IN INFY (BULLISH)", conf: "82%", type: "analyze", cat: "SIG" },
    { time: "09:25:00", agent: "INTRADAY", action: "STOP LOSS HIT HDFC @ 1,420.50. AGENT PAUSED.", conf: "--", type: "loss", cat: "RISK" },
    { time: "09:15:05", agent: "SYSTEM", action: "NSE PRE-OPEN COMPLETE. ALL ACTIVE AGENTS ENGAGED.", conf: "100%", type: "system", cat: "SYS" },
  ];

  const watchlist = [
    { sym: "NIFTY50", price: "22,450.20", chg: "+1.2%", chgVal: "+265.40", type: "up" },
    { sym: "RELIANCE", price: "2,845.30", chg: "+0.8%", chgVal: "+22.60", type: "up" },
    { sym: "TCS", price: "3,910.05", chg: "-0.4%", chgVal: "-15.60", type: "down" },
    { sym: "INFY", price: "1,842.50", chg: "+2.1%", chgVal: "+37.80", type: "up" },
    { sym: "HDFCBANK", price: "1,422.10", chg: "-1.5%", chgVal: "-21.60", type: "down" },
  ];

  const chartData = [...Array(30)].map((_, i) => {
    return 25 + Math.sin(i * 0.4) * 15 + Math.cos(i * 0.3) * 10;
  });
  
  const linePoints = chartData.map((h, i) => `${(i / 29) * 100},${100 - h}`).join(' ');

  return (
    <div className="min-h-screen bg-slate-950 text-slate-300 font-sans flex flex-col overflow-hidden selection:bg-emerald-900 selection:text-emerald-50">
      
      {/* HEADER */}
      <header className="h-12 bg-slate-900 flex items-center justify-between px-4 shrink-0 relative">
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2 text-emerald-400 font-bold tracking-widest text-lg">
            <Cpu className="w-5 h-5" />
            <span>ALGO<span className="text-white">PRO</span> <span className="text-xs text-slate-500 font-mono tracking-normal">v4.2.1</span></span>
          </div>
          <div className="h-5 w-px bg-slate-700 mx-2" />
          <div className="flex items-center gap-2 text-xs font-mono">
            <div className="flex items-center gap-1.5 text-emerald-400 bg-emerald-400/10 px-2 py-1 rounded">
              <span className="relative flex h-2 w-2">
                <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-emerald-400 opacity-75"></span>
                <span className="relative inline-flex rounded-full h-2 w-2 bg-emerald-500"></span>
              </span>
              NSE CONNECTED
            </div>
            <div className="flex items-center gap-1.5 text-slate-400 px-2 py-1">
              <Wifi className="w-3 h-3" /> 12ms
            </div>
          </div>
        </div>

        <div className="flex items-center gap-6">
          <div className="flex flex-col items-end mr-4">
            <div className="flex items-baseline gap-3">
              <span className="text-emerald-400 font-mono font-bold text-[28px] leading-none">+₹53,800.00</span>
              <span className="text-emerald-500 font-mono text-sm font-medium bg-emerald-500/10 px-1.5 py-0.5 rounded flex items-center gap-1">
                <TrendingUp className="w-3 h-3" />
                +3.7% today
              </span>
            </div>
          </div>
          <div className="flex flex-col items-end">
            <span className="text-[10px] text-slate-500 font-mono uppercase tracking-wider">Used Margin</span>
            <span className="text-slate-200 font-mono text-sm">₹4,25,000.00</span>
          </div>
          <div className="flex items-center gap-3 ml-4 pl-4 border-l border-slate-800 h-8">
            <ShieldCheck className="w-4 h-4 text-emerald-500" />
            <span className="font-mono text-sm text-slate-300">{time} IST</span>
          </div>
        </div>
      </header>
      
      {/* MOOD LINE */}
      <div className="h-0.5 w-full bg-emerald-500 shadow-[0_0_8px_#10b981] shrink-0" />

      {/* MAIN LAYOUT */}
      <div className="flex flex-1 overflow-hidden">
        
        {/* LEFT COLUMN - AGENTS & LOGS */}
        <div className="flex-1 flex flex-col min-w-0 border-r border-slate-800 bg-[#070b14]">
          
          {/* AGENTS SECTION */}
          <div className="p-4 shrink-0">
            <div className="flex items-center justify-between mb-4">
              <h2 className="text-sm font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <Zap className="w-4 h-4 text-emerald-500" /> 
                AUTONOMOUS AGENTS
              </h2>
              <div className="text-xs font-mono text-slate-500 flex gap-4">
                <span>ACTIVE: <span className="text-emerald-400">3</span></span>
                <span>PAUSED: <span className="text-amber-500">1</span></span>
                <span>CPU: 42%</span>
              </div>
            </div>
            
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-4">
              {agents.map((agent) => (
                <div key={agent.id} className={`rounded-lg bg-slate-900/50 relative overflow-hidden flex flex-col transition-colors border ${agent.status === 'ACTIVE' ? 'border-slate-800 border-l-2 border-l-emerald-500/60' : 'border-slate-800 opacity-75'}`}>
                  
                  <div className="p-4 flex-1">
                    <div className="flex justify-between items-start mb-1">
                      <div>
                        <div className="flex items-center gap-2">
                          <span className="font-mono text-[10px] text-slate-500">{agent.id}</span>
                          {agent.status === 'ACTIVE' ? (
                            <span className="flex h-1.5 w-1.5 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.8)]"></span>
                          ) : (
                            <span className="flex h-1.5 w-1.5 rounded-full bg-amber-500"></span>
                          )}
                        </div>
                        <h3 className="text-base font-bold text-white leading-tight mt-1">{agent.name}</h3>
                        <p className="text-[11px] text-slate-500 italic mt-0.5">{agent.strategy}</p>
                      </div>
                      <div className={`text-lg font-mono font-bold ${agent.pnlClass}`}>
                        {agent.pnl}
                      </div>
                    </div>
                    
                    <div className="mt-4 bg-slate-950 rounded p-2.5 border border-slate-800">
                      <div className="text-[10px] text-slate-500 uppercase tracking-wider mb-1 flex justify-between">
                        <span>Last Action</span>
                        <span>Conf: {agent.confidence}</span>
                      </div>
                      <div className="font-mono text-xs text-slate-300 truncate" title={agent.lastAction}>
                        {agent.lastAction}
                      </div>
                    </div>
                    
                    <div className="mt-3 flex gap-2">
                      {agent.status === 'ACTIVE' ? (
                        <button className="flex-1 flex items-center justify-center gap-1 bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs py-1.5 rounded transition-colors border border-slate-700">
                          <Square className="w-3 h-3" /> Stop
                        </button>
                      ) : (
                        <button className="flex-1 flex items-center justify-center gap-1 bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 text-xs py-1.5 rounded transition-colors border border-emerald-500/30">
                          <Play className="w-3 h-3 fill-current" /> Start
                        </button>
                      )}
                      <button className="px-2 bg-slate-800 hover:bg-slate-700 text-slate-400 text-xs py-1.5 rounded transition-colors border border-slate-700">
                        <TrendingUp className="w-3 h-3" />
                      </button>
                    </div>
                  </div>

                  {/* BOTTOM STATUS BAR */}
                  <div className={`h-[3px] w-full mt-auto ${agent.status === 'ACTIVE' ? 'bg-emerald-500/80 animate-pulse' : 'bg-amber-500/60'}`} />
                </div>
              ))}
            </div>
          </div>

          {/* ACTIVITY LOG */}
          <div className="flex-1 flex flex-col p-4 border-t border-slate-800 bg-slate-950 overflow-hidden">
            <div className="flex justify-between items-center mb-4 shrink-0">
              <h2 className="text-sm font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <Terminal className="w-4 h-4 text-emerald-500" /> 
                LIVE DECISION STREAM
              </h2>
              <span className="text-xs text-slate-500 font-mono">Last updated: {lastUpdated}s ago</span>
            </div>
            
            <div className="flex-1 overflow-y-auto space-y-0.5 font-mono text-xs pr-2 custom-scrollbar">
              {logs.map((log, i) => {
                if (log.group) {
                  return (
                    <div key={i} className="flex items-center gap-3 py-2 mt-2 first:mt-0">
                      <div className="h-px bg-slate-800 flex-1" />
                      <span className="text-[10px] text-slate-500 tracking-widest uppercase">{log.group}</span>
                      <div className="h-px bg-slate-800 flex-1" />
                    </div>
                  );
                }

                const catColor = 
                  log.cat === 'EXEC' ? 'bg-emerald-500/80' : 
                  log.cat === 'RISK' ? 'bg-rose-500/80' : 
                  log.cat === 'WARN' ? 'bg-amber-500/80' : 
                  log.cat === 'SIG' ? 'bg-blue-500/80' : 'bg-slate-500/80';

                return (
                  <div key={i} className="flex items-stretch hover:bg-slate-900/80 rounded group transition-colors overflow-hidden">
                    <div className={`w-0.5 shrink-0 ${catColor}`} />
                    <div className="flex flex-1 gap-3 py-1.5 px-3">
                      <span className="text-slate-600 shrink-0 w-20">{log.time}</span>
                      <span className="text-slate-500 shrink-0 w-12 text-center text-[10px] bg-slate-900 py-0.5 rounded">{log.cat}</span>
                      <span className="text-slate-400 shrink-0 w-20 truncate">[{log.agent}]</span>
                      <span className={`flex-1 ${
                        log.type === 'buy' ? 'text-emerald-400' :
                        log.type === 'sell' ? 'text-rose-400' :
                        log.type === 'alert' ? 'text-amber-400' :
                        log.type === 'loss' ? 'text-rose-500 font-bold' :
                        'text-slate-300'
                      }`}>
                        {log.action}
                      </span>
                      <span className="text-slate-600 shrink-0 w-16 text-right opacity-0 group-hover:opacity-100 transition-opacity">
                        C: {log.conf}
                      </span>
                    </div>
                  </div>
                );
              })}
              <div className="flex items-stretch rounded overflow-hidden mt-1">
                <div className="w-0.5 shrink-0 bg-transparent" />
                <div className="flex gap-4 py-1.5 px-3">
                  <span className="text-emerald-500/50 shrink-0 w-20 animate-pulse">...</span>
                  <span className="text-slate-600">Waiting for signals...</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* RIGHT SIDEBAR - WATCHLIST & SECONDARY CHART */}
        <div className="w-80 flex flex-col bg-slate-900 shrink-0">
          
          {/* WATCHLIST */}
          <div className="flex-1 border-b border-slate-800 flex flex-col min-h-0">
            <div className="p-3 border-b border-slate-800 flex justify-between items-center bg-slate-950/50 shrink-0">
              <h3 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <Activity className="w-3.5 h-3.5" />
                MARKET OVERVIEW
              </h3>
            </div>
            <div className="flex-1 overflow-y-auto custom-scrollbar">
              {watchlist.map((item, i) => (
                <div key={i} className="flex justify-between items-center p-3 border-b border-slate-800/50 hover:bg-slate-800/30 cursor-pointer transition-colors">
                  <div>
                    <div className="font-bold text-slate-200 text-sm">{item.sym}</div>
                    <div className="text-[10px] text-slate-500 mt-0.5">NSE EQ</div>
                  </div>
                  <div className="text-right">
                    <div className="font-mono text-sm text-slate-200">{item.price}</div>
                    <div className={`font-mono text-[10px] flex items-center justify-end gap-1 mt-0.5 ${item.type === 'up' ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {item.type === 'up' ? <TrendingUp className="w-3 h-3" /> : <TrendingDown className="w-3 h-3" />}
                      {item.chgVal} ({item.chg})
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* SECONDARY CHART CONTEXT */}
          <div className="h-64 p-3 bg-slate-950/30 flex flex-col shrink-0">
            <div className="flex justify-between items-center mb-3">
              <h3 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <BarChart3 className="w-3.5 h-3.5" />
                NIFTY50 TREND
              </h3>
              <span className="text-[10px] bg-slate-800 px-1.5 py-0.5 rounded text-slate-400">1M</span>
            </div>
            
            {/* Chart Area */}
            <div className="flex-1 relative border border-slate-800 rounded bg-[#0b1120] overflow-hidden group">
              {/* Y-axis grid lines */}
              <div className="absolute inset-0 pointer-events-none flex flex-col justify-between py-[12.5%] opacity-20">
                <div className="w-full h-px border-t border-dashed border-slate-400"></div>
                <div className="w-full h-px border-t border-dashed border-slate-400"></div>
                <div className="w-full h-px border-t border-dashed border-slate-400"></div>
              </div>

              <div className="absolute inset-0 flex items-end">
                {/* Deterministic chart bars */}
                <div className="w-full h-full flex items-end justify-between px-1 opacity-40 group-hover:opacity-60 transition-opacity">
                  {chartData.map((h, i) => (
                    <div key={i} className="w-[2%] bg-emerald-500/20 rounded-t-[1px]" style={{ height: `${h}%` }}></div>
                  ))}
                </div>
                {/* SVG Line matching the bars */}
                <svg className="absolute inset-0 h-full w-full opacity-80" viewBox="0 0 100 100" preserveAspectRatio="none">
                  <polyline 
                    points={linePoints}
                    fill="none" 
                    stroke="rgba(16,185,129,0.8)" 
                    strokeWidth="1.5" 
                    vectorEffect="non-scaling-stroke" 
                  />
                  <polygon 
                    points={`0,100 ${linePoints} 100,100`}
                    fill="rgba(16,185,129,0.05)" 
                  />
                </svg>
              </div>

              <div className="absolute bottom-2 right-2 flex items-center gap-1.5 bg-slate-900/80 backdrop-blur border border-slate-700/50 px-2 py-1 rounded text-[10px] font-mono text-emerald-400">
                <Crosshair className="w-3 h-3" />
                UPTREND STRONG
              </div>
            </div>
          </div>
          
        </div>
      </div>
      
      {/* PORTFOLIO SUMMARY STRIP */}
      <div className="bg-slate-900/80 border-t border-slate-800 px-4 py-2 shrink-0 flex items-center gap-6 font-mono text-[10px] text-slate-400">
        <span className="font-bold text-slate-300">SESSION SUMMARY</span>
        <div className="flex items-center gap-6">
          <span>Sessions: <span className="text-slate-200">Day 1 of 5</span></span>
          <span>Trades: <span className="text-slate-200">47</span></span>
          <span>Win Rate: <span className="text-emerald-400">68%</span></span>
          <span>Max DD: <span className="text-rose-400">-₹2,100</span></span>
          <span>Sharpe: <span className="text-emerald-400">1.82</span></span>
        </div>
      </div>

      {/* FOOTER */}
      <footer className="h-8 bg-slate-950 border-t border-slate-900 flex justify-between items-center px-4 text-[10px] font-mono text-slate-500 shrink-0">
        <div className="flex gap-4">
          <span className="flex items-center gap-1"><Database className="w-3 h-3" /> MEM: 3.2GB / 16GB</span>
          <span className="flex items-center gap-1"><Clock className="w-3 h-3" /> UPTIME: 14d 6h</span>
        </div>
        <div className="flex gap-4">
          <span>ALGORITHM ENGINE: ONLINE</span>
          <span>EXECUTION: LIVE</span>
        </div>
      </footer>
      
      <style dangerouslySetInnerHTML={{__html: `
        .custom-scrollbar::-webkit-scrollbar {
          width: 4px;
        }
        .custom-scrollbar::-webkit-scrollbar-track {
          background: rgba(15, 23, 42, 0.5);
        }
        .custom-scrollbar::-webkit-scrollbar-thumb {
          background: rgba(51, 65, 85, 0.5);
          border-radius: 4px;
        }
        .custom-scrollbar::-webkit-scrollbar-thumb:hover {
          background: rgba(71, 85, 105, 0.8);
        }
      `}} />
    </div>
  );
}
