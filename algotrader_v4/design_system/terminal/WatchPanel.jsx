/* WatchPanel — left rail. Search + dense ticker rows w/ heat bar + sparkline */
function WatchSpark({ data, up }) {
  const w = 52, h = 18;
  const min = Math.min(...data), max = Math.max(...data), rng = max - min || 1;
  const pts = data.map((v, i) => `${(i / (data.length - 1)) * w},${h - ((v - min) / rng) * h}`).join(' ');
  return (
    <svg width={w} height={h} className="wl-spark">
      <polyline points={pts} fill="none" stroke={up ? 'var(--up)' : 'var(--down)'} strokeWidth="1.25" strokeLinejoin="round" />
    </svg>
  );
}

function WatchRow({ t, selected, onSelect }) {
  const up = t.change_pct >= 0;
  const heat = Math.min(100, Math.abs(t.change_pct) / 2.2 * 100);
  return (
    <button className={tcx('wl-row', selected && 'sel')} onClick={() => onSelect(t.symbol)}>
      <div className={tcx('wl-heat', up ? 'pos' : 'neg')} style={{ width: `${heat}%` }} />
      <div className="wl-main">
        <div className="wl-l">
          <span className="wl-sym">{t.symbol}</span>
          <span className="wl-seg">{t.seg}</span>
        </div>
        <WatchSpark data={t.spark} up={up} />
        <div className="wl-r">
          <span className="wl-ltp mono">{fmt(t.ltp)}</span>
          <span className={tcx('wl-chg mono', up ? 'pos' : 'neg')}>{sgn(t.change_pct)}{t.change_pct.toFixed(2)}%</span>
        </div>
      </div>
    </button>
  );
}

function WatchPanel({ ticks, selected, onSelect }) {
  const [q, setQ] = React.useState('');
  const syms = Object.keys(ticks).filter(s => s.includes(q.toUpperCase()));
  return (
    <aside className="watch">
      <div className="panel-head">
        <span className="panel-title">WATCHLIST</span>
        <span className="panel-meta mono">{syms.length}</span>
      </div>
      <div className="wl-search">
        <TIcon name="search" size={13} />
        <input value={q} onChange={e => setQ(e.target.value)} placeholder="Search symbol…" spellCheck="false" />
      </div>
      <div className="wl-list">
        {syms.map(s => <WatchRow key={s} t={ticks[s]} selected={s === selected} onSelect={onSelect} />)}
      </div>
    </aside>
  );
}
window.WatchPanel = WatchPanel;
