import React, { useState, useEffect } from "react";
import { 
  Activity, 
  Terminal, 
  Cpu, 
  Wifi, 
  ShieldCheck, 
  AlertTriangle, 
  TrendingUp, 
  TrendingDown,
  BarChart3,
  Clock,
  Zap,
  Play,
  Square,
  ChevronRight,
  Database,
  Crosshair,
  ChevronRightSquare
} from "lucide-react";

export function AgentCommandCenterV2() {
  const [time, setTime] = useState(new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" }));

  useEffect(() => {
    const timer = setInterval(() => {
      setTime(new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata" }));
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const agents = [
    {
      id: "AGN-01",
      name: "SCALPING",
      status: "ACTIVE",
      strategy: "Orderbook Imbalance",
      trades: 42,
      pnl: "+₹14,250",
      pnlClass: "text-emerald-400",
      uptime: "4h 12m",
      lastAction: "Bought 150 INFY",
      lastActionType: "buy",
      confidence: "84%",
      memory: "128 MB",
    },
    {
      id: "AGN-02",
      name: "MOMENTUM",
      status: "ACTIVE",
      strategy: "VWAP Breakout",
      trades: 18,
      pnl: "+₹32,100",
      pnlClass: "text-emerald-400",
      uptime: "4h 12m",
      lastAction: "Holding RELIANCE",
      lastActionType: "system",
      confidence: "92%",
      memory: "256 MB",
    },
    {
      id: "AGN-03",
      name: "INTRADAY",
      status: "PAUSED",
      strategy: "Mean Reversion",
      trades: 8,
      pnl: "-₹1,450",
      pnlClass: "text-rose-400",
      uptime: "0h 0m",
      lastAction: "Stopped out HDFC",
      lastActionType: "loss",
      confidence: "--",
      memory: "0 MB",
    },
    {
      id: "AGN-04",
      name: "SWING",
      status: "ACTIVE",
      strategy: "Multi-Timeframe Trend",
      trades: 3,
      pnl: "+₹8,900",
      pnlClass: "text-emerald-400",
      uptime: "4h 12m",
      lastAction: "Sold 50 TCS",
      lastActionType: "sell",
      confidence: "76%",
      memory: "512 MB",
    }
  ];

  const logs = [
    { time: "09:42:15", agent: "SCALPING", action: "BOUGHT 150 INFY @ 1,842.50", conf: "84%", type: "buy" },
    { time: "09:41:02", agent: "MOMENTUM", action: "TRAILING SL UPDATED RELIANCE TO 2,840.00", conf: "--", type: "system" },
    { time: "09:38:45", agent: "SWING", action: "SOLD 50 TCS @ 3,920.10 (PROFIT BOOKING)", conf: "91%", type: "sell" },
    { time: "09:35:12", agent: "SYSTEM", action: "MARKET REGIME DETECTED: HIGH VOLATILITY", conf: "98%", type: "alert" },
    { time: "09:31:05", agent: "SCALPING", action: "DETECTED ORDERBOOK IMBALANCE IN INFY (BULLISH)", conf: "82%", type: "analyze" },
    { time: "09:25:00", agent: "INTRADAY", action: "STOP LOSS HIT HDFC @ 1,420.50. AGENT PAUSED.", conf: "--", type: "loss" },
    { time: "09:15:05", agent: "SYSTEM", action: "NSE PRE-OPEN COMPLETE. ALL ACTIVE AGENTS ENGAGED.", conf: "100%", type: "system" },
  ];

  const watchlist = [
    { sym: "NIFTY50", price: "22,450.20", chg: "+1.2%", chgVal: "+265.40", type: "up", vol: 80, agents: 0 },
    { sym: "RELIANCE", price: "2,845.30", chg: "+0.8%", chgVal: "+22.60", type: "up", vol: 65, agents: 2 },
    { sym: "TCS", price: "3,910.05", chg: "-0.4%", chgVal: "-15.60", type: "down", vol: 40, agents: 1 },
    { sym: "INFY", price: "1,842.50", chg: "+2.1%", chgVal: "+37.80", type: "up", vol: 90, agents: 3 },
    { sym: "HDFCBANK", price: "1,422.10", chg: "-1.5%", chgVal: "-21.60", type: "down", vol: 55, agents: 0 },
    { sym: "ITC", price: "1,520.40", chg: "+0.3%", chgVal: "+4.50", type: "up", vol: 30, agents: 1 },
    { sym: "SBIN", price: "740.15", chg: "-0.2%", chgVal: "-1.20", type: "down", vol: 70, agents: 0 },
  ];

  const getLogIcon = (type: string) => {
    switch(type) {
      case 'buy': return <TrendingUp className="w-3 h-3 text-emerald-400" />;
      case 'sell': return <TrendingDown className="w-3 h-3 text-rose-400" />;
      case 'alert':
      case 'loss': return <AlertTriangle className="w-3 h-3 text-amber-400" />;
      default: return <Terminal className="w-3 h-3 text-slate-400" />;
    }
  };

  const getLogBorder = (type: string) => {
    switch(type) {
      case 'buy': return 'border-emerald-500/50';
      case 'sell': return 'border-rose-500/50';
      case 'loss': return 'border-rose-600';
      case 'alert': return 'border-amber-500/50';
      default: return 'border-slate-700/50';
    }
  };
  
  const getActionDot = (type: string) => {
    switch(type) {
      case 'buy': return 'bg-emerald-400';
      case 'sell': return 'bg-rose-400';
      case 'loss': return 'bg-rose-600';
      case 'alert': return 'bg-amber-400';
      default: return 'bg-slate-400';
    }
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-300 font-sans flex flex-col overflow-hidden selection:bg-emerald-900 selection:text-emerald-50">
      
      {/* HEADER */}
      <header className="h-14 bg-slate-900 border-b border-slate-800 flex items-center justify-between px-4 shrink-0">
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
          <div className="flex flex-col items-end">
            <span className="text-[10px] text-slate-500 font-mono uppercase tracking-wider">Net P&L Today</span>
            <span className="text-emerald-400 font-mono font-bold text-base">+₹53,800.00</span>
          </div>
          <div className="flex flex-col items-end">
            <span className="text-[10px] text-slate-500 font-mono uppercase tracking-wider">Used Margin</span>
            <span className="text-slate-200 font-mono text-base">₹4,25,000.00</span>
          </div>
          <div className="flex items-center gap-3 ml-4 pl-4 border-l border-slate-800">
            <ShieldCheck className="w-4 h-4 text-emerald-500" />
            <span className="font-mono text-sm text-slate-300">{time} IST</span>
          </div>
        </div>
      </header>

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
              {agents.map((agent, agentIdx) => (
                <div key={agent.id} className={`border rounded-lg bg-slate-900/50 p-4 relative overflow-hidden transition-colors flex flex-col ${agent.status === 'ACTIVE' ? 'border-emerald-500/40 hover:border-emerald-500/70 shadow-[0_0_15px_rgba(16,185,129,0.05)]' : 'border-slate-800 opacity-80'}`}>
                  {agent.status === 'ACTIVE' && (
                    <div className="absolute top-0 right-0 w-32 h-32 bg-emerald-500/10 rounded-full blur-3xl transform translate-x-1/2 -translate-y-1/2 pointer-events-none" />
                  )}
                  
                  <div className="flex justify-between items-start mb-3">
                    <div>
                      <div className="flex items-center gap-2 mb-1">
                        <span className="font-mono text-[10px] text-slate-500">{agent.id}</span>
                        {agent.status === 'ACTIVE' ? (
                          <span className="flex h-1.5 w-1.5 rounded-full bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,1)]"></span>
                        ) : (
                          <span className="flex h-1.5 w-1.5 rounded-full bg-amber-500"></span>
                        )}
                      </div>
                      <h3 className="font-bold text-slate-200 leading-tight">{agent.name}</h3>
                    </div>
                    <div className={`text-[10px] font-bold tracking-wider px-2.5 py-0.5 rounded-full border ${agent.status === 'ACTIVE' ? 'border-emerald-500/30 text-emerald-400 bg-emerald-500/10 shadow-[0_0_10px_rgba(16,185,129,0.2)]' : 'border-amber-500/30 text-amber-500 bg-amber-500/10'}`}>
                      {agent.status}
                    </div>
                  </div>
                  
                  <div className="space-y-1.5 mb-4">
                    <div className="flex justify-between text-xs items-center">
                      <span className="text-slate-500">Strategy</span>
                      <span className="text-slate-300 font-medium">{agent.strategy}</span>
                    </div>
                    <div className="flex justify-between text-xs items-center">
                      <span className="text-slate-500">Trades Today</span>
                      <span className="text-slate-400 font-mono">{agent.trades}</span>
                    </div>
                    
                    <div className="pt-2 flex justify-between items-end">
                      <div className="flex flex-col">
                         <span className="text-[10px] text-slate-500 uppercase tracking-wider mb-0.5">Day P&L</span>
                         <span className={`font-mono text-lg font-bold leading-none ${agent.pnlClass}`}>{agent.pnl}</span>
                      </div>
                      
                      {/* Mini P&L Sparkline */}
                      <div className="flex items-end gap-[1px] h-6 opacity-80">
                        {[...Array(12)].map((_, i) => {
                          const val = Math.sin((i + agentIdx * 3) * 0.5) * 10 + (agent.pnlClass.includes('emerald') ? 2 : -2);
                          const isPos = val >= 0;
                          return (
                            <div 
                              key={i} 
                              className={`w-1 rounded-sm ${isPos ? 'bg-emerald-500/60' : 'bg-rose-500/60'}`} 
                              style={{ height: `${Math.max(2, Math.abs(val) * 2)}px` }}
                            />
                          );
                        })}
                      </div>
                    </div>
                  </div>

                  <div className="mt-auto">
                    <div className="bg-slate-950 rounded p-2.5 border border-slate-800/80">
                      <div className="text-[10px] text-slate-500 uppercase tracking-wider mb-1.5 flex justify-between items-center">
                        <span>Last Action</span>
                        <span className="font-mono text-slate-400">Conf: {agent.confidence}</span>
                      </div>
                      <div className="font-mono text-[11px] text-slate-300 flex items-center gap-1.5 truncate" title={agent.lastAction}>
                        <div className="flex items-center justify-center relative w-3 h-3 shrink-0">
                          <ChevronRight className="w-3 h-3 text-slate-500 absolute" />
                        </div>
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${getActionDot(agent.lastActionType)}`}></span>
                        <span className="truncate">{agent.lastAction}</span>
                      </div>
                    </div>
                    
                    <div className="mt-3 flex gap-2">
                      {agent.status === 'ACTIVE' ? (
                        <button className="flex-1 flex items-center justify-center gap-1.5 bg-slate-800/50 hover:bg-slate-800 text-slate-300 text-xs py-1.5 rounded transition-colors border border-slate-700/50">
                          <Square className="w-3 h-3" /> Stop
                        </button>
                      ) : (
                        <button className="flex-1 flex items-center justify-center gap-1.5 bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 text-xs py-1.5 rounded transition-colors border border-emerald-500/20">
                          <Play className="w-3 h-3 fill-current" /> Start
                        </button>
                      )}
                      <button className="px-2.5 bg-slate-800/50 hover:bg-slate-800 text-slate-400 text-xs py-1.5 rounded transition-colors border border-slate-700/50">
                        <TrendingUp className="w-3.5 h-3.5" />
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>
          
          {/* PORTFOLIO PULSE ROW */}
          <div className="h-10 bg-slate-900/50 border-y border-slate-800 flex items-center px-4 shrink-0 text-xs font-mono">
            <div className="text-slate-500 mr-4 font-sans tracking-widest text-[10px] font-bold">PORTFOLIO PULSE</div>
            <div className="flex gap-6 items-center flex-1">
              <span className="flex items-center gap-1.5"><span className="text-slate-500">Total Trades:</span> <span className="text-slate-200">47</span></span>
              <span className="flex items-center gap-1.5"><span className="text-slate-500">Open Pos:</span> <span className="text-slate-200">8</span></span>
              <span className="flex items-center gap-1.5"><span className="text-slate-500">Realized:</span> <span className="text-emerald-400">+₹44,900</span></span>
              <span className="flex items-center gap-1.5"><span className="text-slate-500">Unrealized:</span> <span className="text-emerald-400">+₹8,900</span></span>
            </div>
          </div>

          {/* ACTIVITY LOG */}
          <div className="flex-1 flex flex-col p-4 bg-slate-950 overflow-hidden">
            <h2 className="text-sm font-semibold tracking-widest text-slate-400 flex items-center gap-2 mb-3 shrink-0">
              <Terminal className="w-4 h-4 text-emerald-500" /> 
              LIVE DECISION STREAM
            </h2>
            
            <div className="flex-1 overflow-y-auto space-y-1 font-mono text-[11px] pr-2 custom-scrollbar">
              {logs.map((log, i) => (
                <div key={i} className={`flex gap-3 py-2 px-3 bg-slate-900/30 hover:bg-slate-900/80 rounded group border-l-[3px] ${getLogBorder(log.type)} transition-colors items-center`}>
                  <span className="text-slate-500 shrink-0 w-16">{log.time}</span>
                  <div className="flex items-center gap-1.5 shrink-0 w-28 text-slate-400">
                    {getLogIcon(log.type)}
                    <span>{log.agent}</span>
                  </div>
                  <span className={`flex-1 truncate ${
                    log.type === 'buy' ? 'text-emerald-400' :
                    log.type === 'sell' ? 'text-rose-400' :
                    log.type === 'alert' ? 'text-amber-400' :
                    log.type === 'loss' ? 'text-rose-500 font-bold' :
                    'text-slate-300'
                  }`}>
                    {log.action}
                  </span>
                  <span className="text-slate-500 shrink-0 w-16 text-right">
                    C: <span className="text-slate-300">{log.conf}</span>
                  </span>
                </div>
              ))}
              <div className="flex gap-3 py-2 px-3 items-center border-l-[3px] border-transparent">
                <span className="text-emerald-500/50 shrink-0 w-16">...</span>
                <span className="text-slate-600">Waiting for signals...</span>
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
            <div className="flex-1 overflow-y-auto custom-scrollbar p-1">
              {watchlist.map((item, i) => (
                <div key={i} className="flex justify-between items-center py-1.5 px-3 border-b border-slate-800/40 hover:bg-slate-800/50 cursor-pointer transition-colors group">
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-1.5">
                      <div className="font-bold text-slate-200 text-sm truncate">{item.sym}</div>
                      {item.agents > 0 && (
                        <div className="flex items-center gap-0.5 bg-emerald-500/10 text-emerald-400 text-[9px] font-mono px-1 rounded border border-emerald-500/20" title="${item.agents} Agents Watching">
                          <span className="w-1 h-1 rounded-full bg-emerald-500"></span>
                          {item.agents}
                        </div>
                      )}
                    </div>
                    <div className="text-[10px] text-slate-500 mt-0.5 flex items-center gap-1.5">
                      <span>NSE EQ</span>
                      <div className="h-0.5 w-16 bg-slate-800 rounded-full overflow-hidden">
                        <div className="h-full bg-sky-400/30" style={{ width: `${item.vol}%` }}></div>
                      </div>
                    </div>
                  </div>
                  <div className="text-right shrink-0">
                    <div className="font-mono text-[13px] text-slate-200">{item.price}</div>
                    <div className={`font-mono text-[10px] flex items-center justify-end gap-1 mt-0.5 ${item.type === 'up' ? 'text-emerald-400' : 'text-rose-400'}`}>
                      {item.type === 'up' ? <TrendingUp className="w-2.5 h-2.5" /> : <TrendingDown className="w-2.5 h-2.5" />}
                      {item.chgVal} ({item.chg})
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </div>

          {/* SECONDARY CHART CONTEXT */}
          <div className="h-[280px] p-4 bg-slate-950/40 flex flex-col shrink-0">
            <div className="flex justify-between items-center mb-3">
              <h3 className="text-xs font-semibold tracking-widest text-slate-400 flex items-center gap-2">
                <BarChart3 className="w-3.5 h-3.5" />
                NIFTY50 TREND
              </h3>
              <span className="text-[10px] bg-slate-800 px-1.5 py-0.5 rounded text-slate-400 border border-slate-700">1M</span>
            </div>
            
            {/* Deterministic Chart Area */}
            <div className="flex-1 relative border border-slate-800/80 rounded bg-[#0b1120] overflow-hidden group">
              <div className="absolute inset-0 flex items-end">
                {/* Static deterministic chart bars */}
                <div className="w-full h-full flex items-end justify-between px-1 opacity-40 group-hover:opacity-60 transition-opacity pb-1">
                  {[...Array(40)].map((_, i) => {
                    const height = 30 + Math.sin(i * 0.4) * 20 + Math.cos(i * 0.7) * 10;
                    return (
                      <div key={i} className="w-[1.5%] bg-emerald-500/20 rounded-t-sm" style={{ height: `${height}%` }}></div>
                    );
                  })}
                </div>
                {/* Smooth curve SVG */}
                <svg className="absolute inset-0 h-full w-full opacity-70" preserveAspectRatio="none">
                  {/* Generated path matching the bars closely */}
                  <path 
                    d="M 0,100 L 0,80 Q 20,40 40,60 T 100,50 T 150,70 T 200,40 T 250,60 T 300,30 L 300,100 Z" 
                    fill="rgba(16,185,129,0.05)" 
                    vectorEffect="non-scaling-stroke"
                  />
                  <path 
                    d="M 0,80 Q 20,40 40,60 T 100,50 T 150,70 T 200,40 T 250,60 T 300,30" 
                    fill="none" 
                    stroke="rgba(16,185,129,0.8)" 
                    strokeWidth="1.5" 
                    vectorEffect="non-scaling-stroke" 
                  />
                </svg>
              </div>
              <div className="absolute top-2 left-2 flex items-center gap-1.5 bg-slate-900/80 backdrop-blur border border-slate-700/50 px-2 py-1 rounded text-[10px] font-mono text-emerald-400">
                <Crosshair className="w-3 h-3" />
                UPTREND STRONG
              </div>
            </div>
          </div>
          
        </div>
      </div>
      
      {/* FOOTER */}
      <footer className="h-8 bg-slate-950 border-t border-slate-900 flex justify-between items-center px-4 text-[10px] font-mono text-slate-500 shrink-0">
        <div className="flex gap-4">
          <span className="flex items-center gap-1.5"><Database className="w-3 h-3" /> MEM: 3.2GB / 16GB</span>
          <span className="flex items-center gap-1.5"><Clock className="w-3 h-3" /> UPTIME: 14d 6h</span>
        </div>
        <div className="flex gap-4">
          <span className="text-emerald-500/70">● ALGORITHM ENGINE: ONLINE</span>
          <span className="text-emerald-500/70">● EXECUTION: LIVE</span>
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
