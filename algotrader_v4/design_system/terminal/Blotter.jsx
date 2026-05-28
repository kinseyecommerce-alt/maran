/* Blotter — tape strip + tabbed positions / orders / agents */
function Tape({ tape }) {
  return (
    <div className="tape">
      <div className="tape-tag">T&amp;S</div>
      <div className="tape-track">
        {tape.map((e, i) => (
          <span key={e.id + '-' + i} className="tape-item">
            <span className="t-sym">{e.sym}</span>
            <span className={tcx('t-px mono', e.side === 'B' ? 'pos' : 'neg')}>{fmt(e.px)}</span>
            <span className="t-qty mono">×{e.qty}</span>
            <span className={tcx('t-side', e.side === 'B' ? 'pos' : 'neg')}>{e.side}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

const BL_AGENTS = [
  { name: 'INTRADAY', tag: 'VWAP+EMA', win: 64.2 },
  { name: 'SCALPING', tag: 'MICRO-X', win: 58.9 },
  { name: 'SWING', tag: 'EMA200', win: 71.4 },
  { name: 'F&O', tag: 'CE/PE', win: 52.1 },
];

function Blotter({ tab, setTab, positions, orders, ticks, agentState, botRunning, onToggleAgent }) {
  const open = positions.filter(p => p.qty !== 0);
  const TABS = [
    { id: 'pos', label: 'POSITIONS', n: open.length },
    { id: 'ord', label: 'ORDERS', n: orders.length },
    { id: 'agt', label: 'AGENTS', n: BL_AGENTS.filter(a => botRunning && agentState[a.name]).length },
    { id: 'sebi', label: 'SEBI', n: null },
  ];
  return (
    <section className="blotter">
      <div className="bl-tabs">
        {TABS.map(t => (
          <button key={t.id} className={tcx('bl-tab', tab === t.id && 'on')} onClick={() => setTab(t.id)}>
            {t.label}{t.n != null && <span className="bl-count mono">{t.n}</span>}
          </button>
        ))}
        <div className="bl-tabs-fill" />
        <div className="bl-conn"><TIcon name="shield" size={12} />SEBI&nbsp;ACTIVE</div>
      </div>

      <div className="bl-body">
        {tab === 'pos' && (open.length ? (
          <table className="bl-table">
            <thead><tr>{['SYMBOL', 'QTY', 'AVG', 'LTP', 'P&L', 'CHG%', 'PROD'].map(h => <th key={h} className={h === 'P&L' || h === 'CHG%' || h === 'LTP' || h === 'AVG' ? 'r' : ''}>{h}</th>)}</tr></thead>
            <tbody>
              {open.map(p => {
                const ltp = ticks[p.symbol]?.ltp ?? p.avg;
                const pnl = (ltp - p.avg) * p.qty;
                const pct = p.avg > 0 ? (ltp - p.avg) / p.avg * 100 : 0;
                return (
                  <tr key={p.symbol}>
                    <td className="b">{p.symbol}</td>
                    <td><span className={tcx('qty-chip', p.qty > 0 ? 'pos' : 'neg')}>{sgn(p.qty)}{p.qty}</span></td>
                    <td className="r mono dim">{fmt(p.avg)}</td>
                    <td className="r mono">{fmt(ltp)}</td>
                    <td className={tcx('r mono', pnl >= 0 ? 'pos' : 'neg')}>{sgn(pnl)}₹{fmt0(Math.abs(pnl))}</td>
                    <td className={tcx('r mono', pct >= 0 ? 'pos' : 'neg')}>{sgn(pct)}{pct.toFixed(2)}%</td>
                    <td className="dim">{p.product}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        ) : <div className="bl-empty">NO OPEN POSITIONS · place an order to begin</div>)}

        {tab === 'ord' && (orders.length ? (
          <table className="bl-table">
            <thead><tr>{['TIME', 'SYMBOL', 'SIDE', 'QTY', 'TYPE', 'PRICE', 'STATUS'].map(h => <th key={h} className={h === 'QTY' || h === 'PRICE' ? 'r' : ''}>{h}</th>)}</tr></thead>
            <tbody>
              {orders.map(o => (
                <tr key={o.id}>
                  <td className="mono dim">{o.time}</td>
                  <td className="b">{o.symbol}</td>
                  <td><span className={tcx('side-tag', o.side === 'BUY' ? 'pos' : 'neg')}>{o.side}</span></td>
                  <td className="r mono">{o.qty}</td>
                  <td className="dim">{o.type}·{o.product}</td>
                  <td className="r mono">{o.type === 'MARKET' ? 'MKT' : fmt(o.price)}</td>
                  <td><span className="status-ok">● COMPLETE</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : <div className="bl-empty">NO ORDERS TODAY</div>)}

        {tab === 'agt' && (
          <div className="agt-grid">
            {BL_AGENTS.map(a => {
              const on = botRunning && agentState[a.name];
              return (
                <div key={a.name} className={tcx('agt', on && 'on')}>
                  <div className="agt-top">
                    <span className={tcx('agt-dot', on && 'on')} />
                    <span className="agt-name">{a.name}</span>
                    <span className="agt-tag mono">{a.tag}</span>
                  </div>
                  <div className="agt-mid">
                    <div><span className="agt-l">WIN</span><span className={tcx('agt-v mono', a.win >= 55 ? 'pos' : 'neg')}>{a.win}%</span></div>
                    <div><span className="agt-l">TRADES</span><span className="agt-v mono">{on ? Math.floor(Math.random() * 9) : 0}</span></div>
                  </div>
                  <button className={tcx('agt-btn', on ? 'pause' : 'run')} onClick={() => onToggleAgent(a.name)} disabled={!botRunning}>
                    {on ? 'PAUSE' : 'RESUME'}
                  </button>
                </div>
              );
            })}
          </div>
        )}

        {tab === 'sebi' && (
          <div className="sebi-pane">
            <div className="sebi-status"><TIcon name="shield" size={18} /><div><div className="ss-1">COMPLIANCE ENGINE · ACTIVE</div><div className="ss-2 mono">142 orders logged · 2 whitelisted IPs · kill-switch ARMED</div></div></div>
            <button className="kill"><TIcon name="alert" size={14} />EMERGENCY KILL SWITCH</button>
          </div>
        )}
      </div>
    </section>
  );
}
window.Blotter = Blotter;
window.Tape = Tape;
