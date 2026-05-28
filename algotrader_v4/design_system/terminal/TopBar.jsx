/* TopBar — brand, live index strip, session clock, P&L, bot, status */
function IndexPill({ name, val, chg }) {
  const up = chg >= 0;
  return (
    <div className="idx-pill">
      <span className="idx-name">{name}</span>
      <span className="idx-val mono">{fmt(val, val > 1000 ? 0 : 2)}</span>
      <span className={tcx('idx-chg mono', up ? 'pos' : 'neg')}>{sgn(chg)}{chg.toFixed(2)}%</span>
    </div>
  );
}

function TopBar({ indices, botRunning, onToggleBot, totalPnl, clock, theme, onToggleTheme }) {
  const up = totalPnl >= 0;
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-mark"><TIcon name="pulse" size={17} /></div>
        <div className="brand-txt">
          <div className="brand-1">NIRMA<span className="brand-slash">//</span>TERMINAL</div>
          <div className="brand-2">ALGOTRADER&nbsp;PRO&nbsp;·&nbsp;v5</div>
        </div>
      </div>

      <div className="idx-strip">
        {indices.map(i => <IndexPill key={i.name} {...i} />)}
      </div>

      <div className="topbar-right">
        <div className="pnl-chip">
          <span className="pnl-lbl">DAY&nbsp;P&amp;L</span>
          <span className={tcx('pnl-val mono', up ? 'pos glow-pos' : 'neg glow-neg')}>
            {up ? '+' : '−'}₹{fmt0(Math.abs(totalPnl))}
          </span>
        </div>

        <div className="sess">
          <span className="live-dot" />
          <span className="sess-txt">LIVE</span>
          <span className="sess-clock mono">{clock}</span>
        </div>

        <button className="theme-tgl" onClick={onToggleTheme} title="Toggle theme">
          <TIcon name={theme === 'light' ? 'moon' : 'sun'} size={15} />
        </button>

        <button className={tcx('bot-btn', botRunning ? 'on' : 'off')} onClick={onToggleBot}>
          <TIcon name={botRunning ? 'stop' : 'bolt'} size={14} />
          {botRunning ? 'HALT ENGINE' : 'ARM ENGINE'}
        </button>
      </div>
    </header>
  );
}
window.TopBar = TopBar;
