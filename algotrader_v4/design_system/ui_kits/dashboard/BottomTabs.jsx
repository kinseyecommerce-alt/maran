/* Bottom tabs — Positions · Orders · Brackets · Risk · Agents · SEBI */
const TABS = [
  { id: 'positions', label: 'Positions', emoji: '📋' },
  { id: 'orders',    label: 'Orders',    emoji: '📝' },
  { id: 'brackets',  label: 'Brackets',  emoji: '🎯' },
  { id: 'risk',      label: 'Risk',      emoji: '⚠️' },
  { id: 'agents',    label: 'Agents',    emoji: '🤖' },
  { id: 'sebi',      label: 'SEBI',      emoji: '🛡️' },
];

function PositionsTab({ positions, ticks }) {
  const open = positions.filter(p => p.qty !== 0);
  if (!open.length) return <div className="flex items-center justify-center h-full text-slate-400 text-sm">No open positions</div>;
  const total = open.reduce((a, p) => a + ((ticks[p.symbol]?.ltp ?? p.avg) - p.avg) * p.qty, 0);
  return (
    <div className="h-full overflow-auto">
      <div className="flex items-center justify-between px-4 py-2 border-b border-slate-100 bg-white sticky top-0">
        <span className="text-sm font-semibold text-slate-700">{open.length} Open Position{open.length !== 1 ? 's' : ''}</span>
        <div className="flex items-center gap-2"><span className="text-xs text-slate-500">Total P&amp;L:</span><Pnl value={total} /></div>
      </div>
      <table className="w-full text-sm">
        <thead><tr className="border-b border-slate-100 bg-slate-50">
          {['Symbol','Qty','Avg Price','LTP','P&L','Change'].map(h => <th key={h} className="text-left px-4 py-2 text-xs font-medium text-slate-500">{h}</th>)}
        </tr></thead>
        <tbody>
          {open.map(p => {
            const ltp = ticks[p.symbol]?.ltp ?? p.avg;
            const pnl = (ltp - p.avg) * p.qty;
            const pct = p.avg > 0 ? (ltp - p.avg) / p.avg * 100 : 0;
            return (
              <tr key={p.symbol} className="border-b border-slate-50 hover:bg-slate-50 transition-colors">
                <td className="px-4 py-3"><div className="font-semibold text-slate-900">{p.symbol}</div><div className="text-xs text-slate-400">{p.product} · NSE</div></td>
                <td className="px-4 py-3"><Badge variant={p.qty > 0 ? 'buy' : 'sell'}>{p.qty > 0 ? '+' : ''}{p.qty}</Badge></td>
                <td className="px-4 py-3 font-mono text-slate-700">{inr(p.avg)}</td>
                <td className="px-4 py-3 font-mono font-semibold text-slate-900">{inr(ltp)}</td>
                <td className="px-4 py-3"><Pnl value={pnl} /></td>
                <td className="px-4 py-3"><span className={cx('text-xs font-mono font-medium', pct >= 0 ? 'text-green-600' : 'text-red-600')}>{pct >= 0 ? '+' : ''}{pct.toFixed(2)}%</span></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function OrdersTab({ orders }) {
  if (!orders.length) return <div className="flex items-center justify-center h-full text-slate-400 text-sm">No orders today</div>;
  return (
    <div className="h-full overflow-auto">
      <table className="w-full text-sm">
        <thead><tr className="border-b border-slate-100 bg-slate-50">
          {['Time','Symbol','Side','Qty','Type','Status'].map(h => <th key={h} className="text-left px-4 py-2 text-xs font-medium text-slate-500">{h}</th>)}
        </tr></thead>
        <tbody>
          {orders.map(o => (
            <tr key={o.id} className="border-b border-slate-50 hover:bg-slate-50">
              <td className="px-4 py-2.5 font-mono text-xs text-slate-400">{o.time}</td>
              <td className="px-4 py-2.5 font-semibold text-slate-900">{o.symbol}</td>
              <td className="px-4 py-2.5"><Badge variant={o.side === 'BUY' ? 'buy' : 'sell'}>{o.side}</Badge></td>
              <td className="px-4 py-2.5 font-mono text-slate-700">{o.qty}</td>
              <td className="px-4 py-2.5 text-slate-600 text-xs">{o.orderType} · {o.product}</td>
              <td className="px-4 py-2.5"><span className="text-xs font-medium text-green-600">COMPLETE</span></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function BracketsTab() {
  return <div className="flex flex-col items-center justify-center h-full text-slate-400 text-sm gap-1">
    <span className="text-2xl">🎯</span>No active brackets<span className="text-xs">Agent trades create bracket orders (entry + SL + target)</span>
  </div>;
}

function RiskTab({ dailyPnl, openPos }) {
  const maxLoss = 50000, maxPos = 5;
  const lossUsed = Math.min(100, Math.abs(Math.min(0, dailyPnl)) / maxLoss * 100);
  const limitColor = lossUsed > 80 ? '#DC2626' : lossUsed > 50 ? '#F59E0B' : '#16A34A';
  const posUsed = (openPos / maxPos) * 100;
  return (
    <div className="h-full overflow-auto p-4">
      <div className="grid grid-cols-2 gap-4 mb-4">
        <div className="bg-white border border-slate-200 rounded-xl p-4 text-center">
          <div className="text-xs text-slate-500 mb-1">Daily P&amp;L</div>
          <div className="text-2xl font-bold font-mono" style={{ color: dailyPnl >= 0 ? '#16A34A' : '#DC2626' }}>{dailyPnl >= 0 ? '+' : ''}{inr0(Math.abs(dailyPnl))}</div>
          <div className="text-xs text-slate-400 mt-1">Limit: {inr0(maxLoss)}</div>
        </div>
        <div className="bg-white border border-slate-200 rounded-xl p-4 text-center">
          <div className="text-xs text-slate-500 mb-1">Open Positions</div>
          <div className="text-2xl font-bold font-mono text-slate-900">{openPos} / {maxPos}</div>
          <div className="mt-2 h-1.5 rounded-full bg-slate-100 overflow-hidden"><div className="h-full rounded-full bg-indigo-500 transition-all" style={{ width: `${posUsed}%` }} /></div>
        </div>
      </div>
      <div className="bg-white border border-slate-200 rounded-xl p-4">
        <div className="text-xs font-medium text-slate-700 mb-3">Loss Limit Usage</div>
        <div className="flex items-center gap-4">
          <div className="text-3xl font-bold font-mono" style={{ color: limitColor }}>{lossUsed.toFixed(0)}%</div>
          <div className="flex-1 h-2 rounded-full bg-slate-100 overflow-hidden"><div className="h-full rounded-full transition-all" style={{ width: `${lossUsed}%`, background: limitColor }} /></div>
        </div>
      </div>
    </div>
  );
}

const AGENTS = [
  { name: 'intraday', emoji: '⚡', desc: 'VWAP+EMA momentum, MIS, 8 trades/day' },
  { name: 'fno',      emoji: '📊', desc: 'Options CE/PE, NRML, 4 trades/day' },
  { name: 'swing',    emoji: '🌊', desc: 'EMA200 trend, CNC, 3 trades/day' },
  { name: 'scalping', emoji: '🎯', desc: 'Micro EMA cross, MIS, 20 trades/day' },
];

function AgentsTab({ botRunning, agentState, onToggle }) {
  return (
    <div className="h-full overflow-auto p-4 grid grid-cols-2 gap-4 auto-rows-min">
      {AGENTS.map(a => {
        const running = botRunning && agentState[a.name];
        return (
          <div key={a.name} className={cx('bg-white border rounded-xl p-4 transition-all', running ? 'border-green-200 shadow-sm' : 'border-slate-200')}>
            <div className="flex items-start justify-between mb-3">
              <div className="flex items-center gap-2">
                <span className="text-2xl">{a.emoji}</span>
                <div><div className="text-sm font-bold text-slate-900 capitalize">{a.name}</div><div className="text-xs text-slate-500">{a.desc}</div></div>
              </div>
              <span className={cx('w-2.5 h-2.5 rounded-full mt-1', running ? 'bg-green-500' : 'bg-slate-300')} />
            </div>
            <div className="grid grid-cols-2 gap-2 mb-3 text-xs">
              <div><div className="text-slate-400">Trades</div><div className="font-mono font-medium text-slate-800">{running ? Math.floor(Math.random()*6) : 0}</div></div>
              <div><div className="text-slate-400">Win Rate</div><div className={cx('font-mono font-medium', 'text-green-600')}>{running ? '62.5%' : '—'}</div></div>
            </div>
            <Btn variant={running ? 'outline' : 'primary'} size="sm" className="w-full text-xs" onClick={() => onToggle(a.name)} disabled={!botRunning}>
              {running ? 'Pause' : 'Resume'}
            </Btn>
          </div>
        );
      })}
    </div>
  );
}

function SebiTab() {
  return (
    <div className="h-full overflow-auto p-4 space-y-4">
      <div className="flex items-center gap-3 rounded-xl p-4 border bg-green-50 border-green-200 text-green-600">
        <Icon name="shield" size={24} />
        <div>
          <div className="font-bold text-green-800">SEBI Compliance: ACTIVE</div>
          <div className="text-xs text-slate-600 mt-0.5">Orders logged: 142 · IP whitelist: 2 IPs</div>
        </div>
      </div>
      <div className="bg-white border border-slate-200 rounded-xl p-4">
        <div className="text-sm font-semibold text-slate-800 mb-3">Emergency Controls</div>
        <Btn variant="danger">🛑 Emergency Kill Switch</Btn>
      </div>
    </div>
  );
}

function BottomTabs(props) {
  const { active, setActive } = props;
  const openCount = props.positions.filter(p => p.qty !== 0).length;
  return (
    <div className="h-56 shrink-0 flex flex-col border-t border-slate-200 bg-white">
      <div className="flex border-b border-slate-100 overflow-x-auto shrink-0">
        {TABS.map(tab => {
          const badge = tab.id === 'positions' && openCount > 0 ? openCount : undefined;
          return (
            <button key={tab.id} onClick={() => setActive(tab.id)}
              className={cx('flex items-center gap-1.5 px-4 py-2.5 text-xs font-medium whitespace-nowrap transition-colors border-b-2',
                active === tab.id ? 'border-indigo-600 text-indigo-600 bg-indigo-50/50' : 'border-transparent text-slate-500 hover:text-slate-700 hover:bg-slate-50')}>
              <span>{tab.emoji}</span>{tab.label}
              {badge !== undefined && <span className="bg-indigo-600 text-white rounded-full text-xs w-4 h-4 flex items-center justify-center">{badge}</span>}
            </button>
          );
        })}
      </div>
      <div className="flex-1 min-h-0">
        {active === 'positions' && <PositionsTab positions={props.positions} ticks={props.ticks} />}
        {active === 'orders'    && <OrdersTab orders={props.orders} />}
        {active === 'brackets'  && <BracketsTab />}
        {active === 'risk'      && <RiskTab dailyPnl={props.dailyPnl} openPos={openCount} />}
        {active === 'agents'    && <AgentsTab botRunning={props.botRunning} agentState={props.agentState} onToggle={props.onToggleAgent} />}
        {active === 'sebi'      && <SebiTab />}
      </div>
    </div>
  );
}
window.BottomTabs = BottomTabs;
